/**
 * AvatarEngine — central coordinator that owns the RAF loops, the
 * ``beforeModelUpdate`` listener, and the dispatch of store events
 * to a fixed set of registered ``AvatarChannel`` instances.
 *
 * Everything that used to live inside hooks in ``Live2DAvatar.tsx``
 * is migrating here. The engine is intentionally framework-agnostic
 * (no React, no Pixi, no Zustand types beyond a snapshot accessor)
 * so we can construct it under Vitest with the fake adapter and
 * fake clock to run channels deterministically.
 *
 * Lifecycle:
 *
 *   const engine = new AvatarEngine(deps);
 *   engine.register(motionChannel, expressionChannel, ...);
 *   engine.start(adapter);
 *   ...
 *   engine.dispatchReaction("cheerful");
 *   engine.dispatchOverlay(state);
 *   ...
 *   engine.stop();
 *
 * The engine is single-shot: ``start`` exactly once per attached
 * model. Detaching the model (e.g. picking a new persona) requires
 * a fresh ``AvatarEngine`` instance — we do that in
 * ``Live2DAvatar.tsx`` because each model load also rebuilds the
 * Pixi container.
 */
import type {
  AvatarChannel,
  ChannelDeps,
  ChannelStoreSnapshot,
  Live2DModelAdapter,
  MouseSnapshot,
  ResolvedOverlayEvent,
} from "./types";
import type { EngineState } from "./state";
import type {
  AvatarMotionState,
  AvatarOverlayState,
  AvatarProfile,
  MoodState,
  ResolvedOutfit,
} from "../types";

/** External dependencies wired in by ``Live2DAvatar.tsx`` (or test
 * harnesses). Kept tight so the engine stays trivially constructible. */
export interface EngineDependencies {
  manifest: AvatarProfile;
  engineState: EngineState;
  getStoreSnapshot: () => ChannelStoreSnapshot;
  /** Monotonic clock. Defaults to ``performance.now`` if not
   * provided; tests pass ``FakeClock.now``. */
  now?: () => number;
  /** Mouse-event source. Production wires ``window``; tests pass a
   * mock. ``GazeChannel`` is the only consumer and it'll subscribe
   * lazily on attach. */
  mouseSource?: MouseSource;
  /** RAF scheduler. Defaults to ``requestAnimationFrame`` /
   * ``cancelAnimationFrame``; tests pass a manual one. */
  scheduleFrame?: (cb: FrameRequestCallback) => number;
  cancelFrame?: (handle: number) => void;
}

/** Window-event abstraction used by the gaze channel. The engine
 * itself doesn't read mouse state — it only forwards a
 * ``MouseSnapshot`` to ``tickGaze`` derived from this source. The
 * engine subscribes once in ``start`` and unsubscribes in ``stop``. */
export interface MouseSource {
  /** Latest mouse snapshot. Channels read this every gaze tick. */
  snapshot(): MouseSnapshot;
  /** Subscribe so the source can keep its internal state up-to-date.
   * Returns an unsubscribe function. The engine calls this once. */
  subscribe(): () => void;
}

const noopMouseSource: MouseSource = {
  snapshot: () => ({
    x: null,
    y: null,
    lastMoveAt: 0,
    windowFocused: true,
    containerRect: { left: 0, top: 0, width: 0, height: 0 },
    viewportWidth: 0,
    viewportHeight: 0,
  }),
  subscribe: () => () => undefined,
};

export class AvatarEngine {
  private readonly _channels: AvatarChannel[] = [];
  private readonly _deps: EngineDependencies;
  private _channelDeps: ChannelDeps | null = null;
  private _started = false;
  private _disposed = false;

  private _tier3Handle: number | null = null;
  private _gazeHandle: number | null = null;
  private _lastTier3Time = 0;
  private _lastGazeTime = 0;
  private _detachPreUpdate: (() => void) | null = null;
  private _detachMouse: (() => void) | null = null;

  private readonly _now: () => number;
  private readonly _scheduleFrame: (cb: FrameRequestCallback) => number;
  private readonly _cancelFrame: (handle: number) => void;
  private readonly _mouseSource: MouseSource;

  constructor(deps: EngineDependencies) {
    this._deps = deps;
    this._now =
      deps.now ?? (() => (typeof performance !== "undefined" ? performance.now() : Date.now()));
    this._scheduleFrame =
      deps.scheduleFrame ??
      (typeof requestAnimationFrame !== "undefined"
        ? requestAnimationFrame.bind(globalThis)
        : (cb) => setTimeout(() => cb(this._now()), 16) as unknown as number);
    this._cancelFrame =
      deps.cancelFrame ??
      (typeof cancelAnimationFrame !== "undefined"
        ? cancelAnimationFrame.bind(globalThis)
        : (handle) => clearTimeout(handle as unknown as ReturnType<typeof setTimeout>));
    this._mouseSource = deps.mouseSource ?? noopMouseSource;
  }

