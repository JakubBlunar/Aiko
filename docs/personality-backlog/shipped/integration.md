# Shipped -- Integration & wiring (I-series)

Part of the [shipped log index](../shipped.md). The "finish the last mile"
batch: features that were backend-complete but under-wired, closed one by one.
The earlier reliability pass (I1, I2, I4, I5) lives in
[`features.md`](features.md#reliability-pass--i1--i2--i4--i5-finish-the-wiring-batch).
Open items still live in [`integration.md`](../integration.md).

---

## I3. Agenda has no REST endpoint or UI — SHIPPED

**Motivation.** Phase 4a agenda (`[[agenda:...]]` tags, the `agenda`
table, the prompt block, and proactive surfacing) is fully live and
MCP-debuggable (`list_agenda`, `get_agenda_stats`), but there is **no
REST endpoint and no Settings/Memory surface** for the user to see,
complete, or drop agenda items. It's an invisible feature unless you
attach an MCP client.

**Key files.** [`app/web/server.py`](../../../app/web/server.py) (new
`/api/agenda` GET + complete/drop), a new sub-panel under
[`web/src/components/settings/`](../../../web/src/features/settings/),
the agenda store + WS event for live updates.

**Effort.** Medium.

> **Shipped.** The agenda is now a first-class REST + UI surface that
> stays live across all three write paths (inline `[[agenda:...]]`
> tags, the LLM grooming worker, and manual edits).
>
> **Live-update seam.** [`AgendaStore`](../../../app/core/goals/agenda.py)
> gained an `on_change` callback fired after every `add` / `update`
> (and thus `mark_done` / `mark_dropped`), wired in
> [`SpeakingWorkersInitMixin`](../../../app/core/session/speaking_workers_init_mixin.py)
> to a new `_notify_agenda` sink on
> [`MemoryFacadeMixin`](../../../app/core/session/memory_facade_mixin.py).
> Because the notification lives in the *store*, the groom worker and the
> post-turn tag writer surface in the UI for free — no per-call-site
> plumbing. The web layer subscribes via `add_agenda_listener` and
> rebroadcasts a single `agenda_updated` WS event
> ([`server.py`](../../../app/web/server.py)); the client upserts by id.
>
> **REST.** `GET /api/agenda?status=&limit=`, `GET /api/agenda/stats`,
> `POST /api/agenda`, `PATCH /api/agenda/{id}` (status / importance /
> goal / due_at) in
> [`memory_world_routes.py`](../../../app/web/rest/memory_world_routes.py),
> backed by `list_agenda` / `add_agenda` / `update_agenda` /
> `agenda_stats` facade methods that mirror the existing MCP
> `list_agenda` / `get_agenda_stats` JSON shape (`{items, enabled}` /
> `{enabled, **worker.stats()}`).
>
> **Frontend.** New `AgendaItem` / `AgendaResponse` / `StartupNotice`
> types, `api.listAgenda` / `createAgenda` / `updateAgenda`, an
> `agendaView` Zustand slice (`setAgendaView` + an upsert-by-id
> `applyAgendaUpdated` reducer; status filtering is client-side so a
> status flip never needs a refetch), the `agenda_updated` socket case,
> and a new **Agenda** sub-tab in the Memory drawer
> ([`AgendaPanel.tsx`](../../../web/src/features/settings/memory/AgendaPanel.tsx))
> with status filter, inline add, and complete / drop / reopen actions.
> Tests: [`tests/test_agenda.py`](../../../tests/test_agenda.py)
> (`AgendaOnChangeTests`),
> [`tests/test_web_server_agenda.py`](../../../tests/test_web_server_agenda.py),
> [`web/src/stores/slices/agenda.test.ts`](../../../web/src/stores/slices/agenda.test.ts).

---

## I6. Chat history is hard-capped at 200 messages with no "load older" — SHIPPED

**Motivation.** The UI loads at most ~200 messages and the REST
`GET /api/sessions/{id}/messages` only accepts `limit` (the DB layer
already supports `offset`). Long sessions silently truncate older
history in the UI with no affordance to page back, even though the
data is all there.

**Key files.** [`app/web/server.py`](../../../app/web/server.py) (add
`offset` to the messages endpoint),
[`web/src/api.ts`](../../../web/src/api.ts) `loadMessages`,
[`web/src/components/ChatView.tsx`](../../../web/src/features/chat/ChatView.tsx)
("load older" affordance at the top of the scroll).

**Effort.** Medium.

> **Shipped** (keyset pagination, not OFFSET). `GET /api/sessions/{id}/messages`
> ([`sessions_settings_routes.py`](../../../app/web/rest/sessions_settings_routes.py))
> takes a new optional `before_id`: omitted → the newest `limit` rows
> (the existing initial-load contract); given → up to `limit` rows
> *immediately older* than that id, via a new
> [`ChatDatabase.get_messages_before`](../../../app/core/infra/chat_database.py)
> (`id < before_id ORDER BY id DESC LIMIT n`, reversed to oldest-first).
> Keyset anchoring on a real row id is overlap-free and stable under
> concurrent inserts — cleaner than the quirky OFFSET semantics of the
> existing `get_messages`. A short page (`< limit`) is the
> end-of-history signal, so no separate count round-trip is needed.
>
> Frontend: [`api.getMessages`](../../../web/src/api.ts) gained a `beforeId`
> arg; a shared `mapRawMessages` helper now backs both the initial load
> ([`SessionSidebar`](../../../web/src/features/sessions/SessionSidebar.tsx)) and
> the older-page load. New store state `historyHasMore` /
> `setHistoryHasMore` (set true when the initial page comes back full,
> narrowed to false on a short older-page) and a `prependMessages`
> reducer (dedupes by `backendId`, leaves `streamingDraft` untouched).
> [`ChatView`](../../../web/src/features/chat/ChatView.tsx) renders a "Load
> older messages" button in Virtuoso's `Header` and uses Virtuoso's
> `firstItemIndex` prepend pattern (a large baseline decremented by the
> prepended count, derived per-`sessionKey` so a session switch reads
> the baseline in the same render) to keep the viewport anchored instead
> of jumping when older rows land at the top. Tests:
> [`tests/test_chat_database.py`](../../../tests/test_chat_database.py)
> (`get_messages_before`), [`tests/test_web_server_messages.py`](../../../tests/test_web_server_messages.py)
> (routing), [`web/src/stores/slices/chat.pagination.test.ts`](../../../web/src/stores/slices/chat.pagination.test.ts).
>
> **Bundled fix — mobile autoscroll.** Virtuoso's default "at bottom"
> tolerance is 4px, which mobile momentum / rubber-band scrolling and
> fractional device-pixel ratios routinely exceed, leaving the list a
> few px off the bottom so `followOutput` stops sticking on new
> messages. `ChatView` now sets `atBottomThreshold={isMobile ? 120 : 24}`
> so the chat stays pinned to the tail on phones.

---

## I7. Embedding-model swap wipes LanceDB with only a log line — SHIPPED

**Motivation.** When the embedding model or its dimension changes,
`RagStore` drops and rebuilds the LanceDB tables with only a WARNING
log — no user-visible toast or Settings warning. A user who changes the
embed model loses document/message vectors without any in-app signal
that a destructive rebuild happened.

**Key files.** [`app/core/rag/rag_store.py`](../../../app/core/rag/rag_store.py)
L301-309, a `warning` toast over WS, or a confirmation step in the
Settings embed-model control.

**Effort.** Medium.

> **Shipped** (boot-notice → toast, not a confirmation step). The embed
> model is read once at boot (`RagStore` has a single construction site
> in `SessionController.__init__`), so a swap only takes effect on the
> next restart and the rebuild is unavoidable by the time a client could
> confirm it — a *post-hoc warning* is the right shape, not a gate.
>
> [`RagStore._validate_or_stamp_meta`](../../../app/core/rag/rag_store.py)
> now records the destructive rebuild on a new public `embedding_swap`
> attribute (`{from_model, from_dim, to_model, to_dim, at}`) alongside
> the existing WARNING log. Because the rebuild happens before any WS
> client is connected, the notice can't be broadcast live; instead
> [`SessionController`](../../../app/core/session/session_controller.py)
> calls a new `_capture_embedding_swap_notice` after opening the store,
> which queues a one-shot `warning` notice via `_queue_startup_notice`
> on [`LifecycleMixin`](../../../app/core/session/lifecycle_mixin.py). The
> WS `hello` payload carries `notices: session.consume_startup_notices()`
> — **consumed once**, so only the first client to connect after boot
> gets the toast and later reconnects don't repeat a stale warning.
>
> Frontend: the `hello` handler in
> [`useAssistantSocket.ts`](../../../web/src/hooks/useAssistantSocket.ts)
> maps each notice to `pushToast` (warnings stick for 30 s), and the
> hello WS event grew an optional `notices?: StartupNotice[]` field. The
> copy spells out exactly what was lost (semantic search over old
> messages + uploaded docs is empty until re-indexed) and what was kept
> (long-term memories live in SQLite). The plumbing is generic — any
> future boot-time condition can `_queue_startup_notice(...)`. Tests:
> [`tests/test_rag_store.py`](../../../tests/test_rag_store.py)
> (`EmbeddingSwapNoticeTests`),
> [`tests/test_startup_notices.py`](../../../tests/test_startup_notices.py).

---

## I8. No React error boundary — SHIPPED

**Motivation.** A single render exception (Live2D, a settings panel,
a malformed WS payload) white-screens the entire UI with no recovery
affordance — the whole app dies instead of the failing subtree. A top-
level error boundary with a "reload" fallback would contain the blast
radius.

**Key files.** [`web/src/App.tsx`](../../../web/src/App.tsx) (wrap the tree),
a new `ErrorBoundary.tsx`.

**Effort.** Small.

> **Shipped.** A top-level [`ErrorBoundary`](../../../web/src/components/ErrorBoundary.tsx)
> wraps `<App />` in [`main.tsx`](../../../web/src/main.tsx) (inside
> `StrictMode`, so it covers both the main and `#/persona` route trees).
> On a caught render/lifecycle throw it shows a legible dark fallback
> card — the error message, a collapsible stack + React component stack,
> and **Reload app** / **Try again** (reset state) / **Copy details**
> buttons — instead of a blank page.
>
> Because the user's goal was *"find out what is causing it when it
> happens again"*, the crash is also **reported to the backend
> unconditionally**. A new [`crashReport.ts`](../../../web/src/crashReport.ts)
> builds a compact report (`{message, stack, componentStack, source,
> url, userAgent, ts}`) and fire-and-forget POSTs it to the new, always-on
> `POST /api/logs/ui-crash` ([`sessions_settings_routes.py`](../../../app/web/rest/sessions_settings_routes.py)).
> Unlike the opt-in `/api/logs/ui` debug bridge (gated behind
> `logging.ui_log_enabled`), this endpoint is **never gated** —
> [`crash_logging.log_ui_crash`](../../../app/core/infra/crash_logging.py)
> emits one `ERROR [ui] crash …` line on the `app.ui` logger (grep via
> `tail_logs(module_contains="ui", level="ERROR")`) and appends a
> structured entry to `crashlog.txt` so the full stack survives a log
> rotation. Field sizes are clipped server-side (8 KB) and client-side
> (16 KB).
>
> `crashReport.ts` also installs global `window` `error` +
> `unhandledrejection` listeners (via `installGlobalCrashReporters()` in
> `main.tsx`) that report the crashes a React boundary *can't* see
> (event-handler throws, async/promise rejections) — report-only, no UI
> change. The reporter is deduped (identical signatures within 10 s) and
> capped (25 reports/page-load) so a crash-loop can't hammer the backend.
> Tests: [`tests/test_web_server_ui_logs.py`](../../../tests/test_web_server_ui_logs.py)
> (`PostUiCrashTests`), [`web/src/crashReport.test.ts`](../../../web/src/crashReport.test.ts),
> [`web/src/components/ErrorBoundary.test.tsx`](../../../web/src/components/ErrorBoundary.test.tsx).

---

## I10. Make `llm.routes` the single runtime source; retire the legacy `chat_llm` mirror — SHIPPED

**Motivation.** The LLM provider catalogue (`llm.providers` + `llm.routes`)
is the **UI-facing** source of truth, but it is **not** what the runtime
actually builds clients from. The boot path
[`SessionController.__init__`](../../../app/core/session/session_controller.py)
constructs `self._chat_client` via `_build_chat_client(chat_llm, ollama, …)`
— it reads the legacy `chat_llm` block directly. The catalogue stays in
sync only because the controller **mirror-writes both directions**
(`reconfigure_chat_llm` and the `llm_settings_mixin` route-edit paths push
catalogue edits down into `chat_llm`/`ollama`, and `_migrate_legacy_llm`
synthesises the catalogue up from the legacy blocks on first boot). The
result is three overlapping homes for the same setting (model, base_url,
temperature, context_window, max_tokens) across `ollama`, `chat_llm`, and
`llm.routes`, kept consistent by mirror logic that's easy to get subtly
wrong and confusing to configure by hand.

Worker model/ctx is already route-first (`worker_default` via
`_worker_route_model_ctx`, P13). This item finishes the job for the **chat**
path: build `_chat_client` from `llm.routes.main_chat` + its
`_find_llm_provider(route.provider_id)` through the existing `ClientCache`,
and reduce `chat_llm` to a **read-only back-compat shim** (or drop it
entirely once nothing reads it).

**The one field with no route home:** `chat_llm.workers_use_local` (the
global "background workers stay on local Ollama" flag). Its semantic should
migrate to the routing table — "workers use local" simply *is*
`worker_default.provider_id == local_ollama` — so the boolean can be
derived from the routes instead of stored separately.

**Already done (config-file slimming, this pass).** `default.json` no longer
ships the `chat_llm` block or `ollama.context_window`; the `ollama` parser
is tolerant (defaults instead of `_required`) so the block can degrade to
just its infra/embedding keys. `ollama` is now documented as the "local
Ollama base + embeddings" block, not the chat-routing block (see
[`docs/configuration.md`](../../configuration.md)). This item is the *code*
follow-up that removes the runtime dependency on the mirror.

