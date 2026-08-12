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
[`web/src/components/SettingsDrawer.tsx`](../../web/src/features/settings/SettingsDrawer.tsx)
and the `settings/` panels (reflow), `backendBase()` for the
split-origin case.

**Open questions.** Is the goal "use from my phone on the same LAN"
(layer 1 only — cheap, no cert) or "install + auto-update anywhere"
(layer 2 — needs the HTTPS origin + SW update flow)? They have very
different effort profiles; layer 1 is a contained frontend pass, layer
2 is a deployment project.

**Effort.** Medium (responsive layout) / Large (full PWA + hosted
HTTPS + update flow).
