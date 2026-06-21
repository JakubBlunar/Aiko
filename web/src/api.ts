// Thin REST wrapper over the FastAPI endpoints.

import { backendBase } from "./desktop/runtime";
import type {
  AccessoryCatalogue,
  AssistantSettings,
  AttachmentRef,
  AvatarProfile,
  AvatarResponse,
  AvatarSettingsKnobs,
  Belief,
  BeliefsResponse,
  ChatLlmSnapshot,
  ChatMessage,
  Identity,
  LlmProvider,
  LlmProviderPreset,
  LlmProviderTestResult,
  LlmRoute,
  LlmTestConnectionResult,
  Memory,
  MemoriesResponse,
  TaskChildrenResponse,
  TaskEventsResponse,
  TaskInputsResponse,
  TasksListResponse,
  TaskSnapshot,
  TaskStatus,
  MemoryConflictsResponse,
  MemoryCounts,
  MemoryCreatePayload,
  MemoryCreateResponse,
  MemoryOrder,
  MemoryUpdatePatch,
  MetricsResponse,
  PersonaRegressionSnapshot,
  RagDocument,
  SessionRow,
  SharedMoment,
  SharedMomentsResponse,
  TogetherSummary,
  TopicGraphSnapshot,
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
  /** K32: persisted user-reaction counts, restored on history reload. */
  reactions?: Record<string, number> | null;
  /** K31: persisted touch-gesture kinds Aiko emitted on this message. */
  gestures?: string[] | null;
  /** D2 Part B: persisted in-chat attachments restored on reload. */
  attachments?: AttachmentRef[] | null;
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
  listModels: (refresh = false, provider?: string) => {
    const params = new URLSearchParams();
    if (refresh) params.set("refresh", "true");
    if (provider) params.set("provider", provider);
    const qs = params.toString();
    return jsonFetch<string[]>(`/api/models${qs ? `?${qs}` : ""}`);
  },
  listVoices: () => jsonFetch<string[]>("/api/voices"),
  // ── Chat LLM provider ────────────────────────────────────────────
  /** Curated provider preset catalogue. Read-only; renders the
   *  picker cards in Settings → Chat. */
  getLlmPresets: () =>
    jsonFetch<{ presets: LlmProviderPreset[] }>("/api/llm/presets"),
  /** Write-only credentials path. Returns the masked snapshot. */
  setLlmCredentials: (payload: {
    api_key?: string;
    api_key_env?: string;
    base_url?: string;
    extra_headers?: Record<string, string>;
  }) =>
    jsonFetch<ChatLlmSnapshot>("/api/settings/llm-credentials", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  /** Dry-run: ping the candidate provider with a one-token chat call.
   *  Never persists the supplied creds; returns 200 with success=false
   *  on auth/model failure so the UI can show the provider's error. */
  testLlmConnection: (payload: {
    provider: "ollama" | "openai_compatible";
    base_url: string;
    api_key: string;
    model: string;
    reasoning_effort?: string;
    extra_headers?: Record<string, string>;
  }) =>
    jsonFetch<LlmTestConnectionResult>("/api/llm/test-connection", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  // ── PR 2: provider catalogue + role-assignment ───────────────────
  /** List the saved provider catalogue with credentials masked. */
  listLlmProviders: () =>
    jsonFetch<{ providers: LlmProvider[] }>("/api/llm/providers"),
  /** Create a new provider entry. ``template_id`` (optional) seeds
   *  from a row of ``_PROVIDER_PRESETS``; ``draft`` overrides any
   *  field. 409 when the id is taken. */
  addLlmProvider: (payload: {
    template_id?: string;
    draft: Partial<LlmProvider> & { id?: string; api_key?: string };
  }) =>
    jsonFetch<LlmProvider>("/api/llm/providers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  /** Edit non-credential fields on a saved provider. The api_key is
   *  stripped server-side as a safety net — use ``updateLlmProviderCredentials``
   *  for that path. */
  updateLlmProvider: (
    providerId: string,
    patch: Partial<Omit<LlmProvider, "id" | "has_api_key">>,
  ) =>
    jsonFetch<LlmProvider>(
      `/api/llm/providers/${encodeURIComponent(providerId)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      },
    ),
  /** Replace the api_key / api_key_env on a saved provider. Validated:
   *  api_key must be whitespace-free. */
  updateLlmProviderCredentials: (
    providerId: string,
    payload: { api_key?: string; api_key_env?: string },
  ) =>
    jsonFetch<LlmProvider>(
      `/api/llm/providers/${encodeURIComponent(providerId)}/credentials`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    ),
  /** Delete a saved provider. 409 when still referenced by a route. */
  deleteLlmProvider: (providerId: string) =>
    jsonFetch<{ ok: boolean; deleted: string }>(
      `/api/llm/providers/${encodeURIComponent(providerId)}`,
      { method: "DELETE" },
    ),
  /** Run a one-token probe against a saved provider. ``model`` and
   *  ``context_window`` are optional overrides for testing a typed
   *  combobox value before saving. */
  testLlmProvider: (
    providerId: string,
    overrides?: { model?: string; context_window?: number | null },
  ) =>
    jsonFetch<LlmProviderTestResult>(
      `/api/llm/providers/${encodeURIComponent(providerId)}/test`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(overrides ?? {}),
      },
    ),
  /** List role -> provider assignments. */
  listLlmRoutes: () =>
    jsonFetch<{ routes: Record<string, LlmRoute> }>("/api/llm/routes"),
  /** Set ``llm.routes[role]`` from a partial draft. For ``main_chat``
   *  this rebuilds the chat client immediately and broadcasts
   *  ``llm_settings_changed``. */
  updateLlmRoute: (role: string, patch: Partial<LlmRoute>) =>
    jsonFetch<LlmRoute>(
      `/api/llm/routes/${encodeURIComponent(role)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      },
    ),
  listMemories: (
    options: {
      limit?: number;
      offset?: number;
      order?: MemoryOrder;
      kind?: string | null;
      /** Schema v8 tier filter (scratchpad / long_term / archive). */
      tier?: string | null;
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
    if (options.tier) params.set("tier", options.tier);
    return jsonFetch<MemoriesResponse>(`/api/memories?${params.toString()}`);
  },
  /** Schema v8: per-tier memory totals for the Memory tab header. */
  getMemoryCounts: () =>
    jsonFetch<MemoryCounts>("/api/memories/counts"),
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
  // ── Knowledge gaps (F2) ──────────────────────────────────────────
  listKnowledgeGaps: (includeResolved: boolean = false) =>
    jsonFetch<{ gaps: Memory[]; total: number }>(
      `/api/knowledge-gaps?include_resolved=${includeResolved ? "true" : "false"}`,
    ),
  deleteKnowledgeGap: (id: number) =>
    jsonFetch<{ deleted: number }>(`/api/knowledge-gaps/${id}`, {
      method: "DELETE",
    }),
  resolveKnowledgeGap: (id: number, answer?: string) =>
    jsonFetch<{ gap: Memory }>(`/api/knowledge-gaps/${id}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(answer !== undefined ? { answer } : {}),
    }),
  // ── Curiosity seeds (K9) ─────────────────────────────────────────
  /**
   * Trigger one CuriositySeedWorker.run() on the server. Used by the
   * Memory tab "Regenerate now" button so a tester can confirm the
   * worker's output without waiting for the next idle tick. The
   * returned ``result`` object contains the worker's run summary
   * (``wrote``, ``checked``, etc.).
   */
  runCuriositySeedWorker: () =>
    jsonFetch<{ result: Record<string, unknown> }>(
      "/api/curiosity-seeds/run",
      { method: "POST" },
    ),
  runGoalWorker: () =>
    jsonFetch<{ result: Record<string, unknown> }>(
      "/api/goals/run",
      { method: "POST" },
    ),
  // ── Memory conflicts (F5) ────────────────────────────────────────
  listMemoryConflicts: (
    options: {
      limit?: number;
      offset?: number;
      status?: string;
      includeRecent?: boolean;
    } = {},
  ) => {
    const limit = options.limit ?? 50;
    const offset = options.offset ?? 0;
    const params = new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
      include_recent: options.includeRecent === false ? "false" : "true",
    });
    if (options.status) params.set("status", options.status);
    return jsonFetch<MemoryConflictsResponse>(
      `/api/memory-conflicts?${params.toString()}`,
    );
  },
  resolveMemoryConflict: (
    pairId: number,
    payload: { winner_id: number; action?: "demote" | "delete" },
  ) =>
    jsonFetch<{
      pair_id: number;
      winner_id: number;
      loser_id: number;
      action: string;
      status?: string;
      deleted?: boolean;
    }>(`/api/memory-conflicts/${pairId}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  dismissMemoryConflict: (pairId: number) =>
    jsonFetch<{ dismissed: number }>(
      `/api/memory-conflicts/${pairId}/dismiss`,
      { method: "POST" },
    ),
  // ── Topic-graph browser (K9) ─────────────────────────────────────
  getTopicGraph: () => jsonFetch<TopicGraphSnapshot>("/api/topic-graph"),
  // ── Persona regression (K10) ─────────────────────────────────────
  getPersonaDrift: () =>
    jsonFetch<PersonaRegressionSnapshot>("/api/persona-drift"),
  runPersonaDrift: () =>
    jsonFetch<PersonaRegressionSnapshot>("/api/persona-drift/run", {
      method: "POST",
    }),
  // ── Theory-of-mind beliefs (K2) ──────────────────────────────────
  listBeliefs: (
    options: {
      limit?: number;
      offset?: number;
      kind?: "mood" | "opinion";
      status?: "active" | "confirmed" | "contradicted" | "stale";
    } = {},
  ) => {
    const limit = options.limit ?? 50;
    const offset = options.offset ?? 0;
    const params = new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    });
    if (options.kind) params.set("kind", options.kind);
    if (options.status) params.set("status", options.status);
    return jsonFetch<BeliefsResponse>(`/api/beliefs?${params.toString()}`);
  },
  createBelief: (payload: {
    kind: "mood" | "opinion";
    topic: string;
    predicted_state: string;
    confidence?: number;
  }) =>
    jsonFetch<{ belief: Belief }>(`/api/beliefs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  updateBelief: (
    id: number,
    payload: {
      predicted_state?: string;
      confidence?: number;
      status?: "active" | "confirmed" | "contradicted" | "stale";
    },
  ) =>
    jsonFetch<{ belief: Belief }>(`/api/beliefs/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  deleteBelief: (id: number) =>
    jsonFetch<{ deleted: number }>(`/api/beliefs/${id}`, {
      method: "DELETE",
    }),
  // ── Fact-checker status (F1) ─────────────────────────────────────
  factCheckerStatus: () =>
    jsonFetch<{
      enabled: boolean;
      pending: number;
      queue_total: number;
      last_verified_at: string | null;
      hour_used: number;
      hour_cap: number;
      day_used: number;
      day_cap: number;
    }>("/api/fact-checker/status"),
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
  // Phase 4 (expression overhaul): accessory catalogue + PATCH.
  // ``getAvatarAccessories`` returns one row per known accessory
  // with the current value, the rig's availability flag, and the
  // outfit gate. ``patchAvatarAccessories`` accepts a partial merge
  // (any subset of keys) and the backend persists + broadcasts.
  getAvatarAccessories: () =>
    jsonFetch<AccessoryCatalogue>("/api/avatar/accessories"),
  patchAvatarAccessories: async (
    patch: Record<string, string | boolean>,
  ): Promise<AccessoryCatalogue> => {
    return jsonFetch<AccessoryCatalogue>("/api/avatar/accessories", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
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
  // ── In-chat attachments (D2 Part B) ──────────────────────────────
  uploadAttachment: async (file: File): Promise<AttachmentRef> => {
    const form = new FormData();
    form.append("file", file);
    const res = await jsonFetch<{ attachment: AttachmentRef }>(
      "/api/chat/attachments",
      { method: "POST", body: form },
    );
    return res.attachment;
  },
  deleteAttachment: (rel_path: string) => {
    // rel_path is "Attachments:<uuid><ext>"; the endpoint takes the
    // stored name (everything after the colon).
    const storedName = rel_path.includes(":")
      ? rel_path.split(":").slice(1).join(":")
      : rel_path;
    return jsonFetch<{ deleted: boolean; stored_name: string }>(
      `/api/chat/attachments/${encodeURIComponent(storedName)}`,
      { method: "DELETE" },
    );
  },
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
  /** K32 reciprocity: register one click on a user-reaction kind
   * (``heart`` / ``hug`` / ``laugh`` / ``thumbs`` / ``rose`` /
   * ``surprise``). Returns the new full reactions map for the
   * message so the caller can optimistically update before the WS
   * broadcast lands. */
  addReaction: (messageId: number, kind: string) =>
    jsonFetch<{ message_id: number; reactions: Record<string, number> }>(
      `/api/chat/messages/${messageId}/reactions`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind }),
      },
    ),
  /** K32 reciprocity: undo one previously-registered reaction.
   * Decrements the counter; does NOT subtract the relationship-
   * axes nudge (the original act of expressing care still
   * counted). Returns the new full reactions map. */
  removeReaction: (messageId: number, kind: string) =>
    jsonFetch<{ message_id: number; reactions: Record<string, number> }>(
      `/api/chat/messages/${messageId}/reactions/${encodeURIComponent(kind)}`,
      {
        method: "DELETE",
      },
    ),
  getMetrics: () => jsonFetch<MetricsResponse>("/api/metrics"),
  // Identity (first-run onboarding). The frontend reads ``needs_onboarding``
  // from the WS hello on connect; this REST pair is used by the modal
  // submit handler and any "change name" surface in Settings.
  getIdentity: () => jsonFetch<Identity>("/api/settings/identity"),
  setIdentity: (user_display_name: string) =>
    jsonFetch<Identity>("/api/settings/identity", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_display_name }),
    }),

  // ── Background tasks (chunk 13/14) ──────────────────────────────
  //
  // Read-mostly: list / get / cancel / answer. There's deliberately
  // no spawn endpoint — tasks are created exclusively from inside a
  // turn via Aiko's ``start_*`` tools or system code. See
  // ``docs/brain-orchestration.md`` for the design rationale.
  listTasks: (
    options: {
      limit?: number;
      offset?: number;
      status?: TaskStatus | null;
      rootsOnly?: boolean;
    } = {},
  ) => {
    const limit = options.limit ?? 50;
    const offset = options.offset ?? 0;
    const params = new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    });
    if (options.status) params.set("status", options.status);
    if (options.rootsOnly) params.set("roots_only", "true");
    return jsonFetch<TasksListResponse>(`/api/tasks?${params.toString()}`);
  },
  getTask: (id: number) =>
    jsonFetch<{ task: TaskSnapshot }>(`/api/tasks/${id}`),
  /** Schema v17 task tree: the child tasks (workflow steps) of a
   * parent, ascending by spawn order. Fetched lazily when the user
   * expands a parent row in the Tasks tab. */
  listTaskChildren: (id: number) =>
    jsonFetch<TaskChildrenResponse>(`/api/tasks/${id}/children`),
  cancelTask: (id: number) =>
    jsonFetch<{ task_id: number; cancelled: boolean }>(
      `/api/tasks/${id}/cancel`,
      { method: "POST" },
    ),
  /** Resolve an ``awaiting_input`` task with the user's answer. The
   * REST endpoint forgivingly accepts ``input`` or ``answer`` as the
   * body field name; we always send ``input`` to match the canonical
   * docs. */
  answerTask: (id: number, input: string) =>
    jsonFetch<{ task_id: number; accepted: boolean }>(
      `/api/tasks/${id}/answer`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ input }),
      },
    ),
  /** Schema v17: per-task event log. ``order`` defaults to
   * chronological replay; pass ``"desc"`` for newest-first. */
  listTaskEvents: (
    id: number,
    options: { limit?: number; offset?: number; order?: "asc" | "desc" } = {},
  ) => {
    const limit = options.limit ?? 100;
    const offset = options.offset ?? 0;
    const order = options.order ?? "asc";
    const params = new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
      order,
    });
    return jsonFetch<TaskEventsResponse>(
      `/api/tasks/${id}/events?${params.toString()}`,
    );
  },
  /** Schema v17: per-task input/answer history. No pagination —
   * per-task volume is bounded. */
  listTaskInputs: (id: number) =>
    jsonFetch<TaskInputsResponse>(`/api/tasks/${id}/inputs`),
};
