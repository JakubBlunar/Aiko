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
[`shipped.md`](shipped/features.md#reliability-pass--i1--i2--i4--i5-finish-the-wiring-batch).

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

## I8. No React error boundary — SHIPPED

**Motivation.** A single render exception (Live2D, a settings panel,
a malformed WS payload) white-screens the entire UI with no recovery
affordance — the whole app dies instead of the failing subtree. A top-
level error boundary with a "reload" fallback would contain the blast
radius.

**Key files.** [`web/src/App.tsx`](../../web/src/App.tsx) (wrap the tree),
a new `ErrorBoundary.tsx`.

**Effort.** Small.

> **Shipped.** A top-level [`ErrorBoundary`](../../web/src/components/ErrorBoundary.tsx)
> wraps `<App />` in [`main.tsx`](../../web/src/main.tsx) (inside
> `StrictMode`, so it covers both the main and `#/persona` route trees).
> On a caught render/lifecycle throw it shows a legible dark fallback
> card — the error message, a collapsible stack + React component stack,
> and **Reload app** / **Try again** (reset state) / **Copy details**
> buttons — instead of a blank page.
>
> Because the user's goal was *"find out what is causing it when it
> happens again"*, the crash is also **reported to the backend
> unconditionally**. A new [`crashReport.ts`](../../web/src/crashReport.ts)
> builds a compact report (`{message, stack, componentStack, source,
> url, userAgent, ts}`) and fire-and-forget POSTs it to the new, always-on
> `POST /api/logs/ui-crash` ([`sessions_settings_routes.py`](../../app/web/rest/sessions_settings_routes.py)).
> Unlike the opt-in `/api/logs/ui` debug bridge (gated behind
> `logging.ui_log_enabled`), this endpoint is **never gated** —
> [`crash_logging.log_ui_crash`](../../app/core/infra/crash_logging.py)
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
> Tests: [`tests/test_web_server_ui_logs.py`](../../tests/test_web_server_ui_logs.py)
> (`PostUiCrashTests`), [`web/src/crashReport.test.ts`](../../web/src/crashReport.test.ts),
> [`web/src/components/ErrorBoundary.test.tsx`](../../web/src/components/ErrorBoundary.test.tsx).

---

## I9. Mobile responsiveness + PWA installability

**Motivation.** The web UI is desktop-first: the chat column +
`AvatarPanel` are a horizontal flex row gated at `lg+`
([`App.tsx`](../../web/src/App.tsx)), the settings drawer and several
panels assume wide viewports, and there is no manifest / service
worker. A user can't comfortably use Aiko from a phone, let alone
"install" her as a home-screen app. Two separable layers:

1. **Responsive layout (no deployment needed).** Make the main window
   usable at phone widths: stack avatar above/below chat (or make the
   avatar a collapsible header) below a breakpoint, ensure the settings
   drawer + composer + task strip reflow, and respect mobile-safe areas
   / on-screen-keyboard insets. This is pure frontend and works over
   LAN today (point mobile Safari/Chrome at the dev box's
   `http://<lan-ip>:5173`).
2. **PWA installability (needs HTTPS origin).** Add a web app manifest
   (icons, name, display `standalone`, theme color) + a service worker
   so the app is installable and shells offline. **The user's instinct
   is correct:** a real installable PWA with reliable
   update-on-reload needs the bundle served from an **HTTPS origin**
   (service workers are hard-blocked on non-localhost HTTP). Options:
   (a) a self-hosted reverse proxy with a TLS cert (Caddy/Traefik +
   Let's Encrypt) on a domain or Tailscale-funnel hostname; (b) Cursor/
   any static host for the front bundle with the WS pointed at the
   home backend over TLS; (c) localhost-only "installable on this
   machine" which sidesteps the cert but isn't mobile.

**Service-worker update caveat (the user's specific question).** Once a
service worker caches the app shell, "automatic updates" are **not**
automatic by default — the SW serves the cached shell and only fetches
a new one in the background; the user keeps the old version until the
SW activates on a later load (often the *second* visit). Getting
"reload = latest" requires an explicit update flow: register with
`updateViaCache: 'none'`, call `registration.update()` on focus/nav,
and surface a "new version — reload" toast wired to
`skipWaiting()` + `clients.claim()`. Without that, a stale shell can
pin users to an old build indefinitely. A Tauri desktop build is the
escape hatch where update control is fully ours; PWA trades that for
install-anywhere reach.

**Architecture interaction.** The backend already routes every URL
through `backendBase()` ([`web/src/desktop/runtime.ts`](../../web/src/desktop/runtime.ts))
for the Tauri shell — the same indirection is what a remote-hosted PWA
needs (front bundle on the TLS origin, WS/REST pointed at the home
backend). The voice path (client-owned mic PCM over WS) already assumes
a browser client, so mobile voice is mostly a permissions/AudioWorklet
validation pass, not new protocol.

**Key files.** [`web/src/App.tsx`](../../web/src/App.tsx) (responsive
row→stack), [`web/index.html`](../../web/index.html) + a new
`web/public/manifest.webmanifest` + service worker (Vite PWA plugin),
[`web/src/components/SettingsDrawer.tsx`](../../web/src/components/SettingsDrawer.tsx)
and the `settings/` panels (reflow), `backendBase()` for the
split-origin case.

**Open questions.** Is the goal "use from my phone on the same LAN"
(layer 1 only — cheap, no cert) or "install + auto-update anywhere"
(layer 2 — needs the HTTPS origin + SW update flow)? They have very
different effort profiles; layer 1 is a contained frontend pass, layer
2 is a deployment project.

**Effort.** Medium (responsive layout) / Large (full PWA + hosted
HTTPS + update flow).
