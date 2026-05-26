/**
 * Shared types for the Live2D engine + channels.
 *
 * The engine talks to the rig only through ``Live2DModelAdapter``. That
 * narrow surface is everything ``pixi-live2d-display`` exposes that we
 * actually use — wrapped so channel code never touches Pixi types
 * directly. Tests use ``FakeAdapter`` from ``__fixtures__/fake-model.ts``.
 *
 * Channels implement ``AvatarChannel`` and opt into hooks they care
 * about. The engine fans out events / RAF ticks to whichever channels
 * registered for them.
 *
 * NOTE: this file is intentionally small in Phase 2. New event payloads
 * and channel hooks land alongside the channel that needs them so the
 * type surface grows incrementally with the migration.
 */
import type {
  AvatarMotionState,
  AvatarOverlayState,
  AvatarProfile,
  MoodState,
  ResolvedOutfit,
} from "../types";

import type { EngineState } from "./state";

/**
 * The avatar profile is the static rig metadata loaded once per
 * model swap (capabilities, overlays, outfits, motions, parameters,
 * lip-sync ids, ...). The store carries it under ``state.avatar``;
 * the engine receives it via ``ChannelDeps`` so channels can gate on
 * capability flags + look up overlay/outfit bindings.
 *
 * Aliased here for clarity at channel call sites — channels
 * conceptually consume a "manifest" that doesn't change for the
 * lifetime of the engine instance, even though the rig type is
 * shared with the persisted-settings layer.
 */
export type AvatarManifest = AvatarProfile;

/** Narrow surface the engine and channels use to drive the rig.
 * Production wraps ``pixi-live2d-display``'s ``Live2DModel`` here; tests
 * use ``FakeAdapter``. */
export interface Live2DModelAdapter {
  /** Set a single Live2D parameter by id. No-op when the param doesn't
   * exist on the rig (the adapter swallows that). */
  setParam(paramId: string, value: number): void;
  /** Read the *most recently written* value for a param, or ``undefined``
   * if we never set it. The adapter is a write-cache; we don't read
   * back from the underlying core model. Channels rarely need this —
   * it exists for tests + the rare introspection path. */
  getParam(paramId: string): number | undefined;
  /** Apply a named expression (``model.expression(name)``). */
  expression(name: string): void;
  /** Clear the active expression slot
   * (``expressionManager.resetExpression()``). */
  resetExpression(): void;
  /** Fire a motion by group + index
   * (``model.motion(group, idx, priority?)``). Pass ``undefined`` for
   * the index to let the library pick a random one inside the group
   * — that's the standard pixi-live2d-display behaviour for talk +
   * idle motions. */
  motion(group: string, index: number | undefined, priority?: number): void;
  /** Register a callback that fires once per frame, immediately before
   * the model commits parameters to the rig. Returns a function to
   * unregister. ``LipsyncChannel`` is the canonical user — see
   * ``docs/alexia-model-notes.md`` section 5 for why writing
   * ``ParamMouthOpenY`` from anywhere else gets clobbered by motions. */
  onBeforeModelUpdate(listener: () => void): () => void;
  /** Drive the model's internal ``focusController`` directly with
   * normalised ``[-1, 1]`` coordinates. ``(0, 0)`` is straight ahead;
   * positive Y is up. We bypass the public ``model.focus(x, y)``
   * (which expects screen pixels and discards magnitude after an
   * ``atan2``) so the focusController's velocity-smoothed integration
   * runs on the values channels actually want. Library-shape errors
   * are swallowed — minimal rigs without ParamAngle/EyeBall just
   * don't move. */
  focus(x: number, y: number): void;
}

/** Snapshot of the cursor at a single gaze RAF tick. The engine
 * refreshes this from window mouse events (or a test event source)
 * before each tick. ``GazeChannel`` is the only consumer today. */
export interface MouseSnapshot {
  /** Mouse X in client coordinates (CSS pixels), or ``null`` when
   * we never saw one (page just loaded). */
  x: number | null;
  /** Mouse Y in client coordinates (CSS pixels). */
  y: number | null;
  /** Wall-clock ms of the last move event seen, ``0`` when never. */
  lastMoveAt: number;
  /** Whether the renderer's window currently has focus. ``false`` is
   * the cue for gaze idle-return. */
  windowFocused: boolean;
  /** Container bounding box used to clamp gaze direction so the
   * eyes don't track outside the rig's visible volume. */
  containerRect: { left: number; top: number; width: number; height: number };
  /** Viewport size used to normalise the cursor offset against
   * half-viewport (so the screen edge gives roughly ±1 in gaze
   * space). Defaults match ``window.innerWidth/Height`` in
   * production. */
  viewportWidth: number;
  viewportHeight: number;
}

/** Common dependencies every channel needs. ``store`` is the engine's
 * read-only view of the Zustand store — channels never call
 * ``store.set*``; mutation is exclusively the engine's job via the
 * ``Live2DModelAdapter``. */
export interface ChannelDeps {
  /** Monotonic clock in milliseconds. Production passes
   * ``performance.now``; tests pass ``FakeClock.now``. */
  now: () => number;
  /** Static rig metadata (capabilities / overlays / outfits / motions /
   * lip-sync ids). Set once at attach time; channels capture it. */
  manifest: AvatarManifest;
  /** Mutable state shared across channels. Today this only holds the
   * expression-slot lock and the last applied reaction; see
   * ``state.ts`` for the rationale. */
  engineState: EngineState;
  /** Read-only store accessor. Returning ``undefined`` for unwired
   * tests is OK — channels guard before using. */
  getStoreSnapshot: () => ChannelStoreSnapshot;
}