  /** Register one or more channels. Must be called before ``start``.
   * Channels are attached in registration order and detached in
   * reverse — the order rarely matters but we make it deterministic
   * for predictability. Repeated registration of the same instance
   * is silently ignored. */
  register(...channels: AvatarChannel[]): void {
    if (this._started) {
      throw new Error(
        `[AvatarEngine] cannot register channel "${channels[0]?.name ?? "<unknown>"}" after start()`,
      );
    }
    for (const channel of channels) {
      if (this._channels.indexOf(channel) === -1) {
        this._channels.push(channel);
      }
    }
  }

  /** Wire the engine to a freshly-loaded model and kick off the RAF
   * loops. Idempotent: a second call is a no-op (and a warning) so
   * we don't double-attach when React strict-mode runs effects
   * twice in dev. */
  start(adapter: Live2DModelAdapter): void {
    if (this._disposed) {
      throw new Error("[AvatarEngine] cannot start a disposed engine");
    }
    if (this._started) {
      console.warn("[AvatarEngine] start() called twice; ignoring second call");
      return;
    }
    this._started = true;
    this._channelDeps = {
      now: this._now,
      manifest: this._deps.manifest,
      engineState: this._deps.engineState,
      getStoreSnapshot: this._deps.getStoreSnapshot,
    };
    for (const channel of this._channels) {
      try {
        channel.attach(adapter, this._channelDeps);
      } catch (err) {
        console.error(`[AvatarEngine] channel "${channel.name}" attach failed`, err);
      }
    }
    // Wire pre-model lipsync hook + mouse source.
    this._detachPreUpdate = adapter.onBeforeModelUpdate(() => this._tickPreModel());
    this._detachMouse = this._mouseSource.subscribe();
    // Kick off both RAF loops.
    this._lastTier3Time = this._now();
    this._lastGazeTime = this._lastTier3Time;
    this._tier3Handle = this._scheduleFrame(this._runTier3);
    this._gazeHandle = this._scheduleFrame(this._runGaze);
  }

  /** Tear everything down. Cancels RAFs, unsubscribes the mouse +
   * pre-update listeners, and detaches every channel. Safe to call
   * before ``start`` or twice; the engine is single-shot after
   * ``stop``. */
  stop(): void {
    if (this._disposed) {
      return;
    }
    this._disposed = true;
    if (this._tier3Handle !== null) {
      this._cancelFrame(this._tier3Handle);
      this._tier3Handle = null;
    }
    if (this._gazeHandle !== null) {
      this._cancelFrame(this._gazeHandle);
      this._gazeHandle = null;
    }
    if (this._detachPreUpdate) {
      try {
        this._detachPreUpdate();
      } catch (err) {
        console.error("[AvatarEngine] detach pre-update failed", err);
      }
      this._detachPreUpdate = null;
    }
    if (this._detachMouse) {
      try {
        this._detachMouse();
      } catch (err) {
        console.error("[AvatarEngine] detach mouse source failed", err);
      }
      this._detachMouse = null;
    }
    // Detach in reverse order.
    for (let i = this._channels.length - 1; i >= 0; i -= 1) {
      const channel = this._channels[i];
      try {
        channel.detach();
      } catch (err) {
        console.error(`[AvatarEngine] channel "${channel.name}" detach failed`, err);
      }
    }
    this._channelDeps = null;
    this._started = false;
  }

  // ── store-event dispatch (called by Live2DAvatar.tsx subscriptions) ──

  dispatchReaction(reaction: string): void {
    const state = this._deps.engineState;
    if (reaction === state.lastReaction) {
      return;
    }
    state.lastReaction = reaction;
    this._fanOut("onReaction", (channel) => channel.onReaction?.(reaction));
  }

  /** Convert a wall-clock ``avatarOverlay`` into a monotonic-clock
   * ``ResolvedOverlayEvent`` and fan it out. Centralising the
   * conversion here is what fixes the original "overlay never
   * expires" bug — channels can blindly compare against
   * ``deps.now()``. */
  dispatchOverlay(overlay: AvatarOverlayState | null): void {
    if (!overlay) {
      return;
    }
    const wallNow = Date.now();
    const remainingMs = Math.max(0, overlay.expiresAt - wallNow);
    const until = this._now() + remainingMs;
    const event: ResolvedOverlayEvent = { name: overlay.name, until };
    this._fanOut("onOverlay", (channel) => channel.onOverlay?.(event));
  }

