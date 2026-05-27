// Thin REST wrapper over the FastAPI endpoints.

import { backendBase } from "./desktop/runtime";
import type {
  AssistantSettings,
  AvatarProfile,
  AvatarResponse,
  AvatarSettingsKnobs,
  ChatMessage,
  DesktopSettings,
  Memory,
  MemoriesResponse,
  MemoryCreatePayload,
  MemoryCreateResponse,
  MemoryOrder,
  MemoryUpdatePatch,
  MetricsResponse,
  PersonaWindowSettings,
  RagDocument,
  SessionRow,
  SharedMoment,
  SharedMomentsResponse,
  TogetherSummary,
  UploadDocumentResponse,
  WorldItem,
  WorldItemPayload,
  WorldLocation,
  WorldLocationPayload,
  WorldSnapshot,
  WorldStatePatch,
} from "./types";

/** Build a fully-qualified URL for a backend ``/api`` (or other root-relative)
 * path. In a normal browser context this just prefixes the same origin; in
 * a Tauri webview this routes through the absolute backend URL configured
 * in ``desktop/runtime.ts``. */
function backendUrl(path: string): string {
  const base = backendBase().http;
  if (!base) return path;
  return path.startsWith("/") ? `${base}${path}` : `${base}/${path}`;
}

interface SessionListResponse {
  active: string;
  sessions: SessionRow[];
}

interface RawMessage {
  id?: number;
  role: ChatMessage["role"];
  content: string;
  created_at: string;
}

export interface AudioDevices {
  input: { index: number; name: string }[];
  output: { index: number; name: string }[];
}

