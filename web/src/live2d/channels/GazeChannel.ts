/**
 * GazeChannel — drives ``adapter.focus(x, y)`` every frame.
 *
 * Priority pipeline (highest first):
 *
 *   1. **Conversation lock** (``listening`` / ``transcribing`` /
 *      ``speaking``): centred X with slight upward bias so the user
 *      reads as being looked at.
 *
 *   2. **Thinking drift**: slow random wander while the LLM is
 *      composing (no TTS playing yet).
 *
 *   3. **Idle break**: window unfocused OR cursor stopped >
 *      ``IDLE_BREAK_MS`` — ease the current target back to centre.
 *      Saccades + the focusController's own velocity smoothing keep
 *      her alive even at rest.
 *
 *   4. **Cursor follow** (default): track normalised mouse offset,
 *      clamped to a comfortable range so the rig saturates near the
 *      bounds and never feels like she's straining.
 *
 * Micro-saccades fire every 1.5–3s so the gaze never freezes; they
 * decay with a 0.92 per-frame factor.
 *
 * This channel does NOT smooth the target itself — the rig's
 * ``focusController`` runs its own velocity-based easing. Layering
 * ours on top would double-smooth and feel sluggish (we tried that
 * during the original gaze build and it felt wrong).
 *
 * Capability gating: there's nothing to gate. ``adapter.focus()``
 * silently no-ops on rigs without ``ParamAngle`` / ``ParamEyeBall``,
 * so passing values through is safe even on minimal rigs.
 */
import { clamp } from "../math";
import type {
  AvatarChannel,
  ChannelDeps,
  Live2DModelAdapter,
  MouseSnapshot,
} from "../types";

const IDLE_BREAK_MS = 1_500;
const SACCADE_INTERVAL_MIN_MS = 1_500;
const SACCADE_INTERVAL_RANGE_MS = 1_500;
const SACCADE_DECAY = 0.92;
const IDLE_DECAY = 0.92;
/** Eye-contact bias when the conversation has the floor. The user is
 * usually below the screen; lifting the gaze ~0.2 reads as "looking
 * at you" rather than "staring at your hairline". */
const CONVERSATION_LOCK_Y = 0.2;
const CURSOR_X_CLAMP = 0.7;
const CURSOR_Y_LO = -0.5;
const CURSOR_Y_HI = 0.7;

export interface GazeChannelOptions {
  /** Random source, defaults to ``Math.random``. Tests pass a
   * deterministic source so saccades are reproducible. */
  random?: () => number;
}

export class GazeChannel implements AvatarChannel {
  readonly name = "gaze";

  private _adapter: Live2DModelAdapter | null = null;
  private _deps: ChannelDeps | null = null;

  private readonly _target = { x: 0, y: 0 };
  private readonly _microSaccade = { x: 0, y: 0 };
  private _lastSaccadeAt = 0;
  private _nextSaccadeAt = 0;
  private readonly _random: () => number;

  constructor(options: GazeChannelOptions = {}) {
    this._random = options.random ?? Math.random;
  }

  attach(adapter: Live2DModelAdapter, deps: ChannelDeps): void {
    this._adapter = adapter;
    this._deps = deps;
    this._target.x = 0;
    this._target.y = 0;
    this._microSaccade.x = 0;
    this._microSaccade.y = 0;
    this._lastSaccadeAt = deps.now();
    this._nextSaccadeAt = this._lastSaccadeAt + this._saccadeInterval();
  }

  detach(): void {
    this._adapter = null;
    this._deps = null;
    this._target.x = 0;
    this._target.y = 0;
    this._microSaccade.x = 0;
    this._microSaccade.y = 0;
  }

  tickGaze(now: number, _dt: number, mouse: MouseSnapshot): void {
    const adapter = this._adapter;
    const deps = this._deps;
    if (!adapter || !deps) {
      return;
    }
    const snap = deps.getStoreSnapshot();
    const isListening =
      snap.voiceMode === "listening" || snap.voiceMode === "transcribing";
    const isSpeaking = snap.ttsState === "speaking";
    const isThinking =
      snap.voiceMode === "thinking" ||
      (snap.turnInProgress && snap.ttsState !== "speaking");
    const cursorStillActive =
      mouse.lastMoveAt > 0 && now - mouse.lastMoveAt <= IDLE_BREAK_MS;
    const isIdle = !mouse.windowFocused || !cursorStillActive;

    if (isListening || isSpeaking) {
      this._target.x = 0;
      this._target.y = CONVERSATION_LOCK_Y;
    } else if (isThinking) {
      const t = now / 1000;
      this._target.x = 0.35 * Math.sin(t * 0.6);
      this._target.y = 0.18 * Math.cos(t * 0.43) + 0.05;
    } else if (isIdle) {
      this._target.x *= IDLE_DECAY;
      this._target.y *= IDLE_DECAY;
    } else if (mouse.x != null && mouse.y != null) {
      // Cursor follow: normalise against half-viewport and clamp so
      // the rig saturates inside its comfortable range.
      const rect = mouse.containerRect;
      const cx = rect.left + rect.width / 2;
      const cy = rect.top + rect.height / 2;
      const halfW = Math.max(1, mouse.viewportWidth / 2);
      const halfH = Math.max(1, mouse.viewportHeight / 2);
      const nx = (mouse.x - cx) / halfW;
      // Y is flipped because Live2D's focus space uses +Y up while
      // screen Y grows downward.
      const ny = -((mouse.y - cy) / halfH);
      this._target.x = clamp(nx, -CURSOR_X_CLAMP, CURSOR_X_CLAMP);
      this._target.y = clamp(ny, CURSOR_Y_LO, CURSOR_Y_HI);
    } else {
      // No cursor data yet (initial mount) — hold whatever we have.
    }

    if (now >= this._nextSaccadeAt) {
      this._lastSaccadeAt = now;
      this._nextSaccadeAt = now + this._saccadeInterval();
      this._microSaccade.x = (this._random() - 0.5) * 0.1;
      this._microSaccade.y = (this._random() - 0.5) * 0.06;
    }
    this._microSaccade.x *= SACCADE_DECAY;
    this._microSaccade.y *= SACCADE_DECAY;

    adapter.focus(
      this._target.x + this._microSaccade.x,
      this._target.y + this._microSaccade.y,
    );
  }

  // ── test-only accessors ──────────────────────────────────────────
  get target(): { x: number; y: number } {
    return { ...this._target };
  }

  private _saccadeInterval(): number {
    return SACCADE_INTERVAL_MIN_MS + this._random() * SACCADE_INTERVAL_RANGE_MS;
  }
}
