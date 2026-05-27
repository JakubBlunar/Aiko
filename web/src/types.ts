// Wire types shared between the FastAPI server and React.

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  /** ISO timestamp; created when the message lands in the store. */
  createdAt: string;
  /** True while we're still receiving streaming tokens for this bubble. */
  streaming?: boolean;
  /** Optional reaction word emitted by the assistant via [[reaction:X]]. */
  reaction?: string;
  /** Subtype hint -- e.g. "proactive" for unsolicited Aiko nudges. */
  kind?: "proactive";
}

export interface SessionRow {
  session_id: string;
  message_count: number;
  last_activity: string | null;
}

export interface AssistantSettings {
  chat: {
    model: string;
    context_window: number;
    temperature: number;
    max_tokens: number;
  };
  tts: {
    provider: string;
    voice: string;
    enabled: boolean;
  };
  stt: {
    model: string;
    language: string | null;
  };
  audio: {
    microphone_device: number | null;
    output_device: number | null;
    vad_level_threshold: number;
    vad_silence_seconds: number;
    barge_in_enabled: boolean;
  };
  proactive?: {
    silence_seconds: number;
    cooldown_seconds: number;
    /** Typed-mode (non-voice) proactive nudge knobs. Aiko may speak first
     * after a long quiet period in typed chat. Independent of the voice-
     * mode knobs above so the two cadences can differ. Gated client-side
     * by browser visibility / Tauri window focus so a backgrounded app
     * never gets nudged. */
    typed_enabled: boolean;
    silence_seconds_typed: number;
    cooldown_seconds_typed: number;
  };
  /** Activity awareness (desktop opt-in). When enabled and running in
   * the Tauri shell, the foreground app name is forwarded to the
   * backend so Aiko can naturally reference it. App name only —
   * never window titles or URLs. Off by default; browser users see
   * the toggle but it's a no-op there (no signal source). */
  activity?: {
    awareness_enabled: boolean;
  };
  endpointing?: {
    enabled: boolean;
    use_partial_transcript: boolean;
    phrase_silence_seconds: number;
    turn_silence_seconds: number;
    fast_close_silence_seconds: number;
    hesitation_extend_to_turn: boolean;
    barge_in_min_speech_seconds: number;
  };
  tools?: {
    enabled: boolean;
    get_time: boolean;
    recall: boolean;
    web_search: boolean;
    available: string[];
  };
  voice_active?: boolean;
  session_key: string;
}

export interface ToolEvent {
  /** Name of the tool, e.g. "get_time", "recall", "web_search". */
  name: string;
  /** "call" -- model invoked the tool. "result" -- dispatch returned. */
  event: "call" | "result";
  /** True when result was successful. Only set on "result". */
  ok?: boolean;
  /** Truncated JSON preview of the result payload. Only set on "result". */
  preview?: string;
  /** Wall-clock timestamp (ms since epoch) when this event was received. */
  at: number;
}

export type VoiceMode =
  | "off"
  | "listening"
  | "transcribing"
  | "thinking"
  | "speaking";

export type MemoryKind =
  | "fact"
  | "preference"
  | "event"
  | "relationship"
  | "self_tagged"
  | "self"
  | "open_question"
  | "callback"
  | "reflection"
  | "promise"
  | "catchphrase";

export const MEMORY_KINDS: readonly MemoryKind[] = [
  "fact",
  "preference",
  "event",
  "relationship",
  "self",
  "self_tagged",
  "callback",
  "promise",
  "reflection",
  "open_question",
  "catchphrase",
];

export type MemoryOrder = "recent" | "top";

export interface RagDocument {
  document_id: string;
  title: string;
  chunk_count: number;
  created_at: string;
}

export interface UploadDocumentResponse {
  document: {
    document_id: string;
    title: string;
    chunk_count: number;
    bytes_indexed: number;
  };
  documents: RagDocument[];
}

export interface Memory {
  id: number;
  content: string;
  kind: MemoryKind | string;
  salience: number;
  source_session: string | null;
  source_message_id: number | null;
  created_at: string;
  last_used_at: string | null;
  use_count: number;
  pinned: boolean;
}

