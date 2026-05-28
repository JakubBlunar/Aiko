/**
 * GestureChannel — handles the four "named" overlay gestures:
 * ``wink_left`` / ``wink_right`` / ``ear_wiggle`` / ``tail_wag``.
 *
 * These four are dispatched through the same ``[[overlay:X]]``
 * grammar as param pulses + expression pulses, but instead of a
 * simple "param at on_value while alive" they need bespoke
 * per-frame drives:
 *
 *   - **wink_left / wink_right**: clamp the matching ``ParamEyeLOpen`` /
 *     ``ParamEyeROpen`` to ``0`` for the gesture lifetime, then
 *     release back to ``1`` so the EyeBlink driver resumes.
 *
 *   - **ear_wiggle**: 4 Hz sine on every detected ear segment for
 *     the gesture lifetime, then snap back to ``0``. Multiple
 *     segments share the same phase — visually that's exactly the
 *     "twitch in unison" we want. The sine is written in BOTH
 *     ``tickTier3`` (for non-physics rigs that don't run a
 *     ``tickPreModel`` pass) and ``tickPreModel`` (so physics-driven
 *     rigs like Alexia, where the ear params are downstream of
 *     ``ParamEyeROpen`` / ``ParamEyeLOpen`` via the rig's
 *     PhysicsSetting13 / 14, get a write that lands AFTER
 *     ``physics.evaluate`` and therefore wins). The state slot is
 *     nulled exclusively from ``tickPreModel`` so the rest-snap is
 *     guaranteed to be the final write of the expiry frame.
 *
 *   - **tail_wag**: this channel does NOT drive the cat-tail
 *     params directly. The always-on cat-tail sine lives in
 *     ``AmbientBodyChannel`` (it runs every frame for cat rigs at
 *     a baseline arousal-driven rate). When ``tail_wag`` arrives,
 *     this channel writes ``engineState.tailWagBoostUntil = until``
 *     and AmbientBody multiplies the baseline freq+amp by 1.8x
 *     and 1.5x respectively while the deadline is in the future.
 *     Splitting the "discrete LLM event" from the "per-frame sine
 *     drive" keeps each side single-purpose and lets us test the
 *     boost handoff without spinning up the full sine machinery.
 *
 * Capability gating: each gesture is silently dropped when the
 * matching ``has_*`` capability is missing. A poorly-prompted LLM
 * can still emit ``[[overlay:wink_left]]`` on a rig without
 * independent eyes; we want a no-op rather than a confusing
 * partial render.
 */
import type {
  AvatarChannel,
  ChannelDeps,
  Live2DModelAdapter,
  ResolvedOverlayEvent,
} from "../types";

const WINK_LEFT_PARAM = "ParamEyeLOpen";
const WINK_RIGHT_PARAM = "ParamEyeROpen";
const EAR_FREQ_HZ = 4;
const EAR_AMP = 15;

interface ActiveGesture {
  /** Monotonic deadline. */
  until: number;
  /** Whether the channel has fired the on-expiry "release" write yet.
   * Used so winks don't keep writing ``1`` every frame after the
   * gesture ends — the legacy code released exactly once. */
  released: boolean;
}

export class GestureChannel implements AvatarChannel {
  readonly name = "gesture";

  private _adapter: Live2DModelAdapter | null = null;
  private _deps: ChannelDeps | null = null;

  private _winkLeft: ActiveGesture | null = null;
  private _winkRight: ActiveGesture | null = null;
  private _earWiggle: ActiveGesture | null = null;

  attach(adapter: Live2DModelAdapter, deps: ChannelDeps): void {
    this._adapter = adapter;
    this._deps = deps;
    this._winkLeft = null;
    this._winkRight = null;
    this._earWiggle = null;
  }

  detach(): void {
    // Best-effort release on detach so a remount doesn't inherit a
    // half-winked eye or twitching ears.
    const adapter = this._adapter;
    const manifest = this._deps?.manifest;
    if (adapter && manifest) {
      const caps = manifest.capabilities ?? {};
      if (caps.has_wink) {
        adapter.setParam(WINK_LEFT_PARAM, 1);
        adapter.setParam(WINK_RIGHT_PARAM, 1);
      }
      if (caps.has_ear_wiggle) {
        for (const id of manifest.cat_ear_param_ids ?? []) {
          adapter.setParam(id, 0);
        }
      }
    }
    if (this._deps) {
      this._deps.engineState.tailWagBoostUntil = 0;
    }
    this._adapter = null;
    this._deps = null;
    this._winkLeft = null;
    this._winkRight = null;
    this._earWiggle = null;
  }

