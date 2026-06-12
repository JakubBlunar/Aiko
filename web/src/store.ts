import { create } from "zustand";
import { DEFAULT_LOGGING_SETTINGS } from "./types";
import type {
  AttachmentRef,
  AvatarMotionState,
  AvatarOverlayState,
  AvatarProfile,
  AvatarSettingsKnobs,
  AvatarTouchPayload,
  BackchannelHint,
  Belief,
  BeliefKind,
  BeliefStatus,
  ChatMessage,
  CircadianPeriod,
  CompanionSettings,
  Identity,
  LlmProvider,
  LlmRoute,
  LoggingSettings,
  Memory,
  MemoryCounts,
  MemoryTier,
  MetricsSnapshot,
  MoodState,
  RelationshipAxes,
  ResolvedOutfit,
  SharedMoment,
  TaskProgressPatch,
  TaskSnapshot,
  TaskStatus,
  TogetherSummary,
  ToolEvent,
  VoiceMode,
  WorldItem,
  WorldLocation,
  WorldPatch,
  WorldSnapshot,
  WorldState,
} from "./types";

interface ConnectionState {
  status: "disconnected" | "connecting" | "connected";
  lastError: string | null;
}

interface AssistantState {
  // Connection
  connection: ConnectionState;
  setConnection: (next: Partial<ConnectionState>) => void;

  // Identity / config snapshot
  sessionKey: string;
  model: string;
  ttsEnabled: boolean;
  ttsState: "idle" | "speaking";
  ttsText: string;
  reaction: string;
  setSessionKey: (key: string) => void;
  setModel: (model: string) => void;
  setTtsEnabled: (enabled: boolean) => void;
  setTtsState: (
    state: "idle" | "speaking",
    text?: string,
    reaction?: string,
  ) => void;

  /**
   * Per-connection WebSocket identity. ``clientId`` is the id the server
   * stamped on *our* socket inside the ``hello`` envelope; we compare
   * it against ``voiceOwnerId`` to decide if we own the microphone.
   * Both are empty / null until the first ``hello`` lands.
   */
  clientId: string;
  voiceOwnerId: string | null;
  setClientId: (clientId: string) => void;
  setVoiceOwnerId: (ownerId: string | null) => void;

  /**
   * The single client the server elected to play TTS / earcon audio.
   * The desktop shell keeps the persona window's webview alive but
   * hidden, so without this lock both it and the main window would
   * play every clip ~tens of ms apart (audible as an echo on the
   * first sentence). The audio frame handler only plays PCM when
   * ``audioOwnerId`` is null (pre-election) or equals ``clientId``.
   * Hydrated from the ``hello`` envelope and refreshed on every
   * ``audio_owner_changed`` broadcast.
   */
  audioOwnerId: string | null;
  setAudioOwnerId: (ownerId: string | null) => void;

  /**
   * First-run identity. Hydrated from the WS ``hello`` snapshot and
   * the REST ``GET /api/settings/identity`` fallback, then refreshed
   * on every ``identity_changed`` broadcast. ``null`` only before the
   * first connect; ``needs_onboarding`` is the gate the modal watches.
   */
  identity: Identity | null;
  setIdentity: (identity: Identity | null) => void;

  /** Companion soft-physicality knobs (touch / reactions / persona
   * banner). Seeded from the WS ``hello`` payload and kept live by
   * ``companion_settings_changed``. The persona overlay reads the
   * banner flags from here so they honour the master switch + duration
   * instead of hardcoded defaults (I5). A partial because ``hello``
   * only carries the soft-physicality subset. */
  companionSettings: Partial<CompanionSettings> | null;
  setCompanionSettings: (patch: Partial<CompanionSettings>) => void;

  // Chat transcript
  messages: ChatMessage[];
  /**
   * P9: per-turn draft for the active assistant bubble. Streamed
   * tokens land here (one O(1) write per chunk) instead of
   * cloning ``messages`` per token; the streaming MessageBubble
   * subscribes to this slice directly so the rest of the
   * transcript and Virtuoso's ``data`` reference stay stable
   * across the whole turn. ``finishAssistantBubble`` commits the
   * draft into the matching message and clears the slice; any
   * path that wipes the transcript (``setMessages``,
   * ``clearMessages``, session change) clears the draft too.
   * ``null`` between turns.
   */
  streamingDraft: {
    id: string;
    content: string;
    reaction: string | undefined;
  } | null;
  setMessages: (msgs: ChatMessage[]) => void;
  appendUserMessage: (content: string) => void;
  appendAssistantBubble: () => string; // returns id
  appendAssistantToken: (chunk: string) => void;
  finishAssistantBubble: () => void;
  /** K32: stamp the just-finished assistant bubble with its persisted
   * SQLite ``messages.id`` (delivered on ``turn_done``) so the reaction
   * tray + "mark as moment" turn on without waiting for a history
   * reload. No-op when ``backendId`` is null (empty/aborted turn) or the
   * last message isn't a freshly-committed assistant bubble. */
  stampAssistantBackendId: (backendId: number | null | undefined) => void;
  appendProactiveMessage: (content: string, backendId?: number) => void;
  pushSystemMessage: (content: string) => void;
  clearMessages: () => void;
  /** K32: merge a fresh reactions counter map onto the matching
   * message (matched by ``backendId``). Server-driven via the
   * ``message_reaction_updated`` WS broadcast so a click in the
   * chat window updates the persona banner and vice versa. */
  applyMessageReactions: (
    backendId: number,
    reactions: Record<string, number>,
  ) => void;
  /** D2 Part B: stamp the attachments the user sent onto the most
   * recent user bubble. Driven by the ``user_attachments`` WS event
   * that follows the generic ``message`` broadcast for a typed turn. */
  attachLastUserAttachments: (attachments: AttachmentRef[]) => void;
  /** K31: stamp the kinds Aiko emitted this turn onto an assistant
   * bubble. Driven by the ``avatar_touch`` WS event so the badge
   * appears the instant the rig leans in (independent of the
   * eventual ``messages`` round-trip). The function matches by
   * the latest assistant message that's still streaming OR the
   * most recent assistant message overall when no streaming
   * draft is active. */
  appendGestureToCurrentTurn: (kind: string) => void;

  // Status / metrics
  status: string;
  setStatus: (msg: string) => void;
  metrics: MetricsSnapshot;
  setMetrics: (m: MetricsSnapshot) => void;
  /** Shallow-merge a partial metrics snapshot (back-fills like tts_ms). */
  mergeMetrics: (m: MetricsSnapshot) => void;
  /** Last-known context window from /api/metrics or hello/ws. */
  contextWindow: number;
  contextSource: string;
  setContextInfo: (window: number, source: string) => void;

  // Turn lifecycle
  turnInProgress: boolean;
  setTurnInProgress: (inProgress: boolean) => void;

  // Continuous voice mode
  voiceMode: VoiceMode;
  audioLevel: number;
  lastTranscript: string;
  /**
   * Live partial transcript (Phase 5 of listening_window_prefetch).
   * Set on each stt_partial_live broadcast; cleared on stt_final or when
   * the voice session ends. Rendered as a single transient "Hearing: …"
   * line above the chat input — never appended to the chat history.
   */
  currentPartial: string;
  setVoiceMode: (mode: VoiceMode) => void;
  setAudioLevel: (level: number) => void;
  setLastTranscript: (text: string) => void;
  setCurrentPartial: (text: string) => void;

