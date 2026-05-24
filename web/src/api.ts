// Thin REST wrapper over the FastAPI endpoints.

import type { AssistantSettings, ChatMessage, SessionRow } from "./types";

interface SessionListResponse {
  active: string;
  sessions: SessionRow[];
}

interface RawMessage {
  role: ChatMessage["role"];
  content: string;
  created_at: string;
}

export interface AudioDevices {
  input: { index: number; name: string }[];
  output: { index: number; name: string }[];
}

async function jsonFetch<T>(input: RequestInfo, init?: RequestInit): Promise<T> {
  const response = await fetch(input, init);
  if (!response.ok) {
    let body = "";
    try {
      body = await response.text();
    } catch {
      // ignore
    }
    throw new Error(
      `${response.status} ${response.statusText}${body ? ` - ${body}` : ""}`,
    );
  }
  return (await response.json()) as T;
}

export const api = {
  listSessions: () => jsonFetch<SessionListResponse>("/api/sessions"),
  newSession: () =>
    jsonFetch<{ session_id: string; session_key: string }>(
      "/api/sessions/new",
      { method: "POST" },
    ),
  switchSession: (session_id: string) =>
    jsonFetch<{ session_key: string }>("/api/sessions/switch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id }),
    }),
  deleteSession: (session_id: string) =>
    jsonFetch<{ deleted: string }>(
      `/api/sessions/${encodeURIComponent(session_id)}`,
      { method: "DELETE" },
    ),
  clearActive: () =>
    jsonFetch<{ cleared: string }>("/api/sessions/clear", { method: "POST" }),
  getMessages: (session_id: string, limit = 200) =>
    jsonFetch<RawMessage[]>(
      `/api/sessions/${encodeURIComponent(session_id)}/messages?limit=${limit}`,
    ),
  getSettings: () => jsonFetch<AssistantSettings>("/api/settings"),
  patchSettings: (patch: Partial<AssistantSettings> | Record<string, unknown>) =>
    jsonFetch<AssistantSettings>("/api/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  listModels: (refresh = false) =>
    jsonFetch<string[]>(`/api/models${refresh ? "?refresh=true" : ""}`),
  listVoices: () => jsonFetch<string[]>("/api/voices"),
  listAudioDevices: () => jsonFetch<AudioDevices>("/api/audio/devices"),
};
