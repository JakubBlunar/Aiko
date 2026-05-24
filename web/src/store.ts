import { create } from "zustand";
import type { ChatMessage, MetricsSnapshot, VoiceMode } from "./types";

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
  pushSystemMessage: (content: string) => void;
  clearMessages: () => void;

  // Status / metrics
  status: string;
  setStatus: (msg: string) => void;
  metrics: MetricsSnapshot;
  setMetrics: (m: MetricsSnapshot) => void;

  // Turn lifecycle
  turnInProgress: boolean;
  setTurnInProgress: (inProgress: boolean) => void;

  // Continuous voice mode
  voiceMode: VoiceMode;
  audioLevel: number;
  lastTranscript: string;
  setVoiceMode: (mode: VoiceMode) => void;
  setAudioLevel: (level: number) => void;
  setLastTranscript: (text: string) => void;
}

const REACTION_TAG_RE = /\[\[reaction:(\w+)\]\]/i;

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
  setMessages: (msgs) => set({ messages: msgs }),
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
      const cleaned = merged.replace(/\[\[reaction:\w+\]\]/gi, "");
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
  clearMessages: () => {
    bubbleCounter = 0;
    set({ messages: [] });
  },

  status: "",
  setStatus: (status) => set({ status }),
  metrics: {},
  setMetrics: (metrics) => set({ metrics }),

  turnInProgress: false,
  setTurnInProgress: (inProgress) => set({ turnInProgress: inProgress }),

  voiceMode: "off",
  audioLevel: 0,
  lastTranscript: "",
  setVoiceMode: (mode) => set({ voiceMode: mode }),
  setAudioLevel: (level) =>
    set({ audioLevel: Math.max(0, Math.min(1, level)) }),
  setLastTranscript: (text) => set({ lastTranscript: text }),
}));

// Convenience getter without subscribing (used inside the WS hook).
export const getStore = useAssistantStore.getState;