**Key files.**
[`session_controller.py`](../../../app/core/session/session_controller.py)
(`_build_chat_client`, the `__init__` client-build block, `_effective_*`
resolution),
[`llm_settings_mixin.py`](../../../app/core/session/llm_settings_mixin.py) +
[`llm_clients_mixin.py`](../../../app/core/session/llm_clients_mixin.py) (the
mirror-write + reconfigure paths),
[`settings.py`](../../../app/core/infra/settings.py) (`_migrate_legacy_llm`,
the `chat_llm` parse + the `workers_use_local` home),
[`sessions_settings_routes.py`](../../../app/web/rest/sessions_settings_routes.py)
+ the `chat_llm` REST/WS surface,
`ChatProviderSection.tsx` (decide whether the single-provider preset UX stays
or folds into the routes table — it folded, and the file is deleted; see the
shipped note below). Tests: `test_session_controller_provider_switch.py`,
`test_web_server_chat_llm.py`, `test_settings_llm_migration.py`,
`test_session_controller_llm_catalogue.py`.

**Open questions.** Keep `chat_llm` as a thin read-only shim for external
scripts (the original back-compat rationale) or remove it outright and
accept a one-time break? Does the legacy migration path stay forever (for
users upgrading from pre-catalogue configs) or get a sunset version?