export interface MemoriesResponse {
  memories: Memory[];
  count: number;
  total: number;
  cap: number;
  enabled: boolean;
}

export interface MemoryUpdatePatch {
  content?: string;
  kind?: MemoryKind | string;
  salience?: number;
}

export interface MemoryCreatePayload {
  content: string;
  kind?: MemoryKind | string;
  salience?: number;
}

/** Server response for ``POST /api/memories``. Either ``memory`` (a brand
 * new row was created) or ``deduped_into`` (the new content collapsed into
 * an existing near-duplicate whose salience was bumped). */
export interface MemoryCreateResponse {
  memory?: Memory;
  deduped_into?: Memory;
}

// ── Live2D avatar (fixed Alexia bundle) ─────────────────────────────

export interface ExpressionRef {
  name: string;
  file: string;
}

export interface MotionRef {
  name: string;
  file: string;
}

/** Single overlay binding (sweat / blush / dizzy / question / ...). */
export interface OverlayBinding {
  /**
   * Either a real Live2D parameter id (drives the param directly) or
   * a synthetic ``"expr:<name>"`` value pointing at an expression
   * file the renderer can call ``model.expression(name)`` on.
   */
  param_id: string;
  on_value: number;
  decay_ms: number;
  label_en: string;
}

/** One parameter contribution inside a multi-param outfit binding. */
export interface OutfitParam {
  param_id: string;
  on_value: number;
}

/** One parameter contribution inside an expression file binding.
 * Mirrors :class:`app.core.avatar_profile.ExpressionParam` and is
 * consumed by the renderer's ExpressionChannel arousal-scaler so a
 * single ``cheerful`` reaction reads quieter at low arousal. */
export interface ExpressionParam {
  param_id: string;
  on_value: number;
}

/** Outfit binding (day clothes / pajamas). Composed of one-or-more
 * parameter contributions because real Cubism rigs almost always
 * encode an outfit as a *combination* (clothes body + hood + pose
 * flag), not a single toggle. */
export interface OutfitBinding {
  params: OutfitParam[];
  label_en: string;
  /** Other outfit names this one excludes (e.g. ``["day_clothes"]``). */
  mutex_with: string[];
}

export interface CdiParameter {
  id: string;
  name: string;
  group_id?: string;
}

export interface CdiPart {
  id: string;
  name: string;
}

export interface AvatarSettingsKnobs {
  scale_multiplier: number;
  /**
   * Body-language intensity multiplier consumed by the renderer.
   * ``0.0`` mutes every mood-driven amplitude (breath sway, body
   * tilts, expression strength, sass burst, ...); ``1.0`` is the
   * authored default; ``1.5`` exaggerates within safe rig limits.
   * Backend clamps to [0.0, 1.5] in ``AppSettings.avatar``.
   */
  expressiveness: number;
  /**
   * Outfit selection mode. Mirrors the Python ``OUTFIT_MODES``
   * allow-list in ``app/core/settings.py`` -- update both sides in
   * lockstep when adding a new outfit.
   *  - ``auto``           -> circadian-driven (pajamas at night)
   *  - ``day``            -> always day clothes (baseline)
   *  - ``pajamas``        -> always pajamas (no sleeping cap)
   *  - ``pajamas_hooded`` -> always pajamas with sleeping cap
   */
  auto_outfit: "auto" | "day" | "pajamas" | "pajamas_hooded";
}

