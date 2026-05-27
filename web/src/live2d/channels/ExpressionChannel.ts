/**
 * ExpressionChannel — owns ``model.expression(name)`` /
 * ``model.resetExpression()`` calls.
 *
 * Three concurrent priorities, highest wins:
 *
 *   1. **Voice mode** ("listening" / "transcribing" / "thinking") —
 *      while the user is mid-utterance or the LLM is composing,
 *      a thoughtful expression takes the slot until the mode leaves.
 *
 *   2. **Backchannel hint** ("agreement" / "surprise" / ...) — a
 *      transient overlay applied for ~1.8s on the rising edge of
 *      a backchannel hint. Restored to the persistent reaction (or
 *      the current voice mode's expression) after the window.
 *
 *   3. **Persistent reaction** — the LLM's reaction tag. The
 *      baseline expression while the assistant is idle / responding.
 *
 * Coordination with OverlayChannel: an ``[[overlay:grin]]`` pulse
 * is fired by ``OverlayChannel`` directly via ``adapter.expression(name)``
 * and writes ``engineState.exprSlotLockUntil`` to a future deadline.
 * While that deadline is in the future, ExpressionChannel skips
 * any reaction / mode / backchannel write — the overlay owns the
 * slot. When the engine fans ``onExpressionSlotReleased`` (the
 * deadline passed), ExpressionChannel re-applies whatever its
 * current target is. This is the regression-fix path that
 * originally motivated the refactor: a single ``[[overlay:grin]]``
 * on a neutral turn used to leave the rig smiling forever.
 *
 * Empty / unmapped reactions: when ``resolveReactionExpression``
 * returns ``undefined``, the channel calls
 * ``adapter.resetExpression()`` instead of doing nothing. The
 * legacy bug was leaving the previous expression on the face when
 * a turn ended on a "neutral" reaction with an empty mapping.
 *
 * Continuous-expressiveness layer (B1):
 *   - ``tickPreModel`` writes the current expression's params with
 *     value = ``on_value * arousalScale * expressiveness`` directly
 *     in ``beforeModelUpdate``. The rig's ``expressionManager`` is
 *     still firing its Add-blend on the same params; our absolute
 *     write happens last and dominates the final committed value.
 *   - Skipped while ``engineState.exprSlotLockUntil`` is in the
 *     future (overlay owns the slot) — the overlay channel writes
 *     its own value and we must not stomp it.
 *   - ``arousalScale = clamp(0.4 + 0.6 * arousal, 0.4, 1.0)``: a
 *     ``cheerful`` reaction reads ~46% on a low-arousal day, ~94%
 *     when Aiko is excited. ``expressiveness`` from the user
 *     slider applies on top.
 *
 * Mouth-overlay lip-sync suppression:
 *   - Some rigs ship a stylised "mouth shape" overlay that lives
 *     on its own param (Alexia's ``Param54`` "Grin", activated by
 *     the ``lzx`` expression). When the rig speaks while a grin-
 *     bearing expression is active, the toothy grin overlay sits on
 *     top of the lip-synced ``ParamMouthOpenY`` and you visibly see
 *     two mouths at once.
 *   - The avatar profile exposes ``mouth_overlay_param_ids`` for
 *     these. Per frame, we drive a smoothed lip-sync suppression
 *     factor off ``audioAmplitude`` (gain × clamp01) and multiply
 *     ``(1 - factor)`` into any binding whose id is in that set.
 *     Non-mouth bindings (cheek tilts, eye squint params on the
 *     same expression) are unaffected, so the rest of the smile
 *     keeps reading while the toothy overlay tapers out.
 */
import type { BackchannelHint, ExpressionParam } from "../../types";
import { approach } from "../math";
import type {
  AvatarChannel,
  AvatarManifest,
  ChannelDeps,
  Live2DModelAdapter,
} from "../types";

