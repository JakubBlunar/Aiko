# Aiko personality backlog

Ideas surfaced during the personality brainstorm that we *didn't* ship in
the depth pass that delivered A1 (narrative inner-monologue), A2 (Aiko
reading Jacob's affect), and A3 (RAG recency / revival). Each section is
short on purpose: motivation, key files, sketched approach, and one or
two open questions. Pick any item up later as a standalone plan.

The numbering matches the labels used during the brainstorm so chat
history stays grep-able.

---

## B. Continuous expressiveness (renderer side)

### B1. Body-language driven by the mood vector continuously — DONE

Shipped in the **continuous expressiveness B1 + B2** pass:

- `AmbientBodyChannel.tickPreModel` drives `ParamBreath` with an
  arousal-scaled sine wave (~0.16-0.25 Hz around the default 0.21 Hz)
  and adds a smoothed valence-tilt bias to `ParamBodyAngleY`. Both
  paths gate on capability flags (`has_breath`, `has_body_angle_y`)
  and are scaled by the new `avatar.expressiveness` slider.
- `ExpressionChannel.tickPreModel` performs arousal-scaled writes to
  the parameters declared in each expression file, so e.g. `cheerful`
  reads quieter at low arousal and louder at high arousal. The
  override skips while an overlay owns the expression slot
  (`engineState.exprSlotLockUntil`).
- New "Body language intensity" slider in the Settings drawer
  (range 0.0-1.5, default 1.0) wires `avatar.expressiveness` through
  the WS payload, the Zustand store, and the channel snapshot.
  `0.0` = effective no-op for the new continuous overrides; `1.5` =
  amplified but still capped by the rig's authored on-values.

**Follow-up.** Blink-rate modulation (originally listed in this
section) was deferred to a new entry below — see B3.

---

### B2. Listening micro-nods on backchannel hints — DONE

Shipped alongside B1:

- `_emit_backchannel_motion` in `app/core/session_controller.py` maps
  `agreement` → `Tap/nod`, `disagreement` → `Tap/shake`,
  `thinking` → alternating `Backchannel/tilt_left` /
  `Backchannel/tilt_right`, `confused` → `Backchannel/microshake`.
  `surprise` / `amusement` / `concern` are intentionally skipped
  because they're already covered by the reaction-overlay path.
- A separate `_BackchannelMotionGate` rate-limits at 1.5 s so a chatty
  listening window can't spam the rig.
- New motion files (`tilt_left`, `tilt_right`, `microshake`) live in a
  new `Backchannel` motion group, added by
  [`scripts/generate_alexia_motions.py`](../scripts/generate_alexia_motions.py).
- The WS payload carries `priority: "idle"`, which the frontend's
  `MotionChannel` translates into pixi-live2d-display's
  `MotionPriority.IDLE`. A regular `[[motion:X]]` reaction motion
  fired during the same listening window cleanly pre-empts the
  micro-cue without explicit cancellation logic.

---

### B3. Blink-rate modulation by arousal (deferred follow-up to B1)

**Why deferred.** B1's plan considered tying blink interval to the
arousal axis (faster blinks under high arousal, slower under low),
but pixi-live2d-display does not expose a public
`setBlinkingInterval` setter — only the `beforeModelUpdate` event
hook is documented. Overriding the auto-blink driver from
`tickPreModel` every frame would conflict with the existing wink
gesture and is brittle when the library upgrades. Held until we either
swap blink drivers or upstream a setter.

**Sketched approach (when we revisit).**
- Replace the auto blink driver with a custom one that exposes a
  setter; or fork `EyeBlink.update` and own the parameters via
  `tickPreModel`.
- Map arousal → blink-interval multiplier (e.g. 0.7-1.4 around the
  rig's authored mean) plus a small jitter so the cadence doesn't
  read as metronomic.
- Reuse the `avatar.expressiveness` slider so the user can dampen the
  blink modulation along with the rest of the body-language overlays.

**Open questions.**
- Is the cleanest path forking the `EyeBlink` controller or upstreaming
  a setter PR? The fork is faster, but means we own that surface
  forever.

---

## C. Proactive outside Live voice

### C1. Proactive ping in text-mode chat

**Motivation.** `ProactiveDirector` only fires from `live_session.py`
when the user is in voice mode and silence exceeds a threshold. In typed
chat Aiko is purely reactive — even if she has a fresh callback she
wants to bring up, she waits forever for Jacob to type first.

**Key files.**
- [`app/core/proactive_director.py`](../app/core/proactive_director.py) —
  director already speaks via `PreparedNudge.consume`.
- [`app/core/live_session.py`](../app/core/live_session.py) — current
  silence-trigger call site.
- [`app/core/session_controller.py`](../app/core/session_controller.py) —
  would need a typed-mode silence timer.

**Sketched approach.**
- Add an optional silence timer to `SessionController` that arms when a
  typed turn ends and disarms on the next user input.
- Threshold: `agent.proactive_silence_seconds_typed` (separate config
  knob, default ~3-5 minutes — much longer than voice's 45 s so it
  doesn't feel spammy).
- On fire: call `_consume_prepared_nudge` and dispatch an assistant
  message via the same WS path as a normal turn (so the UI shows it as
  "Aiko ·"). Treat it like a barge-in turn — don't run RAG or
  expensive workers, just speak.
- Cooldown: `agent.proactive_cooldown_seconds_typed` so we never fire
  twice in a row even if the user keeps ignoring her.

**Open questions.**
- Persist last-fired timestamp to disk (so a browser refresh doesn't
  reset cooldown)? `config/user.json` like `last_active_id` is the
  obvious place.
- Distinct browser-tab-visibility behaviour? `document.visibilityState`
  on the frontend could mute the ping when the tab is hidden.

---

## D. New tools / capabilities

### D1. Calendar / reminders tool

**Motivation.** `promise` memories already capture "I'll do X" but they
have no time component. A real reminders tool would let Aiko answer
"remind me about the dentist on Tuesday" and surface it at the right
moment via the existing proactive director.

**Key files (new + existing).**
- New: `app/core/reminders_store.py` (SQLite-backed, simple `id, text,
  due_at, fired_at, source_message_id` table).
- New: `app/llm/tools/reminders.py` — `set_reminder(text, when)` and
  `list_reminders()` agent tools.
- Existing: [`app/llm/tools/builtins.py`](../app/llm/tools/builtins.py)
  `build_default_registry` — register the new tools, gated on a
  config flag.
- Existing: [`app/core/proactive_director.py`](../app/core/proactive_director.py)
  — extend `_pick_topic` to surface a due-but-unfired reminder ahead of
  generic nudges.

**Sketched approach.**
- Tool: parse `when` as ISO-8601 OR a small natural-language helper
  (`dateparser` or a tiny regex set: "tomorrow at 3pm", "in 2 hours").
  Don't reach for a full NLP stack — keep it boring.
- A periodic check (~60 s) in `SessionController` polls the store for
  reminders whose `due_at <= now` and `fired_at IS NULL`, picks the
  earliest, marks fired, and triggers a proactive turn (reuses C1 if
  shipped).
- Visible in the web UI via a small "reminders" panel reading the same
  table over an `/api/reminders` endpoint.

**Open questions.**
- Recurring reminders (every Tuesday)? Out of scope for v1; one-shot is
  the 80% case.
- Notifications when the browser tab is closed? Web Push is heavy; a
  dock badge / system notification via Tauri/Electron would be cleaner.

---

### D2. Image vision tool

**Motivation.** Ollama supports vision models (`llava`, `qwen2.5-vl`,
etc.). Letting Jacob drop an image into the chat and have Aiko comment
on it ("oh, that's a cute desk setup — what's that on your monitor?")
is a huge presence multiplier and pairs naturally with her curiosity.

**Key files.**
- [`app/llm/ollama_client.py`](../app/llm/ollama_client.py) —
  `chat_with_tools` would need to accept image attachments. Ollama's
  `/api/chat` already supports `images: [base64]` in the message body.
- New: `app/llm/tools/vision.py` — `describe_image(path)` tool that
  routes to a vision model.
- Existing: web upload path already handles images for documents; would
  need a new branch that doesn't chunk them.

**Sketched approach.**
- Frontend: drag-drop image into the chat composer → POST to
  `/api/chat/image` → backend stores it briefly and includes a tool-call
  hint in the next turn ("Jacob just shared an image — call
  `describe_image` to see it").
- Vision tool runs the configured vision model, returns the description
  as the tool result; Aiko's spoken reply uses it naturally.
- Image is NOT persisted to memory by default (privacy). Aiko could tag
  `[[remember:Jacob shared a desk photo]]` if it's notable.

**Open questions.**
- Vision model size — default to a quantised 3-7 B model so it runs on
  the same box as the chat model? Or always cloud-route image calls?
- Fallback when no vision model is available: gracefully skip the tool
  and let Aiko say "I can't actually see that yet, sorry".

---

## E. Memory architecture

### E1. Scratchpad / archive memory tiers

**Motivation.** Today there are effectively two tiers — the rolling
per-session summary (`SummaryWorker` writes `session_summaries`) and
the long-term `MemoryStore` (capped at `memory.max_memories`,
distilled by `MemoryExtractor` / `ReflectionWorker` /
`PromiseExtractor` / `[[remember:...]]` tags). Cap-bump (500 → 5000)
plus the new `pinned` semantic from the memory-editor pass cover
"don't forget what matters" cheaply, but everything in the long-term
store is still a flat pool: a half-baked observation from yesterday
sits next to a year-old anchor relationship fact, and `decay()` /
`prune()` treat them with the same coarse `salience + use_count`
heuristic. A real two-tier split would let recent unverified
observations live in a faster-decay "scratchpad" that auto-promotes
or auto-forgets, while long-stable memories drift into a low-touch
"archive" that retrieval still hits but writes never disturb.

**Key files.**
- [`app/core/memory_store.py`](../app/core/memory_store.py) — would
  grow per-tier `decay_rate` / `prune_score` knobs and a `tier` column
  alongside the new `pinned` from the memory-editor pass.
- [`app/core/chat_database.py`](../app/core/chat_database.py) —
  schema bump to add `tier TEXT NOT NULL DEFAULT 'long_term'`.
- New: `app/core/memory_promotion_worker.py` — a daily worker that
  walks scratchpad rows older than `scratchpad_ttl_days`, promotes
  rows that retrieved at least N times, deletes the rest. Mirrors the
  `MemoryConsolidator` cadence.
- [`app/core/rag_retriever.py`](../app/core/rag_retriever.py) —
  per-tier scoring offsets so scratchpad hits are cheaper to surface
  on recency but archive hits aren't penalised on dormancy.

**Sketched approach.**
- Three tiers: `scratchpad` (fast decay, days-to-weeks lifetime),
  `long_term` (today's default), `archive` (decay rate ≈ 0, retrieval
  bonus dampened so it doesn't crowd recent context).
- New writes from `MemoryExtractor` land in `scratchpad`. `[[remember:
  self:...]]` self-tags and `PromiseExtractor` go straight to
  `long_term` (the user already explicitly anchored those).
- Promotion: `scratchpad` → `long_term` after surviving ~7 days AND
  retrieved ≥ 3 times, OR pinned (pin always promotes). Demotion:
  `long_term` → `archive` after ~180 days untouched.
- `MemoryStore.prune()` enforces a per-tier cap so a noisy week of
  scratchpad churn can't push out long-term memories.
- The Memory tab gains a tier column + per-tier filter (mirrors
  the kind filter pattern from the editor work).

**Open questions.**
- Should the tier be auto-derived from kind + age, or stored
  explicitly? Explicit is cleaner for the UI ("I want to *force*
  this into archive") but adds writes; derivation keeps the table
  thinner.
- Does archive need its own LanceDB table (cheaper indexing) or is a
  filtered query on the existing one enough?
- Interaction with the existing `MemoryConsolidator` — does it
  consolidate within a tier only, or across? Probably within, otherwise
  a scratchpad merge could corrupt a long-term row.

---

## Other ideas considered

- **Second TTS provider behind `TtsEngine`.** Pocket-TTS is the only
  implemented backend. Adding e.g. Piper, Coqui, or an OpenAI-compatible
  cloud voice would let users pick a different timbre / language without
  swapping the whole pipeline. The `TtsEngine` protocol in
  [`app/tts/base.py`](../app/tts/base.py) is the extension point.
- **SSML prosody for emotional speech.** Pocket-TTS supports speed +
  pitch but not full SSML; the prosody dispatcher in
  [`app/core/cadence.py`](../app/core/cadence.py) does what it can with
  per-sentence reaction overrides. A real SSML pass — emphasis on key
  words, micro-pauses tied to commas, pitch contour for excitement —
  would be a much bigger expression bump than a new voice file.
- **Barge-in enabled by default for Live mode.** Currently
  `audio.barge_in_enabled: false` in [`config/default.json`](../config/default.json).
  The plumbing is there in [`app/core/live_session.py`](../app/core/live_session.py);
  flip the flag and validate against the existing
  `barge_in_min_speech_seconds` floor.
- **User-facing memory editor in the web UI.** DONE — shipped as a
  dedicated "Memory" tab in `SettingsDrawer.tsx`. Adds full
  edit-in-place / manual create / pin toggle / kind filter / sort /
  paginated list (page size 50). Pinned rows are skipped by `decay()`
  and never selected as `prune()` victims; `RagRetriever` adds a
  `+0.05` score bonus for pinned hits via a SQLite-mirror lookup
  (LanceDB stays untouched to avoid a destructive vector-store
  rebuild). Cap bumped from 500 to 5000. Pagination + filter live on
  the server side (`GET /api/memories` grew `offset` / `kind` query
  params and a `total` / `cap` response). New WS event:
  `memory_updated`. New endpoints: `PATCH /api/memories/{id}`,
  `POST /api/memories`, `POST /api/memories/{id}/pin`. Schema v5
  migration in `chat_database.py` adds the `pinned` column.
- **"Shared moments" episodic memory kind.** Today
  `event` / `callback` / `reflection` are loosely episodic but flat.
  A new `shared_moment` kind with structured `(when, what, vibe)`
  metadata, surfaced as "remember when …" anniversaries, would deepen
  the relationship arc.
- **A4 (split off from the depth pass).** Salience auto-decay tied to
  recall cadence: memories the user genuinely cares about (high
  cosine-similar follow-ups, `[[remember:self:...]]` self-tags) gain
  salience over time; memories that retrieve but never lead anywhere
  decay faster. Plumbing extension to
  [`app/core/memory_store.py::decay`](../app/core/memory_store.py).

---

## How to pick one up

1. Re-read the relevant section.
2. Spin up a plan. Each item is small enough to fit in a single
   `CreatePlan` invocation; nothing here needs to be split into phases.
3. Validate the same way we did for A1/A2/A3:
   focused suite -> full `pytest -q` -> spot-check the running app.
4. Update the relevant doc (this file, `AGENTS.md`,
   `docs/alexia-model-notes.md`) when the work lands.
