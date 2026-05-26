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
 *     "twitch in unison" we want.
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

    // Ear-wiggle: 4Hz sine on every ear segment while alive, then
    // snap back to 0 once on expiry. ``cat_ear_param_ids`` may be
    // empty even when ``has_ear_wiggle`` is true (ear capability
    // detection on a rig without independent segments) — we use
    // the same loop guard as the legacy code.
    if (caps.has_ear_wiggle && this._earWiggle) {
      const ids = deps.manifest.cat_ear_param_ids ?? [];
      if (ids.length > 0) {
        if (now < this._earWiggle.until) {
          const t = now / 1000;
          const value = Math.sin(2 * Math.PI * EAR_FREQ_HZ * t) * EAR_AMP;
          for (const id of ids) {
            adapter.setParam(id, value);
          }
        } else {
          for (const id of ids) {
            adapter.setParam(id, 0);
          }
          this._earWiggle = null;
        }
      } else if (now >= this._earWiggle.until) {
        this._earWiggle = null;
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