/** The slice of the Zustand store the channels read.
 *
 * Channels read what they need on every tick (fresh value, no
 * subscription) plus receive event callbacks from the engine for
 * the discrete state transitions (reaction change, overlay arrival,
 * motion fired). This split keeps the dispatch logic explicit
 * without forcing every channel to manage its own subscription
 * unsubscribe ritual. */
export interface ChannelStoreSnapshot {
  reaction: string;
  ttsState: "idle" | "speaking";
  voiceMode: "off" | "listening" | "transcribing" | "speaking" | string;
  turnInProgress: boolean;
  audioAmplitude: number;
  avatarOverlay: AvatarOverlayState | null;
  avatarMotion: AvatarMotionState | null;
  mood: MoodState;
  resolvedOutfit: ResolvedOutfit;
  /** Free-form backchannel hint string — ``"agreement"``, ``"thinking"``,
   * etc. ``""`` when no hint is active. ``ExpressionChannel`` consumes
   * this to fire a transient backchannel expression. */
  backchannelHint: string;
  /** Server-provided circadian bucket — ``"morning"``, ``"day"``,
   * ``"evening"``, ``"late_night"``, etc. ``undefined`` /
   * ``""`` when no avatar profile is loaded yet.
   * ``AmbientBodyChannel`` reads this to trigger the tired-slump
   * body lean during late-night low-arousal states. Optional
   * because the legacy snapshot shape pre-dates this field. */
  circadianPeriod?: string;
}

/** Base channel contract. Each method is optional — channels opt in
 * to the hooks they actually need, the engine ignores the rest. New
 * hooks land here as channels are migrated. */
export interface AvatarChannel {
  /** Symbolic name used in error logs / engine diagnostics. */
  readonly name: string;

  /** Wire the channel into a freshly-loaded model. Called once after
   * the engine has the adapter. Channels capture refs/state here. */
  attach(adapter: Live2DModelAdapter, deps: ChannelDeps): void;
  /** Tear down listeners + reset internal state. Channels must be
   * idempotent here so a remount doesn't leak rAF handles or
   * ``setInterval`` timers. */
  detach(): void;

  // ── discrete event callbacks (optional) ───────────────────────────

  /** Fired when ``state.reaction`` changes value. The engine handles
   * dedup against ``EngineState.lastReaction`` and the overlay-slot
   * lock; channels just react to "the reaction is now X". */
  onReaction?(reaction: string): void;

  /** Fired when a fresh ``avatarOverlay`` arrives from the WS. The
   * engine converts ``expiresAt`` from wall-clock to monotonic clock
   * so channels can compare against ``now()`` directly. */
  onOverlay?(event: ResolvedOverlayEvent): void;

  /** Fired when a new ``avatarMotion`` arrives. */
  onMotion?(event: AvatarMotionState): void;

  /** Fired when ``state.resolvedOutfit`` changes. */
  onOutfitChange?(outfit: ResolvedOutfit): void;

  /** Fired on every TTS state transition (``idle <-> speaking``). */
  onTtsState?(state: "idle" | "speaking"): void;

  /** Fired when the mood vector changes. */
  onMood?(mood: MoodState): void;

  /** Fired when ``state.voiceMode`` changes
   * (``off`` / ``listening`` / ``transcribing`` / ``thinking`` /
   * ``speaking`` / future). ``ExpressionChannel`` swaps to a
   * mode-specific expression while the user is mid-utterance. */
  onVoiceMode?(mode: string): void;

  /** Fired when a fresh backchannel hint lands. The ``hint`` matches
   * the ``BackchannelHint`` enum on the store. ``ExpressionChannel``
   * shows a transient expression and schedules a restore. */
  onBackchannel?(hint: string): void;

  /** Fired when the expression-slot lock expires (the active
   * ``expr:``-bound overlay pulse just ended). The engine emits this
   * so ``ExpressionChannel`` can re-apply the persistent reaction. */
  onExpressionSlotReleased?(): void;

  // ── per-frame ticks (optional) ────────────────────────────────────

  /** Tier-3 RAF tick: outfits, overlay pulses, gestures, body
   * language. Most stateful channels live here. */
  tickTier3?(now: number, dt: number): void;

  /** Gaze RAF tick: separate loop so head/eye angle keeps up with
   * mouse movement even when the tier-3 work is busy. */
  tickGaze?(now: number, dt: number, mouse: MouseSnapshot): void;

  /** ``beforeModelUpdate`` hook on the rig — fires once per Pixi
   * frame, immediately before the rig commits parameters to the
   * model. ``LipsyncChannel`` is the canonical user (writing
   * ``ParamMouthOpenY`` from anywhere else gets clobbered by motions
   * — see ``docs/alexia-model-notes.md`` section 5). */
  tickPreModel?(): void;
}

/** ``avatarOverlay`` from the store after the engine has converted
 * ``expiresAt`` from wall-clock (``Date.now() + duration``) to
 * monotonic clock (``performance.now() + remainingMs``). Channels
 * never see the wall-clock form — they always compare against
 * ``deps.now()``. */
export interface ResolvedOverlayEvent {
  /** Overlay name, e.g. ``"grin"``, ``"tail_wag"``, ``"stars"``. */
  name: string;
  /** Monotonic deadline (``performance.now()``-based) at which the
   * pulse should expire. Already converted by the engine; do NOT
   * compare against ``Date.now()``. */
  until: number;
}