/** Fallback reaction-name chain used when the model doesn't directly
 * map a reaction. The server-side ``avatar_profile`` is responsible
 * for pre-baking direct mappings; this just covers the case where
 * the LLM emits a fresh label that post-dates the model load. */
const _REACTION_NEIGHBOURS: Record<string, string[]> = {
  amused: ["cheerful", "playful", "friendly", "warm", "neutral"],
  playful: ["amused", "cheerful", "excited", "friendly", "warm"],
  enthusiastic: ["excited", "cheerful", "playful", "friendly"],
  curious: ["thoughtful", "surprised", "friendly", "neutral"],
  tender: ["warm", "gentle", "friendly", "calm", "neutral"],
  warm: ["friendly", "gentle", "tender", "cheerful", "neutral"],
  thoughtful: ["serious", "calm", "concerned", "neutral"],
  wistful: ["sad", "melancholy", "thoughtful", "calm", "gentle"],
  concerned: ["serious", "sad", "thoughtful", "neutral"],
  melancholy: ["sad", "wistful", "tired", "calm", "neutral"],
  tired: ["calm", "melancholy", "neutral", "sad"],
  frustrated: ["angry", "concerned", "serious", "neutral"],
  gentle: ["warm", "calm", "friendly", "tender", "neutral"],
  friendly: ["warm", "cheerful", "neutral", "calm"],
  calm: ["neutral", "thoughtful", "gentle", "warm"],
  serious: ["thoughtful", "concerned", "neutral"],
  surprised: ["excited", "curious", "amused", "neutral"],
  cheerful: ["amused", "friendly", "warm", "playful", "neutral"],
  excited: ["enthusiastic", "cheerful", "playful", "surprised", "neutral"],
  sad: ["melancholy", "wistful", "concerned", "neutral"],
  angry: ["frustrated", "serious", "concerned", "neutral"],
  neutral: ["calm", "friendly", "warm"],
};

const _BACKCHANNEL_TO_REACTION: Record<BackchannelHint, string[]> = {
  agreement: ["cheerful", "friendly", "warm"],
  disagreement: ["serious", "concerned", "thoughtful"],
  surprise: ["surprised", "excited", "amazed"],
  amusement: ["cheerful", "amused", "playful"],
  concern: ["concerned", "sad", "gentle"],
  confused: ["confused", "thoughtful", "curious"],
  thinking: ["thoughtful", "calm", "neutral"],
};

const _MODE_TO_REACTION: Record<"listening" | "thinking", string[]> = {
  listening: ["thoughtful", "calm", "neutral", "friendly", "attentive"],
  thinking: ["thoughtful", "concerned", "calm", "serious", "neutral"],
};

const BACKCHANNEL_RESTORE_MS = 1_800;
/** Floor on the arousal scale so a ``cheerful`` at sub-zero arousal
 * doesn't read as a flat-faced neutral. */
const AROUSAL_SCALE_FLOOR = 0.4;
/** Ceiling on the arousal scale — caps at the rig's authored
 * ``on_value`` so ``expressiveness=1.5`` doesn't push us past
 * the file's intended max. */
const AROUSAL_SCALE_CEILING = 1.0;
/** Time constant for the amplitude-scale smoothing — fast enough
 * that a reaction transition doesn't feel laggy, slow enough that
 * the smile doesn't pulse at the audio_amplitude rate. */
const AMPLITUDE_TIME_CONSTANT_S = 0.4;
/** Gain applied to ``audioAmplitude`` before clamping into a
 * ``[0, 1]`` "lip-sync activity" factor. The raw amplitude bounces
 * between roughly 0.0 and 0.4 during normal speech (chunked TTS
 * with peaks well below 1.0), so a gain of ~6 means ordinary mid-
 * sentence amplitude saturates the suppression — the toothy grin
 * overlay drops away as soon as the mouth is actually moving, not
 * only at TTS peaks. Tweak in tandem with the lipsync smoothing
 * factor in ``LipsyncChannel``. */
