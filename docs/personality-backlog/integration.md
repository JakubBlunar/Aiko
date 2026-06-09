# Integration / wiring gaps

Features that are backend-complete but under-wired: no UI surface, no
live WebSocket updates, or a failure path that drops something the user
would care about. None of these are new capabilities — they're the
"finish the last mile" work that makes already-shipped features
trustworthy. Cheap individually; compounding in aggregate.

Surfaced during a June 2026 codebase scan. Each entry notes effort and
the verifying file:line.

**Shipped:** I1 (Beliefs tab live updates), I2 (MessageIndexer
retry/back-off), I4 (Settings-drawer coverage), and I5 (persona-window
banner master switches) landed in the reliability pass — see
[`shipped.md`](shipped.md#reliability-pass--i1--i2--i4--i5-finish-the-wiring-batch).

---

## I3. Agenda has no REST endpoint or UI

**Motivation.** Phase 4a agenda (`[[agenda:...]]` tags, the `agenda`
table, the prompt block, and proactive surfacing) is fully live and
MCP-debuggable (`list_agenda`, `get_agenda_stats`), but there is **no
REST endpoint and no Settings/Memory surface** for the user to see,
complete, or drop agenda items. It's an invisible feature unless you
attach an MCP client.

**Key files.** [`app/web/server.py`](../../app/web/server.py) (new
`/api/agenda` GET + complete/drop), a new sub-panel under
[`web/src/components/settings/`](../../web/src/components/settings/),
the agenda store + WS event for live updates.

**Effort.** Medium.

---

## I6. Chat history is hard-capped at 200 messages with no "load older"

**Motivation.** The UI loads at most ~200 messages and the REST
`GET /api/sessions/{id}/messages` only accepts `limit` (the DB layer
already supports `offset`). Long sessions silently truncate older
history in the UI with no affordance to page back, even though the
data is all there.

**Key files.** [`app/web/server.py`](../../app/web/server.py) (add
`offset` to the messages endpoint),
[`web/src/api.ts`](../../web/src/api.ts) `loadMessages`,
[`web/src/components/ChatView.tsx`](../../web/src/components/ChatView.tsx)
("load older" affordance at the top of the scroll).

**Effort.** Medium.

---

## I7. Embedding-model swap wipes LanceDB with only a log line

**Motivation.** When the embedding model or its dimension changes,
`RagStore` drops and rebuilds the LanceDB tables with only a WARNING
log — no user-visible toast or Settings warning. A user who changes the
embed model loses document/message vectors without any in-app signal
that a destructive rebuild happened.

**Key files.** [`app/core/rag/rag_store.py`](../../app/core/rag/rag_store.py)
L301-309, a `warning` toast over WS, or a confirmation step in the
Settings embed-model control.

**Effort.** Medium.

---

## I8. No React error boundary

**Motivation.** A single render exception (Live2D, a settings panel,
a malformed WS payload) white-screens the entire UI with no recovery
affordance — the whole app dies instead of the failing subtree. A top-
level error boundary with a "reload" fallback would contain the blast
radius.

**Key files.** [`web/src/App.tsx`](../../web/src/App.tsx) (wrap the tree),
a new `ErrorBoundary.tsx`.

**Effort.** Small.