export interface AvatarProfile {
  display_name: string;
  /** Filename within /avatar/ (e.g. ``Alexia.model3.json``). */
  entry_filename: string;
  cubism_version: number;
  expressions: ExpressionRef[];
  motions: Record<string, MotionRef[]>;
  /** Mapping from reaction (cheerful/sad/...) to expression.name. */
  reaction_mapping: Record<string, string>;
  idle_motion_group: string | null;
  talk_motion_group: string | null;
  lip_sync_ids?: string[];
  eye_blink_ids?: string[];
  parameters: CdiParameter[];
  parts: CdiPart[];
  /** Capability flags keyed by ``has_<name>`` (has_pajamas, has_blush, ...). */
  capabilities: Record<string, boolean>;
  overlays: Record<string, OverlayBinding>;
  outfits: Record<string, OutfitBinding>;
  /** Expression-file → list of (Param ID, Value) bindings parsed from
   * each rig's ``.exp3.json``. The ExpressionChannel reads this to
   * arousal-scale the same params the rig's ``expressionManager`` is
   * Add-blending each frame, so a single ``cheerful`` reaction reads
   * quieter at low arousal. Optional for forward compatibility with
   * minimal rigs / older cached payloads. */
  expression_params?: Record<string, ExpressionParam[]>;
  /** Param IDs that paint a stylised mouth-shape overlay on top of
   * the rig's real lip-synced mouth (e.g. ``Param54`` "Grin" on
   * Alexia). When non-empty, ``ExpressionChannel`` tapers any
   * expression-param write whose id is in this list against the
   * live audio amplitude — so the grin fades out while Aiko is
   * speaking and snaps back in as soon as she falls silent.
   * Optional for backwards compatibility with cached payloads. */
  mouth_overlay_param_ids?: string[];
  /** All cat-tail param IDs in declaration order. Empty when the
   * loaded model isn't a cat-girl rig. */
  cat_tail_param_ids: string[];
  /** All cat-ear segment param IDs in declaration order. Empty when
   * the model has no per-side ear segments addressable. */
  cat_ear_param_ids: string[];
  /** User-tunable runtime knobs layered on top of the immutable profile. */
  settings: AvatarSettingsKnobs;
  /** False = the bundle directory was missing on disk at boot. */
  loaded: boolean;
  /** Latest circadian period (drives auto-outfit). */
  circadian_period?: CircadianPeriod;
  /** Resolved outfit ("pajamas"/"day"/""), recomputed server-side. */
  resolved_outfit?: ResolvedOutfit;
}

export interface AvatarResponse {
  avatar: AvatarProfile;
}

/** Transient overlay pulse driven by ``[[overlay:X]]`` tags from the LLM. */
export interface AvatarOverlayState {
  name: string;
  /** Wall-clock ms (Date.now() + duration_ms) when the pulse fades out. */
  expiresAt: number;
}

/** One-shot motion playback driven by ``[[motion:X]]`` tags from the LLM.
 * The renderer subscribes by reference identity (a fresh object indicates a
 * new motion to play) and calls ``model.motion(group, index)``. */
export interface AvatarMotionState {
  name: string;
  group: string;
  index: number;
  /** Wall-clock ms when the directive arrived; used as a debounce key. */
  firedAt: number;
  /** Optional priority lane the renderer should enqueue this motion on:
   *   - ``idle`` (B2 listening micro-cues): low priority, pre-empted
   *     by any ``normal`` motion that lands during the same listening
   *     window;
   *   - ``normal`` (default): the LLM-driven gesture path;
   *   - ``force``: bypasses the lane and stops whatever is playing.
   * Backwards-compatible: payloads without this field are treated as
   * ``normal``. */
  priority?: "idle" | "normal" | "force";
}

export interface MetricsSnapshot {
  mode?: string;
  capture_ms?: number;
  stt_ms?: number;
  llm_ms?: number;
  tts_ms?: number;
  total_ms?: number;
  // Token totals (combined streaming + tool-pass).
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  // Ollama timing breakdown.
  total_duration_ms?: number;
  eval_duration_ms?: number;
  prompt_eval_duration_ms?: number;
  tokens_per_second?: number;
  // Context fill.
  context_window?: number;
  context_source?: "config" | "ollama_show" | "fallback" | string;
  prompt_pct?: number;
  // Prompt-assembly telemetry.
  system_tokens?: number;
  summary_tokens?: number;
  rag_tokens?: number;
  history_tokens?: number;
  user_tokens?: number;
  tool_tokens?: number;
  history_messages_kept?: number;
  history_dropped_count?: number;
  summary_active?: boolean;
  summary_messages?: number;
  // Compaction state.
  compaction_triggered?: boolean;
  compactions_total?: number;
  // Phase 1c: time-to-first-stream-delta + slow-token filler.
  first_token_ms?: number;
  filler_emitted?: boolean;
}