const LIPSYNC_SUPPRESSION_GAIN = 6;
/** Time constant for the lip-sync-suppression factor itself. A
 * touch faster than the amplitude-scale TC so the grin cleanly
 * disappears within ~150 ms of speech onset (perceptually
 * synchronous with the mouth flapping) and re-emerges at a similar
 * rate once she falls silent. */
const LIPSYNC_SUPPRESSION_TIME_CONSTANT_S = 0.15;

export interface ExpressionChannelOptions {
  /** Optional schedule + cancel pair, defaulting to setTimeout /
   * clearTimeout. Tests inject a manual timer so the backchannel
   * restore window is deterministic. */
  schedule?: (cb: () => void, ms: number) => unknown;
  cancel?: (handle: unknown) => void;
}

export class ExpressionChannel implements AvatarChannel {
  readonly name = "expression";

  private _adapter: Live2DModelAdapter | null = null;
  private _deps: ChannelDeps | null = null;

  /** Latest reaction the LLM provided — the "baseline". */
  private _currentReaction = "";
  /** Latest voice mode value seen, used to gate reaction restores. */
  private _voiceMode: string = "off";
  /** Active backchannel restore timer handle, ``null`` when idle. */
  private _restoreHandle: unknown = null;
  /** Timestamp of the most recent backchannel hint dispatched, used
   * to detect "newer hint arrived before restore" race. */
  private _backchannelSeq = 0;
  /** Name of the expression file we believe is currently active —
   * used by ``tickPreModel`` to look up the right param bindings.
   * Mirrors what ``adapter.expression(name)`` was last called with
   * for non-overlay paths; overlay pulses bypass this so we don't
   * trip our own slot-lock guard. */
  private _activeExpressionName: string = "";
  /** Critically-damped amplitude scale tracking ``arousal *
   * expressiveness``. ``approach()`` smooths this every frame so a
   * sudden arousal shift doesn't pop the expression's loudness. */
  private _amplitudeScale = 0;
  /** Critically-damped "lip-sync suppression" factor in ``[0, 1]``.
   * Driven by ``audioAmplitude * LIPSYNC_SUPPRESSION_GAIN`` clamped
   * to ``[0, 1]``; multiplied into mouth-overlay bindings (e.g.
   * Alexia's ``Param54`` Grin) as ``(1 - lipsyncSuppression)`` so
   * the toothy grin fades while she's actually speaking and snaps
   * back when she falls silent. Non-mouth bindings ignore it. */
  private _lipsyncSuppression = 0;
  /** Cached set of param IDs that paint a stylised mouth-shape
   * overlay (Alexia: ``["Param54"]``). Populated once at attach
   * time from ``manifest.mouth_overlay_param_ids`` so the per-frame
   * write loop avoids re-allocating the Set. */
  private _mouthOverlayIds: Set<string> = new Set();
  /** Monotonic timestamp of the last ``tickPreModel`` call. ``0``
   * before the first tick; used to derive a frame ``dt`` since the
   * engine's ``beforeModelUpdate`` doesn't pass one. */
  private _lastPreModelAt = 0;

  private readonly _schedule: (cb: () => void, ms: number) => unknown;
  private readonly _cancel: (handle: unknown) => void;

  constructor(options: ExpressionChannelOptions = {}) {
    this._schedule =
      options.schedule ??
      ((cb, ms) => {
        if (typeof window !== "undefined") {
          return window.setTimeout(cb, ms);
        }
        return setTimeout(cb, ms) as unknown;
      });
    this._cancel =
      options.cancel ??
      ((handle) => {
        if (handle == null) {
          return;
        }
        if (typeof window !== "undefined") {
          window.clearTimeout(handle as number);
        } else {
          clearTimeout(handle as ReturnType<typeof setTimeout>);
        }
      });
  }