**Effort.** Medium–Large (touches the boot client-build, both mirror
mixins, REST/WS, the frontend Chat tab, and the migration/round-trip
tests).

> **Shipped** — full removal, not a shim. `ChatLlmSettings`,
> `_parse_chat_llm`, `AppSettings.chat_llm`, `reconfigure_chat_llm`, both
> mirror helpers, the `chat_llm` REST/WS payloads, the
> `PUT /api/settings/llm-credentials` endpoint and the frontend
> `ChatProviderSection` are all deleted.
>
> **Runtime source.** `SessionController.__init__` builds the chat client
> from `llm.routes.main_chat` through `build_client_for_route` +
> `ClientCache`, the same path the workers already used.
> `_build_chat_client` is gone; the credential-probe case (candidate creds
> that were never saved) went to a dedicated `factory.build_probe_client`
> so it can't poison the shared cache. `update_route` is now the single
> mutation path and owns the live-rebuild cascade.
>
> **The two homeless fields found their homes.** Embeddings moved into a
> new `llm.embedding` block (`provider_id` / `model` / `num_ctx` /
> `num_gpu`), so `Embedder` takes an `LlmEmbedding` + its provider instead
> of the old `OllamaSettings`. `workers_use_local` is simply deleted — it
> *is* `worker_default.provider_id`, so storing it separately could only
> ever disagree with the routes.
>
> **`AppSettings.ollama` survives as a derived read-only view** of
> `routes.worker_default` + `llm.embedding`, which is what kept the
> ~40 legacy read sites from all needing to change in one commit. Nothing
> writes to it.
>
> **Migration is one-shot and persisted.** `_migrate_legacy_llm`
> synthesises the catalogue from whatever legacy blocks a `user.json`
> still has, writes the resulting `llm` block back, and prunes the retired
> keys via the new `prune_user_override_keys` — so the second boot reads
> the new block with no legacy code in the path. The legacy keychain entry
> is adopted onto the `main_chat` provider's account during secrets
> hydration, so an existing OpenAI key keeps working without a re-entry.
>
> **First-run fallout.** With one config home it became worth fixing the
> onboarding: the default is now `qwen3.5:9b` at an explicit 65 536
> context on all three routes (auto-detect asks Ollama for the advertised
> max, and recent Qwen tags advertise 256 k, which doesn't fit in consumer
> VRAM). `prewarm_runtime` no longer raises when the model isn't
> downloaded — it reports `missing_chat_model` in the WS hello so the UI
> can reach onboarding — and a new model step lists what's installed,
> lets you pick, and pulls the missing one with a progress bar over
> `POST /api/models/pull` + `model_pull_progress` events.