export interface MetricsConfig {
  model: string;
  context_window: number;
  context_source: string;
  max_prompt_tokens_pct: number;
  summary_idle_seconds: number;
  summary_min_unsummarized_messages: number;
  summary_target_tokens: number;
}

export interface MetricsResponse {
  last: MetricsSnapshot;
  average: MetricsSnapshot & { window?: number };
  config: MetricsConfig;
}

// ── WebSocket message envelopes ──────────────────────────────────────

/** Phase 2b: mood_state — a continuous valence/arousal/named-mood snapshot. */
export interface MoodState {
  label: string;
  intensity: number;
  valence: number;
  arousal: number;
}

export type CircadianPeriod =
  | "late_night"
  | "early_morning"
  | "morning"
  | "midday"
  | "afternoon"
  | "evening"
  | "night"
  | "";

/** Resolved outfit name. ``""`` means the avatar has no outfit toggles at all. */
export type ResolvedOutfit = "pajamas" | "pajamas_hooded" | "day" | "";

/** Phase 1a: backchannel hint derived from a stt_partial transcript. */
export type BackchannelHint =
  | "agreement"
  | "disagreement"
  | "surprise"
  | "amusement"
  | "concern"
  | "confused"
  | "thinking";

/** Persona-window settings emitted from the Python backend. The Tauri
 * shell consumes these to resize / pin the persona window; browser
 * deployments simply ignore them. ``always_on_top`` is omitted from
 * the type union to keep the payload trivially serializable in tests. */
export interface PersonaWindowSettings {
  width: number;
  height: number;
  always_on_top: boolean;
}

export interface DesktopSettings {
  persona_window: PersonaWindowSettings;
}

// ── Aiko's room (virtual world) ────────────────────────────────────

export type WorldKind =
  | "food"
  | "book"
  | "gadget"
  | "furniture"
  | "toy"
  | "keepsake"
  | "decor"
  | "other";

export const WORLD_KINDS: readonly WorldKind[] = [
  "food",
  "book",
  "gadget",
  "toy",
  "keepsake",
  "decor",
  "furniture",
  "other",
];

export type WorldPosture =
  | "lying"
  | "sitting"
  | "standing"
  | "curled_up"
  | "leaning";

export const WORLD_POSTURES: readonly WorldPosture[] = [
  "sitting",
  "lying",
  "standing",
  "curled_up",
  "leaning",
];

export type WorldActivity =
  | "idle"
  | "reading"
  | "tinkering"
  | "napping"
  | "watching_screens"
  | "thinking"
  | "snacking"
  | "stretching"
  | "looking_outside"
  | "doodling";

export const WORLD_ACTIVITIES: readonly WorldActivity[] = [
  "idle",
  "watching_screens",
  "reading",
  "tinkering",
  "thinking",
  "snacking",
  "napping",
  "stretching",
  "looking_outside",
  "doodling",
];

export interface WorldLocation {
  id: number;
  slug: string;
  name: string;
  description: string;
  position: number;
}

