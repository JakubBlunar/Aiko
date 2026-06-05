/**
 * ReachChannel — K31 soft physicality.
 *
 * Plays the per-frame "Aiko reaches toward the user" animation
 * whenever an ``avatar_touch`` WS event arrives. Because the
 * Alexia rig has no Z-depth ("dolly in") parameter and no
 * dedicated arm-control parameters, the renderer approximates a
 * reach with two layered effects:
 *
 *   1. **Head + body pitch tilt** — ``ParamAngleY`` and
 *      ``ParamBodyAngleY`` both bias forward by the gesture's
 *      ``lean_amount`` (degrees). The amount is rig-friendly:
 *      small enough that the always-on body-language layer
 *      (lean-in / slump / bounce / valence-tilt) stays visible
 *      stacked on top, but big enough to read as "she just
 *      leaned in for a second". The amount is animated on a
 *      symmetric ease-out / ease-in curve over ``duration_ms``
 *      so the lean grows toward peak around the midpoint and
 *      eases back to zero at the deadline. Outside the window
 *      the channel writes nothing (so it composes additively
 *      with whatever AmbientBodyChannel already wrote that
 *      frame; see ``tickTier3`` for the read-modify-write
 *      handoff).
 *
 *   2. **Paired overlays** — every gesture in the K31 taxonomy
 *      carries a list of ``overlays`` (blush, warm smile, etc.)
 *      that the backend already dispatched through the existing
 *      ``[[overlay:X]]`` path. We don't fire them from here;
 *      OverlayChannel + GestureChannel already own that.
 *
 * The literal meaning of the gesture (a poke vs a hug vs a head
 * pat) is conveyed by the bubble badge / persona action banner —
 * the avatar lean is just the body-language *fingerprint* that
 * all eight kinds share.
 *
 * Capability gating: ``has_body_angle_y`` is required (every
 * non-trivial rig has it). The head-pitch addition is gated on
 * ``has_head_angle_y`` separately so a body-only rig still gets
 * the lean. When both are missing the channel silently no-ops.
 *
 * State management: at most one reach is in flight at a time —
 * a new ``onTouch`` arriving mid-animation simply restarts the
 * timeline. The expiry-frame writes 0 to both params (one-shot)
 * and clears the active slot so the channel becomes invisible
 * to the param fan-out until the next gesture lands.
 */
import type {
  AvatarChannel,
  ChannelDeps,
  Live2DModelAdapter,
  ResolvedTouchEvent,
} from "../types";

const HEAD_PARAM = "ParamAngleY";
const BODY_PARAM = "ParamBodyAngleY";

/** Fraction of ``lean_amount`` the head adds on top of the body
 * tilt. A small multiplier so the head doesn't overshoot the body
 * (which would read as "she's nodding off" not "leaning in"). */
const HEAD_LEAN_RATIO = 0.6;

interface ActiveReach {
  /** Monotonic timestamp the animation started at. */
  startedAt: number;
  /** Total lifetime in ms (= ``until - startedAt``). */
  durationMs: number;
  /** Peak forward-tilt in degrees on the body param. */
  leanAmount: number;
  /** Has the on-expiry rest-write fired yet? Prevents the
   * channel from writing ``0`` every frame after the deadline. */
  released: boolean;
}

export class ReachChannel implements AvatarChannel {
  readonly name = "reach";

  private _adapter: Live2DModelAdapter | null = null;
  private _deps: ChannelDeps | null = null;
  private _active: ActiveReach | null = null;

  attach(adapter: Live2DModelAdapter, deps: ChannelDeps): void {
    this._adapter = adapter;
    this._deps = deps;
    this._active = null;
  }

  detach(): void {
    // Best-effort release so a remount doesn't inherit a half-tilted
    // pose. Capability-gated so a body-only rig doesn't write a
    // head-pitch zero (which would still be a no-op on the adapter,
    // but we keep the same pattern as GestureChannel.detach()).
    const adapter = this._adapter;
    const deps = this._deps;
    if (adapter && deps) {
      const caps = deps.manifest.capabilities ?? {};
      if (caps.has_body_angle_y) {
        adapter.setParam(BODY_PARAM, 0);
      }
      if (caps.has_head_angle_y) {
        adapter.setParam(HEAD_PARAM, 0);
      }
    }
    this._adapter = null;
    this._deps = null;
    this._active = null;
  }