  // Long-term memories. The Memory tab in the SettingsDrawer uses
  // ``memoryView`` (paginated + filtered slice the user is currently
  // looking at). The ``memoriesEnabled`` flag mirrors the backend's
  // ``memory.enabled`` config so the UI can grey out the tab when the
  // memory subsystem is off.
  memoryView: {
    items: Memory[];
    /** Total matching rows on the server (after applying ``kindFilter``
     * and ``tierFilter``). Pagination math uses this. */
    total: number;
    /** Configured ``memory.max_memories`` cap, surfaced in the UI hint. */
    cap: number;
    /** Zero-based page index. */
    page: number;
    pageSize: number;
    kindFilter: string | null;
    /** Schema v8: optional tier filter. ``null`` means "all tiers". */
    tierFilter: MemoryTier | null;
    order: "recent" | "top";
    /** Schema v8: per-tier counts (drives the header line). Filled
     * from ``/api/memories/counts``. ``null`` before the first fetch. */
    counts: MemoryCounts | null;
  };
  memoriesEnabled: boolean;
  setMemoryView: (view: {
    items: Memory[];
    total: number;
    cap: number;
    enabled: boolean;
    page: number;
    pageSize: number;
    kindFilter: string | null;
    tierFilter?: MemoryTier | null;
    order: "recent" | "top";
  }) => void;
  setMemoryPage: (page: number) => void;
  setMemoryKindFilter: (kind: string | null) => void;
  setMemoryTierFilter: (tier: MemoryTier | null) => void;
  setMemoryOrder: (order: "recent" | "top") => void;
  setMemoryCounts: (counts: MemoryCounts | null) => void;
  /** Reducer for the ``memory_added`` WS event. Only prepends to the
   * current page when we're on page 0, the order is "recent", and the
   * new memory matches the active kind filter. Otherwise the row stays
   * out of view but ``total`` bumps so the pager updates. */
  applyMemoryAdded: (memory: Memory) => void;
  /** Reducer for the ``memory_updated`` WS event. Replaces the row in
   * place if it's currently rendered; no-op otherwise. */
  applyMemoryUpdated: (memory: Memory) => void;
  /** Reducer for the ``memory_deleted`` WS event. Removes the row,
   * decrements ``total``. The page-step-back when the current page
   * empties is owned by the caller (a re-fetch hook in the Memory
   * tab) so we don't spawn a refetch from the store. */
  applyMemoryDeleted: (id: number) => void;

  // ── K2 theory-of-mind beliefs ────────────────────────────────────
  //
  // The Beliefs sub-panel in the Memory tab reads this slice. Unlike
  // ``memoryView`` it isn't paginated (the panel fetches up to 100
  // rows at once), but it mirrors the same WS-reducer shape so the
  // panel stays live as the K2 worker / ``[[predict:...]]`` self-tags
  // / the gap detector flip beliefs in the background. ``kindFilter``
  // and ``statusFilter`` live here (not in the panel) because the
  // ``applyBelief*`` reducers need them to decide whether an incoming
  // row belongs in the current view. ``counts`` stays server-provided
  // (refetched on filter change / manual refresh); the reducers only
  // maintain ``items`` — same trade-off memory's per-tier counts make.
  beliefView: {
    items: Belief[];
    counts: {
      active: number;
      confirmed: number;
      contradicted: number;
      stale: number;
    } | null;
    enabled: boolean;
    kindFilter: BeliefKind | "all";
    statusFilter: BeliefStatus | "all";
  };
  setBeliefView: (view: {
    items: Belief[];
    counts?: {
      active: number;
      confirmed: number;
      contradicted: number;
      stale: number;
    } | null;
    enabled: boolean;
  }) => void;
  setBeliefKindFilter: (kind: BeliefKind | "all") => void;
  setBeliefStatusFilter: (status: BeliefStatus | "all") => void;
  /** Reducer for ``belief_added``: prepend if it matches the active
   * kind + status filter and isn't already present; no-op otherwise. */
  applyBeliefAdded: (belief: Belief) => void;
  /** Reducer for ``belief_updated``: the high-value case is a status
   * flip (active -> contradicted). Re-evaluates filter membership —
   * replaces in place, prepends if it newly matches, or drops it if it
   * no longer matches (e.g. flipped out of the "active" view). */
  applyBeliefUpdated: (belief: Belief) => void;
  /** Reducer for ``belief_deleted``: remove the row by id. */
  applyBeliefDeleted: (id: number) => void;

  // ── Background tasks (chunk 14) ──────────────────────────────────
  //
  // The brain orchestration tasks API surfaces in two places: a
  // compact ``TaskStrip`` above the chat (the canonical live UI for
  // running + recently-completed tasks) and a paginated
  // ``TasksTab`` in the SettingsDrawer for full history.
  //
  // ``tasksById`` is the canonical map keyed by ``task.id``; both
  // surfaces project from it. ``activeIds`` is the "show me right
  // now" projection — running + awaiting_input + recently-completed
  // (within ``TASK_RECENT_FADE_MS`` of ``completed_at``) — sorted
  // newest-id-first. ``historyOrder`` is the current Settings tab
  // page (REST-driven), preserving server order.
  //
  // The split lets a chip slide out 20 s after completion without
  // dropping the row from the Tasks history viewer.
  tasksView: {
    /** Canonical map: every task seen by either WS event or REST
     * fetch lives here. Never mutate in place. */
    tasksById: Record<number, TaskSnapshot>;
    /** Strip projection: active + recently-completed task ids,
     * newest-first. Rotation owned by the WS reducers + a slow
     * sweep when ``setTasksRecentSweep`` fires. */
    activeIds: number[];
    /** Settings tab page (REST). Newest-first by server order. */
    historyOrder: number[];
    /** Pagination math for the Settings tab. */
    total: number;
    page: number;
    pageSize: number;
    /** Currently-applied status filter on the Settings tab.
     * ``null`` means "all statuses". */
    statusFilter: TaskStatus | null;
    /** Loading flag for the REST fetch. */
    loading: boolean;
    /** True iff the backend reported ``enabled: true``. False when
     * the task subsystem is off (REST returns an empty list). */
    enabled: boolean;
    /** Wall-clock ms at which the latest broadcast landed. Used
     * by the sweep helper to decide which recently-completed
     * chips have lived past their grace window. */
    lastEventAt: number;
  };
  /** Reducer for ``task_started`` WS event. Inserts the row in
   * ``tasksById``; prepends to ``activeIds``; prepends to
   * ``historyOrder`` only when the user is on page 0 with a
   * matching status filter. Bumps ``total`` either way. */
  applyTaskStarted: (task: TaskSnapshot) => void;
  /** Reducer for ``task_progress``. Merges the patch onto the
   * existing snapshot. No-op when the task id is unknown — the
   * strip can render later when ``task_started`` lands. */
  applyTaskProgress: (taskId: number, patch: TaskProgressPatch) => void;
  /** Reducer for ``task_input_needed``. Replaces the snapshot.
   * The full snapshot is broadcast so status flips to
   * ``awaiting_input`` and ``input_request`` populates in one pass. */
  applyTaskInputNeeded: (task: TaskSnapshot) => void;
  /** Reducer for ``task_completed``. Replaces the snapshot, marks
   * the row's ``completed_at`` so the strip's grace timer can
   * fade it. Keeps the chip in ``activeIds`` until the next sweep
   * or an explicit ``dismissTaskFromStrip(id)``. */
  applyTaskCompleted: (task: TaskSnapshot) => void;
  /** REST load: ``GET /api/tasks`` paginated. Replaces
   * ``historyOrder`` for the current page and merges rows into
   * ``tasksById``. Does NOT touch ``activeIds`` so a tab refresh
   * doesn't clobber the strip projection. */
  setTasksPage: (response: {
    tasks: TaskSnapshot[];
    total: number;
    page: number;
    pageSize: number;
    enabled: boolean;
  }) => void;
  /** Settings tab user actions. */
  setTaskStatusFilter: (status: TaskStatus | null) => void;
  setTasksLoading: (loading: boolean) => void;
  /** Explicitly drop a chip from the strip — used by the chip's
   * dismiss button + the sweep helper. Idempotent. */
  dismissTaskFromStrip: (taskId: number) => void;
  /** Sweep the strip: drop terminal tasks whose ``completed_at``
   * is older than ``maxAgeMs`` from the current wall clock. Used
   * by the strip's mount-time interval so chips fade after a
   * grace window. */
  sweepRecentlyCompletedTasks: (maxAgeMs: number) => void;