export interface WorldItem {
  id: number;
  slug: string;
  name: string;
  description: string;
  kind: WorldKind | string;
  consumable: boolean;
  quantity: number;
  location_id: number | null;
  state: Record<string, unknown>;
  given_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface WorldState {
  location_id: number | null;
  posture: WorldPosture | string;
  activity: WorldActivity | string;
  mood_note: string;
  updated_at: string;
}

export interface WorldSnapshot {
  state: WorldState;
  locations: WorldLocation[];
  items: WorldItem[];
  enabled: boolean;
}

/** Surgical patch broadcast over WS after every world write. The reducer
 * applies whichever discriminator field is present. */
export type WorldPatch =
  | { state: WorldState }
  | { location: WorldLocation }
  | { item: WorldItem }
  | { deleted_location_id: number }
  | { deleted_item_id: number }
  | {
      snapshot: {
        state: WorldState;
        locations: WorldLocation[];
        items: WorldItem[];
      };
    };

export interface WorldStatePatch {
  location_id?: number | null;
  posture?: WorldPosture | string;
  activity?: WorldActivity | string;
  mood_note?: string;
}

export interface WorldLocationPayload {
  name: string;
  description?: string;
  slug?: string;
}

export interface WorldItemPayload {
  name: string;
  kind?: WorldKind | string;
  description?: string;
  slug?: string;
  location_id?: number | null;
  consumable?: boolean;
  quantity?: number;
  state?: Record<string, unknown>;
  given_by?: string;
}

export type WsServerEvent =
  | {
      type: "hello";
      session: string;
      model: string;
      tts_enabled: boolean;
      voice_active?: boolean;
      context_window?: number;
      context_source?: string;
      avatar?: AvatarProfile;
      /** Desktop / Tauri shell knobs broadcast to every connecting
       * client so a freshly-opened window already knows its target
       * geometry without an extra REST round-trip. */
      desktop?: DesktopSettings;
    }
  | { type: "token"; chunk: string }
  | { type: "turn_done"; metrics: MetricsSnapshot }
  | { type: "metrics_update"; metrics: MetricsSnapshot }
  | {
      type: "context_window";
      context_window: number;
      context_source: string;
      model: string;
    }
  | { type: "model_changed"; model: string }
  | {
      type: "tts_state";
      event: "start" | "end";
      text?: string;
      reaction?: string;
    }
  | { type: "stt_partial"; text: string }
  | { type: "stt_partial_live"; text: string }
  | { type: "stt_final"; text: string }
  | { type: "voice_state"; state: VoiceMode }
  | { type: "audio_level"; level: number }
  | {
      type: "message";
      role: string;
      speaker: string;
      content: string;
      /** Subtype hint -- e.g. "proactive" for unsolicited Aiko nudges. */
      kind?: "proactive";
    }
  | { type: "session_changed"; session: string }
  | { type: "history_cleared"; session: string }
  | { type: "status"; message: string }
  | { type: "error"; message: string }
  | { type: "memory_added"; memory: Memory }
  | { type: "memory_updated"; memory: Memory }
  | { type: "memory_deleted"; id: number }
  | { type: "world_updated"; patch: WorldPatch }
  | {
      type: "avatar_settings_changed";
      settings: AvatarSettingsKnobs;
      resolved_outfit?: ResolvedOutfit;
      circadian_period?: CircadianPeriod;
    }
  | {
      type: "desktop_settings_changed";
      persona_window: PersonaWindowSettings;
    }
  | { type: "avatar_overlay"; name: string; duration_ms: number }
  | {
      type: "avatar_motion";
      name: string;
      group: string;
      index: number;
      /** Optional priority lane (B2 listening micro-cues use ``"idle"``).
       * Backwards-compatible: payloads without this field are
       * treated as the default normal lane. */
      priority?: "idle" | "normal" | "force";
    }
  | { type: "audio_amplitude"; level: number }
  | {
      type: "tool_event";
      event: "call" | "result";
      payload: {
        name: string;
        ok?: boolean;
        preview?: string;
        arguments?: Record<string, unknown>;
      };
    }
  | ({
      type: "mood_state";
      circadian_period?: CircadianPeriod;
      resolved_outfit?: ResolvedOutfit;
    } & MoodState)
  | { type: "backchannel"; hint: BackchannelHint; partial: string }
  | { type: "pong" };

export type WsClientCommand =
  | { type: "chat"; text: string }
  | { type: "stop" }
  | { type: "switch_session"; session_id: string }
  | { type: "new_session" }
  | { type: "clear" }
  | { type: "voice_start" }
  | { type: "voice_stop" }
  | { type: "ping" }
  /** Single boolean carrying both browser tab visibility AND Tauri
   * window focus; the client AND-folds them so the backend doesn't
   * need to know which signal flipped. Gates the typed-mode
   * proactive-silence timer so a backgrounded UI never gets nudged. */
  | { type: "presence"; visible: boolean }
  /** Foreground app the user is in. Desktop-only; browser shells
   * never emit this. ``null`` covers "couldn't determine" / "user
   * is in our own window". Backend silently drops these events when
   * ``activity.awareness_enabled`` is false. */
  | { type: "user_activity"; app: string | null };
