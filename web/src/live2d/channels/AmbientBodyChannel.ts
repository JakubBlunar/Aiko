/**
 * AmbientBodyChannel — every "she's alive" effect that runs every
 * frame regardless of LLM events:
 *
 *   - Auto-blush envelope (mood-driven).
 *   - Auto-sweat envelope (mood-driven OR reaction-driven).
 *   - Cat-tail steady-state sine (arousal baseline + tail-wag boost
 *     deadline read from ``engineState.tailWagBoostUntil``).
 *   - Body language on ``ParamBodyAngleY`` / ``ParamBodyAngleZ``:
 *       (a) listening lean-in, (b) tired slump, (c) excited bounce,
 *       (d) idle breathing sway, (e) sass tilt on amused / playful
 *       reaction transitions.
 *   - Continuous-expressiveness drivers (``tickPreModel``):
 *       (i) ``ParamBreath`` — arousal-scaled sine that overrides the
 *           rig's built-in breath driver. Faster + deeper at high
 *           arousal, gentler at low arousal.
 *       (ii) ``ParamBodyAngleY`` valence-tilt bias — positive
 *            valence reads as a slight lean forward / chest-up,
 *            negative valence as a downcast tilt. Smoothed with
 *            ``approach()`` so a mood flip doesn't snap.
 *
 * Cleanup writes (zeroing every owned param) happen in ``detach``
 * so a remount doesn't inherit a half-applied envelope.
 *
 * Design notes:
 *
 *   - Capability gating is inside each per-effect block so a rig
 *     missing one (e.g. no ``has_body_angle_z``) silently no-ops
 *     that slice without skipping the rest.
 *
 *   - The reaction edge for the sass tilt uses the
 *     ``onReaction`` discrete event rather than polling the store
 *     snapshot. That way two consecutive turns with the same
 *     ``amused`` reaction don't re-trigger the burst — the same
 *     contract the legacy code had.
 *
 *   - Cat-tail boost: GestureChannel writes
 *     ``engineState.tailWagBoostUntil`` on ``[[overlay:tail_wag]]``
 *     and self-clears it on expiry. We just read it; if the
 *     deadline is in the future, multiply freq by 1.8 and amp by
 *     1.5. No private state needed here.
 *
 *   - The continuous drivers run from ``tickPreModel`` because
 *     pixi-live2d-display's update order writes Add-blend
 *     contributions to ``ParamBreath`` and ``ParamBodyAngle*`` from
 *     its built-in breath / focus drivers; only ``beforeModelUpdate``
 *     sees the final pre-commit state and lets us absolute-write
 *     overrides. See ``docs/alexia-model-notes.md`` §5 for the
 *     write-order audit.
 *
 *   - Every amplitude on the body-language layer is multiplied by
 *     ``snap.expressiveness`` (default ``1``) so the Settings drawer
 *     slider damps or amplifies the entire renderer uniformly. A
 *     value of ``0`` mutes the mood-driven drivers; ``1.5`` is the
 *     authored upper bound. Backwards-compatible: missing field
 *     defaults to ``1``.
 */
import { approach } from "../math";
import {
  ambientBodyTargets, breathMotionFor, isTailWagActive, tailMotionFor,
} from "../behavior/ambientBodyStates";
import type {
  AvatarChannel,
  ChannelDeps,
  Live2DModelAdapter,
} from "../types";

const SASS_REACTIONS = new Set(["amused", "playful"]);

const BLUSH_TIME_CONSTANT_S = 0.6;
const SWEAT_TIME_CONSTANT_S = 1.5;
const LEAN_IN_TIME_CONSTANT_S = 0.4;
const SLUMP_TIME_CONSTANT_S = 0.8;
const SASS_DURATION_S = 0.8;
const SASS_AMPLITUDE = 5;
const LEAN_IN_AMPLITUDE = 6;
const SLUMP_AMPLITUDE = -3;
const BREATH_AMPLITUDE = 1.5;
const BREATH_PERIOD_S = 6;
/** Base breath frequency (Hz) — matches the pixi-live2d-display
 * default of ~0.21 Hz / 4.8s period. We modulate around this with
 * arousal: low arousal -> slower breath, high arousal -> faster. */