  // Aiko's room (virtual world). Single in-memory snapshot — small
  // enough that we don't need pagination. ``world`` is null until the
  // first GET /api/world resolves.
  world: WorldSnapshot | null;
  setWorld: (snapshot: WorldSnapshot | null) => void;
  /** Reducer for the ``world_updated`` WS event. Surgically merges the
   * patch (state / location / item / deleted_*_id / snapshot). */
  applyWorldPatch: (patch: WorldPatch) => void;

  // ── PR 2: LLM provider catalogue + role assignments ─────────────
  //
  // Loaded once when the Settings drawer opens (or on a WS
  // ``llm_settings_changed`` broadcast). Both null until the first
  // ``GET /api/llm/{providers,routes}`` resolves.
  llmProviders: LlmProvider[] | null;
  llmRoutes: Record<string, LlmRoute> | null;
  setLlmProviders: (providers: LlmProvider[] | null) => void;
  setLlmRoutes: (routes: Record<string, LlmRoute> | null) => void;
  /** Insert / replace a single provider entry (used after PATCH /
   *  POST). The match is by ``id``; missing ids are appended. */
  upsertLlmProvider: (provider: LlmProvider) => void;
  /** Remove a provider by id (used after DELETE). */
  removeLlmProvider: (providerId: string) => void;
  /** Set or replace a route by role (used after PATCH /api/llm/routes/{role}). */
  setLlmRoute: (role: string, route: LlmRoute) => void;

  // Live2D avatar (fixed Alexia bundle).
  avatar: AvatarProfile | null;
  /** Lip-sync amplitude in [0, 1]; updated at <=30 Hz from the WS. */
  audioAmplitude: number;
  /**
   * Latest transient overlay pulse fired by the LLM via ``[[overlay:X]]``.
   * Cleared when ``expiresAt`` passes (renderer effects watch this).
   */
  avatarOverlay: AvatarOverlayState | null;
  /** Latest LLM-driven ``[[motion:X]]`` directive. The renderer subscribes
   * by reference identity (object changes only when a new motion fires)
   * and calls ``model.motion(group, index)``. */
  avatarMotion: AvatarMotionState | null;
  setAvatar: (avatar: AvatarProfile | null) => void;
  /** Patch only the user-tunable runtime knobs without rebuilding the profile. */
  setAvatarSettings: (settings: Partial<AvatarSettingsKnobs>) => void;
  /**
   * Patch the world-state pieces of the avatar (circadian period, resolved
   * outfit) that get refreshed by post-turn ``mood_state`` broadcasts.
   */
  updateAvatarWorldState: (next: {
    circadian_period?: CircadianPeriod;
    resolved_outfit?: ResolvedOutfit;
  }) => void;
  setAvatarOverlay: (overlay: AvatarOverlayState | null) => void;
  setAvatarMotion: (motion: AvatarMotionState | null) => void;
  setAudioAmplitude: (level: number) => void;

  // K31: latest avatar_touch payload + dedup counter. The Live2D
  // engine subscribes to ``avatarTouchAt`` (the counter) so
  // back-to-back gestures with the same kind still re-fire the
  // animation. Chat / persona surfaces read ``avatarTouch`` for
  // the banner label + emoji.
  avatarTouch: AvatarTouchPayload | null;
  avatarTouchAt: number;
  pushAvatarTouch: (payload: AvatarTouchPayload) => void;

  // Phase 2b: persistent mood snapshot, updated post-turn.
  mood: MoodState;
  setMood: (mood: MoodState) => void;

  /** Whether the detached persona window is currently visible. Driven
   * by the ``persona-visibility`` Tauri event in ``App.tsx``. The main
   * window uses this to hide the redundant inline avatar rail when
   * Aiko has been popped out into the floating window. Always
   * ``false`` in a regular browser. */
  personaWindowVisible: boolean;
  setPersonaWindowVisible: (visible: boolean) => void;

  // Phase 1a: transient backchannel hints from STT partials.
  /** ID of the latest backchannel; consumers compare to detect changes. */
  backchannelHint: BackchannelHint | null;
  backchannelAt: number; // Date.now() of last hint
  pushBackchannel: (hint: BackchannelHint) => void;

  // Toasts (transient corner notifications, e.g. "Aiko remembered something")
  toasts: Toast[];
  pushToast: (kind: ToastKind, text: string, ttlMs?: number) => void;
  dismissToast: (id: string) => void;
  /** Push every live toast's deadline out by ``deltaMs`` -- used to
   * "pause" auto-dismiss while the user is hovering the stack to read. */
  extendToasts: (deltaMs: number) => void;

  // Tool activity strip (show "Aiko is checking the time / web / notebook")
  toolActivity: ToolEvent[];
  pushToolEvent: (event: ToolEvent) => void;
  clearToolActivity: () => void;

  /** Activity awareness toggle (desktop opt-in). Mirrors the settings
   * drawer's checkbox so the activity reporter hook can start/stop
   * the polling loop without a reload. Default ``false`` so a user
   * who never opened the drawer still gets the privacy-respecting
   * default. Browser shells render the toggle but can never produce
   * a non-null active app, so flipping it has no observable effect. */
  activityAwarenessEnabled: boolean;
  setActivityAwarenessEnabled: (enabled: boolean) => void;
  /** Last foreground app reported by the activity reporter loop, used
   * solely for the live "Currently sees: <App>" readout under the
   * settings toggle. ``null`` covers "couldn't determine", "user is
   * in our own window", or "feature disabled". Never used for any
   * decision, only display. */
  liveActiveApp: string | null;
  setLiveActiveApp: (app: string | null) => void;

  /** Debug-logging bridge knobs (mirrors ``LoggingSettings`` on the
   * backend). The Settings drawer's "Debug logging" toggle PATCHes
   * ``ui_log_enabled``; everything else is read-only metadata the
   * backend uses to bound how much a misbehaving client can write.
   * Synced on ``hello`` and on ``logging_settings_changed`` WS events;
   * a side-effect subscriber flips ``debugLog.setEnabled`` whenever
   * ``ui_log_enabled`` changes so the batcher stops/starts cleanly. */
  loggingSettings: LoggingSettings;
  setLoggingSettings: (settings: LoggingSettings) => void;
  patchLoggingSettings: (patch: Partial<LoggingSettings>) => void;

  /** Schema v7: "Together" tab slice — phase/days/turns header,
   * milestones, relationship axes bars, anniversary card, and a
   * paginated timeline of shared moments. Loaded on tab open and
   * kept in sync via the ``shared_moment_updated`` /
   * ``relationship_axes_updated`` WS events. */
  togetherView: TogetherViewSlice;
  setTogetherSummary: (summary: TogetherSummary | null) => void;
  setSharedMoments: (
    moments: SharedMoment[],
    total: number,
    page: number,
    pageSize: number,
    vibeFilter: string | null,
  ) => void;
  setTogetherLoading: (loading: boolean) => void;
  setTogetherVibeFilter: (vibe: string | null) => void;
  upsertSharedMoment: (moment: SharedMoment) => void;
  removeSharedMoment: (momentId: number) => void;
  setRelationshipAxes: (axes: RelationshipAxes) => void;