  attach(adapter: Live2DModelAdapter, deps: ChannelDeps): void {
    this._adapter = adapter;
    this._deps = deps;
    this._currentReaction = deps.getStoreSnapshot().reaction || "";
    this._voiceMode = deps.getStoreSnapshot().voiceMode || "off";
    this._activeExpressionName = "";
    this._amplitudeScale = 0;
    this._lipsyncSuppression = 0;
    this._lastPreModelAt = 0;
    this._mouthOverlayIds = new Set(deps.manifest.mouth_overlay_param_ids ?? []);
    // Apply the initial reaction once at attach so a fresh model
    // doesn't pop in with the default expression while the engine
    // is still wiring channels.
    this._applyTarget();
  }

  detach(): void {
    this._adapter = null;
    this._deps = null;
    this._currentReaction = "";
    this._voiceMode = "off";
    this._activeExpressionName = "";
    this._amplitudeScale = 0;
    this._lipsyncSuppression = 0;
    this._lastPreModelAt = 0;
    this._mouthOverlayIds = new Set();
    this._cancelRestore();
    this._backchannelSeq = 0;
  }

  // ── event handlers ───────────────────────────────────────────────

  onReaction(reaction: string): void {
    this._currentReaction = reaction || "";
    if (this._isExprSlotLocked()) {
      // Overlay owns the slot — the engine will fire
      // ``onExpressionSlotReleased`` when the deadline passes; we'll
      // re-apply at that point with the current value.
      return;
    }
    if (this._isVoiceModeOverriding()) {
      // Voice mode is dominating; the new reaction value is recorded
      // for later (when voice mode leaves) but no expression write
      // happens now — the screen is already showing the mode's
      // expression and we don't want to thrash.
      return;
    }
    if (this._restoreHandle !== null) {
      // Backchannel is in its 1.8s restore window — let it finish.
      return;
    }
    this._applyTarget();
  }

  onVoiceMode(mode: string): void {
    if (mode === this._voiceMode) {
      return;
    }
    this._voiceMode = mode;
    if (this._isExprSlotLocked()) {
      return;
    }
    // Cancel any in-flight backchannel restore — voice mode has a
    // higher priority. The new mode's expression takes over.
    this._cancelRestore();
    this._applyTarget();
  }

  onBackchannel(hint: string): void {
    if (!hint || !this._adapter || !this._deps) {
      return;
    }
    if (this._isExprSlotLocked()) {
      return;
    }
    const exprName = this._pickBackchannelExpression(hint);
    if (!exprName) {
      return;
    }
    this._backchannelSeq += 1;
    const seq = this._backchannelSeq;
    this._adapter.expression(exprName);
    this._cancelRestore();
    this._restoreHandle = this._schedule(() => {
      this._restoreHandle = null;
      if (seq !== this._backchannelSeq) {
        // A newer backchannel landed; that one's restore will run.
        return;
      }
      if (this._isExprSlotLocked()) {
        // Overlay grabbed the slot in the meantime; let the engine
        // release path re-apply.
        return;
      }
      this._applyTarget();
    }, BACKCHANNEL_RESTORE_MS);
  }

  onExpressionSlotReleased(): void {
    // Overlay pulse just ended. Unconditionally re-apply our current
    // target so a stuck expression clears.
    this._applyTarget();
  }

  /** Continuous-expressiveness arousal scaler.
   *
   * Runs every Pixi frame in ``beforeModelUpdate`` (the last
   * writable point before ``model.update`` commits parameters) so
   * our absolute writes win over the rig's
   * ``expressionManager`` Add-blend on the same params. The trick
   * is that we don't *replace* the manager — it's still doing the
   * fade-in / fade-out / lifecycle. Our write just fixes the final
   * committed amplitude per param.
   *
   * Skipped when:
   *   - no adapter / deps yet (pre-attach);
   *   - the overlay channel owns the slot
   *     (``engineState.exprSlotLockUntil > now()``);
   *   - we have no active expression name to look up (e.g. the
   *     reaction unmapped to anything and we called
   *     ``resetExpression``);
   *   - the manifest doesn't carry ``expression_params`` for the
   *     active expression (legacy / minimal rig — let the manager's
   *     natural amplitude through).
   */
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