  /** Engine-dispatched on every ``avatar_touch`` WS frame. A new
   * event mid-animation simply restarts the timeline — multiple
   * back-to-back gestures (rare but possible) read as one
   * continuous lean rather than overlapping tilts. */
  onTouch(event: ResolvedTouchEvent): void {
    if (!this._deps) {
      return;
    }
    const caps = this._deps.manifest.capabilities ?? {};
    // No body angle + no head angle = nothing to animate. Bail
    // before touching state so test fixtures with minimal rigs
    // don't accumulate noise on the deps.debug hook.
    if (!caps.has_body_angle_y && !caps.has_head_angle_y) {
      return;
    }
    const now = this._deps.now();
    const durationMs = Math.max(
      0,
      // ``until`` is monotonic; the engine guarantees that.
      event.until - now,
    );
    if (durationMs <= 0) {
      // Already expired by the time we got here -- skip.
      return;
    }
    this._active = {
      startedAt: now,
      durationMs,
      leanAmount: event.leanAmount,
      released: false,
    };
    this._deps.debug?.("channel.reach", "onTouch", {
      kind: event.kind,
      leanAmount: event.leanAmount,
      durationMs,
    });
  }

  tickTier3(now: number, _dt: number): void {
    const adapter = this._adapter;
    const deps = this._deps;
    if (!adapter || !deps) {
      return;
    }
    const active = this._active;
    if (!active) {
      return;
    }
    const caps = deps.manifest.capabilities ?? {};
    const elapsed = now - active.startedAt;
    if (elapsed >= active.durationMs) {
      // Expiry frame: write rest exactly once, then drop the slot.
      if (!active.released) {
        if (caps.has_body_angle_y) {
          // Don't snap to absolute 0 -- that would clobber whatever
          // AmbientBodyChannel wrote this frame. We read-modify-
          // write: subtract our last contribution by writing the
          // adapter's current value minus the (already-zero) lean.
          // In practice the channel just stops contributing; the
          // next AmbientBody tick (within ~16ms) overwrites cleanly.
          const current = adapter.getParam(BODY_PARAM) ?? 0;
          adapter.setParam(BODY_PARAM, current);
        }
        if (caps.has_head_angle_y) {
          const current = adapter.getParam(HEAD_PARAM) ?? 0;
          adapter.setParam(HEAD_PARAM, current);
        }
        active.released = true;
        deps.debug?.("channel.reach", "expired", {});
      }
      this._active = null;
      return;
    }

    // Symmetric ease-out / ease-in: peak at the midpoint, smooth
    // ramp-up and ramp-down. ``sin(pi * t)`` gives exactly that
    // shape on ``t in [0, 1]``: 0 at t=0, 1 at t=0.5, 0 at t=1.
    const t = elapsed / active.durationMs;
    const easing = Math.sin(Math.PI * Math.max(0, Math.min(1, t)));
    const bodyDelta = easing * active.leanAmount;
    const headDelta = easing * active.leanAmount * HEAD_LEAN_RATIO;

    // Read-modify-write so we layer on top of AmbientBody's lean-in
    // / slump / bounce / valence-tilt rather than clobbering it.
    // The ``getParam`` returns the *last value the engine wrote*
    // (write-cache) which is exactly what AmbientBody set earlier
    // in the same tick (AmbientBody runs before ReachChannel by
    // registration order in StoreBridge).
    if (caps.has_body_angle_y) {
      const baseline = adapter.getParam(BODY_PARAM) ?? 0;
      adapter.setParam(BODY_PARAM, baseline + bodyDelta);
    }
    if (caps.has_head_angle_y) {
      const baseline = adapter.getParam(HEAD_PARAM) ?? 0;
      adapter.setParam(HEAD_PARAM, baseline + headDelta);
    }
  }

  // ── test-only accessors ──────────────────────────────────────────
  /** Whether a reach animation is currently in flight. */
  get isActive(): boolean {
    return this._active !== null;
  }
}