  // ── Layout (in-window) ─────────────────────────────────────────────
  /**
   * P-layout: client-only chrome state. Both fields persist to
   * ``localStorage`` so a reload restores the user's chosen
   * layout. They never round-trip through the backend -- layout
   * is a per-device preference, not a session-shared one.
   *
   * ``leftSidebarCollapsed`` flips between the full 288px
   * conversations sidebar and a 56px icon rail (settings, expand,
   * persona toggle, new session). ``personaPanelWidth`` is the
   * inline avatar column's pixel width, clamped to
   * ``[MIN_PERSONA_PANEL_W, MAX_PERSONA_PANEL_W]`` so a stale
   * localStorage value can't render the panel offscreen.
   */
  leftSidebarCollapsed: boolean;
  personaPanelWidth: number;
  /**
   * Whether the detached persona window should stay above other
   * windows. Persisted client-side and reapplied via the
   * ``set_persona_always_on_top`` Tauri command on every persona
   * open transition (the OS doesn't keep this flag across window
   * recreations, and we deliberately moved this off the server
   * since browsers have no persona window).
   */
  personaAlwaysOnTop: boolean;
  toggleLeftSidebar: () => void;
  setLeftSidebarCollapsed: (collapsed: boolean) => void;
  setPersonaPanelWidth: (px: number) => void;
  setPersonaAlwaysOnTop: (on: boolean) => void;
}

/** State for the "Together" tab. ``page`` is zero-indexed. */
export interface TogetherViewSlice {
  summary: TogetherSummary | null;
  moments: SharedMoment[];
  total: number;
  page: number;
  pageSize: number;
  vibeFilter: string | null;
  loading: boolean;
}

export type ToastKind = "memory" | "info" | "warning" | "error";

export interface Toast {
  id: string;
  kind: ToastKind;
  text: string;
  /** Wall-clock millis when the toast was created -- used for auto-dismiss. */
  createdAt: number;
  /** How long until auto-dismiss; 0 means sticky. */
  ttlMs: number;
}

const REACTION_TAG_RE = /\[\[reaction:(\w+)\]\]/i;

/**
 * Defense-in-depth: strip every meta marker the assistant might emit, so the
 * UI is bulletproof regardless of source (live stream, history fetch, MCP
 * message, future model swap). Mirrors
 * ``app/core/services/response_text_service.strip_all_meta_tags``.
 */
function stripMetaMarkers(s: string): string {
  if (!s) return s;
  return (
    s
      // Drop full [[detail]]...[[/detail]] blocks (and any unclosed tail).
      .replace(/\[\[detail\]\][\s\S]*?(?:\[\[\/detail\]\]|$)/gi, "")
      // Drop [[remember:...]] tags entirely (private notebook).
      .replace(/\[\[remember:[^\]]*?\]\]/gi, "")
      // Drop unclosed remember at end-of-string.
      .replace(/\[\[remember:[^\]]*$/gi, "")
      // Strip [[spoken]]/[[/spoken]] markers (keep content).
      .replace(/\[\[\/?spoken\]\]/gi, "")
      // Strip [[reaction:X]] markers (kept separately as state).
      .replace(/\[\[reaction:\w+\]\]/gi, "")
      // Collapse runaway blank lines left over from removed blocks.
      .replace(/\n{3,}/g, "\n\n")
  );
}

let bubbleCounter = 0;
const nextId = (): string => {
  bubbleCounter += 1;
  return `m_${Date.now().toString(36)}_${bubbleCounter}`;
};

// ── Layout persistence helpers ──────────────────────────────────────
//
// These run at store-creation time to seed the layout slice from the
// previous session's localStorage values, and on every setter to write
// the new value back. ``localStorage`` access is wrapped in
// ``try/catch`` because some environments (incognito, restricted
// embeds, server-side render) make it throw on access -- the layout
// just falls back to defaults rather than crashing the app.

export const MIN_PERSONA_PANEL_W = 320;
export const MAX_PERSONA_PANEL_W = 720;
export const DEFAULT_PERSONA_PANEL_W = 440;

const LS_LEFT_COLLAPSED = "aiko.layout.left_collapsed";
const LS_PERSONA_PANEL_W = "aiko.layout.persona_panel_w";
const LS_PERSONA_ALWAYS_ON_TOP = "aiko.persona.always_on_top";

function clampPanelWidth(value: number): number {
  if (!Number.isFinite(value)) return DEFAULT_PERSONA_PANEL_W;
  return Math.max(MIN_PERSONA_PANEL_W, Math.min(MAX_PERSONA_PANEL_W, value));
}

function readBool(key: string, fallback: boolean): boolean {
  try {
    const raw = localStorage.getItem(key);
    if (raw == null) return fallback;
    if (raw === "1" || raw === "true") return true;
    if (raw === "0" || raw === "false") return false;
    return fallback;
  } catch {
    return fallback;
  }
}

function writeBool(key: string, value: boolean): void {
  try {
    localStorage.setItem(key, value ? "1" : "0");
  } catch {
    // Storage quota / permissions / SSR -- not worth surfacing.
  }
}

function readPersonaPanelWidth(): number {
  try {
    const raw = localStorage.getItem(LS_PERSONA_PANEL_W);
    if (raw == null) return DEFAULT_PERSONA_PANEL_W;
    const parsed = Number.parseFloat(raw);
    if (!Number.isFinite(parsed)) return DEFAULT_PERSONA_PANEL_W;
    return clampPanelWidth(parsed);
  } catch {
    return DEFAULT_PERSONA_PANEL_W;
  }
}

function writePersonaPanelWidth(value: number): void {
  try {
    localStorage.setItem(LS_PERSONA_PANEL_W, String(Math.round(value)));
  } catch {
    // No-op; see ``writeBool``.
  }
}

/** True when ``belief`` belongs in the currently-filtered Beliefs view.
 * Used by the ``applyBelief*`` WS reducers so an incoming row is only
 * shown / kept when it matches the active kind + status filter. */
function beliefMatchesFilter(
  belief: Belief,
  view: { kindFilter: BeliefKind | "all"; statusFilter: BeliefStatus | "all" },
): boolean {
  const kindOk = view.kindFilter === "all" || belief.kind === view.kindFilter;
  const statusOk =
    view.statusFilter === "all" || belief.status === view.statusFilter;
  return kindOk && statusOk;
}

