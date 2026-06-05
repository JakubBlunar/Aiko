/**
 * Glue between the Zustand store and the ``AvatarEngine``.
 *
 * The engine itself is framework-agnostic. Instead of reaching into
 * the store from inside channels, we subscribe to the relevant
 * slices here and forward changes through ``engine.dispatch*``. This
 * keeps the engine's tree-shape obvious: every state mutation flows
 * through one of a handful of dispatch entry points.
 *
 * Compared to the old pile of ``useEffect``s, this layer:
 *
 * - converts mood / TTS / overlay changes into single dispatch
 *   calls (instead of every channel re-subscribing)
 * - centralises overlay wall-clock -> monotonic conversion at the
 *   engine boundary (see ``AvatarEngine.dispatchOverlay``)
 * - cleanly tears down all subscriptions in a single call
 */
import type { StoreApi, UseBoundStore } from "zustand";
import type { AvatarEngine } from "./AvatarEngine";
import type { AvatarTouchPayload } from "./types";
import type {
  AvatarMotionState,
  AvatarOverlayState,
  AvatarProfile,
  MoodState,
  ResolvedOutfit,
} from "../types";

/** The exact slice of the store the bridge cares about. We define
 * it locally instead of leaking the full ``AssistantState`` type
 * through this module — the bridge only forwards values onwards. */
export interface BridgedState {
  reaction: string;
  ttsState: "idle" | "speaking";
  voiceMode: string;
  audioAmplitude: number;
  avatarOverlay: AvatarOverlayState | null;
  avatarMotion: AvatarMotionState | null;
  avatar: AvatarProfile | null;
  mood: MoodState;
  backchannelHint: string | null;
  backchannelAt: number;
  /** K31: the most recent ``avatar_touch`` payload. Same
   * "increment a dedup counter so we re-fire on identical
   * payloads" idiom as ``backchannel``. */
  avatarTouch: AvatarTouchPayload | null;
  avatarTouchAt: number;
}

/** Strict shape required of the store passed to the bridge.
 * Compatible with Zustand's ``UseBoundStore<StoreApi<T>>`` and the
 * vanilla ``StoreApi<T>``. */
export type BridgedStore =
  | UseBoundStore<StoreApi<BridgedState>>
  | (StoreApi<BridgedState> & { getState: () => BridgedState });

export class StoreBridge {
  private readonly _engine: AvatarEngine;
  private readonly _store: BridgedStore;
  private _unsubReaction: (() => void) | null = null;
  private _unsubTts: (() => void) | null = null;
  private _unsubOverlay: (() => void) | null = null;
  private _unsubMotion: (() => void) | null = null;
  private _unsubMood: (() => void) | null = null;
  private _unsubOutfit: (() => void) | null = null;
  private _unsubVoiceMode: (() => void) | null = null;
  private _unsubBackchannel: (() => void) | null = null;
  private _unsubTouch: (() => void) | null = null;

  constructor(engine: AvatarEngine, store: BridgedStore) {
    this._engine = engine;
    this._store = store;
  }

  /** Wire up the subscriptions. Call exactly once after
   * ``engine.start()``. */
  start(): void {
    const initial = this._store.getState();
    // Dispatch initial reaction so channels see "the current state"
    // not just diffs going forward.
    this._engine.dispatchReaction(initial.reaction);
    this._engine.dispatchTtsState(initial.ttsState);
    this._engine.dispatchMood(initial.mood);
    this._engine.dispatchVoiceMode(initial.voiceMode);
    if (initial.avatar?.resolved_outfit) {
      this._engine.dispatchOutfit(initial.avatar.resolved_outfit);
    }
    if (initial.avatarOverlay) {
      this._engine.dispatchOverlay(initial.avatarOverlay);
    }
    if (initial.avatarMotion) {
      this._engine.dispatchMotion(initial.avatarMotion);
    }

    this._unsubReaction = this._subscribe(
      (s) => s.reaction,
      (next) => this._engine.dispatchReaction(next),
    );
    this._unsubTts = this._subscribe(
      (s) => s.ttsState,
      (next) => this._engine.dispatchTtsState(next),
    );
    this._unsubOverlay = this._subscribe(
      (s) => s.avatarOverlay,
      (next) => this._engine.dispatchOverlay(next),
    );
    this._unsubMotion = this._subscribe(
      (s) => s.avatarMotion,
      (next) => this._engine.dispatchMotion(next),
    );
    this._unsubMood = this._subscribe(
      (s) => s.mood,
      (next) => this._engine.dispatchMood(next),
    );
    this._unsubOutfit = this._subscribe(
      (s) => s.avatar?.resolved_outfit ?? "",
      (next) => this._engine.dispatchOutfit(next as ResolvedOutfit),
    );
    this._unsubVoiceMode = this._subscribe(
      (s) => s.voiceMode,
      (next) => this._engine.dispatchVoiceMode(next),
    );
    // Backchannel: subscribe to ``backchannelAt`` (the dedup key)
    // and dispatch the *current* hint string. ``backchannelAt``
    // increments on every fresh hint even if the hint label
    // happens to match — that's intentional, the timestamp is the
    // identity key the legacy code used.
    this._unsubBackchannel = this._subscribe(
      (s) => s.backchannelAt,
      () => {
        const hint = this._store.getState().backchannelHint ?? "";
        this._engine.dispatchBackchannel(hint);
      },
    );
    // K31: subscribe to the dedup counter so back-to-back gestures
    // with the same kind still re-fire. The payload itself is
    // fetched fresh from the store inside the listener.
    this._unsubTouch = this._subscribe(
      (s) => s.avatarTouchAt,
      () => {
        const payload = this._store.getState().avatarTouch;
        this._engine.dispatchTouch(payload);
      },
    );
  }

  /** Unsubscribe from everything. Idempotent. */
  stop(): void {
    for (const off of [
      this._unsubReaction,
      this._unsubTts,
      this._unsubOverlay,
      this._unsubMotion,
      this._unsubMood,
      this._unsubOutfit,
      this._unsubVoiceMode,
      this._unsubBackchannel,
      this._unsubTouch,
    ]) {
      if (off) {
        try {
          off();
        } catch {
          /* swallow */
        }
      }
    }
    this._unsubReaction = null;
    this._unsubTts = null;
    this._unsubOverlay = null;
    this._unsubMotion = null;
    this._unsubMood = null;
    this._unsubOutfit = null;
    this._unsubVoiceMode = null;
    this._unsubBackchannel = null;
    this._unsubTouch = null;
  }

  // ── internal helpers ─────────────────────────────────────────────

  /** Zustand v5 dropped the older ``subscribe(selector, listener)``
   * overload. We polyfill it here so the bridge code stays
   * declarative — selectors are just diff-and-fire functions. */
  private _subscribe<T>(
    selector: (state: BridgedState) => T,
    listener: (next: T) => void,
  ): () => void {
    let previous = selector(this._store.getState());
    return this._store.subscribe((state) => {
      const next = selector(state);
      if (Object.is(next, previous)) {
        return;
      }
      previous = next;
      listener(next);
    });
  }
}
