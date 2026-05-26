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
 */
import type { BackchannelHint } from "../../types";
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
      return;
    }
    adapter.expression(expressionName);
  }

  private _applyExpressionByName(
    adapter: Live2DModelAdapter,
    expressionName: string | undefined,
  ): void {
    if (!expressionName) {
      return;
    }
    adapter.expression(expressionName);
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