async function jsonFetch<T>(input: RequestInfo, init?: RequestInit): Promise<T> {
  const url =
    typeof input === "string" && input.startsWith("/")
      ? backendUrl(input)
      : input;
  const response = await fetch(url, init);
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
  listMemories: (
    options: {
      limit?: number;
      offset?: number;
      order?: MemoryOrder;
      kind?: string | null;
    } = {},
  ) => {
    const limit = options.limit ?? 50;
    const offset = options.offset ?? 0;
    const order = options.order ?? "recent";
    const params = new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
      order,
    });
    if (options.kind) params.set("kind", options.kind);
    return jsonFetch<MemoriesResponse>(`/api/memories?${params.toString()}`);
  },
  deleteMemory: (id: number) =>
    jsonFetch<{ deleted: number }>(`/api/memories/${id}`, {
      method: "DELETE",
    }),
  updateMemory: (id: number, patch: MemoryUpdatePatch) =>
    jsonFetch<{ memory: Memory }>(`/api/memories/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  createMemory: (payload: MemoryCreatePayload) =>
    jsonFetch<MemoryCreateResponse>("/api/memories", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  pinMemory: (id: number, pinned: boolean) =>
    jsonFetch<{ memory: Memory }>(`/api/memories/${id}/pin`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pinned }),
    }),
  getAvatar: () => jsonFetch<AvatarResponse>("/api/avatar"),
  patchAvatarSettings: async (
    patch: Partial<AvatarSettingsKnobs>,
  ): Promise<AvatarProfile> => {
    const result = await jsonFetch<AvatarResponse>("/api/avatar", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    return result.avatar;
  },
  listDocuments: () =>
    jsonFetch<{ documents: RagDocument[] }>("/api/documents"),
  uploadDocument: async (file: File): Promise<UploadDocumentResponse> => {
    const form = new FormData();
    form.append("file", file);
    return jsonFetch<UploadDocumentResponse>("/api/documents/upload", {
      method: "POST",
      body: form,
    });
  },
  deleteDocument: (document_id: string) =>
    jsonFetch<{ deleted: string; documents: RagDocument[] }>(
      `/api/documents/${encodeURIComponent(document_id)}`,
      { method: "DELETE" },
    ),
  // ── World (Aiko's room) ──────────────────────────────────────────
  getWorld: () => jsonFetch<WorldSnapshot>("/api/world"),
  patchWorldState: (patch: WorldStatePatch) =>
    jsonFetch<{ state: WorldSnapshot["state"] }>("/api/world/state", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  createWorldLocation: (payload: WorldLocationPayload) =>
    jsonFetch<{ location: WorldLocation }>("/api/world/locations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  updateWorldLocation: (
    id: number,
    patch: { name?: string; description?: string; position?: number },
  ) =>
    jsonFetch<{ location: WorldLocation }>(`/api/world/locations/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  deleteWorldLocation: (id: number) =>
    jsonFetch<{ deleted_location_id: number }>(`/api/world/locations/${id}`, {
      method: "DELETE",
    }),
  createWorldItem: (payload: WorldItemPayload) =>
    jsonFetch<{ item: WorldItem }>("/api/world/items", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  updateWorldItem: (
    id: number,
    patch: Partial<WorldItemPayload> & { state?: Record<string, unknown> },
  ) =>
    jsonFetch<{ item: WorldItem }>(`/api/world/items/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  deleteWorldItem: (id: number) =>
    jsonFetch<{ deleted_item_id: number }>(`/api/world/items/${id}`, {
      method: "DELETE",
    }),
  consumeWorldItem: (id: number, amount = 1) =>
    jsonFetch<
      | { item: WorldItem; consumed: number }
      | { deleted_item_id: number; consumed: number }
    >(`/api/world/items/${id}/consume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ amount }),
    }),
  /** "Give Aiko a cookie" shortcut. Drops an item into the kitchenette
   * (or the location matching ``location_id`` if you pass one) attributed
   * to ``given_by="user"``. The give is silent — Aiko only notices on
   * her next turn through the world prompt block. */
  giveItem: (
    payload: Omit<WorldItemPayload, "given_by"> & { name: string },
  ) =>
    jsonFetch<{ item: WorldItem }>("/api/world/items", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...payload, given_by: "user" }),
    }),
  reseedWorld: (force = false) =>
    jsonFetch<WorldSnapshot>(
      `/api/world/seed${force ? "?force=true" : ""}`,
      { method: "POST" },
    ),
  // ── Shared moments + Together tab (schema v7) ─────────────────────
  getTogether: () => jsonFetch<TogetherSummary>("/api/together"),
  listSharedMoments: (
    offset = 0,
    limit = 20,
    vibe?: string | null,
  ) => {
    const params = new URLSearchParams({
      offset: String(offset),
      limit: String(limit),
    });
    if (vibe) params.set("vibe", vibe);
    return jsonFetch<SharedMomentsResponse>(
      `/api/shared-moments?${params.toString()}`,
    );
  },
  createSharedMoment: (payload: {
    summary: string;
    vibe?: string;
    when?: string | null;
  }) =>
    jsonFetch<{ moment: SharedMoment }>("/api/shared-moments", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  updateSharedMoment: (
    id: number,
    patch: {
      summary?: string;
      vibe?: string;
      when?: string;
      pinned?: boolean;
    },
  ) =>
    jsonFetch<{ moment: SharedMoment }>(`/api/shared-moments/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  deleteSharedMoment: (id: number) =>
    jsonFetch<{ deleted_moment_id: number }>(`/api/shared-moments/${id}`, {
      method: "DELETE",
    }),
  markMessageAsMoment: (messageId: number, vibe = "general") =>
    jsonFetch<{ moment: SharedMoment }>(
      `/api/chat/messages/${messageId}/mark-moment`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ vibe }),
      },
    ),
  getMetrics: () => jsonFetch<MetricsResponse>("/api/metrics"),
  getDesktop: () => jsonFetch<DesktopSettings>("/api/desktop"),
  patchPersonaWindow: (patch: Partial<PersonaWindowSettings>) =>
    jsonFetch<DesktopSettings>("/api/desktop/persona-window", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
};