export const useAssistantStore = create<AssistantState>((set) => ({
  connection: { status: "disconnected", lastError: null },
  setConnection: (next) =>
    set((state) => ({ connection: { ...state.connection, ...next } })),

  sessionKey: "",
  model: "",
  ttsEnabled: true,
  ttsState: "idle",
  ttsText: "",
  reaction: "neutral",
  setSessionKey: (key) => set({ sessionKey: key }),
  setModel: (model) => set({ model }),
  setTtsEnabled: (enabled) => set({ ttsEnabled: enabled }),
  setTtsState: (state, text = "", reaction = "neutral") =>
    set({ ttsState: state, ttsText: text, reaction }),

  clientId: "",
  voiceOwnerId: null,
  setClientId: (clientId) => set({ clientId }),
  setVoiceOwnerId: (voiceOwnerId) => set({ voiceOwnerId }),
  audioOwnerId: null,
  setAudioOwnerId: (audioOwnerId) => set({ audioOwnerId }),

  identity: null,
  setIdentity: (identity) => set({ identity }),
  companionSettings: null,
  setCompanionSettings: (patch) =>
    set((state) => ({
      companionSettings: { ...(state.companionSettings ?? {}), ...patch },
    })),

  messages: [],
  streamingDraft: null,
  setMessages: (msgs) =>
    set({
      messages: msgs.map((m) =>
        m.role === "assistant"
          ? { ...m, content: stripMetaMarkers(m.content) }
          : m,
      ),
      // History reload nukes any in-flight draft -- the bubble it
      // referenced is gone, and a fresh ``setMessages`` always lands
      // outside a turn.
      streamingDraft: null,
    }),
  appendUserMessage: (content) =>
    set((state) => ({
      messages: [
        ...state.messages,
        {
          id: nextId(),
          role: "user",
          content,
          createdAt: new Date().toISOString(),
        },
      ],
    })),
  appendAssistantBubble: () => {
    const id = nextId();
    set((state) => ({
      messages: [
        ...state.messages,
        {
          id,
          role: "assistant",
          // Mid-stream content lives in ``streamingDraft``; the
          // placeholder's ``content`` stays empty and is overwritten
          // once at commit. Keeps ``messages`` stable across the
          // whole turn so non-streaming bubbles never re-render.
          content: "",
          createdAt: new Date().toISOString(),
          streaming: true,
        },
      ],
      streamingDraft: { id, content: "", reaction: undefined },
    }));
    return id;
  },
  appendAssistantToken: (chunk) =>
    set((state) => {
      const draft = state.streamingDraft;
      if (!draft) {
        return state;
      }
      // ``stripMetaMarkers`` is idempotent on already-cleaned text,
      // so doing it per chunk keeps the draft canonical even when a
      // marker sits across two chunks (the half-marker tail stays in
      // ``content`` until the next chunk completes it). Cost is
      // O(m) per token same as before -- the win is that this no
      // longer drags the whole ``messages`` array along with it.
      const merged = draft.content + chunk;
      const reactionMatch = REACTION_TAG_RE.exec(merged);
      const reaction = reactionMatch
        ? reactionMatch[1].toLowerCase()
        : draft.reaction;
      const cleaned = stripMetaMarkers(merged);
      return {
        streamingDraft: { id: draft.id, content: cleaned, reaction },
      };
    }),
  finishAssistantBubble: () =>
    set((state) => {
      const draft = state.streamingDraft;
      if (state.messages.length === 0) {
        return draft ? { streamingDraft: null } : state;
      }
      const last = state.messages[state.messages.length - 1];
      if (last.role !== "assistant" || !last.streaming) {
        // No streaming bubble to commit into -- just clear any
        // stale draft so we never leak across turns.
        return draft ? { streamingDraft: null } : state;
      }
      // Two failure modes to absorb here:
      //   1. ``turn_done`` arrives before any token landed (rare but
      //      possible if the agent produces no content). ``draft``
      //      is the empty placeholder; we still flip ``streaming``.
      //   2. ``error`` mid-stream -- we want partial text to stick
      //      so the user sees what Aiko managed to say, then the
      //      bubble exits the streaming state cleanly. Same code
      //      path as a normal commit.
      const committed = draft && draft.id === last.id ? draft : null;
      return {
        messages: [
          ...state.messages.slice(0, -1),
          {
            ...last,
            content: committed ? committed.content : last.content,
            reaction: committed?.reaction ?? last.reaction,
            streaming: false,
          },
        ],
        streamingDraft: null,
      };
    }),
  stampAssistantBackendId: (backendId) =>
    set((state) => {
      if (backendId == null || state.messages.length === 0) return state;
      // Only the just-committed bubble (the last message) is a candidate.
      // Guard against clobbering an already-stamped bubble (e.g. a
      // proactive line that carried its id at append time) and against
      // empty/aborted turns where the last message is the user's.
      const idx = state.messages.length - 1;
      const last = state.messages[idx];
      if (last.role !== "assistant" || last.backendId != null) return state;
      const messages = state.messages.slice();
      messages[idx] = { ...last, backendId };
      return { messages };
    }),
  pushSystemMessage: (content) =>
    set((state) => ({
      messages: [
        ...state.messages,
        {
          id: nextId(),
          role: "system",
          content,
          createdAt: new Date().toISOString(),
        },
      ],
    })),
  appendProactiveMessage: (content, backendId) =>
    set((state) => ({
      messages: [
        ...state.messages,
        {
          id: nextId(),
          role: "assistant",
          content: stripMetaMarkers(content),
          createdAt: new Date().toISOString(),
          kind: "proactive",
          // K32: proactive bubbles carry their persisted id from the
          // ``message`` WS event so reactions work on them immediately.
          ...(backendId != null ? { backendId } : {}),
        },
      ],
    })),
  clearMessages: () => {
    bubbleCounter = 0;
    set({ messages: [], streamingDraft: null });
  },
  applyMessageReactions: (backendId, reactions) =>
    set((state) => ({
      messages: state.messages.map((m) =>
        m.backendId === backendId ? { ...m, reactions: { ...reactions } } : m,
      ),
    })),
  attachLastUserAttachments: (attachments) =>
    set((state) => {
      if (!attachments || attachments.length === 0) return state;
      for (let i = state.messages.length - 1; i >= 0; i -= 1) {
        if (state.messages[i].role === "user") {
          const next = [...state.messages];
          next[i] = { ...next[i], attachments: [...attachments] };
          return { messages: next };
        }
      }
      return state;
    }),
  appendGestureToCurrentTurn: (kind) =>
    set((state) => {
      // Prefer the streaming bubble if one is in flight (the
      // common case for an LLM-emitted touch). Otherwise stamp
      // the most recent assistant bubble (e.g. proactive or
      // MCP-forced ``send_touch``).
      const draftId = state.streamingDraft?.id;
      let targetIndex = -1;
      if (draftId) {
        targetIndex = state.messages.findIndex((m) => m.id === draftId);
      }
      if (targetIndex < 0) {
        for (let i = state.messages.length - 1; i >= 0; i -= 1) {
          if (state.messages[i].role === "assistant") {
            targetIndex = i;
            break;
          }
        }
      }
      if (targetIndex < 0) {
        return state;
      }
      const target = state.messages[targetIndex];
      const existing = target.gestures ?? [];
      if (existing.includes(kind)) {
        return state;
      }
      const next = [...state.messages];
      next[targetIndex] = { ...target, gestures: [...existing, kind] };
      return { messages: next };
    }),

  status: "",
  setStatus: (status) => set({ status }),
  metrics: {},
  setMetrics: (metrics) => set({ metrics }),
  mergeMetrics: (m) =>
    set((state) => ({ metrics: { ...state.metrics, ...m } })),
  contextWindow: 0,
  contextSource: "fallback",
  setContextInfo: (window, source) =>
    set({ contextWindow: window || 0, contextSource: source || "fallback" }),

  turnInProgress: false,
  setTurnInProgress: (inProgress) => set({ turnInProgress: inProgress }),

  voiceMode: "off",
  audioLevel: 0,
  lastTranscript: "",
  currentPartial: "",
  setVoiceMode: (mode) =>
    set(() => {
      const next: Partial<AssistantState> = { voiceMode: mode };
      // Voice session ended -> the live "Hearing: …" line should disappear
      // even if no stt_final lands (e.g. user toggled mic off mid-utterance).
      if (mode === "off") {
        next.currentPartial = "";
      }
      return next;
    }),
  setAudioLevel: (level) =>
    set({ audioLevel: Math.max(0, Math.min(1, level)) }),
  setLastTranscript: (text) => set({ lastTranscript: text }),
  setCurrentPartial: (text) => set({ currentPartial: text }),

  memoryView: {
    items: [],
    total: 0,
    cap: 5000,
    page: 0,
    pageSize: 50,
    kindFilter: null,
    tierFilter: null,
    order: "recent",
    counts: null,
  },
  memoriesEnabled: true,
  setMemoryView: ({
    items,
    total,
    cap,
    enabled,
    page,
    pageSize,
    kindFilter,
    tierFilter,
    order,
  }) =>
    set((state) => ({
      memoryView: {
        items,
        total,
        cap,
        page,
        pageSize,
        kindFilter,
        tierFilter: tierFilter ?? state.memoryView.tierFilter,
        order,
        counts: state.memoryView.counts,
      },
      memoriesEnabled: enabled,
    })),
  setMemoryPage: (page) =>
    set((state) => ({
      memoryView: { ...state.memoryView, page: Math.max(0, page) },
    })),
  setMemoryKindFilter: (kind) =>
    set((state) => ({
      memoryView: { ...state.memoryView, kindFilter: kind, page: 0 },
    })),
  setMemoryTierFilter: (tier) =>
    set((state) => ({
      memoryView: { ...state.memoryView, tierFilter: tier, page: 0 },
    })),
  setMemoryOrder: (order) =>
    set((state) => ({
      memoryView: { ...state.memoryView, order, page: 0 },
    })),
  setMemoryCounts: (counts) =>
    set((state) => ({
      memoryView: { ...state.memoryView, counts },
    })),
  applyMemoryAdded: (memory) =>
    set((state) => {
      const view = state.memoryView;
      const kindMatches = !view.kindFilter || view.kindFilter === memory.kind;
      const tierMatches = !view.tierFilter || view.tierFilter === memory.tier;
      const filterMatches = kindMatches && tierMatches;
      const onFirstPageRecent = view.page === 0 && view.order === "recent";
      // Always bump total when the new row would belong in the
      // current filter. Pagers across other tabs / windows then
      // re-render with the right "X of Y" label even though the row
      // itself isn't visible here.
      const nextTotal = filterMatches ? view.total + 1 : view.total;
      if (filterMatches && onFirstPageRecent) {
        // Prepend; trim to pageSize so the visible page count matches
        // the page-size contract.
        const next = [memory, ...view.items.filter((m) => m.id !== memory.id)];
        return {
          memoryView: {
            ...view,
            items: next.slice(0, view.pageSize),
            total: nextTotal,
          },
        };
      }
      return {
        memoryView: { ...view, total: nextTotal },
      };
    }),
  applyMemoryUpdated: (memory) =>
    set((state) => {
      const view = state.memoryView;
      const idx = view.items.findIndex((m) => m.id === memory.id);
      if (idx < 0) return {};
      const next = view.items.slice();
      next[idx] = memory;
      return { memoryView: { ...view, items: next } };
    }),
  applyMemoryDeleted: (id) =>
    set((state) => {
      const view = state.memoryView;
      const wasOnPage = view.items.some((m) => m.id === id);
      return {
        memoryView: {
          ...view,
          items: view.items.filter((m) => m.id !== id),
          total: wasOnPage ? Math.max(0, view.total - 1) : view.total,
        },
      };
    }),

  // ── K2 theory-of-mind beliefs ────────────────────────────────────
  beliefView: {
    items: [],
    counts: null,
    enabled: true,
    kindFilter: "all",
    statusFilter: "active",
  },
  setBeliefView: (view) =>
    set((state) => ({
      beliefView: {
        ...state.beliefView,
        items: view.items,
        counts: view.counts ?? null,
        enabled: view.enabled,
      },
    })),
  setBeliefKindFilter: (kind) =>
    set((state) => ({
      beliefView: { ...state.beliefView, kindFilter: kind },
    })),
  setBeliefStatusFilter: (status) =>
    set((state) => ({
      beliefView: { ...state.beliefView, statusFilter: status },
    })),
  applyBeliefAdded: (belief) =>
    set((state) => {
      const view = state.beliefView;
      const matches = beliefMatchesFilter(belief, view);
      if (!matches || view.items.some((b) => b.id === belief.id)) return {};
      return { beliefView: { ...view, items: [belief, ...view.items] } };
    }),
  applyBeliefUpdated: (belief) =>
    set((state) => {
      const view = state.beliefView;
      const matches = beliefMatchesFilter(belief, view);
      const idx = view.items.findIndex((b) => b.id === belief.id);
      if (matches) {
        const next = view.items.slice();
        if (idx >= 0) next[idx] = belief;
        else next.unshift(belief);
        return { beliefView: { ...view, items: next } };
      }
      // No longer matches the current view (e.g. flipped out of
      // "active"): drop it if it was visible, otherwise nothing to do.
      if (idx < 0) return {};
      return {
        beliefView: {
          ...view,
          items: view.items.filter((b) => b.id !== belief.id),
        },
      };
    }),
  applyBeliefDeleted: (id) =>
    set((state) => {
      const view = state.beliefView;
      if (!view.items.some((b) => b.id === id)) return {};
      return {
        beliefView: {
          ...view,
          items: view.items.filter((b) => b.id !== id),
        },
      };
    }),

  // ── Background tasks (chunk 14) ──────────────────────────────────
  tasksView: {
    tasksById: {},
    activeIds: [],
    historyOrder: [],
    total: 0,
    page: 0,
    pageSize: 50,
    statusFilter: null,
    loading: false,
    enabled: true,
    lastEventAt: 0,
  },
  applyTaskStarted: (task) =>
    set((state) => {
      const view = state.tasksView;
      const nextById = { ...view.tasksById, [task.id]: task };
      // Strip projection: prepend only if the id wasn't already
      // present (a server-side double-fire would otherwise stack).
      const nextActive = view.activeIds.includes(task.id)
        ? view.activeIds
        : [task.id, ...view.activeIds];
      // History projection: prepend only when the user is on
      // page 0 AND the filter matches (mirror of memory_added).
      const filterMatches =
        view.statusFilter === null || view.statusFilter === task.status;
      const onFirstPage = view.page === 0;
      const nextHistory =
        onFirstPage && filterMatches && !view.historyOrder.includes(task.id)
          ? [task.id, ...view.historyOrder].slice(0, view.pageSize)
          : view.historyOrder;
      // ``total`` always bumps so the pager updates even when the
      // row didn't land on the visible page.
      const nextTotal = filterMatches ? view.total + 1 : view.total;
      return {
        tasksView: {
          ...view,
          tasksById: nextById,
          activeIds: nextActive,
          historyOrder: nextHistory,
          total: nextTotal,
          lastEventAt: Date.now(),
        },
      };
    }),
  applyTaskProgress: (taskId, patch) =>
    set((state) => {
      const view = state.tasksView;
      const existing = view.tasksById[taskId];
      if (!existing) return {};
      const merged: TaskSnapshot = {
        ...existing,
        status: patch.status ?? existing.status,
        progress:
          typeof patch.progress === "number"
            ? patch.progress
            : existing.progress,
        last_message:
          typeof patch.last_message === "string"
            ? patch.last_message
            : existing.last_message,
        // Schema v17: ``phase`` rides on the same patch.
        // ``undefined`` = handler didn't supply one; ``null`` = clear
        // the existing phase; a string = the new value.
        phase:
          patch.phase === undefined ? (existing.phase ?? null) : patch.phase,
      };
      return {
        tasksView: {
          ...view,
          tasksById: { ...view.tasksById, [taskId]: merged },
          lastEventAt: Date.now(),
        },
      };
    }),
  applyTaskInputNeeded: (task) =>
    set((state) => {
      const view = state.tasksView;
      const previouslyKnown = task.id in view.tasksById;
      // Keep the chip on the strip; ensure it's there if the row
      // is somehow new to us (broadcast race between client init
      // and a fast handler).
      const nextActive = view.activeIds.includes(task.id)
        ? view.activeIds
        : [task.id, ...view.activeIds];
      // History: same prepend rule as ``applyTaskStarted`` but
      // we don't bump ``total`` — the row already existed.
      const filterMatches =
        view.statusFilter === null || view.statusFilter === task.status;
      const onFirstPage = view.page === 0;
      const inHistory = view.historyOrder.includes(task.id);
      const nextHistory =
        onFirstPage && filterMatches && !inHistory && !previouslyKnown
          ? [task.id, ...view.historyOrder].slice(0, view.pageSize)
          : view.historyOrder;
      return {
        tasksView: {
          ...view,
          tasksById: { ...view.tasksById, [task.id]: task },
          activeIds: nextActive,
          historyOrder: nextHistory,
          lastEventAt: Date.now(),
        },
      };
    }),
  applyTaskCompleted: (task) =>
    set((state) => {
      const view = state.tasksView;
      // Keep the row in ``tasksById`` so the strip can render
      // "done" / "failed" / "cancelled" briefly before the sweep
      // drops it.
      const nextActive = view.activeIds.includes(task.id)
        ? view.activeIds
        : [task.id, ...view.activeIds];
      // History: ensure terminal rows show up on a fresh load
      // even when the user wasn't on page 0 when the start fired.
      const filterMatches =
        view.statusFilter === null || view.statusFilter === task.status;
      const onFirstPage = view.page === 0;
      const inHistory = view.historyOrder.includes(task.id);
      const inTasksById = task.id in view.tasksById;
      const nextHistory =
        onFirstPage && filterMatches && !inHistory && !inTasksById
          ? [task.id, ...view.historyOrder].slice(0, view.pageSize)
          : view.historyOrder;
      return {
        tasksView: {
          ...view,
          tasksById: { ...view.tasksById, [task.id]: task },
          activeIds: nextActive,
          historyOrder: nextHistory,
          lastEventAt: Date.now(),
        },
      };
    }),
  setTasksPage: ({ tasks, total, page, pageSize, enabled }) =>
    set((state) => {
      const view = state.tasksView;
      const nextById = { ...view.tasksById };
      for (const t of tasks) {
        nextById[t.id] = t;
      }
      return {
        tasksView: {
          ...view,
          tasksById: nextById,
          historyOrder: tasks.map((t) => t.id),
          total,
          page,
          pageSize,
          enabled,
          loading: false,
        },
      };
    }),
  setTaskStatusFilter: (status) =>
    set((state) => ({
      tasksView: {
        ...state.tasksView,
        statusFilter: status,
        page: 0,
      },
    })),
  setTasksLoading: (loading) =>
    set((state) => ({
      tasksView: { ...state.tasksView, loading },
    })),
  dismissTaskFromStrip: (taskId) =>
    set((state) => {
      const view = state.tasksView;
      if (!view.activeIds.includes(taskId)) return {};
      return {
        tasksView: {
          ...view,
          activeIds: view.activeIds.filter((id) => id !== taskId),
        },
      };
    }),
  sweepRecentlyCompletedTasks: (maxAgeMs) =>
    set((state) => {
      const view = state.tasksView;
      if (view.activeIds.length === 0) return {};
      const now = Date.now();
      // A task is sweep-eligible when it's terminal AND its
      // ``completed_at`` (or our local lastEventAt as a fallback)
      // is older than ``maxAgeMs``.
      const TERMINAL = new Set<TaskStatus>([
        "done",
        "failed",
        "cancelled",
        "interrupted",
      ]);
      const remaining = view.activeIds.filter((id) => {
        const row = view.tasksById[id];
        if (!row) return false;
        if (!TERMINAL.has(row.status)) return true;
        const completedAt = row.completed_at
          ? Date.parse(row.completed_at)
          : NaN;
        const referenceAt = Number.isFinite(completedAt)
          ? completedAt
          : view.lastEventAt || now;
        return now - referenceAt < maxAgeMs;
      });
      if (remaining.length === view.activeIds.length) return {};
      return {
        tasksView: { ...view, activeIds: remaining },
      };
    }),

  // PR 2: provider catalogue + role assignments.
  llmProviders: null,
  llmRoutes: null,
  setLlmProviders: (providers) => set({ llmProviders: providers }),
  setLlmRoutes: (routes) => set({ llmRoutes: routes }),
  upsertLlmProvider: (provider) =>
    set((state) => {
      const list = state.llmProviders ?? [];
      const idx = list.findIndex((p) => p.id === provider.id);
      const next =
        idx >= 0
          ? [...list.slice(0, idx), provider, ...list.slice(idx + 1)]
          : [...list, provider];
      return { llmProviders: next };
    }),
  removeLlmProvider: (providerId) =>
    set((state) => ({
      llmProviders: (state.llmProviders ?? []).filter(
        (p) => p.id !== providerId,
      ),
    })),
  setLlmRoute: (role, route) =>
    set((state) => ({
      llmRoutes: { ...(state.llmRoutes ?? {}), [role]: route },
    })),

  world: null,
  setWorld: (snapshot) => set({ world: snapshot }),
  applyWorldPatch: (patch) =>
    set((state) => {
      const current = state.world;
      if (!current) {
        // Patches landing before the initial snapshot are dropped on the
        // floor — the World tab refetches on mount so we'll catch up.
        if ("snapshot" in patch) {
          return {
            world: {
              state: patch.snapshot.state,
              locations: patch.snapshot.locations,
              items: patch.snapshot.items,
              enabled: true,
            },
          };
        }
        return {};
      }
      if ("snapshot" in patch) {
        return {
          world: {
            state: patch.snapshot.state,
            locations: patch.snapshot.locations,
            items: patch.snapshot.items,
            enabled: true,
          },
        };
      }
      if ("state" in patch) {
        return { world: { ...current, state: patch.state as WorldState } };
      }
      if ("location" in patch) {
        const next = (patch as { location: WorldLocation }).location;
        const idx = current.locations.findIndex((l) => l.id === next.id);
        const locations =
          idx >= 0
            ? current.locations.map((l) => (l.id === next.id ? next : l))
            : [...current.locations, next];
        locations.sort((a, b) => a.position - b.position || a.id - b.id);
        return { world: { ...current, locations } };
      }
      if ("item" in patch) {
        const next = (patch as { item: WorldItem }).item;
        const idx = current.items.findIndex((i) => i.id === next.id);
        const items =
          idx >= 0
            ? current.items.map((i) => (i.id === next.id ? next : i))
            : [...current.items, next];
        return { world: { ...current, items } };
      }
      if ("deleted_location_id" in patch) {
        const lid = patch.deleted_location_id;
        return {
          world: {
            ...current,
            locations: current.locations.filter((l) => l.id !== lid),
            // Items that lived in this location now have their
            // location_id cleared. The backend has already done this in
            // SQLite; mirror it here so the UI doesn't flash a stale
            // location reference until the next snapshot arrives.
            items: current.items.map((i) =>
              i.location_id === lid ? { ...i, location_id: null } : i,
            ),
            state:
              current.state.location_id === lid
                ? { ...current.state, location_id: null }
                : current.state,
          },
        };
      }
      if ("deleted_item_id" in patch) {
        const iid = patch.deleted_item_id;
        return {
          world: {
            ...current,
            items: current.items.filter((i) => i.id !== iid),
          },
        };
      }
      return {};
    }),

  avatar: null,
  audioAmplitude: 0,
  avatarOverlay: null,
  avatarMotion: null,
  setAvatar: (avatar) => set({ avatar }),
  setAvatarSettings: (settings) =>
    set((state) => {
      if (!state.avatar) {
        return state;
      }
      return {
        avatar: {
          ...state.avatar,
          settings: { ...state.avatar.settings, ...settings },
        },
      };
    }),
  updateAvatarWorldState: (next) =>
    set((state) => {
      if (!state.avatar) {
        return state;
      }
      const merged: AvatarProfile = { ...state.avatar };
      if (next.circadian_period !== undefined) {
        merged.circadian_period = next.circadian_period;
      }
      if (next.resolved_outfit !== undefined) {
        merged.resolved_outfit = next.resolved_outfit;
      }
      return { avatar: merged };
    }),
  setAvatarOverlay: (overlay) => set({ avatarOverlay: overlay }),
  setAvatarMotion: (motion) => set({ avatarMotion: motion }),
  setAudioAmplitude: (level) =>
    set({ audioAmplitude: Math.max(0, Math.min(1, level)) }),

  avatarTouch: null,
  avatarTouchAt: 0,
  pushAvatarTouch: (payload) =>
    set((state) => ({
      avatarTouch: payload,
      // Increment by at least 1 (and use Date.now() as a coarse
      // wall-clock id) so the engine subscribes to a
      // monotonically-increasing counter — back-to-back ``hug``
      // gestures still fan out as two distinct dispatches.
      avatarTouchAt: Math.max(state.avatarTouchAt + 1, Date.now()),
    })),

  mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.4 },
  setMood: (mood) => set({ mood }),

  personaWindowVisible: false,
  setPersonaWindowVisible: (visible) =>
    set({ personaWindowVisible: Boolean(visible) }),

  backchannelHint: null,
  backchannelAt: 0,
  pushBackchannel: (hint) =>
    set({ backchannelHint: hint, backchannelAt: Date.now() }),

  toasts: [],
  // Default toast lifetime. Bumped over time because users couldn't
  // read the longer "Aiko remembered: ..." / memory-merged toasts
  // before they vanished. Hovering the stack now pauses the countdown
  // (see ``extendToasts`` + ``Toasts.tsx``), so this is just the
  // hands-off lifetime. Callers can still pass a shorter ttlMs.
  pushToast: (kind, text, ttlMs = 12000) =>
    set((state) => ({
      toasts: [
        ...state.toasts,
        {
          id: nextId(),
          kind,
          text,
          createdAt: Date.now(),
          ttlMs,
        },
      ],
    })),
  dismissToast: (id) =>
    set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) })),
  extendToasts: (deltaMs) =>
    set((state) => {
      if (deltaMs <= 0 || state.toasts.length === 0) {
        return {};
      }
      return {
        toasts: state.toasts.map((t) =>
          t.ttlMs > 0 ? { ...t, createdAt: t.createdAt + deltaMs } : t,
        ),
      };
    }),

  toolActivity: [],
  pushToolEvent: (event) =>
    set((state) => {
      // Keep the strip short -- the latest 8 events are enough context.
      const next = [...state.toolActivity, event];
      const trimmed = next.length > 8 ? next.slice(next.length - 8) : next;
      return { toolActivity: trimmed };
    }),
  clearToolActivity: () => set({ toolActivity: [] }),

  activityAwarenessEnabled: false,
  setActivityAwarenessEnabled: (enabled) =>
    set({ activityAwarenessEnabled: Boolean(enabled) }),
  liveActiveApp: null,
  setLiveActiveApp: (app) => set({ liveActiveApp: app ?? null }),

  loggingSettings: { ...DEFAULT_LOGGING_SETTINGS },
  setLoggingSettings: (settings) =>
    set({ loggingSettings: { ...DEFAULT_LOGGING_SETTINGS, ...settings } }),
  patchLoggingSettings: (patch) =>
    set((state) => ({
      loggingSettings: { ...state.loggingSettings, ...patch },
    })),

  togetherView: {
    summary: null,
    moments: [],
    total: 0,
    page: 0,
    pageSize: 20,
    vibeFilter: null,
    loading: false,
  },
  setTogetherSummary: (summary) =>
    set((state) => ({
      togetherView: { ...state.togetherView, summary },
    })),
  setSharedMoments: (moments, total, page, pageSize, vibeFilter) =>
    set((state) => ({
      togetherView: {
        ...state.togetherView,
        moments,
        total,
        page,
        pageSize,
        vibeFilter,
      },
    })),
  setTogetherLoading: (loading) =>
    set((state) => ({
      togetherView: { ...state.togetherView, loading: Boolean(loading) },
    })),
  setTogetherVibeFilter: (vibe) =>
    set((state) => ({
      togetherView: { ...state.togetherView, vibeFilter: vibe, page: 0 },
    })),
  upsertSharedMoment: (moment) =>
    set((state) => {
      const tv = state.togetherView;
      // Filter mismatch — drop from current page, but bump total.
      if (tv.vibeFilter && moment.vibe !== tv.vibeFilter) {
        const existing = tv.moments.findIndex((m) => m.id === moment.id);
        if (existing >= 0) {
          const next = tv.moments.slice();
          next.splice(existing, 1);
          return {
            togetherView: {
              ...tv,
              moments: next,
              total: Math.max(0, tv.total - 1),
            },
          };
        }
        return state;
      }
      const idx = tv.moments.findIndex((m) => m.id === moment.id);
      if (idx >= 0) {
        const next = tv.moments.slice();
        next[idx] = moment;
        return { togetherView: { ...tv, moments: next } };
      }
      // Insert in the right chronological place (newest first by 'when').
      const next = tv.moments.slice();
      const insertAt = next.findIndex((m) => moment.when > m.when);
      if (insertAt < 0) {
        next.push(moment);
      } else {
        next.splice(insertAt, 0, moment);
      }
      return {
        togetherView: { ...tv, moments: next, total: tv.total + 1 },
      };
    }),
  removeSharedMoment: (momentId) =>
    set((state) => {
      const tv = state.togetherView;
      const idx = tv.moments.findIndex((m) => m.id === momentId);
      if (idx < 0) return state;
      const next = tv.moments.slice();
      next.splice(idx, 1);
      return {
        togetherView: {
          ...tv,
          moments: next,
          total: Math.max(0, tv.total - 1),
        },
      };
    }),
  setRelationshipAxes: (axes) =>
    set((state) => ({
      togetherView: {
        ...state.togetherView,
        summary: state.togetherView.summary
          ? { ...state.togetherView.summary, axes }
          : state.togetherView.summary,
      },
    })),

  // ── Layout slice ────────────────────────────────────────────────
  leftSidebarCollapsed: readBool(LS_LEFT_COLLAPSED, false),
  personaPanelWidth: readPersonaPanelWidth(),
  personaAlwaysOnTop: readBool(LS_PERSONA_ALWAYS_ON_TOP, false),
  toggleLeftSidebar: () =>
    set((state) => {
      const next = !state.leftSidebarCollapsed;
      writeBool(LS_LEFT_COLLAPSED, next);
      return { leftSidebarCollapsed: next };
    }),
  setLeftSidebarCollapsed: (collapsed) => {
    writeBool(LS_LEFT_COLLAPSED, collapsed);
    set({ leftSidebarCollapsed: collapsed });
  },
  setPersonaPanelWidth: (px) => {
    const clamped = clampPanelWidth(px);
    writePersonaPanelWidth(clamped);
    set({ personaPanelWidth: clamped });
  },
  setPersonaAlwaysOnTop: (on) => {
    writeBool(LS_PERSONA_ALWAYS_ON_TOP, on);
    set({ personaAlwaysOnTop: on });
  },
}));

// Convenience getter without subscribing (used inside the WS hook).
export const getStore = useAssistantStore.getState;
