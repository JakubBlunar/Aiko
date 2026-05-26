import { create } from "zustand";
import type {
  AvatarMotionState,
  AvatarOverlayState,
  AvatarProfile,
  AvatarSettingsKnobs,
  BackchannelHint,
  ChatMessage,
  CircadianPeriod,
  DesktopSettings,
  Memory,
  MetricsSnapshot,
  MoodState,
  PersonaWindowSettings,
  ResolvedOutfit,
  ToolEvent,
  VoiceMode,
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
  setTtsState: (state: "idle" | "speaking", text?: string, reaction?: string) => void;

  // Chat transcript
  messages: ChatMessage[];
  setMessages: (msgs: ChatMessage[]) => void;
  appendUserMessage: (content: string) => void;
  appendAssistantBubble: () => string; // returns id
  appendAssistantToken: (chunk: string) => void;
  finishAssistantBubble: () => void;
  appendProactiveMessage: (content: string) => void;
  pushSystemMessage: (content: string) => void;
  clearMessages: () => void;

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

  // Long-term memories
  memories: Memory[];
  memoriesEnabled: boolean;
  setMemories: (memories: Memory[], enabled?: boolean) => void;
  upsertMemory: (memory: Memory) => void;
  removeMemory: (id: number) => void;

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

  // Phase 2b: persistent mood snapshot, updated post-turn.
  mood: MoodState;
  setMood: (mood: MoodState) => void;

  /** Desktop / Tauri shell knobs. Only consumed by the persona window;
   * browser tabs leave them alone. ``null`` until the WS ``hello`` lands. */
  desktop: DesktopSettings | null;
  setDesktop: (next: DesktopSettings | null) => void;
  setPersonaWindow: (patch: Partial<PersonaWindowSettings>) => void;
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
  backchannelAt: number;  // Date.now() of last hint
  pushBackchannel: (hint: BackchannelHint) => void;

  // Toasts (transient corner notifications, e.g. "Aiko remembered something")
  toasts: Toast[];
  pushToast: (kind: ToastKind, text: string, ttlMs?: number) => void;
  dismissToast: (id: string) => void;

  // Tool activity strip (show "Aiko is checking the time / web / notebook")
  toolActivity: ToolEvent[];
  pushToolEvent: (event: ToolEvent) => void;
  clearToolActivity: () => void;
}

export type ToastKind = "memory" | "info" | "warning";

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

  messages: [],
  setMessages: (msgs) =>
    set({
      messages: msgs.map((m) =>
        m.role === "assistant"
          ? { ...m, content: stripMetaMarkers(m.content) }
          : m,
      ),
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
          content: "",
          createdAt: new Date().toISOString(),
          streaming: true,
        },
      ],
    }));
    return id;
  },
  appendAssistantToken: (chunk) =>
    set((state) => {
      if (state.messages.length === 0) {
        return state;
      }
      const last = state.messages[state.messages.length - 1];
      if (last.role !== "assistant" || !last.streaming) {
        return state;
      }
      const merged = last.content + chunk;
      const reactionMatch = REACTION_TAG_RE.exec(merged);
      const reaction = reactionMatch
        ? reactionMatch[1].toLowerCase()
        : last.reaction;
      const cleaned = stripMetaMarkers(merged);
      return {
        messages: [
          ...state.messages.slice(0, -1),
          { ...last, content: cleaned, reaction },
        ],
      };
    }),
  finishAssistantBubble: () =>
    set((state) => {
      if (state.messages.length === 0) {
        return state;
      }
      const last = state.messages[state.messages.length - 1];
      if (last.role !== "assistant" || !last.streaming) {
        return state;
      }
      return {
        messages: [
          ...state.messages.slice(0, -1),
          { ...last, streaming: false },
        ],
      };
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
  appendProactiveMessage: (content) =>
    set((state) => ({
      messages: [
        ...state.messages,
        {
          id: nextId(),
          role: "assistant",
          content: stripMetaMarkers(content),
          createdAt: new Date().toISOString(),
          kind: "proactive",
        },
      ],
    })),
  clearMessages: () => {
    bubbleCounter = 0;
    set({ messages: [] });
  },

  status: "",
  setStatus: (status) => set({ status }),
  metrics: {},
  setMetrics: (metrics) => set({ metrics }),
  mergeMetrics: (m) => set((state) => ({ metrics: { ...state.metrics, ...m } })),
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

  memories: [],
  memoriesEnabled: true,
  setMemories: (memories, enabled) =>
    set((state) => ({
      memories,
      memoriesEnabled: enabled ?? state.memoriesEnabled,
    })),
  upsertMemory: (memory) =>
    set((state) => {
      const existing = state.memories.findIndex((m) => m.id === memory.id);
      if (existing >= 0) {
        const next = state.memories.slice();
        next[existing] = memory;
        return { memories: next };
      }
      return { memories: [memory, ...state.memories] };
    }),
  removeMemory: (id) =>
    set((state) => ({
      memories: state.memories.filter((m) => m.id !== id),
    })),

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

  mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.4 },
  setMood: (mood) => set({ mood }),

  desktop: null,
  setDesktop: (next) => set({ desktop: next }),
  setPersonaWindow: (patch) =>
    set((state) => {
      const current =
        state.desktop?.persona_window ?? {
          width: 320,
          height: 480,
          always_on_top: true,
        };
      return {
        desktop: {
          persona_window: { ...current, ...patch },
        },
      };
    }),
  personaWindowVisible: false,
  setPersonaWindowVisible: (visible) =>
    set({ personaWindowVisible: Boolean(visible) }),

  backchannelHint: null,
  backchannelAt: 0,
  pushBackchannel: (hint) =>
    set({ backchannelHint: hint, backchannelAt: Date.now() }),

  toasts: [],
  pushToast: (kind, text, ttlMs = 4500) =>
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

  toolActivity: [],
  pushToolEvent: (event) =>
    set((state) => {
      // Keep the strip short -- the latest 8 events are enough context.
      const next = [...state.toolActivity, event];
      const trimmed = next.length > 8 ? next.slice(next.length - 8) : next;
      return { toolActivity: trimmed };
    }),
  clearToolActivity: () => set({ toolActivity: [] }),
}));

// Convenience getter without subscribing (used inside the WS hook).
export const getStore = useAssistantStore.getState;