    if (this._isExprSlotLocked()) {
      // Overlay owns the slot. Reset our amplitude smoothing so the
      // next non-overlay frame ramps in cleanly instead of snapping
      // back from the overlay's amplitude.
      this._amplitudeScale = 0;
      this._lipsyncSuppression = 0;
      return;
    }
    const expressionName = this._activeExpressionName;
    if (!expressionName) {
      this._amplitudeScale = 0;
      this._lipsyncSuppression = 0;
      return;
    }
    const bindings = pickExpressionBindings(deps.manifest, expressionName);
    if (!bindings || bindings.length === 0) {
      this._amplitudeScale = 0;
      this._lipsyncSuppression = 0;
      return;
    }

    const snap = deps.getStoreSnapshot();
    const arousal = clamp01(snap.mood?.arousal ?? 0.4);
    const expressiveness = clampExpressiveness(snap.expressiveness);
    const arousalScale = clamp(
      AROUSAL_SCALE_FLOOR + (1 - AROUSAL_SCALE_FLOOR) * arousal,
      AROUSAL_SCALE_FLOOR,
      AROUSAL_SCALE_CEILING,
    );
    const targetScale = arousalScale * expressiveness;
    const rate = dt > 0 ? dt / AMPLITUDE_TIME_CONSTANT_S : 0;
    this._amplitudeScale = approach(this._amplitudeScale, targetScale, rate);

    // Lip-sync suppression: drive a separate smoothed factor off the
    // raw audio amplitude so any "draws-a-mouth-shape" expression
    // param (Alexia's Param54 Grin) tapers out while she's speaking.
    // Computed even when there are no overlay bindings so the value
    // stays warm — switching to a grin reaction mid-speech ramps
    // smoothly instead of snapping in fully visible.
    const lipsyncTarget = clamp(
      (snap.audioAmplitude || 0) * LIPSYNC_SUPPRESSION_GAIN,
      0,
      1,
    );
    const lipsyncRate =
      dt > 0 ? dt / LIPSYNC_SUPPRESSION_TIME_CONSTANT_S : 0;
    this._lipsyncSuppression = approach(
      this._lipsyncSuppression,
      lipsyncTarget,
      lipsyncRate,
    );
    const mouthScale =
      this._mouthOverlayIds.size > 0 ? 1 - this._lipsyncSuppression : 1;

