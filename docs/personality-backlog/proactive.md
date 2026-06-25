# Proactive + presence follow-ups

Deferred follow-ups from the typed-proactive / activity awareness pass
(C1, see [`shipped.md`](shipped.md)). C5 (per-tab presence aggregation)
was dropped during the May 2026 cleanup — re-derive it from a real bug
report if multi-tab presence ever becomes a real complaint.

---

## C2. Window-title-aware activity

**Motivation.** App name only ships in v1 of activity awareness; window
titles would let Aiko reference doc / file names she sees in Jacob's
foreground app, but leaks bank URLs and private chat targets if naively
forwarded. Worth picking up once we have a privacy story strong enough
to support it.

**Key files.** [`web/src-tauri/src/lib.rs`](../../web/src-tauri/src/lib.rs)
`get_active_app`, [`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
`set_user_active_app` + `_render_activity_block`,
[`web/src/hooks/useActivityReporter.ts`](../../web/src/hooks/useActivityReporter.ts).

**Sketched approach.** Per-app allowlist (`activity.title_allowlist:
{"Cursor": true, "Code": true}`) gated on a settings toggle that's
*also* OFF by default. Forwarded titles get the same privacy footer
treatment as the live readout — visible to the user before they
opt in. Persona update tells Aiko she may reference the title casually
but never quote URLs or chat-target names verbatim.

**Open questions.** Allowlist by app name, or also by app + title-
regex pair so we can let "Cursor" through while still redacting an
incognito tab in the same browser?

---

## C3. Persisting last-fired typed cooldown to disk

**Motivation.** Today the typed-proactive cooldown lives in process
memory (`_last_typed_run_monotonic`) and resets on backend restart.
Fine for the 80% case but a quick restart in the middle of a typed
session can re-arm an immediate proactive nudge, which reads weirdly.

**Key files.** [`config/user.json`](../../config/user.json) (alongside
`last_active_id`), [`app/core/proactive/proactive_director.py`](../../app/core/proactive/proactive_director.py)
`_last_typed_run_monotonic` plus a `_last_typed_run_iso` mirror,
[`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
boot hook that loads the persisted timestamp.

**Sketched approach.** On every successful typed-proactive fire, write
`last_typed_proactive_at: <iso>` to `config/user.json` (debounced ~5s).
On boot, load it; convert to a monotonic offset so the existing
cooldown maths still work.

**Open questions.** Does it matter if the wall-clock between sessions
exceeds the configured cooldown by a large margin (e.g. a week)? We
already have the typed-proactive eligibility predicate guarding the
rest; this is purely about not re-firing back-to-back across a
restart.

---

## C4. TTS-on-typed-proactive toggle

**Motivation.** Typed proactive nudges are text-only by design today.
A "speak typed proactive nudges aloud" knob is cheap to add when the
use case appears (e.g. Jacob wants ambient audio presence even while
typing).

**Key files.** [`app/core/proactive/proactive_director.py`](../../app/core/proactive/proactive_director.py)
`_run_typed` (currently bypasses the TTS pipeline),
[`app/core/infra/settings.py`](../../app/core/infra/settings.py) `AgentSettings`
(new `proactive_typed_speak: bool = False`),
[`web/src/components/SettingsDrawer.tsx`](../../web/src/components/SettingsDrawer.tsx)
Proactive section.

**Sketched approach.** A boolean switch in settings that, when on,
routes the typed-proactive reply through the regular TTS path. Keep
the prepared-nudge fast-path text-only either way (those are barely
worth speaking).

**Open questions.** Do we keep the default OFF (current behaviour) or
flip the default ON so the feature is discoverable? Probably OFF
forever — typed-proactive is *meant* to be text-only.

---

## C6. Worker-model decides task-result interrupt-worthiness — SHIPPED

Shipped as the worker-model task-report decision + angle cue. See
[`shipped.md` → Worker-model task-report decision](shipped/proactive-tasks.md#worker-model-task-report-decision--angle-cue-c6).