const BREATH_BASE_HZ = 0.21;
/** Peak amplitude (degrees) of the valence tilt. Positive valence
 * yields a slight lean forward; negative valence a downcast tilt.
 * Kept small so it stacks cleanly with lean-in / slump / bounce. */
const VALENCE_TILT_AMPLITUDE = 3;
/** Time constant for the valence-tilt smoothing — slow enough that a
 * post-turn mood flip eases in instead of snapping, fast enough that
 * the user feels the change on the next reply. */
const VALENCE_TILT_TIME_CONSTANT_S = 1.2;

export class AmbientBodyChannel implements AvatarChannel {
  readonly name = "ambientBody";

  private _adapter: Live2DModelAdapter | null = null;
  private _deps: ChannelDeps | null = null;

  private _blush = 0;
  private _sweat = 0;
  private _leanIn = 0;
  private _slump = 0;
  private _bodyY = 0;
  private _bodyZ = 0;
  private _lastReaction = "";
  /** Monotonic timestamp of the most recent rising-edge sass
   * trigger. ``-Infinity`` so the first frame's "now - sassAt"
   * comparison cleanly excludes the burst. */
  private _sassTriggeredAt = -Infinity;
  /** Critically-damped envelope tracking the latest mood ``valence``.
   * Ranges over [-1, 1]. Read every ``tickPreModel`` to bias
   * ``ParamBodyAngleY``. We smooth on this side rather than in the
   * write so the mood-flip is gradual even at high frame rates. */
  private _valenceTilt = 0;
  /** Monotonic timestamp of the most recent ``tickPreModel`` call.
   * ``0`` until the first tick — used to compute a stable ``dt``
   * inside ``tickPreModel`` since the engine doesn't pass one. */
  private _lastPreModelAt = 0;

  attach(adapter: Live2DModelAdapter, deps: ChannelDeps): void {
    this._adapter = adapter;
    this._deps = deps;
    this._blush = 0;
    this._sweat = 0;
    this._leanIn = 0;
    this._slump = 0;
    this._bodyY = 0;
    this._bodyZ = 0;
    this._lastReaction = deps.getStoreSnapshot().reaction || "";
    this._sassTriggeredAt = -Infinity;
    this._valenceTilt = 0;
    this._lastPreModelAt = 0;
  }

  detach(): void {
    const adapter = this._adapter;
    const manifest = this._deps?.manifest;
    if (adapter && manifest) {
      const caps = manifest.capabilities ?? {};
      const overlays = manifest.overlays ?? {};
      if (caps.has_blush && overlays.blush) {
        adapter.setParam(overlays.blush.param_id, 0);
      }
      if (caps.has_sweat && overlays.sweat) {
        adapter.setParam(overlays.sweat.param_id, 0);
      }
      if (caps.has_body_angle_y) {
        adapter.setParam("ParamBodyAngleY", 0);
      }
      if (caps.has_body_angle_z) {
        adapter.setParam("ParamBodyAngleZ", 0);
      }
    }
    this._adapter = null;
    this._deps = null;
  }

  /** Reaction transition is handled via the engine's discrete event
   * so the sass burst only fires on a true rising edge — not every
   * frame the snapshot happens to hold ``amused``. */
  onReaction(reaction: string): void {
    if (!this._deps) {
      return;
    }
    const next = (reaction || "").toLowerCase();
    if (next !== this._lastReaction && SASS_REACTIONS.has(next)) {
      this._sassTriggeredAt = this._deps.now();
    }
    this._lastReaction = next;
  }