  dispatchMotion(motion: AvatarMotionState | null): void {
    if (!motion) {
      return;
    }
    this._fanOut("onMotion", (channel) => channel.onMotion?.(motion));
  }

  dispatchOutfit(outfit: ResolvedOutfit): void {
    this._fanOut("onOutfitChange", (channel) => channel.onOutfitChange?.(outfit));
  }

  dispatchTtsState(next: "idle" | "speaking"): void {
    const state = this._deps.engineState;
    if (next === state.ttsState) {
      return;
    }
    state.ttsState = next;
    this._fanOut("onTtsState", (channel) => channel.onTtsState?.(next));
  }

  dispatchMood(mood: MoodState): void {
    this._fanOut("onMood", (channel) => channel.onMood?.(mood));
  }

  dispatchVoiceMode(mode: string): void {
    this._fanOut("onVoiceMode", (channel) => channel.onVoiceMode?.(mode));
  }

  /** Fan a backchannel hint out to channels. The bridge calls this
   * when ``backchannelAt`` increments — the channel only sees the
   * hint string, not the timestamp. Empty/falsy hints are dropped. */
  dispatchBackchannel(hint: string): void {
    if (!hint) {
      return;
    }
    this._fanOut("onBackchannel", (channel) => channel.onBackchannel?.(hint));
  }

  // ── internals ────────────────────────────────────────────────────

  private _runTier3 = (): void => {
    if (this._disposed || !this._started) {
      return;
    }
    const now = this._now();
    const dt = Math.max(0, (now - this._lastTier3Time) / 1000);
    this._lastTier3Time = now;
    // Check for expiry of the expression-slot lock first so the
    // ``onExpressionSlotReleased`` callback fires before per-channel
    // tier-3 work — that lets ExpressionChannel re-apply the
    // persistent reaction in the same frame, avoiding a one-frame
    // gap where the rig has no expression at all.
    const state = this._deps.engineState;
    if (state.exprSlotLockUntil > 0 && now >= state.exprSlotLockUntil) {
      state.exprSlotLockUntil = 0;
      this._fanOut("onExpressionSlotReleased", (channel) =>
        channel.onExpressionSlotReleased?.(),
      );
    }
    for (const channel of this._channels) {
      if (!channel.tickTier3) {
        continue;
      }
      try {
        channel.tickTier3(now, dt);
      } catch (err) {
        console.error(`[AvatarEngine] channel "${channel.name}" tickTier3 failed`, err);
      }
    }
    this._tier3Handle = this._scheduleFrame(this._runTier3);
  };

  private _runGaze = (): void => {
    if (this._disposed || !this._started) {
      return;
    }
    const now = this._now();
    const dt = Math.max(0, (now - this._lastGazeTime) / 1000);
    this._lastGazeTime = now;
    const mouse = this._mouseSource.snapshot();
    for (const channel of this._channels) {
      if (!channel.tickGaze) {
        continue;
      }
      try {
        channel.tickGaze(now, dt, mouse);
      } catch (err) {
        console.error(`[AvatarEngine] channel "${channel.name}" tickGaze failed`, err);
      }
    }
    this._gazeHandle = this._scheduleFrame(this._runGaze);
  };

  private _tickPreModel(): void {
    if (this._disposed) {
      return;
    }
    for (const channel of this._channels) {
      if (!channel.tickPreModel) {
        continue;
      }
      try {
        channel.tickPreModel();
      } catch (err) {
        console.error(`[AvatarEngine] channel "${channel.name}" tickPreModel failed`, err);
      }
    }
  }

  private _fanOut(label: string, invoke: (channel: AvatarChannel) => void): void {
    for (const channel of this._channels) {
      try {
        invoke(channel);
      } catch (err) {
        console.error(`[AvatarEngine] channel "${channel.name}" ${label} failed`, err);
      }
    }
  }

  // ── test-only accessors ──────────────────────────────────────────

  /** Test helper: returns whether the engine is currently
   * scheduling RAFs. Production code never reads this. */
  get isRunning(): boolean {
    return this._started && !this._disposed;
  }

  /** Test helper: registered channel count. */
  get channelCount(): number {
    return this._channels.length;
  }

  /** Read access to the shared engine state. The Live2DAvatar
   * component uses this during the migration to keep legacy
   * useEffects coordinated with channel-owned state (notably
   * ``exprSlotLockUntil``). Once every ``useEffect`` is migrated
   * this getter has no production callers. */
  get engineState(): EngineState {
    return this._deps.engineState;
  }
}
