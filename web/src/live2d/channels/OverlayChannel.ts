/**
 * OverlayChannel — handles ``[[overlay:X]]`` LLM tags.
 *
 * Two subkinds today:
 *
 *   1. **Param pulses**: the binding has a normal ``param_id``
 *      (``ParamBlush``, ``ParamHeartEyes``, ...). The channel writes
 *      ``on_value`` every frame while alive, then writes ``0`` once
 *      on expiry. Subsequent frames don't overwrite anything (the
 *      pulse is gone from the map), so the AmbientBodyChannel /
 *      EngineState owners are free to re-assert their auto values.
 *
 *   2. **Expression-bound pulses**: the binding's ``param_id`` starts
 *      with ``expr:NAME``. The channel calls
 *      ``adapter.expression(NAME)`` exactly *once* on first frame
 *      (so we don't reset the expression slot every RAF tick which
 *      would freeze the rig) and writes ``engineState.exprSlotLockUntil
 *      = pulse.until``. The engine notices the deadline expire on
 *      a later tier-3 tick and emits ``onExpressionSlotReleased``
 *      to ExpressionChannel so the persistent reaction is re-applied.
 *      This is the regression-fix path that originally motivated
 *      the refactor — see ``docs/alexia-model-notes.md`` §5.
 *
 * Gesture-named overlays (``wink_left``, ``tail_wag``, ``ear_wiggle``)
 * are NOT handled here — they go to ``GestureChannel`` because
 * they need bespoke per-frame sine drives + capability gating
 * against ``has_wink`` / ``has_tail_wag`` / ``has_ear_wiggle``.
 *
 * Clock model: the channel only ever sees monotonic deadlines.
 * ``ResolvedOverlayEvent.until`` is already in ``performance.now()``
 * units thanks to ``AvatarEngine.dispatchOverlay`` doing the wall-
 * clock -> monotonic conversion at ingest. This is what fixed the
 * "overlay never expires" regression — channels can blindly compare
 * against ``deps.now()``.
 *
 * Registration ordering: register OverlayChannel AFTER any channel
 * that writes the same param ids on auto-envelopes (today:
 * AmbientBodyChannel for blush/sweat). With the engine running
 * channels in registration order on each tick, "auto then overlay"
 * means the overlay write wins for the duration of the pulse, then
 * the auto layer re-asserts on the next frame after expiry.
 */
import type {
  AvatarChannel,
  ChannelDeps,
  Live2DModelAdapter,
  ResolvedOverlayEvent,
} from "../types";

/** Gesture-named overlays handled by ``GestureChannel`` instead. */
const GESTURE_NAMES = new Set(["wink_left", "wink_right", "tail_wag", "ear_wiggle"]);

/** Optional bridge callback used during the staged refactor.
 *
 * Phases 5/6 introduce OverlayChannel + LipsyncChannel before the
 * ExpressionChannel arrives in Phase 7. While both layers coexist,
 * the legacy ``Live2DAvatar.tsx`` still owns the expression-slot
 * defer/restore logic and reads its deadline from
 * ``lastExprOverlayUntilRef.current``. ``onExprSlotChange`` is the
 * one-line mirror that keeps that ref in lockstep with
 * ``engineState.exprSlotLockUntil`` so the legacy paths keep working
 * unchanged. The callback goes away in Phase 7 once ExpressionChannel
 * subscribes to ``onExpressionSlotReleased`` and the legacy code is
 * deleted. */
export type OverlayExprSlotMirror = (until: number) => void;

export interface OverlayChannelOptions {
  onExprSlotChange?: OverlayExprSlotMirror;
}

interface ActivePulse {
  /** Monotonic deadline (already converted by the engine). */
  until: number;
  /** Param to write while alive. ``""`` for expression-only bindings. */
  paramId: string;
  /** Value to write while alive. */
  onValue: number;
  /** Expression name to fire once for ``expr:`` bindings. */
  exprName: string | null;
  /** Whether ``adapter.expression(exprName)`` has already fired this
   * pulse. Prevents resetting the expression slot every frame. */
  exprFired: boolean;
}

export class OverlayChannel implements AvatarChannel {
  readonly name = "overlay";

  private _adapter: Live2DModelAdapter | null = null;
  private _deps: ChannelDeps | null = null;
  private readonly _pulses: Map<string, ActivePulse> = new Map();
  private readonly _onExprSlotChange: OverlayExprSlotMirror | null;

  constructor(options: OverlayChannelOptions = {}) {
    this._onExprSlotChange = options.onExprSlotChange ?? null;
  }

  attach(adapter: Live2DModelAdapter, deps: ChannelDeps): void {
    this._adapter = adapter;
    this._deps = deps;
    this._pulses.clear();
  }

  detach(): void {
    this._adapter = null;
    this._deps = null;
    this._pulses.clear();
  }

  onOverlay(event: ResolvedOverlayEvent): void {
    if (!this._adapter || !this._deps) {
      return;
    }
    if (GESTURE_NAMES.has(event.name)) {
      // Handled by GestureChannel.
      return;
    }
    const binding = this._deps.manifest.overlays?.[event.name];
    if (!binding) {
      return;
    }
    const isExpr =
      typeof binding.param_id === "string" && binding.param_id.startsWith("expr:");
    const exprName = isExpr ? binding.param_id.slice("expr:".length) : null;
    this._pulses.set(event.name, {
      until: event.until,
      paramId: isExpr ? "" : binding.param_id,
      onValue: binding.on_value,
      exprName,
      exprFired: false,
    });
    this._deps.debug?.("channel.overlay", "pulseStart", {
      name: event.name,
      until: event.until,
      bindingKind: isExpr ? "expression" : "param",
      target: isExpr ? exprName : binding.param_id,
    });
  }

  tickTier3(now: number, _dt: number): void {
    if (!this._adapter || !this._deps || this._pulses.size === 0) {
      return;
    }
    const expired: string[] = [];
    for (const [name, pulse] of this._pulses) {
      if (now >= pulse.until) {
        // One-shot decay write so we don't sit on the on_value when
        // the pulse ends. Expression-only pulses don't have a param
        // to clear; the engine will fire ``onExpressionSlotReleased``
        // to ExpressionChannel which re-applies the persistent
        // reaction (see EngineState.exprSlotLockUntil semantics).
        if (pulse.paramId) {
          this._adapter.setParam(pulse.paramId, 0);
        }
        expired.push(name);
        continue;
      }
      if (pulse.paramId) {
        this._adapter.setParam(pulse.paramId, pulse.onValue);
      }
      if (pulse.exprName && !pulse.exprFired) {
        this._adapter.expression(pulse.exprName);
        pulse.exprFired = true;
        // Hand the deadline to the engine so it can re-apply the
        // persistent reaction once the pulse ends.
        this._deps.engineState.exprSlotLockUntil = pulse.until;
        // Mirror to the legacy ref while ExpressionChannel hasn't
        // landed yet (see ``OverlayChannelOptions`` rationale).
        if (this._onExprSlotChange) {
          this._onExprSlotChange(pulse.until);
        }
      }
    }
    for (const name of expired) {
      this._pulses.delete(name);
      this._deps?.debug?.("channel.overlay", "pulseExpired", { name });
    }
  }

  /** Test-only accessor: how many pulses are currently active. */
  get pulseCount(): number {
    return this._pulses.size;
  }
}