  tickTier3(now: number, dt: number): void {
    const adapter = this._adapter;
    const deps = this._deps;
    if (!adapter || !deps) {
      return;
    }
    const caps = deps.manifest.capabilities ?? {};
    const snap = deps.getStoreSnapshot();
    const arousal = clamp01(snap.mood?.arousal ?? 0.4);
    const expressiveness = clampExpressiveness(snap.expressiveness);
    const targets = ambientBodyTargets(snap, arousal);

    this._updateFace(adapter, deps.manifest, targets, dt);
    if (caps.has_cat_tail) {
      const motion = tailMotionFor(
        snap.sleepStatus, isTailWagActive(now, deps.engineState.tailWagBoostUntil),
      );
      this._writeTail(adapter, deps.manifest.cat_tail_param_ids ?? [], motion, now, arousal);
    }
    if (caps.has_body_angle_y || caps.has_body_angle_z) {
      this._updatePose(targets, now, dt, expressiveness);
      this._writePose(adapter, caps);
    }
  }

  private _updateFace(
    adapter: Live2DModelAdapter,
    manifest: ChannelDeps["manifest"],
    targets: ReturnType<typeof ambientBodyTargets>,
    dt: number,
  ): void {
    const caps = manifest.capabilities ?? {};
    const overlays = manifest.overlays ?? {};
    if (caps.has_blush && overlays.blush) {
      this._blush = approach(this._blush, targets.blush, dt / BLUSH_TIME_CONSTANT_S);
      adapter.setParam(overlays.blush.param_id, this._blush * overlays.blush.on_value);
    }
    if (caps.has_sweat && overlays.sweat) {
      this._sweat = approach(this._sweat, targets.sweat, dt / SWEAT_TIME_CONSTANT_S);
      adapter.setParam(overlays.sweat.param_id, this._sweat * overlays.sweat.on_value);
    }
  }

  private _writeTail(
    adapter: Live2DModelAdapter,
    ids: readonly string[],
    motion: ReturnType<typeof tailMotionFor>,
    now: number,
    arousal: number,
  ): void {
    const angle = 2 * Math.PI * (0.3 + 1.1 * arousal) * motion.frequency * (now / 1000);
    const amplitude = (4 + 12 * arousal) * motion.amplitude;
    for (let i = 0; i < ids.length; i += 1) {
      adapter.setParam(ids[i], Math.sin(angle + i * 0.7) * amplitude);
    }
  }

  private _updatePose(
    targets: ReturnType<typeof ambientBodyTargets>,
    now: number,
    dt: number,
    expressiveness: number,
  ): void {
    this._leanIn = approach(this._leanIn, targets.lean, dt / LEAN_IN_TIME_CONSTANT_S);
    this._slump = approach(this._slump, targets.slump, dt / SLUMP_TIME_CONSTANT_S);
    const phase = (now / 1000) * 2 * Math.PI;
    const bounce = Math.sin(phase * 1.4) * targets.bounce;
    const sway = Math.sin(phase / BREATH_PERIOD_S) * BREATH_AMPLITUDE;
    const sassAge = (now - this._sassTriggeredAt) / 1000;
    const sass = sassAge >= 0 && sassAge < SASS_DURATION_S
      ? SASS_AMPLITUDE * (1 - sassAge / SASS_DURATION_S)
      : 0;
    // These envelopes are independent. A listening lean and a tired slump
    // can overlap while each eases toward its own target.
    this._bodyY = (this._leanIn * LEAN_IN_AMPLITUDE + this._slump * SLUMP_AMPLITUDE + bounce)
      * expressiveness;
    this._bodyZ = (sway + sass) * expressiveness;
  }

  private _writePose(
    adapter: Live2DModelAdapter,
    caps: NonNullable<ChannelDeps["manifest"]["capabilities"]>,
    valenceBias = 0,
  ): void {
    if (caps.has_body_angle_y) adapter.setParam("ParamBodyAngleY", this._bodyY + valenceBias);
    if (caps.has_body_angle_z) adapter.setParam("ParamBodyAngleZ", this._bodyZ);
  }