  onOverlay(event: ResolvedOverlayEvent): void {
    if (!this._deps) {
      return;
    }
    const caps = this._deps.manifest.capabilities ?? {};
    switch (event.name) {
      case "wink_left":
        if (caps.has_wink) {
          this._winkLeft = { until: event.until, released: false };
        }
        return;
      case "wink_right":
        if (caps.has_wink) {
          this._winkRight = { until: event.until, released: false };
        }
        return;
      case "ear_wiggle":
        if (caps.has_ear_wiggle) {
          this._earWiggle = { until: event.until, released: false };
        }
        return;
      case "tail_wag":
        if (caps.has_tail_wag) {
          this._deps.engineState.tailWagBoostUntil = event.until;
        }
        return;
      default:
        // Non-gesture overlays handled by OverlayChannel.
        return;
    }
  }

  tickTier3(now: number, _dt: number): void {
    const adapter = this._adapter;
    const deps = this._deps;
    if (!adapter || !deps) {
      return;
    }
    const caps = deps.manifest.capabilities ?? {};

    // Winks: drive the eye-open param to 0 while alive, release to 1
    // exactly once on expiry, then drop the active record.
    if (caps.has_wink) {
      this._tickWink(adapter, now, this._winkLeft, WINK_LEFT_PARAM, (v) => {
        this._winkLeft = v;
      });
      this._tickWink(adapter, now, this._winkRight, WINK_RIGHT_PARAM, (v) => {
        this._winkRight = v;
      });
    }

    // Ear-wiggle (non-physics fallback). Writes the same 4 Hz sine /
    // rest-snap that ``tickPreModel`` writes; on physics-driven rigs
    // these are clobbered by ``physics.evaluate`` before render, but
    // on rigs without a physics file (Mini fixture, future minimal
    // rigs) ``tickPreModel`` may still no-op so this branch is what
    // produces the visible twitch. State management (slot nulling)
    // lives exclusively in ``tickPreModel`` so the final-frame
    // rest-write wins on physics rigs.
    if (caps.has_ear_wiggle && this._earWiggle) {
      const ids = deps.manifest.cat_ear_param_ids ?? [];
      if (ids.length > 0) {
        if (now < this._earWiggle.until) {
          const value = this._earWiggleValue(now);
          for (const id of ids) {
            adapter.setParam(id, value);
          }
        } else {
          for (const id of ids) {
            adapter.setParam(id, 0);
          }
        }
      }
    }

    // Tail-wag boost: AmbientBodyChannel reads ``tailWagBoostUntil``,
    // we just clear the deadline once it's in the past so the engine
    // state stays clean (and AmbientBody's read becomes a single
    // ``0`` comparison instead of "is this stale").
    if (
      deps.engineState.tailWagBoostUntil > 0 &&
      now >= deps.engineState.tailWagBoostUntil
    ) {
      deps.engineState.tailWagBoostUntil = 0;
    }
  }

  /** Post-physics pass. Mirrors the ``tickTier3`` ear-wiggle write
   * so physics-driven rigs (Alexia: ear params are downstream of
   * ``ParamEyeROpen`` / ``ParamEyeLOpen`` via PhysicsSetting13 / 14)
   * get a write that lands AFTER ``physics.evaluate`` and therefore
   * wins. Slot nulling on expiry is owned here so the rest-snap is
   * the last write of the expiry frame across both passes. */
  tickPreModel(): void {
    const adapter = this._adapter;
    const deps = this._deps;
    if (!adapter || !deps) {
      return;
    }
    const caps = deps.manifest.capabilities ?? {};
    if (!(caps.has_ear_wiggle && this._earWiggle)) {
      return;
    }
    const now = deps.now();
    const ids = deps.manifest.cat_ear_param_ids ?? [];
    if (ids.length === 0) {
      // No ear segments to write — still need to retire the slot once
      // the gesture window passes so we don't leak state.
      if (now >= this._earWiggle.until) {
        this._earWiggle = null;
      }
      return;
    }
    if (now < this._earWiggle.until) {
      const value = this._earWiggleValue(now);
      for (const id of ids) {
        adapter.setParam(id, value);
      }
    } else {
      for (const id of ids) {
        adapter.setParam(id, 0);
      }
      this._earWiggle = null;
    }
  }

  private _earWiggleValue(now: number): number {
    const t = now / 1000;
    return Math.sin(2 * Math.PI * EAR_FREQ_HZ * t) * EAR_AMP;
  }

  private _tickWink(
    adapter: Live2DModelAdapter,
    now: number,
    gesture: ActiveGesture | null,
    paramId: string,
    setSlot: (next: ActiveGesture | null) => void,
  ): void {
    if (!gesture) {
      return;
    }
    if (now < gesture.until) {
      adapter.setParam(paramId, 0);
      return;
    }
    if (!gesture.released) {
      adapter.setParam(paramId, 1);
      gesture.released = true;
    }
    setSlot(null);
  }

  // ── test-only accessors ──────────────────────────────────────────
  /** Whether any gesture (wink, ear) is currently active. */
  get isAnyActive(): boolean {
    return Boolean(this._winkLeft || this._winkRight || this._earWiggle);
  }
}
