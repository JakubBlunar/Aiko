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
  | "self_tagged";

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
}

export interface MemoriesResponse {
  memories: Memory[];
  count: number;
  enabled: boolean;
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
  | { type: "memory_deleted"; id: number }
  | {
      type: "avatar_settings_changed";
      settings: AvatarSettingsKnobs;
      resolved_outfit?: ResolvedOutfit;
      circadian_period?: CircadianPeriod;
    }
  | { type: "avatar_overlay"; name: string; duration_ms: number }
  | {
      type: "avatar_motion";
      name: string;
      group: string;
      index: number;
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
  | { type: "ping" };