  /** Continuous-expressiveness layer.
   *
   * Runs in ``beforeModelUpdate`` so we *win* over the rig's
   * built-in breath driver (which writes Add-blend on
   * ``ParamBreath`` at the end of ``saveParameters``). For the
  * body angles we commit the cached body-language pose plus the
  * valence tilt after native motion, focus, and physics writes. */
  tickPreModel(): void {
    const adapter = this._adapter;
    const deps = this._deps;
    if (!adapter || !deps) {
      return;
    }
    const now = deps.now();
    const dt =
      this._lastPreModelAt > 0
        ? Math.max(0, Math.min(0.25, (now - this._lastPreModelAt) / 1000))
        : 0;
    this._lastPreModelAt = now;
    const snap = deps.getStoreSnapshot();
    const arousal = clamp01(snap.mood?.arousal ?? 0.4);
    const valence = clampSigned(snap.mood?.valence ?? 0);
    const expressiveness = clampExpressiveness(snap.expressiveness);
    const caps = deps.manifest.capabilities ?? {};

    // (i) ParamBreath — arousal-scaled wave that overrides the
    // built-in breath driver. Range: 0..1 (matches the rig's
    // authored convention for ``ParamBreath``). At expressiveness 0
    // we still need the rig to *breathe* a little — completely
    // muting it looks unsettling — so we collapse to a fixed
    // mid-value (0.5) instead of zero. Frequency scales linearly
    // with arousal in the [0.7, 1.4] band around ``BREATH_BASE_HZ``.
    if (caps.has_breath) {
      const boosted = caps.has_tail_wag === true
        && isTailWagActive(now, deps.engineState.tailWagBoostUntil);
      this._writeBreath(
        adapter, breathMotionFor(expressiveness, boosted), now, arousal, expressiveness,
      );
    }

    if (caps.has_body_angle_y) {
      this._valenceTilt = approach(this._valenceTilt, valence, dt / VALENCE_TILT_TIME_CONSTANT_S);
    }
    this._writePose(adapter, caps, this._valenceTilt * VALENCE_TILT_AMPLITUDE * expressiveness);
  }

  private _writeBreath(
    adapter: Live2DModelAdapter,
    motion: ReturnType<typeof breathMotionFor>,
    now: number,
    arousal: number,
    expressiveness: number,
  ): void {
    // Some rigs drive their tail from breath physics. Boost that input
    // here, after the native driver has run.
    const frequency = BREATH_BASE_HZ * (0.7 + 0.7 * arousal) * motion.frequency;
    const amplitude = Math.min(0.5, 0.5 * Math.min(1, expressiveness) * motion.amplitude);
    const value = amplitude === 0 ? 0.5 : 0.5 + amplitude * Math.sin(2 * Math.PI * frequency * now / 1000);
    adapter.setParam("ParamBreath", value);
  }

  // ── test-only accessors ──────────────────────────────────────────
  get blushEnvelope(): number {
    return this._blush;
  }
  get sweatEnvelope(): number {
    return this._sweat;
  }
  get leanInEnvelope(): number {
    return this._leanIn;
  }
  get slumpEnvelope(): number {
    return this._slump;
  }
  get sassTriggeredAt(): number {
    return this._sassTriggeredAt;
  }
  get valenceTiltEnvelope(): number {
    return this._valenceTilt;
  }
}

function clamp01(value: number): number {
  if (value < 0) return 0;
  if (value > 1) return 1;
  return value;
}

function clampSigned(value: number): number {
  if (!Number.isFinite(value)) return 0;
  if (value < -1) return -1;
  if (value > 1) return 1;
  return value;
}

/** Fall back to ``1`` for legacy snapshots that pre-date the
 * ``expressiveness`` field, and clamp the user-driven slider value
 * into the documented [0, 1.5] band so a runaway value can't
 * amplify writes past safe rig limits. */
function clampExpressiveness(value: number | undefined): number {
  if (value === undefined || value === null || !Number.isFinite(value)) {
    return 1;
  }
  if (value < 0) return 0;
  if (value > 1.5) return 1.5;
  return value;
}
