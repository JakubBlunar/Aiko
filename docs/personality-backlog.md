# Aiko personality backlog

Ideas surfaced during the personality brainstorms that we *didn't* ship
in the depth passes (A1 narrative inner-monologue, A2 reading Jacob's
affect, A3 RAG recency / revival, the memory-editor pass, the world /
room pass, the typed-proactive pass, the shared-moments + relationship-
axes pass). Each open entry is short on purpose: motivation, key files,
sketched approach, and one or two open questions. Pick any item up
later as a standalone plan.

The numbering matches the labels used during the brainstorms so chat
history stays grep-able. New ideas inherit the same shape (sections
F / G / H added during the May 2026 cleanup). Items that have already
shipped live in the [Shipped (kept for reference)](#shipped-kept-for-reference)
appendix at the bottom, one paragraph each, with links to the detail
doc that owns them.

---

## B. Avatar + expressiveness

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

### B4. Map remaining Alexia expressions — **visual audit resolved**

**Status.** The visual identity audit landed (see
[`docs/Alexia-my-observation.md`](Alexia-my-observation.md) for the
user's per-expression observations and
[`docs/alexia-model-notes.md`](alexia-model-notes.md) §3 / §3a / §3b /
§3c for the codified reading). Every expression in the rig has a
confirmed visual identity now; the only remaining work is **using**
the new vocabulary in the persona + LLM grammar (the bullet under
"Follow-ups" below).

**Final mapping after the audit.**

- **Mapped to canonical reactions.** `lzx` (`cheerful` / `amused`,
  with the lip-sync taper from §3b), `k` (`sad` / `melancholy` /
  `concerned`, and the fallback for `cry`), `sq` (`angry` /
  `frustrated`), `wh` (`surprised` / `curious`), `xxy` (`excited` /
  `enthusiastic`), `lh` (`warm` / `tender` / `gentle`), `y` (the new
  `confused` reaction — NOT `tired`, which now routes to body-slump
  via `AmbientBodyChannel`), `zs1` (`playful` in day clothes; falls
  through to `amused` → `lzx` in pajamas via the outfit gate, §3c).
- **Accessory-tier (no reaction maps to them, available via
  `[[overlay:X]]` and Phase 4's persistent toggles).** `bbt` →
  `has_lollipop` (NOT a cry overlay — see §3a regression history),
  `dyj` → `has_eyeglasses`, `mj` → `has_head_sunglasses` (perched on
  the hair, hence the rename from the earlier `has_sunglasses`),
  `yjys1` / `yjys2` → `has_eye_color_a` / `has_eye_color_b`.
- **Outfit envelopes (driven by `OutfitChannel`, not reactions).**
  `yf` / `yfmz` + the synthetic `day_clothes` baseline.
- **Sweat / question / nervous overlays.** `h` (sweat) and `wh`
  (question) are emotional overlays sitting on the standard
  `has_sweat` / `has_question` capabilities; both are usable via
  `[[overlay:sweat]]` / `[[overlay:question]]` today.

**Follow-ups** (these become Phase 5 of the expression-overhaul
plan):

- Mint new canonical reactions to fill the remaining emotional
  textures the rig can now reach: `embarrassed` → `lh` (blush),
  `nervous` → `h` (sweat), `defiant` → `zs1` (outfit-gated). All
  three flow through the same `REACTIONS` / `_REACTION_NEIGHBOURS`
  pipeline that `confused` uses today so non-Alexia rigs still get
  *something*.
- Teach the persona the stacked-overlay idiom (`[[overlay:A+B]]`)
  once Phase 3's compositor lands, so the LLM can express two
  textures at once (`blush+grin`, `sweat+question`,
  `stars+blush`).

**Key files.**
- [`app/core/avatar_profile.py`](../app/core/avatar_profile.py) —
  `_ALEXIA_REACTION_MAP`, `_ALEXIA_EXPR_TO_CAPABILITY`,
  `_CAPABILITY_SYNONYMS`, `_detect_mouth_blocking_expressions`,
  `_detect_outfit_gated_expressions`.
- [`docs/alexia-model-notes.md`](alexia-model-notes.md) §3 — the
  authoritative per-expression audit.
- [`web/src/components/ChatView.tsx`](../web/src/components/ChatView.tsx)
  `REACTION_EMOJI` — already has `confused`; add `embarrassed` /
  `nervous` / `defiant` in Phase 5.
- [`data/persona/aiko_companion.txt`](../data/persona/aiko_companion.txt)
  reaction vocabulary line — already mentions `confused`; extend in
  Phase 5.

---

### B5. Auto-cascade safety — voice mode / backchannel must not pick "heavy" expressions

**Status.** **Shipped.** Discovered live: a perfectly cheerful turn
visibly rendered Alexia crying for the 2-4 s thinking window while
she resolved tool calls (`recall` then `change_posture`). Root cause
was the auto-cascade chain inside
[`ExpressionChannel.ts`](../web/src/live2d/channels/ExpressionChannel.ts):

- `_MODE_TO_REACTION.thinking = ["thoughtful", "concerned", ...]` —
  Alexia's `thoughtful` is `""` (no direct expression, lean on
  body-language), so the cascade fell through to `concerned`, which
  maps to **`k` (Param59 = tear streaks)**.
- `_BACKCHANNEL_TO_REACTION.concern = ["concerned", "sad", "gentle"]`
  — same problem: any user message with `tired` / `stressed` /
  `frustrated` / etc. fired a 1.8 s tear pulse on top of whatever the
  current reaction was.
- `_BACKCHANNEL_TO_REACTION.disagreement = ["serious", "concerned",
  "thoughtful"]` — same problem on the disagreement branch.

**Fix.** Auto-cascades now route to soft / neutral alternatives only;
the explicit `[[reaction:concerned]]` from the LLM still resolves to
the rig's mapping (intentional empathy beat). Three regression tests
land in
[`ExpressionChannel.test.ts`](../web/src/live2d/channels/ExpressionChannel.test.ts)
under "ExpressionChannel — auto-cascade avoids heavy expressions":
they build an Alexia-like manifest where `thoughtful` is empty and
`concerned`/`sad` route to `ExprCry`, then assert that
`thinking` mode, `concern` backchannel, and `disagreement`
backchannel never pick `ExprCry`.

**Design rule going forward.** When adding entries to
`_MODE_TO_REACTION` or `_BACKCHANNEL_TO_REACTION`, every candidate
must read as a *micro-expression* on any rig. Reactions that imply
strong narrative emotion on at least one supported rig
(`concerned`, `sad`, `melancholy`, `cry`, `angry`, `frustrated`,
`defiant`) belong only in the *explicit* `[[reaction:X]]` path, never
the auto-cascade fallback.

**Follow-up — neighbour-chain crybug entrypoint.** The first fix
closed the auto-cascade paths (voice mode / backchannel). A second
trace surfaced a *second* path to the same `k` (cry) expression
through the explicit `[[reaction:X]]` neighbour fallback in
[`reactions.py`](../app/core/reactions.py) /
[`ExpressionChannel.ts`](../web/src/live2d/channels/ExpressionChannel.ts)
`_REACTION_NEIGHBOURS`: non-sad reactions (`thoughtful`, `serious`,
`frustrated`, `angry`) chained *through* `concerned` as a fallback.
A single `[[reaction:thoughtful]]` from the LLM (or the
filler-injector's default "thoughtful" carry-over on a fresh-boot
turn with no prior reaction) would silently land on `concerned` →
`k` and paint tears with no narrative justification. Fix: dropped
`concerned` (and any other sad-family entry) from non-sad chains;
the sad family (`sad` / `melancholy` / `wistful` / `concerned` /
`tired` / `cry`) still chains within itself so legitimate
`[[reaction:sad]]` emits still paint the right tears. Both mirrors
updated; backend lock-in lives in
[`tests/test_reactions.py`](../tests/test_reactions.py)
`CryCascadeGuardTests`, frontend lock-in in the existing
`auto-cascade avoids heavy expressions` block.

---

## C. Proactive + presence follow-ups

These are the deferred follow-ups from the typed-proactive / activity
awareness pass (C1, see [Shipped appendix](#shipped-kept-for-reference)).

### C2. Window-title-aware activity

**Motivation.** App name only ships in v1 of activity awareness; window
titles would let Aiko reference doc / file names she sees in Jacob's
foreground app, but leaks bank URLs and private chat targets if naively
forwarded. Worth picking up once we have a privacy story strong enough
to support it.

**Key files.** [`web/src-tauri/src/lib.rs`](../web/src-tauri/src/lib.rs)
`get_active_app`, [`app/core/session_controller.py`](../app/core/session_controller.py)
`set_user_active_app` + `_render_activity_block`,
[`web/src/hooks/useActivityReporter.ts`](../web/src/hooks/useActivityReporter.ts).

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

### C3. Persisting last-fired typed cooldown to disk

**Motivation.** Today the typed-proactive cooldown lives in process
memory (`_last_typed_run_monotonic`) and resets on backend restart.
Fine for the 80% case but a quick restart in the middle of a typed
session can re-arm an immediate proactive nudge, which reads weirdly.

**Key files.** [`config/user.json`](../config/user.json) (alongside
`last_active_id`), [`app/core/proactive_director.py`](../app/core/proactive_director.py)
`_last_typed_run_monotonic` plus a `_last_typed_run_iso` mirror,
[`app/core/session_controller.py`](../app/core/session_controller.py)
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

### C4. TTS-on-typed-proactive toggle

**Motivation.** Typed proactive nudges are text-only by design today.
A "speak typed proactive nudges aloud" knob is cheap to add when the
use case appears (e.g. Jacob wants ambient audio presence even while
typing).

**Key files.** [`app/core/proactive_director.py`](../app/core/proactive_director.py)
`_run_typed` (currently bypasses the TTS pipeline),
[`app/core/settings.py`](../app/core/settings.py) `AgentSettings`
(new `proactive_typed_speak: bool = False`),
[`web/src/components/SettingsDrawer.tsx`](../web/src/components/SettingsDrawer.tsx)
Proactive section.

**Sketched approach.** A boolean switch in settings that, when on,
routes the typed-proactive reply through the regular TTS path. Keep
the prepared-nudge fast-path text-only either way (those are barely
worth speaking).

**Open questions.** Do we keep the default OFF (current behaviour) or
flip the default ON so the feature is discoverable? Probably OFF
forever — typed-proactive is *meant* to be text-only.

---

### C5. Per-tab presence aggregation across multiple windows

**Motivation.** Today presence is last-write-wins on the WS
connection. Multi-tab presence (the same Jacob with the same backend
in two browser windows) would benefit from a server-side OR-fold
across all active sockets so a focused tab keeps the proactive
director quiet even when an unfocused tab last sent a presence-false.

**Key files.** [`app/web/server.py`](../app/web/server.py) WS hub /
`presence` command, [`app/core/session_controller.py`](../app/core/session_controller.py)
`set_user_present`.

**Sketched approach.** Track presence per WS connection id in the hub;
the boolean `set_user_present` receives is `any(client_presence
for client_presence in active_clients)`. Clean up on disconnect.

**Open questions.** Does multi-tab even matter for a single-user
setup? Probably no; deferred until we hear "I have it open in two
tabs and the proactive nudges are confused" from a real user.

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
  earliest, marks fired, and triggers a proactive turn (reuses C1).
- Visible in the web UI via a small "reminders" panel reading the same
  table over an `/api/reminders` endpoint.

**Open questions.**
- Recurring reminders (every Tuesday)? Out of scope for v1; one-shot is
  the 80% case.
- Notifications when the browser tab is closed? Web Push is heavy; a
  dock badge / system notification via Tauri is cleaner.

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

E1 (scratchpad / archive tiers) and E2 (salience auto-decay tied to
recall cadence) shipped together as schema v8 in May 2026. See the
[`Shipped`](#shipped-kept-for-reference) appendix and
[`docs/memory-tiers.md`](memory-tiers.md) for the implementation
details.

---

## F. Awareness + grounding

New section (May 2026). The goal is to reduce confident hallucination
by making Aiko's uncertainty visible to herself — both as structured
state she can act on and as background work that closes gaps over
time. F1 / F2 / F3 stand together: F2 captures uncertainty, F3 gives
each memory a confidence number, F1 closes the loop by checking
claims in the background.

### F1. Background fact-checker worker

**Motivation.** Aiko sometimes states facts with full confidence that
she's actually guessing (the classic LLM hallucination shape). A
background worker that fact-checks recently surfaced claims during
idle (uses `web_search` already in [`app/llm/tools/`](../app/llm/tools))
and updates the relevant memory's `confidence` and content would
close that gap without touching the main pipeline. Gated on presence
+ cooldown + system idle so the fast path stays fast.

**Key files.**
- New: `app/core/idle_fact_checker.py` — worker class registered with
  the G1 scheduler.
- [`app/core/session_controller.py`](../app/core/session_controller.py)
  — turn-end hook that scans newly-written memories for "factual
  claim" markers and pushes them onto the queue.
- [`app/core/memory_store.py`](../app/core/memory_store.py) — gains a
  `confidence` column (overlaps with F3 — pick up F3 first so the
  column exists to update).
- [`app/llm/tools/builtins.py`](../app/llm/tools/builtins.py) — the
  worker reuses `web_search` directly (calling the helper, not as a
  tool call) so we don't pollute the agent's tool-call history.

**Sketched approach.**
- A queue keyed by `(memory_id, claim_text)` lives in the worker's
  process state.
- Turn-end scans newly-written memories for "factual claim" markers
  (numbers, dates, named entities — a cheap regex pass, not an LLM
  call) and pushes them onto the queue.
- The worker wakes via the G1 scheduler. It only runs when: no LLM
  call in flight, `presence=true` but no user input in the last 90 s,
  queue non-empty.
- Each tick: pop the head of the queue, run `web_search`, then a small
  distil prompt — "does the search result support / contradict / not
  address this claim?". Update `memory.confidence` accordingly and
  optionally rewrite `memory.content` if the search clearly corrects
  a number / date.
- Worker is invisible to the chat; a small footer in the Memory tab
  shows "last checked X min ago" so the user sees it's working.

**Open questions.**
- Do we surface fact-check results to the user (a small "verified" /
  "uncertain" pip in the Memory tab) or only adjust internal
  confidence? Probably both — the user can audit individual
  rewrites.
- What's the rate-limit envelope on `web_search` so a busy queue
  doesn't burn API quota? Per-day cap + per-hour cap, exposed as
  settings.

---

### F2. Knowledge-gap journal

**Motivation.** When Aiko says "I don't know" / "I'm not sure" she
usually moves on. Capturing those gaps as structured entries lets the
background worker (F1) close them later, and lets the prompt
resurface them when the topic returns. Mirrors how `promise` already
works but for self-tagged uncertainty.

**Key files.**
- New: `app/core/knowledge_gap_extractor.py` — regex over assistant
  text for "I don't know <what>" / explicit `[[gap:topic:question]]`
  tag; mirrors the `promise_extractor.py` shape.
- New: `app/core/knowledge_gaps_store.py` — thin wrapper over
  `MemoryStore` using a new `"knowledge_gap"` kind via the existing
  `VALID_KINDS` extension pattern.
- [`app/core/session_controller.py`](../app/core/session_controller.py)
  `_post_turn_inner_life` — extraction hook (right next to the
  shared-moment tag extraction).
- [`app/core/prompt_assembler.py`](../app/core/prompt_assembler.py)
  — a small "Things you were unsure about with Jacob:" inner-life
  block, only when there are unresolved gaps + only when the current
  topic semantically matches.
- Persona update ([`data/persona/aiko_companion.txt`](../data/persona/aiko_companion.txt))
  to teach the inline `[[gap:]]` tag.

**Sketched approach.**
- Persona update tells Aiko she may emit `[[gap:topic:short question]]`
  inline when something she'd genuinely like to know surfaces.
  Stripped from user-visible text (mirrors `[[remember:]]`).
- Gaps get a `resolved_at` field that F1 stamps when its check
  returns a usable answer. Resolved gaps become regular memories
  with high confidence; the gap row is kept for audit.
- The inner-life block surfaces at most one gap per turn, picked by
  cosine similarity to the current user turn so only relevant gaps
  re-enter the conversation.

**Open questions.**
- Should we cap the queue size (e.g. 20 pending gaps) so a single
  chatty session can't blow it up?
- Do gaps decay if never revisited (months-long stale)? Probably yes
  — auto-expire after 90 days unless picked up by F1 or the user
  pins.

---

### F3. Confidence tier on memories

**Motivation.** Today every memory has the same `salience` lever;
there's no distinction between "Jacob told me this himself" and "I
think I read this somewhere". Adding an explicit `confidence` column
lets RAG demote uncertain memories during retrieval, lets the prompt
nudge "(you only think this — verify before stating)", and gives F1
something to update. This is the foundational change that F1 and F5
build on.

**Key files.**
- [`app/core/chat_database.py`](../app/core/chat_database.py) —
  schema bump (v7 → v8) to add `confidence REAL NOT NULL DEFAULT 0.7`
  to `memories`.
- [`app/core/memory_store.py`](../app/core/memory_store.py) — add
  `confidence` to `Memory`, plumb through `add()` / `update()` /
  `_reload_mirror`.
- [`app/core/rag_retriever.py`](../app/core/rag_retriever.py) —
  small score multiplier: confidence below `0.5` gets a `-0.05`
  to `-0.15` penalty proportional to the gap.
- [`app/core/prompt_assembler.py`](../app/core/prompt_assembler.py)
  — low-confidence retrieved memories get a `(uncertain)` suffix in
  the memory block so the LLM hedges.
- [`web/src/components/SettingsDrawer.tsx`](../web/src/components/SettingsDrawer.tsx)
  Memory tab gains a confidence column + filter.

**Sketched approach.**
- Defaults — `MemoryExtractor` writes at `0.7`, `[[remember:self:…]]`
  self-tags at `0.85`, `[[remember:…]]` factual user-confirmed tags
  at `0.9`, tool-result memories (RAG / web) at `0.95`, manual
  memory-tab creates at `1.0`.
- F1's worker pushes confidence up toward `0.95` on positive
  verification and down to `0.4` on contradiction (with a
  `[[conflict]]` flag that the F5 sub-tab can surface).
- Migration: existing rows default to `0.7`. Pinned rows clamp to
  `>= 0.9` since the user explicitly anchored them.

**Open questions.**
- Visualise confidence as a numeric column, a coloured pip, or both?
  Probably both — numeric for filtering, pip for at-a-glance.
- Should low-confidence memories be excluded entirely from `recall`
  tool output below some threshold, or always returned with the
  `uncertain` tag? Probably always returned but tagged — never hiding
  things from Aiko is the simpler invariant.

---

### F4. Source-cited memories

When a memory originates from a tool call (`web_search` / `recall` /
document upload), persist the source URL or document id in
`metadata.source_url` (reuses the v7 generic metadata column). Aiko
cites naturally ("according to a thing I read last week..."). The
Memory tab grows a "from web" badge that links out. Key files:
[`app/core/memory_store.py`](../app/core/memory_store.py),
[`app/llm/tools/web_search.py`](../app/llm/tools/web_search.py),
Memory tab in [`web/src/components/SettingsDrawer.tsx`](../web/src/components/SettingsDrawer.tsx).
Pairs naturally with F1, which would stamp its own `source_url` on
fact-check rewrites.

---

### F5. Conflicting-memory detector

Periodic background worker (G1) that scans pairs of memories with
high cosine similarity but lexically contradicting content (`hates X`
vs `loves X`). Surfaces in a "Conflicts" sub-tab of the Memory tab
for the user to resolve, with a one-click "keep this, drop the other"
action. Persona allows `[[conflict:reason]]` self-tag for Aiko to
flag a contradiction she notices in flight. Key files: new
`app/core/memory_conflict_worker.py`, [`app/core/memory_store.py`](../app/core/memory_store.py),
[`web/src/components/SettingsDrawer.tsx`](../web/src/components/SettingsDrawer.tsx)
Memory tab.

---

## G. Background workers

New section (May 2026). The point is one shared scheduler that knows
when the system is idle, so each background job (fact-checker,
curiosity worker, schedule learner) doesn't have to re-implement the
"is it safe to run right now" check.

G1 (IdleWorker framework) shipped as part of the schema v8 memory-tier
work in May 2026. See the
[`Shipped`](#shipped-kept-for-reference) appendix and
[`docs/memory-tiers.md`](memory-tiers.md). New workers should register
with the existing `IdleWorkerScheduler` (`app/core/idle_worker_scheduler.py`)
rather than spinning up their own threads.

---

### G2. Schedule-learning worker

Background scan of `messages.created_at` patterns. Learns "Jacob is
mostly here in evenings (8-11 pm)" / "weekends only" / "mornings on
weekdays", stores as a user-state fact via `UserProfile`. Aiko's
openers become context-aware ("haven't seen you in a few mornings,
big day?") without ever making the user feel surveilled — only the
day-of-week / hour-of-day buckets are stored, not the message content.
Key files: new `app/core/schedule_learner.py` (registered with G1
scheduler), [`app/core/user_profile.py`](../app/core/user_profile.py)
(new `usual_hours` field).

---

### G3. Curiosity worker

Aiko's own `open_question` memories are picked one at a time during
idle and `web_search`'d. The result is stored as a high-confidence
memory tagged with the open-question id, surfaceable in a later turn
as "I was reading about X — turns out…". Distinct from F1 (which
checks her *claims*); this is proactive curiosity that builds her
knowledge during downtime. Rate-limited tight enough that a
multi-week absence doesn't produce a wall of "by the way" openers
when Jacob comes back. Key files: new `app/core/curiosity_worker.py`
(registered with G1), [`app/core/memory_store.py`](../app/core/memory_store.py),
[`app/llm/tools/web_search.py`](../app/llm/tools/web_search.py).

---

## H. Immersion polish

New section (May 2026). Small additions that compound.

### H1. Conversation-arc surfacing via tag

[`app/core/conversation_arc.py`](../app/core/conversation_arc.py)
already infers arcs internally; expose them as `[[arc:vulnerable]]` /
`[[arc:silly]]` / `[[arc:focused]]` self-tags Aiko can emit, stored on
the relevant `messages` row. Useful for the Together-tab timeline
(filter by arc), retrieval scoring (arc-matched memories score
slightly higher when the current arc matches), and post-hoc analysis
of what kind of conversations were most common. Key files: persona
file, [`app/core/conversation_arc.py`](../app/core/conversation_arc.py),
[`app/core/rag_retriever.py`](../app/core/rag_retriever.py).

---

### H2. Calendar / time context block

A small inner-life provider that summarises "what's true right now"
— time of day (morning / afternoon / evening / late), day of week,
season, holiday proximity (Christmas in 4 days, Jacob's birthday
next week). Lets Aiko say "Sunday morning vibes" naturally without
calling `get_time` every turn. Pairs nicely with G2 — once she knows
Jacob's usual hours, she can comment when he's online unusually
early or late. Key files: new helper in
[`app/core/session_controller.py`](../app/core/session_controller.py)
`_render_time_context_block`, wired into
[`app/core/prompt_assembler.py`](../app/core/prompt_assembler.py)
right after `world_block` and dropped in `aggressive` mode.

---

### H3. Mood drift narrator

Read-only periodic check on `affect_state` history and
`relationship_axes`. If Jacob's mood has been low for 3+ sessions or
Aiko's axes have drifted notably in a single direction (e.g.
`closeness` has been climbing for two weeks), surface a small
reflective note for Aiko to acknowledge gently — never mechanically
("you seem to be in a better place lately, I've noticed"). Key
files: [`app/core/affect_state.py`](../app/core/affect_state.py),
[`app/core/relationship_axes.py`](../app/core/relationship_axes.py),
[`app/core/session_controller.py`](../app/core/session_controller.py)
inner-life providers.

---

### H4. Document-recall recency boost

Documents Jacob uploaded in the last 7 days get a `+0.05` retrieval
score in [`app/core/rag_retriever.py`](../app/core/rag_retriever.py)
so newly-added knowledge surfaces preferentially without crowding
out long-term anchors. Cheap to ship; gives uploaded docs a chance
to feel "current" before fading into the long-term pool.

---

## J. Shared-moments follow-ups

Promoted from the shared-moments + relationship-axes shipped entry
(see [Shipped appendix](#shipped-kept-for-reference)).

### J1. Multi-user moments / participant attribution

Today every moment is keyed implicitly to Jacob. A future extension
would attribute moments to multiple participants (`participants:
[user_id, ...]` already exists in the metadata shape but is never
read) so a multi-user setup (Jacob + a partner, or a family
deployment) can have separate timelines. Key files:
[`app/core/shared_moments.py`](../app/core/shared_moments.py),
[`app/web/server.py`](../app/web/server.py) `/api/together` filter,
Together tab UI.

---

### J2. Exportable timeline

Markdown or PDF export of the moments timeline so Jacob has a
keepsake of the relationship arc he can read outside the app. Key
files: new `app/core/shared_moments_export.py`,
[`app/web/server.py`](../app/web/server.py) (new
`GET /api/together/export?format=md|pdf`), Together tab UI (export
button).

---

### J3. Axes-aware proactive nudges

The relationship axes are read-only into the prompt today. A clean
follow-up is letting `ProactiveDirector` consume them — e.g.
`comfort < -0.3` → bias the next nudge toward checking in on Jacob
rather than picking up a thread. Don't let the axes *trigger* a nudge
on their own (would feel like surveillance); just colour the topic
selection when a nudge fires for other reasons. Key files:
[`app/core/proactive_director.py`](../app/core/proactive_director.py)
`_pick_topic`, [`app/core/relationship_axes.py`](../app/core/relationship_axes.py).

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
- **Multi-room / outdoor world (follow-up to Aiko's room).** Today
  the world is exactly one room. A natural extension is a second
  scene (a balcony, a coffee shop, a library) with travel
  semantics: Aiko picks the scene appropriate to the conversation
  ("let's go grab tea") and the prompt block flips. Would need a
  `scene_id` column on `world_state`, a Tool to switch scenes, and
  some thinking about whether items move with her or stay in their
  scene. Out of scope for v1 because a single cozy room already
  covers the cookie use case.

---

## Shipped (kept for reference)

These are summary entries for completed work — the detail lives in
the linked doc, not here.

- **B1 / B2 — Continuous expressiveness + listening micro-nods.**
  `AmbientBodyChannel` drives `ParamBreath` from arousal and
  `ParamBodyAngleY` from valence; `ExpressionChannel.tickPreModel`
  does arousal-scaled overrides on the parameters declared in each
  expression file. A new `avatar.expressiveness` slider (0.0-1.5)
  scales the lot. Backchannel micro-nods (`_emit_backchannel_motion`)
  map `agreement` / `disagreement` / `thinking` / `confused` onto
  rate-limited idle-priority motions. See
  [`docs/alexia-model-notes.md`](alexia-model-notes.md) §3 and
  [`AGENTS.md`](../AGENTS.md).

- **C1 — Typed-mode proactive ping + activity awareness.** Typed-
  mode `ProactiveDirector` path with a prepared-nudge fast path and
  an LLM "pick up the thread" fallback, gated by a presence boolean
  (browser tab visibility AND Tauri window focus, AND-folded client-
  side). Defaults: 4 min silence, 10 min cooldown, text-only.
  Desktop-only opt-in activity awareness forwards the foreground app
  *name* (never titles or URLs) so Aiko can reference what Jacob is
  doing. Off by default. See
  [`docs/presence-and-activity.md`](presence-and-activity.md).

- **User-facing memory editor.** Dedicated "Memory" tab in
  `SettingsDrawer.tsx`. Full edit-in-place / manual create / pin
  toggle / kind filter / sort / paginated list (page size 50).
  Pinned rows are skipped by `decay()` and never selected as
  `prune()` victims; `RagRetriever` adds a `+0.05` score bonus for
  pinned hits via a SQLite-mirror lookup (LanceDB stays untouched).
  Cap bumped from 500 to 5000. Pagination + filter live on the
  server (`GET /api/memories` grew `offset` / `kind` query params
  and a `total` / `cap` response). New WS event:
  `memory_updated`. New endpoints: `PATCH /api/memories/{id}`,
  `POST /api/memories`, `POST /api/memories/{id}/pin`. Schema v5
  added the `pinned` column. No separate detail doc — the Memory tab
  in the app is self-explanatory.

- **Aiko's room — virtual space with locations + items.**
  [`WorldStore`](../app/core/world_store.py) backs a small persistent
  SQLite world (locations, items with consume semantics, a singleton
  state row holding posture / activity / location). A default rich
  room is seeded once on first boot. The room flows into the LLM via
  a `world` inner-life provider, five new agent tools
  (`look_around`, `move_to`, `change_posture`, `inspect_item`,
  `consume_item`), and a `world_updated` WS event. "Give Aiko a
  cookie" is intentionally silent. Schema v6 added
  `world_locations` / `world_items` / `world_state`. See
  [`docs/aiko-room.md`](aiko-room.md).

- **Shared moments + relationship axes (schema v7).** Structured
  `shared_moment` memory kind with `(when, what, vibe,
  source_message_ids, last_anniversaried_at)` metadata on the new
  `memories.metadata` JSON column. Three detection tracks (inline
  `[[moment:vibe:text]]` tag, a Track-2 LLM detector gated on
  affect/reaction/milestone/gift signals, manual "Mark as moment"
  chat action). Anniversary inner-life block (1mo / 3mo / 6mo / 1yr
  ± 1 day, 6h per-moment rate limit) + small RAG bonus. New
  `relationship_axes` table (closeness / humor / trust / comfort,
  ~30-day decay, ±0.08-per-turn drift caps). New "Together" UI tab.
  Follow-ups still open: J1 (multi-user), J2 (exportable timeline),
  J3 (axes-aware proactive nudges). See
  [`docs/shared-moments-and-relationship.md`](shared-moments-and-relationship.md).

- **Memory tiers + revival drift + IdleWorker framework (schema v8).**
  E1 (tiers), E2 (revival-rebated decay), and G1 (idle scheduler)
  shipped together. The `memories` table grew `tier` (`scratchpad` /
  `long_term` / `archive`) and `revival_score` columns plus a new
  `kv_meta` key-value table for cross-restart worker bookkeeping.
  `MemoryStore.decay` is now wall-clock-driven (proportional to elapsed
  time since `memory.last_decay_run_at`, clamped by
  `decay_max_catchup_days`) with per-tier rates and a revival rebate
  (`salience += revival_coefficient * elapsed * revival_score`).
  `prune()` enforces per-tier caps independently. A new
  `IdleWorkerScheduler` (`app/core/idle_worker_scheduler.py`) wakes
  during quiet windows (no Live mode + no recent user activity) and
  runs one registered worker per tick. First two workers:
  `MemoryPromotionWorker` (promotes scratchpad rows on
  age + use_count OR revival ≥ 0.3, demotes idle long_term rows after
  180 days, deletes dead scratchpad after the TTL) and
  `MemoryDecayWorker` (thin wrapper around `MemoryStore.decay`). New
  REST endpoints: `tier` query param + `revival_score` in `GET
  /api/memories`, `GET /api/memories/counts`, `tier` on PATCH/POST.
  Frontend Memory tab gained a tier pill, tier filter, per-tier counts
  header, and revival % readout. All producers were classified by
  trust: `MemoryExtractor`, `ReflectionWorker`, `DreamWorker` write to
  scratchpad; `PromiseExtractor`, `CatchphraseMiner`,
  `RelationshipPulse`, `SharedMoments`, `[[remember:...]]` tags, the
  manual REST/UI path, and milestone memories go straight to
  long_term. `MemoryConsolidator` now clusters within-tier only. See
  [`docs/memory-tiers.md`](memory-tiers.md).

---

## How to pick one up

1. Re-read the relevant section.
2. Spin up a plan. Each item is small enough to fit in a single
   `CreatePlan` invocation; nothing here needs to be split into phases.
3. Validate the same way we did for A1/A2/A3:
   focused suite -> full `pytest -q` -> spot-check the running app.
4. Update the relevant doc (this file, `AGENTS.md`,
   `docs/alexia-model-notes.md`) when the work lands.
