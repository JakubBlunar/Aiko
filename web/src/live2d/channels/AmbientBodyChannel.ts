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
 */
import { approach } from "../math";
import type {
  AvatarChannel,
  ChannelDeps,
  Live2DModelAdapter,
} from "../types";

const SWEAT_MOOD_LABELS = new Set(["concerned", "confused", "frustrated"]);
const BLUSH_MOOD_LABELS = new Set(["tender", "warm"]);
const SWEAT_REACTIONS = new Set(["concerned", "confused", "frustrated"]);
const SASS_REACTIONS = new Set(["amused", "playful"]);
const SLUMP_MOOD_LABELS = new Set(["tired", "exhausted"]);
const LISTENING_VOICE_MODES = new Set(["listening", "transcribing"]);

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
const TAIL_BOOST_FREQ_MUL = 1.8;
const TAIL_BOOST_AMP_MUL = 1.5;

export class AmbientBodyChannel implements AvatarChannel {
  readonly name = "ambientBody";

  private _adapter: Live2DModelAdapter | null = null;
  private _deps: ChannelDeps | null = null;

  private _blush = 0;
  private _sweat = 0;
  private _leanIn = 0;
  private _slump = 0;
  private _lastReaction = "";
  /** Monotonic timestamp of the most recent rising-edge sass
   * trigger. ``-Infinity`` so the first frame's "now - sassAt"
   * comparison cleanly excludes the burst. */
  private _sassTriggeredAt = -Infinity;

  attach(adapter: Live2DModelAdapter, deps: ChannelDeps): void {
    this._adapter = adapter;
    this._deps = deps;
    this._blush = 0;
    this._sweat = 0;
    this._leanIn = 0;
    this._slump = 0;
    this._lastReaction = deps.getStoreSnapshot().reaction || "";
    this._sassTriggeredAt = -Infinity;
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
    const overlays = deps.manifest.overlays ?? {};
    const catTailIds = deps.manifest.cat_tail_param_ids ?? [];
    const snap = deps.getStoreSnapshot();
    const moodLabel = (snap.mood?.label || "").toLowerCase();
    const moodIntensity = snap.mood?.intensity ?? 0;
    const arousal = clamp01(snap.mood?.arousal ?? 0.4);
    const reaction = (snap.reaction || "").toLowerCase();
    const circadian = snap.circadianPeriod || "";

    // ── auto-blush ───────────────────────────────────────────────
    if (caps.has_blush && overlays.blush) {
      const target =
        BLUSH_MOOD_LABELS.has(moodLabel) && moodIntensity > 0.4 ? 1 : 0;
      this._blush = approach(this._blush, target, dt * (1 / BLUSH_TIME_CONSTANT_S));
      adapter.setParam(overlays.blush.param_id, this._blush * overlays.blush.on_value);
    }

    // ── auto-sweat ───────────────────────────────────────────────
    if (caps.has_sweat && overlays.sweat) {
      const target =
        SWEAT_MOOD_LABELS.has(moodLabel) || SWEAT_REACTIONS.has(reaction) ? 1 : 0;
      this._sweat = approach(this._sweat, target, dt * (1 / SWEAT_TIME_CONSTANT_S));
      adapter.setParam(overlays.sweat.param_id, this._sweat * overlays.sweat.on_value);
    }

    // ── cat-tail steady-state + tail-wag boost ───────────────────
    if (caps.has_cat_tail && catTailIds.length > 0) {
      const boostUntil = deps.engineState.tailWagBoostUntil;
      const tailBoost = boostUntil > 0 && now < boostUntil;
      const freq = (0.3 + 1.1 * arousal) * (tailBoost ? TAIL_BOOST_FREQ_MUL : 1);
      const amp = (4 + 12 * arousal) * (tailBoost ? TAIL_BOOST_AMP_MUL : 1);
      const t = now / 1000;
      for (let i = 0; i < catTailIds.length; i += 1) {
        const phase = i * 0.7;
        const value = Math.sin(2 * Math.PI * freq * t + phase) * amp;
        adapter.setParam(catTailIds[i], value);
      }
    }

    // ── body language ────────────────────────────────────────────
    if (caps.has_body_angle_y || caps.has_body_angle_z) {
      let bodyY = 0;
      let bodyZ = 0;

      // (a) Listening lean-in.
      const isListeningNow = LISTENING_VOICE_MODES.has(snap.voiceMode);
      this._leanIn = approach(
        this._leanIn,
        isListeningNow ? 1 : 0,
        dt / LEAN_IN_TIME_CONSTANT_S,
      );
      bodyY += this._leanIn * LEAN_IN_AMPLITUDE;

      // (b) Tired slump.
      const slumpTrigger =
        SLUMP_MOOD_LABELS.has(moodLabel) ||
        (circadian === "late_night" && arousal < 0.3);
      this._slump = approach(
        this._slump,
        slumpTrigger ? 1 : 0,
        dt / SLUMP_TIME_CONSTANT_S,
      );
      bodyY += this._slump * SLUMP_AMPLITUDE;

      // (c) Excited bounce.
      if (arousal > 0.6) {
        bodyY += Math.sin((now / 1000) * 2 * Math.PI * 1.4) * (1 + arousal * 2);
      }

      // (d) Idle breathing sway.
      bodyZ += Math.sin(((now / 1000) * 2 * Math.PI) / BREATH_PERIOD_S) * BREATH_AMPLITUDE;

      // (e) Sass tilt on rising edge.
      const sassAge = (now - this._sassTriggeredAt) / 1000;
      if (sassAge >= 0 && sassAge < SASS_DURATION_S) {
        bodyZ += SASS_AMPLITUDE * (1 - sassAge / SASS_DURATION_S);
      }

      if (caps.has_body_angle_y) {
        adapter.setParam("ParamBodyAngleY", bodyY);
      }
      if (caps.has_body_angle_z) {
        adapter.setParam("ParamBodyAngleZ", bodyZ);
      }
    }
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
}

function clamp01(value: number): number {
  if (value < 0) return 0;
  if (value > 1) return 1;
  return value;
}