    for (const binding of bindings) {
      const isMouthOverlay = this._mouthOverlayIds.has(binding.param_id);
      const value =
        binding.on_value *
        this._amplitudeScale *
        (isMouthOverlay ? mouthScale : 1);
      adapter.setParam(binding.param_id, value);
    }
  }

  // ── internals ────────────────────────────────────────────────────

  private _isExprSlotLocked(): boolean {
    const deps = this._deps;
    if (!deps) {
      return false;
    }
    return deps.engineState.exprSlotLockUntil > deps.now();
  }

  private _isVoiceModeOverriding(): boolean {
    return (
      this._voiceMode === "listening" ||
      this._voiceMode === "transcribing" ||
      this._voiceMode === "thinking"
    );
  }

  /** Apply whichever target our current state implies, in priority
   * order: voice mode > persistent reaction. (Backchannel is a
   * one-shot overlay, not a sticky target — it's applied directly
   * in ``onBackchannel`` and restored via timer.) */
  private _applyTarget(): void {
    const adapter = this._adapter;
    const deps = this._deps;
    if (!adapter || !deps) {
      return;
    }
    if (this._voiceMode === "listening" || this._voiceMode === "transcribing") {
      const expr = pickModeExpression(deps.manifest, "listening");
      this._applyExpressionByName(adapter, expr);
      return;
    }
    if (this._voiceMode === "thinking") {
      const expr = pickModeExpression(deps.manifest, "thinking");
      this._applyExpressionByName(adapter, expr);
      return;
    }
    // No mode override — apply the persistent reaction.
    const expressionName = resolveReactionExpression(
      deps.manifest,
      this._currentReaction,
    );
    if (!expressionName) {
      // Empty / unmapped reaction — clear any active overlay so the
      // rig returns to its default. ``resetExpression`` runs the
      // ExpressionManager's empty default motion, releasing the slot.
      adapter.resetExpression();
      this._activeExpressionName = "";
      return;
    }
    adapter.expression(expressionName);
    this._activeExpressionName = expressionName;
  }

  private _applyExpressionByName(
    adapter: Live2DModelAdapter,
    expressionName: string | undefined,
  ): void {
    if (!expressionName) {
      return;
    }
    adapter.expression(expressionName);
    this._activeExpressionName = expressionName;
  }

  private _pickBackchannelExpression(hint: string): string | undefined {
    const deps = this._deps;
    if (!deps) {
      return undefined;
    }
    const manifest = deps.manifest;
    const candidates = _BACKCHANNEL_TO_REACTION[hint as BackchannelHint] || [];
    for (const reaction of candidates) {
      const expr = manifest.reaction_mapping[reaction];
      if (expr) {
        return expr;
      }
    }
    // Last resort: any expression whose name contains the hint keyword.
    for (const expr of manifest.expressions) {
      if (expr.name.toLowerCase().includes(hint)) {
        return expr.name;
      }
    }
    return undefined;
  }

  private _cancelRestore(): void {
    if (this._restoreHandle !== null) {
      this._cancel(this._restoreHandle);
      this._restoreHandle = null;
    }
  }

  // ── test-only accessors ──────────────────────────────────────────

  /** Whether a backchannel restore timer is currently armed. */
  get backchannelRestoreArmed(): boolean {
    return this._restoreHandle !== null;
  }

  /** Smoothed amplitude scale currently driving ``tickPreModel``
   * writes. Tests assert this converges proportionally to arousal /
   * expressiveness. */
  get amplitudeScale(): number {
    return this._amplitudeScale;
  }

  /** Name of the expression file we last asked the rig to apply.
   * ``""`` when the active reaction unmapped to nothing. */
  get activeExpressionName(): string {
    return this._activeExpressionName;
  }
}

function resolveReactionExpression(
  manifest: AvatarManifest,
  reaction: string,
): string | undefined {
  if (!reaction) {
    return undefined;
  }
  const direct = manifest.reaction_mapping[reaction];
  if (direct) {
    return direct;
  }
  const neighbours = _REACTION_NEIGHBOURS[reaction] || [];
  for (const fallback of neighbours) {
    const expr = manifest.reaction_mapping[fallback];
    if (expr) {
      return expr;
    }
  }
  return undefined;
}

function pickModeExpression(
  manifest: AvatarManifest,
  mode: "listening" | "thinking",
): string | undefined {
  const candidates = _MODE_TO_REACTION[mode];
  for (const reaction of candidates) {
    const expr = manifest.reaction_mapping[reaction];
    if (expr) {
      return expr;
    }
  }
  for (const expr of manifest.expressions) {
    if (expr.name.toLowerCase().includes(mode)) {
      return expr.name;
    }
  }
  return undefined;
}

function pickExpressionBindings(
  manifest: AvatarManifest,
  expressionName: string,
): ExpressionParam[] | undefined {
  const map = manifest.expression_params;
  if (!map) {
    return undefined;
  }
  return map[expressionName];
}

function clamp(value: number, min: number, max: number): number {
  if (value < min) return min;
  if (value > max) return max;
  return value;
}

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0;
  if (value < 0) return 0;
  if (value > 1) return 1;
  return value;
}

/** Mirror of ``AmbientBodyChannel.clampExpressiveness`` — kept local
 * to avoid a cross-channel utility module for a one-line clamp. */
function clampExpressiveness(value: number | undefined): number {
  if (value === undefined || value === null || !Number.isFinite(value)) {
    return 1;
  }
  if (value < 0) return 0;
  if (value > 1.5) return 1.5;
  return value;
}
