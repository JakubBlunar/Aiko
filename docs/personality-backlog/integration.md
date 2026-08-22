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

---

## I11. Graduate a corroborated belief into a concept

**Motivation.** Two subsystems hold "what Aiko knows about Jacob" and
neither can see the other. The K2 belief store holds short, checkable
claims — *"open-plan offices is a mistake, to him"* — with a lifecycle,
a corroboration count and a contradiction path. The L-series concept
graph holds ~4,000 durable subjects with salience, diets, clustering,
drift detection and the whole surfacing apparatus. A belief that has
been observed repeatedly over months is, by any reading, a durable fact
about him; it just lives in the wrong table, ages out at
`belief_stale_after_days`, and takes its evidence with it.

H51 made this newly worth doing rather than merely tidy. Before it, no
belief was ever corroborated in a way anything read, so there was
nothing to promote. Now `confirmed` means something — two same-state
observations with no contradiction on file — and `list_trusted` is a
real, small, quality-gated set. That set is the natural input to a
graduation rule.

**What it would buy.** Beliefs currently reach the prompt through two
narrow paths (`belief_gaps_block` on a mismatch, `trusted_beliefs_block`
as four lines of standing context) and are invisible to everything else.
A graduated concept inherits the entire concept surface for free:
turn-relevance scoring into `relevant_context`, cluster co-activation,
drift and contradiction workers, the timeline and learning-event
records, the Concepts UI. It also gives the belief layer an exit other
than decay.

**Why it needs thinking through, not just wiring.** Four decisions that
are not obvious:

1. **The bar.** `confirmed` alone is too cheap — two observations inside
   one evening would qualify. Something like *N distinct observations
   spanning M days with no `gap_seen_at`* is closer, but the constants
   want measuring against the real store rather than picking.
2. **Direction and ownership.** Does the belief row survive graduation,
   become a pointer, or get deleted? If both rows persist, which one
   does the gap detector check, and what happens when the concept layer's
   contradiction worker and the belief gap detector disagree about the
   same claim? Two subsystems asserting the same fact with independent
   lifecycles is the failure this must avoid.
3. **Shape mismatch.** A belief is a `(topic, predicted_state)` pair
   written to be read back inside a fixed frame; a concept is a subject
   with a body and a diet. The translation is not mechanical, and doing
   it with the maintenance model reintroduces an LLM call on a path that
   is currently pure.
4. **Kind asymmetry.** An `opinion` belief is plausibly durable. A
   `mood` belief is about how he feels *right now* about something and
   should almost certainly never graduate, however often it recurs —
   unless a recurring mood is itself the durable fact, which is a
   different claim than the row makes.

**Key files.** [`belief_store.py`](../../app/core/relationship/belief_store.py)
(`list_trusted`, the `observations` metadata counter),
[`belief_worker.py`](../../app/core/relationship/belief_worker.py),
[`concept_store.py`](../../app/core/concepts/concept_store.py) and the
L17 drift / learning-event path, [`concepts.md`](concepts.md) for the
concept-side design.

**Effort.** Medium — small in code, most of the cost is in settling the
four questions above and then measuring the bar against the live store
before it starts writing.
