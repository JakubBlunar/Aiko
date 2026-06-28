import type { CompanionSettings, Identity } from "@/types";
import type { SliceCreator } from "../types";

export interface ConnectionState {
  status: "disconnected" | "connecting" | "connected";
  lastError: string | null;
}

export interface SessionSlice {
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

  /** Per-connection WebSocket identity (from the ``hello`` envelope). */
  clientId: string;
  voiceOwnerId: string | null;
  setClientId: (clientId: string) => void;
  setVoiceOwnerId: (ownerId: string | null) => void;

  /** The single client elected to play TTS / earcon audio. */
  audioOwnerId: string | null;
  setAudioOwnerId: (ownerId: string | null) => void;

  /** First-run identity (hydrated from ``hello`` + identity_changed). */
  identity: Identity | null;
  setIdentity: (identity: Identity | null) => void;

  /** Companion soft-physicality knobs (touch / reactions / banner). */
  companionSettings: Partial<CompanionSettings> | null;
  setCompanionSettings: (patch: Partial<CompanionSettings>) => void;

  // Status
  status: string;
  setStatus: (msg: string) => void;

  // Turn lifecycle
  turnInProgress: boolean;
  setTurnInProgress: (inProgress: boolean) => void;

  /** K21: bumped on each ``thread_note_updated`` WS event so the session
   * sidebar can refetch its list (titles changed). */
  sessionListSignal: number;
  bumpSessionListSignal: () => void;
}

export const createSessionSlice: SliceCreator<SessionSlice> = (set) => ({
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

  status: "",
  setStatus: (status) => set({ status }),

  turnInProgress: false,
  setTurnInProgress: (inProgress) => set({ turnInProgress: inProgress }),

  sessionListSignal: 0,
  bumpSessionListSignal: () =>
    set((state) => ({ sessionListSignal: state.sessionListSignal + 1 })),
});
