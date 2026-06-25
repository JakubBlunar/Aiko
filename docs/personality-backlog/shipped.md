# Shipped (kept for reference)

Summary entries for completed work. Detail lives in the linked
implementation files / docs, not here. One paragraph per entry,
listed roughly in shipping order (oldest at top).

---

## B1 / B2. Continuous expressiveness + listening micro-nods

`AmbientBodyChannel` drives `ParamBreath` from arousal and
`ParamBodyAngleY` from valence; `ExpressionChannel.tickPreModel`
does arousal-scaled overrides on the parameters declared in each
expression file. A new `avatar.expressiveness` slider (0.0-1.5)
scales the lot. Backchannel micro-nods (`_emit_backchannel_motion`)
map `agreement` / `disagreement` / `thinking` / `confused` onto
rate-limited idle-priority motions. See
[`docs/alexia-model-notes.md`](../alexia-model-notes.md) §3 and
[`AGENTS.md`](../../AGENTS.md).

---

## B4. Alexia visual-identity audit

The visual identity audit landed for the full Alexia rig — every
expression has a confirmed identity now. `lzx` (cheerful / amused),
`k` (sad / melancholy / concerned, plus `cry` fallback), `sq`
(angry / frustrated), `wh` (surprised / curious), `xxy` (excited /
enthusiastic), `lh` (warm / tender / gentle), `y` (the new
`confused` reaction — NOT `tired`, which now routes to body-slump
via `AmbientBodyChannel`), `zs1` (playful in day clothes;
falls through to amused / `lzx` in pajamas via the outfit gate).
Accessory tier (`bbt`, `dyj`, `mj`, `yjys1`, `yjys2`) is now reachable
via `[[overlay:X]]` + Phase 4 persistent toggles. Outfit envelopes
(`yf` / `yfmz` + the synthetic `day_clothes` baseline) drive
`OutfitChannel`. See [`docs/Alexia-my-observation.md`](../Alexia-my-observation.md)
and [`docs/alexia-model-notes.md`](../alexia-model-notes.md) §3 / §3a /
§3b / §3c.

**Phase 5 close-out.** The remaining vocabulary work landed:
`embarrassed` (→ `lh`, the shy / inward-tilted smile), `nervous`
(intentionally unmapped — falls through to the `concerned` →
`serious` → `thoughtful` neighbour chain so we don't fire the
`yfmz` pajamas envelope as a side effect; persona stacks
`[[reaction:nervous+sweat]]` for the visible Param44 sweat drop),
and `defiant` (→ `mj`, the head-sunglasses-on-hair tilt that reads
as cocky / "whatever" without disturbing outfit state). The
TypeScript `_REACTION_NEIGHBOURS` mirror in
[`web/src/live2d/channels/ExpressionChannel.ts`](../../web/src/live2d/channels/ExpressionChannel.ts)
is now in lock-step with Python (covers all 27 reactions), with a
Vitest parity test that fails if a Python `REACTIONS` key gains a
neighbour chain on one side without the other. Persona idiom
`[[reaction:defiant+pout]]` (a silent no-op; no `pout` capability
exists) was replaced with `[[reaction:defiant+question]]`, which
routes to the `wh.exp3` question-mark pulse.

**Tail wag + ear wiggle on physics-driven rigs.** `[[overlay:tail_wag]]`
on Alexia was visibly broken because `Param_Angle_Rotation_*_ArtMesh202`
is a *physics output* of `ParamBreath` (`PhysicsSetting16`), and the
existing `tickTier3` direct-sine boost was overwritten by
`physics.evaluate()` every frame. Fix: in
[`AmbientBodyChannel.ts`](../../web/src/live2d/channels/AmbientBodyChannel.ts)
`tickPreModel` (which runs *after* physics), boost `ParamBreath`
freq by 2.5x and amplitude by 1.5x while
`engineState.tailWagBoostUntil` is in the future and
`has_tail_wag` is on. Physics propagates the faster wave naturally
into the five tail segments. The `tickTier3` direct-sine boost
stays as a non-physics fallback for the Mini fixture and any
future minimal rigs. Overlay duration bumped from 1500 ms to 2000
ms (via a new `_OVERLAY_DURATION_OVERRIDES_MS` table in
[`avatar_mixin.py`](../../app/core/session/avatar_mixin.py)) to
match the persona prompt's "~2 s burst" copy. The ear wiggle had
two compounding bugs: (a) Alexia's ear params are named `Hair 5`
/ `Hair 5-1` / `Hair 5-2` / `Hair 5-3` (Param13 / 14 / 15 / 18)
after the cdi3 translation pass, none of which match the
`_EAR_SEGMENT_SYNONYMS` list, so synonym detection set
`has_ear_wiggle=false`; (b) those four params are physics outputs
of `ParamEyeR/LOpen` (`PhysicsSetting13` / `14` — ears flick on
every blink), so even with detection working, `tickTier3` writes
would be clobbered. Fixed by adding an optional per-rig
`avatar_overrides.json` lookup in
[`avatar_profile.py`](../../app/core/persona/avatar_profile.py) (supported
keys this pass: `cat_ear_param_ids`, `cat_tail_param_ids`),
shipping the Alexia override
([`data/personas/active/Alexia/avatar_overrides.json`](../../data/personas/active/Alexia/avatar_overrides.json))
that pins the four `Hair 5*` IDs, and adding a `tickPreModel`
ear-wiggle write path in
[`GestureChannel.ts`](../../web/src/live2d/channels/GestureChannel.ts)
that mirrors the `tickTier3` 4 Hz / amp 15 sine but lands after
physics. Slot lifecycle (rest-snap-then-null on expiry) now lives
exclusively in `tickPreModel` so the post-physics rest-write is
the last write of the expiry frame.

---

## B5. Auto-cascade safety — voice mode / backchannel must not pick "heavy" expressions

A perfectly cheerful turn was visibly rendering Alexia crying for a
2-4 s thinking window while she resolved tool calls (`recall` then
`change_posture`). Root cause was the auto-cascade chain inside
[`ExpressionChannel.ts`](../../web/src/live2d/channels/ExpressionChannel.ts):
`_MODE_TO_REACTION.thinking` cascaded `thoughtful` (empty on Alexia)
into `concerned` -> `k` (Param59 = tear streaks). Same shape on
`_BACKCHANNEL_TO_REACTION.concern` and `.disagreement`. Fix routes
auto-cascades to soft / neutral alternatives only; the explicit
`[[reaction:concerned]]` from the LLM still resolves to the rig's
mapping (intentional empathy beat). A second trace surfaced a
follow-up path through the explicit `[[reaction:X]]` neighbour
fallback in [`reactions.py`](../../app/core/affect/reactions.py) /
[`ExpressionChannel.ts`](../../web/src/live2d/channels/ExpressionChannel.ts)
`_REACTION_NEIGHBOURS`: non-sad reactions (`thoughtful`, `serious`,
`frustrated`, `angry`) chained through `concerned` as fallback. Fix
dropped `concerned` (and any other sad-family entry) from non-sad
chains; the sad family still chains within itself so legitimate
`[[reaction:sad]]` emits paint the right tears. Lock-in:
[`tests/test_reactions.py`](../../tests/test_reactions.py)
`CryCascadeGuardTests` plus the existing
"auto-cascade avoids heavy expressions" block in
[`ExpressionChannel.test.ts`](../../web/src/live2d/channels/ExpressionChannel.test.ts).

**Design rule going forward.** When adding entries to
`_MODE_TO_REACTION` or `_BACKCHANNEL_TO_REACTION`, every candidate
must read as a *micro-expression* on any rig. Reactions that imply
strong narrative emotion on at least one supported rig
(`concerned`, `sad`, `melancholy`, `cry`, `angry`, `frustrated`,
`defiant`) belong only in the *explicit* `[[reaction:X]]` path,
never the auto-cascade fallback.

---

## B6. UI debug logging bridge

The cry-cascade investigation (B5) needed a single timeline that
showed *both* what the backend emitted (mood / reaction tag /
filler / tool dispatch / voice mode) and what the renderer actually
did with it (which reaction the channel picked, which `.exp3.json`
it landed on, when overlays expired, when the WS reconnected).
Previously only the backend half existed in
[`data/app.log`](../../data/app.log); the UI half lived in DevTools
and didn't survive a tab refresh. Sharing one file when reporting a
bug now reconstructs the whole flow.

`logging.ui_log_enabled` (added to
[`LoggingSettings`](../../app/core/infra/settings.py)) gates the feature;
off by default. The "Debug logging" block in
**Settings drawer -> Chat -> Diagnostics** flips it via
`PATCH /api/settings`, the server broadcasts
`logging_settings_changed` over the WS, and every tab's
`debugLog.setEnabled` mirrors the new value. When enabled, the
browser captures structured events (`{ ts, source, kind, payload }`)
into a 2000-entry ring buffer
([`web/src/log.ts`](../../web/src/log.ts)) and batches them out
every ~500 ms to `POST /api/logs/ui`
([`app/web/server.py`](../../app/web/server.py)). The handler caps
the batch, allow-lists `source` by prefix, truncates oversized
payloads, and emits each entry on the `app.ui` logger as
`INFO [ui] {source} {kind} {payload_json}` so it interleaves into
`data/app.log` with the existing backend lines. The "Download
buffer" button serialises the in-memory ring to
`alexia-ui-log-<iso>.json` for cases where the backend isn't
responding. Disabling the toggle returns `403` on `/api/logs/ui`,
the batcher drains, and `debugLog.log` becomes a free no-op.

Sources instrumented today (kept tight to the cry-cascade /
lip-sync / reconnection forensic surface): `ws`, `voice`,
`channel.expression`, `channel.overlay`, `channel.motion`,
`channel.outfit`, `channel.accessory`. Per-frame work (lip-sync
amplitude, Pixi ticks) is intentionally not logged. Tests:
[`tests/test_web_server_ui_logs.py`](../../tests/test_web_server_ui_logs.py),
[`tests/test_web_server_settings.py`](../../tests/test_web_server_settings.py)
(`LoggingSettingsRoundTripTests`),
[`web/src/log.test.ts`](../../web/src/log.test.ts),
[`web/src/store.logging.test.ts`](../../web/src/store.logging.test.ts),
and the "debug instrumentation" block in
[`web/src/live2d/channels/ExpressionChannel.test.ts`](../../web/src/live2d/channels/ExpressionChannel.test.ts).

---

## C1. Typed-mode proactive ping + activity awareness

Typed-mode `ProactiveDirector` path with a prepared-nudge fast path
and an LLM "pick up the thread" fallback, gated by a presence
boolean (browser tab visibility AND Tauri window focus, AND-folded
client-side). Defaults: 4 min silence, 10 min cooldown, text-only.
Desktop-only opt-in activity awareness forwards the foreground app
*name* (never titles or URLs) so Aiko can reference what Jacob is
doing. Off by default. See
[`docs/presence-and-activity.md`](../presence-and-activity.md).
Open follow-ups (window-title-aware activity, cooldown persistence,
TTS-on-typed-proactive) live in [`proactive.md`](proactive.md).

---

## User-facing memory editor

Dedicated "Memory" tab in
[`web/src/components/SettingsDrawer.tsx`](../../web/src/components/SettingsDrawer.tsx).
Full edit-in-place / manual create / pin toggle / kind filter / sort
/ paginated list (page size 50). Pinned rows are skipped by
`decay()` and never selected as `prune()` victims; `RagRetriever`
adds a `+0.05` score bonus for pinned hits via a SQLite-mirror
lookup (LanceDB stays untouched). Cap bumped from 500 to 5000.
Pagination + filter live on the server (`GET /api/memories` grew
`offset` / `kind` query params and a `total` / `cap` response).
New WS event: `memory_updated`. New endpoints:
`PATCH /api/memories/{id}`, `POST /api/memories`,
`POST /api/memories/{id}/pin`. Schema v5 added the `pinned`
column. No separate detail doc — the Memory tab in the app is
self-explanatory.

---

## Aiko's room — virtual space with locations + items

[`WorldStore`](../../app/core/world/world_store.py) backs a small persistent
SQLite world (locations, items with consume semantics, a singleton
state row holding posture / activity / location). A default rich
room is seeded once on first boot. The room flows into the LLM via
a `world` inner-life provider, five new agent tools
(`look_around`, `move_to`, `change_posture`, `inspect_item`,
`consume_item`), and a `world_updated` WS event. "Give Aiko a
cookie" is intentionally silent. Schema v6 added `world_locations`
/ `world_items` / `world_state`. See
[`docs/aiko-room.md`](../aiko-room.md).

---

## Aiko's living garden — outdoor plot + plant growth loop

Extends the world model with a `garden` location seeded
idempotently on every boot
([`WorldStore.ensure_garden_seed`](../../app/core/world/world_store.py))
plus two new item kinds: `plant` (with `species` / `stage` /
`lifecycle` in `state`) and `seed`. Plants advance through
`sprout -> sapling -> growing -> flowering -> mature` over wall-clock
time via a `PlantGrowthWorker` (hourly, hooked into the existing
`IdleWorkerScheduler`). A `GardenVisitWorker` wanders her outside
during quiet daylight windows, waters every plant, and auto-harvests
any that are mature — produce lands in `kitchenette` as a fresh
`food` item, annuals drop a replacement seed in inventory,
perennials reset to `growing` so the cycle keeps going. Three new
agent tools (`water_plant`, `plant_seed`, `harvest_plant`) let her
interact on-demand; the persona prompt picks up garden + harvest
framing. `render_block` flips to an outdoor phrasing when she's
there ("You are at home, currently outside in the garden…") and
surfaces stage cues including the loud "(mature, ready to
harvest)" hint. UI lights up automatically — `WORLD_KINDS` gained
`plant`/`seed`, and the World tab item row shows a stage badge. No
schema migration (rides on the existing `state_json` column).
Deferred: no wilting / death yet, no scene system; the garden is a
single location. See [`docs/aiko-room.md`](../aiko-room.md) under
"Garden". H5 (second scene / travel semantics) in
[`immersion.md`](immersion.md) is the natural follow-up.

---

## Shared moments + relationship axes (schema v7)

Structured `shared_moment` memory kind with `(when, what, vibe,
source_message_ids, last_anniversaried_at)` metadata on the new
`memories.metadata` JSON column. Three detection tracks (inline
`[[moment:vibe:text]]` tag, a Track-2 LLM detector gated on
affect/reaction/milestone/gift signals, manual "Mark as moment"
chat action). Anniversary inner-life block (1mo / 3mo / 6mo / 1yr
± 1 day, 6h per-moment rate limit) + small RAG bonus. New
`relationship_axes` table (closeness / humor / trust / comfort,
~30-day decay, ±0.08-per-turn drift caps). New "Together" UI tab.
Open follow-ups (J1 multi-user, J2 exportable timeline, J3 axes-aware
nudges) live in [`moments.md`](moments.md). See
[`docs/shared-moments-and-relationship.md`](../shared-moments-and-relationship.md).

---

## Memory tiers + revival drift + IdleWorker framework (schema v8)

E1 (tiers), E2 (revival-rebated decay), and G1 (idle scheduler)
shipped together. The `memories` table grew `tier` (`scratchpad` /
`long_term` / `archive`) and `revival_score` columns plus a new
`kv_meta` key-value table for cross-restart worker bookkeeping.
`MemoryStore.decay` is now wall-clock-driven (proportional to
elapsed time since `memory.last_decay_run_at`, clamped by
`decay_max_catchup_days`) with per-tier rates and a revival rebate
(`salience += revival_coefficient * elapsed * revival_score`).
`prune()` enforces per-tier caps independently. A new
[`IdleWorkerScheduler`](../../app/core/proactive/idle_worker_scheduler.py) wakes
during quiet windows (no Live mode + no recent user activity) and
runs one registered worker per tick. First two workers:
`MemoryPromotionWorker` (promotes scratchpad rows on
age + use_count OR revival ≥ 0.3, demotes idle long_term rows after
180 days, deletes dead scratchpad after the TTL) and
`MemoryDecayWorker` (thin wrapper around `MemoryStore.decay`). New
REST endpoints: `tier` query param + `revival_score` in
`GET /api/memories`, `GET /api/memories/counts`, `tier` on
PATCH/POST. Frontend Memory tab gained a tier pill, tier filter,
per-tier counts header, and revival % readout. All producers were
classified by trust: `MemoryExtractor`, `ReflectionWorker`,
`DreamWorker` write to scratchpad; `PromiseExtractor`,
`CatchphraseMiner`, `RelationshipPulse`, `SharedMoments`,
`[[remember:...]]` tags, the manual REST/UI path, and milestone
memories go straight to long_term. `MemoryConsolidator` now
clusters within-tier only. See
[`docs/memory-tiers.md`](../memory-tiers.md).

---

## F1. Background fact-checker worker

Idle worker that fact-checks recently surfaced claims in the
background and updates the originating memory's `confidence` (and
optionally its content) when the search clearly corrects a number /
date. Lives in
[`app/core/memory/idle_fact_checker.py`](../../app/core/memory/idle_fact_checker.py)
and registers with the shipped `IdleWorkerScheduler`. Privacy is
enforced by [`fact_check_privacy.py`](../../app/core/memory/fact_check_privacy.py)
which blocks personal claims at classification time and scrubs the
search query (drops emails, phone numbers, names, addresses) before
it ever leaves the box. Per-hour and per-day budgets live in
[`fact_check_rate_limiter.py`](../../app/core/memory/fact_check_rate_limiter.py)
backed by `kv_meta`. Each phase logs at INFO with timing + previews
(`start`, `scrubbed`, `search done`, `distil done`, `apply done`)
so [`data/app.log`](../../data/app.log) is the audit trail. Tests:
[`tests/test_idle_fact_checker.py`](../../tests/test_idle_fact_checker.py),
[`tests/test_fact_check_privacy.py`](../../tests/test_fact_check_privacy.py),
[`tests/test_fact_check_rate_limiter.py`](../../tests/test_fact_check_rate_limiter.py).

---

## F2. Knowledge-gap journal

Captures Aiko's "I don't know" moments as structured
`knowledge_gap` memories so F1 can close them later and the prompt
can resurface them when the topic returns. Extraction lives in
[`app/core/memory/knowledge_gap_extractor.py`](../../app/core/memory/knowledge_gap_extractor.py)
(regex + the inline `[[gap:topic:question]]` self-tag, mirroring
the promise extractor shape). Storage reuses `MemoryStore` via the
`knowledge_gap` kind in [`memory_store.py`](../../app/core/memory/memory_store.py).
Resolved gaps gain a `resolved_at` metadata stamp from the F1
worker and the original gap row is kept for audit. Surfacing in the
prompt is gated on cosine similarity to the current turn so only
relevant gaps re-enter the conversation.

---

## F2.1. Knowledge-gap auto-resolver (memory-match + user-answer)

F2 only had one closure path: F1's idle fact-checker, which goes to
the *web* to look the answer up. In practice that means a gap minted
on Day 1 ("does Jacob listen to specific genres while watching anime")
never closes — F1 won't web-search a personal question about the user,
the post-summary `MemoryExtractor` writes the user's actual answer
into a fresh `preference` row hours later, and nothing cross-references
the gap against existing memory. So the `Things you've been wondering
about with Jacob` block keeps re-injecting the same question into the
prompt every session for weeks until the user explicitly notices
("you maybe forgot...") and Aiko apologises but the loop continues.

F2.1 adds two complementary closure paths, both stamping
`metadata.resolved_at` + `resolved_by_memory_id` (and a new
`metadata.resolved_by` audit field) via the existing
[`KnowledgeGapStore.mark_resolved`](../../app/core/memory/knowledge_gap_extractor.py)
API:

* **Idle memory-match resolver** — a new
  [`IdleGapResolver`](../../app/core/conversation/idle_gap_resolver.py) registered
  with `IdleWorkerScheduler`. Each tick (default 600 s) walks
  `KnowledgeGapStore.list_open()` and calls `MemoryStore.search` with
  the gap's *already-stored* embedding (no re-embed cost). Hits are
  filtered to `_ANSWER_KINDS` (`fact`, `preference`, `event`,
  `relationship`, `promise`, `shared_moment`, `curiosity_finding`,
  `reflection`) so a gap can never resolve itself or be closed by an
  Aiko-side `self_tagged` row. Bounded per-tick (default 5 gaps) so a
  burst of new gaps doesn't eat the scheduler's CPU budget. Backfill
  is automatic: first tick after app start handles every legacy gap.
  Audit log mirrors the F1 shape (`gap_resolver: resolved gap_id=X
  by memory_id=Y score=0.78 ...`).

* **Post-turn user-answer resolver** — new
  `_resolve_knowledge_gaps` method on
  [`PostTurnMixin`](../../app/core/session/post_turn_mixin.py),
  modeled directly on `_resolve_curiosity_seeds`. After every turn it
  embeds `user_text + assistant_text` once and cosines against every
  open gap's stored embedding. Anything above
  `agent.gap_user_answer_resolve_threshold` (default 0.50) closes
  with `resolved_by="user_answer"`. This catches the answer the
  moment the user speaks it; the idle worker mops up the rest.

Tunables on
[`AgentSettings`](../../app/core/infra/settings.py):
`gap_resolver_enabled`, `gap_resolver_interval_seconds` (600),
`gap_resolver_threshold` (0.55 — slightly stricter than the seed
resolver's 0.50 because closing a gap is a stronger claim than
consuming a seed), `gap_resolver_per_tick` (5),
`gap_user_answer_resolve_threshold` (0.50).

Tests:
[`tests/test_idle_gap_resolver.py`](../../tests/test_idle_gap_resolver.py)
(15 cases: backfill happy path, kind filtering, threshold clamps,
per-tick cap, `is_ready` gates, INFO audit log) and
[`tests/test_session_controller_gap_resolver.py`](../../tests/test_session_controller_gap_resolver.py)
(8 cases mirroring the K9 seed-resolve fixture pattern).

---

## F3. Confidence column on memories

`confidence REAL NOT NULL DEFAULT 0.7` added to the `memories`
table; `Memory` dataclass + `MemoryStore.add` / `update` / mirror
plumbing all carry it now. Defaults: extractor `0.7`,
`[[remember:self:...]]` self-tags `0.85`, `[[remember:...]]`
user-confirmed tags `0.9`, tool-result memories (RAG / web) `0.95`,
manual memory-tab creates `1.0`. F1 pushes confidence up toward
`0.95` on positive verification and down to `0.4` on contradiction.
[`rag_retriever.py`](../../app/core/rag/rag_retriever.py) penalises
hits with `confidence < 0.5` and appends an `(uncertain)` suffix
in the rendered memory block so the LLM hedges. Memory tab in
[`SettingsDrawer.tsx`](../../web/src/components/SettingsDrawer.tsx)
gained a confidence column + filter. Pinned rows clamp to `>= 0.9`.

---

## F5. Conflicting-memory detector (schema v11)

Periodic background worker that scans pairs of allow-listed memories
(`fact` / `preference` / `relationship` / `event`) with high cosine
similarity but lexically contradicting content. New
[`memory_conflicts`](../../app/core/infra/chat_database.py) table (schema
v11) records each detected pair with the heuristic signals,
optional LLM verdict, and a status of `open` / `auto_resolved` /
`user_resolved` / `dismissed`. The
[`MemoryConflictStore`](../../app/core/memory/memory_conflict_store.py)
wraps it with `record` / `list_open` / `mark_user_resolved` /
`dismiss` / `delete_for_memory` (cascade-cleanup hook on
`MemoryStore.delete`).

Detection is hybrid: a cheap heuristic gate in
[`conflict_heuristics.py`](../../app/core/memory/conflict_heuristics.py)
(negation flip, antonym table, numerical mismatch) labels each
candidate pair `definite` (skip LLM, resolve immediately),
`borderline` (LLM verifies via a `YES` / `NO` / `UNRELATED` JSON
prompt, rate-limited through a dedicated
[`FactCheckRateLimiter`](../../app/core/memory/fact_check_rate_limiter.py)
with `state_key="conflict_detector.rate_state"`), or `no` (drop
without LLM cost). Confirmed conflicts with `|conf_a - conf_b| >=
0.30` (default) auto-demote the loser to `tier=archive`,
`confidence=0.20`, with `metadata.superseded_by` stamped — the rest
surface in the new Conflicts sub-tab on the Memory drawer for the
user to resolve via Keep-this / dismiss buttons. The worker
[`MemoryConflictWorker`](../../app/core/memory/memory_conflict_worker.py)
registers with the shipped `IdleWorkerScheduler` on an hourly
cadence and respects per-tick caps (`max_corpus=1000`,
`max_pairs_per_run=50`) so an O(n²) sweep can never tank a tick.

Aiko can also self-flag mid-turn with `[[conflict:short reason]]`
(parsed in
[`response_text_service.py`](../../app/core/services/response_text_service.py),
stripped from chat/TTS, dispatched in
[`SessionController._post_turn_inner_life`](../../app/core/session/session_controller.py)
to `IdleWorkerScheduler.force_run` so the worker runs immediately
instead of waiting for the next hour). REST endpoints
`/api/memory-conflicts` (GET / resolve / dismiss) in
[`app/web/server.py`](../../app/web/server.py) back the new
Conflicts sub-tab in
[`SettingsDrawer.tsx`](../../web/src/components/SettingsDrawer.tsx),
which renders a side-by-side card per pair with similarity, both
confidences, the heuristic signals chips, and the LLM reason when
present. A collapsed "Recently auto-resolved" tail provides a
read-only audit log. Tests:
[`tests/test_conflict_heuristics.py`](../../tests/test_conflict_heuristics.py),
[`tests/test_memory_conflict_store.py`](../../tests/test_memory_conflict_store.py),
[`tests/test_memory_conflict_worker.py`](../../tests/test_memory_conflict_worker.py),
plus extensions to `tests/test_response_text_service.py` and
`tests/test_web_server_memories.py`.

---

## K2. Theory-of-mind / belief tracking (schema v12)

A persistent model of what Aiko *thinks* Jacob believes / feels,
kept separate from the facts she knows. New
[`beliefs`](../../app/core/infra/chat_database.py) table (schema v12) holds
two shapes in one store, distinguished by the `kind` column:
`mood` beliefs carry numeric `valence` / `arousal` so the gap
detector can compare directly against the live
[`AffectState`](../../app/core/affect/affect_state.py), and `opinion`
beliefs hold a free-text predicted state ("rust is overhyped"). The
[`BeliefStore`](../../app/core/relationship/belief_store.py) wraps it with
`upsert` (dedupes by `(user_id, kind, topic)` plus topic-embedding
cosine ≥ 0.88) / `list_active` / `mark_contradicted` /
`mark_confirmed` / `mark_stale` / `delete` / `count_by_status`,
mirroring the F5 store shape.

Two write paths feed the store. The self-tag fast path adds a new
`[[predict:kind:topic:state:confidence]]` grammar to
[`response_text_service.py`](../../app/core/services/response_text_service.py)
(parsed alongside `[[conflict:...]]`, stripped from chat/TTS,
dispatched in `_post_turn_inner_life`); the
[`BeliefInferenceWorker`](../../app/core/relationship/belief_worker.py) mines
recent user turns once an hour, privacy-scrubs the transcript via
[`fact_check_privacy.scrub_claim_for_search`](../../app/core/memory/fact_check_privacy.py),
spends one rate-limited LLM call through a dedicated
[`FactCheckRateLimiter`](../../app/core/memory/fact_check_rate_limiter.py)
(`state_key="belief_worker.rate_state"`) to extract a JSON array of
`{kind, topic, predicted_state, confidence}` tuples, then upserts
with `source="worker"`. Self-tagged beliefs at higher confidence are
preserved over worker rewrites.

The
[`BeliefGapDetector`](../../app/core/relationship/belief_gap_detector.py) runs
each post-turn and surfaces mismatches: for each active mood belief
younger than `belief_recent_window_hours` (default 24h), it
flips the row to `contradicted` when
`|val_pred - val_obs| > belief_gap_valence_threshold` (default 0.30),
`|aro_pred - aro_obs| > belief_gap_arousal_threshold` (default 0.25),
or the recomputed valence band lands in opposing territory. Opinion
beliefs use
[`conflict_heuristics.classify_pair`](../../app/core/memory/conflict_heuristics.py)
against the user's recent message — a `definite` heuristic flips to
`contradicted`, a strong Jaccard overlap nudges to `confirmed`, and
beliefs untouched for `belief_stale_after_days` (default 90) bulk-
flip to `stale`. Surfaced gaps render up to two lines into the next
turn's prompt via a new `belief_gaps` inner-life provider
("Your nervous read on tokyo trip isn't matching the live affect.
Name the gap once and gently if it fits, then move on.").

REST endpoints `/api/beliefs` (GET / POST / PATCH / DELETE) in
[`app/web/server.py`](../../app/web/server.py) back a new Beliefs
sub-tab in
[`SettingsDrawer.tsx`](../../web/src/components/SettingsDrawer.tsx),
grouped by kind with a per-row gap pulse + filter chips for kind /
status. WebSocket events `belief_added` / `belief_updated` /
`belief_deleted` keep the panel live without polling. Persona
guidance in
[`aiko_companion.txt`](../../data/persona/aiko_companion.txt)
teaches the `[[predict:...]]` tag and the gentle gap-naming beat.
Tests:
[`tests/test_belief_store.py`](../../tests/test_belief_store.py),
[`tests/test_belief_worker.py`](../../tests/test_belief_worker.py),
[`tests/test_belief_gap_detector.py`](../../tests/test_belief_gap_detector.py),
[`tests/test_web_server_beliefs.py`](../../tests/test_web_server_beliefs.py),
plus extensions to `tests/test_response_text_service.py`.

---

## K6. Surprise / novelty detector

A per-turn signal that lets Aiko react with real surprise when Jacob
pivots away from the recent topic baseline, instead of accepting an
out-of-the-blue message with the same flat acknowledgement she'd
give a continuation. No new schema, no REST surface — the detector
is in-process and the signal lives entirely in the inner-life
prompt.

The [`NoveltyDetector`](../../app/core/conversation/novelty_detector.py) keeps an
in-memory `collections.deque[np.ndarray]` of size `novelty_window`
(default 12) on each `SessionController`. On the first `detect()`
call per session it lazily warms the ring from
[`RagStore.list_recent_user_vectors`](../../app/core/rag/rag_store.py)
filtered by the current user prefix (`session_id` starts with
`{user_id}:`) so a topic genuinely discussed yesterday won't re-fire
"this is new" today. On every turn it embeds `user_text`
synchronously via the shared `Embedder`, computes
`distance = 1 - cosine(vec, centroid)` against the renormalised mean
of the ring, and classifies into two bands:
`distance >= novelty_strong_threshold` (default 0.55) -> `strong_novelty`,
`>= novelty_mild_threshold` (default 0.35) -> `mild_shift`,
otherwise silent. The current vector is appended to the ring on
every call (silent / banded / cooldown) so the baseline keeps
moving with the conversation. After a hit the detector enters a
`novelty_cooldown_turns` suppression window (default 2) so a run of
genuinely-novel turns doesn't pile "you keep saying surprising
things" beats on top of each other. A short (`< 8` chars) text or a
ring still below `novelty_warmup_min` (default 3) returns `None`
silently — cold-start installs don't blare novelty on their first
three turns.

The signal surfaces through a new `novelty` inner-life provider on
[`PromptAssembler`](../../app/core/session/prompt_assembler.py) (same shape
as `knowledge_gaps`: takes the live `user_text`, called inside
`assemble_with_budget`, dropped under `aggressive=True`). The
provider's banded copy lands in the system prompt right after
`belief_gaps_block`, before `knowledge_gaps_block`, clustering all
"things on Aiko's mind" cues together. Persona guidance in
[`aiko_companion.txt`](../../data/persona/aiko_companion.txt)
("Surprise and novelty") teaches Aiko to acknowledge the pivot
once with the mild band and to ask a real follow-up with the strong
band, without performing surprise when no note is present.

Settings live on `AgentSettings` (`novelty_detection_enabled`,
master switch) and `MemorySettings`
(`novelty_window`, `novelty_warmup_min`,
`novelty_mild_threshold`, `novelty_strong_threshold`,
`novelty_cooldown_turns`), mirrored in
[`config/default.json`](../../config/default.json). The detector
module logs one INFO line per turn
(`novelty-detector: distance=%.3f band=%s window=%d user=%s`)
plus a one-shot
`novelty-detector: warmed ring=N user=X` on the first detect of a
session — both grep-friendly via MCP `tail_logs(module_contains="novelty")`.

Tests: [`tests/test_novelty_detector.py`](../../tests/test_novelty_detector.py)
(cold start / warm prefill / band classification / cooldown / ring
maxlen / short-text skip / lazy warm called-once / warm failure
fallback), plus extensions to
[`tests/test_prompt_assembler.py`](../../tests/test_prompt_assembler.py)
(novelty block lands, silent when empty, dropped under aggressive,
exceptions swallowed) and
[`tests/test_rag_store.py`](../../tests/test_rag_store.py)
(role + session-prefix filtering, recency order, limit, empty
result, empty-prefix matches all users).

---

## K16. Unified ambient grounding line

Today the system prompt carries seven separate "ambient" inner-life
blocks (circadian, world, activity-awareness, affect/mood,
relationship-pulse, user_state, ambient_noise) plus their carryover
mood hint — eight blocks, each with its own "but only mention when
natural" tail. The LLM sees that as eight facts to recite. Companion-AI
grounding research (and a year of running with the granular blocks)
points the other way: one fused paragraph reads as continuous awareness
and ducks the surveillance-theatre tic that comes from repeating the
"don't recite this" guard eight times per turn.

K16 ships a new
[`GroundingLineRenderer`](../../app/core/conversation/grounding_line.py) that
consumes a structured `GroundingContext` (built once per turn from the
same store getters the granular block providers already use) and
composes a deterministic, template-driven 1-3 sentence paragraph at
the top of the system prompt. The renderer is **pure / no LLM call /
no randomness**: tests in
[`tests/test_grounding_line.py`](../../tests/test_grounding_line.py)
lock the texture for representative slot combinations so a refactor
that intends to change the texture has to update the tests first.

The fusion scope is intentionally conservative. Fused into the line:
circadian (time + day + drowsy), activity-awareness ("Jacob's in
Cursor"), user_state ("reads upbeat, energy normal"), affect ("your
private feeling is content"), relationship phase + age, world
(location + posture + activity), ambient_noise (loud / soft hum
rider on sentence 1). Always-standalone in every K16 mode (each
carries data fusion would dilute): anniversary, profile bullets,
pajama, knowledge_gaps, belief_gaps, novelty, stagnation, agenda,
axes, petname, vocal_tone, catchphrase, narrative, arc.

### The three-mode config (canonical reference)

K16 ships behind `agent.grounding_line_mode`, a string-valued setting
in [`AgentSettings`](../../app/core/infra/settings.py) (default `"off"`,
mirrored in [`config/default.json`](../../config/default.json)).
Invalid values clamp to `"off"` with a debug log so a typo never
wedges the prompt. Three modes:

- `off` (default): no grounding line; all eight granular ambient
  blocks render exactly as before. Safe rollback target — flip back
  here instantly if `replace` or `split` reads worse than the status
  quo.
- `replace`: the grounding line replaces all eight ambient blocks.
  Cleanest test of the "one paragraph reads as continuous awareness"
  hypothesis. Most aggressive.
- `split`: middle ground. The grounding line replaces situational
  signals (circadian, world, activity, ambient_noise) but keeps
  {affect, mood_hint, relationship, user_state} as standalone
  because they carry trend / phase phrasing the fused line cannot
  represent without dilution.

### Suppression matrix

Which blocks render in which mode:

| Block                                    | `off`  | `split` | `replace` |
|------------------------------------------|--------|---------|-----------|
| grounding_line                           | empty  | shown   | shown     |
| circadian                                | shown  | dropped | dropped   |
| world                                    | shown  | dropped | dropped   |
| activity                                 | shown  | dropped | dropped   |
| ambient_noise                            | shown  | dropped | dropped   |
| affect                                   | shown  | shown   | dropped   |
| mood_hint                                | shown  | shown   | dropped   |
| relationship                             | shown  | shown   | dropped   |
| user_state                               | shown  | shown   | dropped   |
| anniversary, profile, pajama, novelty,   | shown  | shown   | shown     |
| stagnation, knowledge_gaps, belief_gaps, |        |         |           |
| agenda, axes, petname, vocal_tone,       |        |         |           |
| catchphrase, narrative, arc              |        |         |           |

The mode arg is stored on the assembler via
`PromptAssembler.set_grounding_line_mode(mode)` (called once at boot
by `SessionController` and again on any settings reload) rather than
threaded through `assemble_with_budget` — saves `TurnRunner` from
caring about a runtime knob it doesn't otherwise consume. The
suppression logic lives inline in `assemble_with_budget` keyed off
that stored value, with a defensive gate that refuses to append the
grounding block when `mode == "off"` even if a misbehaving provider
returns text.

### When to use which

- **Picking a default**: `off` until persona is retuned (the K16
  persona note is in
  [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt)
  under "Where you are right now") and at least a few sessions have
  been A/B'd against `replace`.
- **Companion-feel comparison**: flip between `off` and `replace`
  over comparable conversations and read the assistant's replies
  side by side.
- **Isolating fusion vs. trend phrasing**: `split` keeps the trend
  signals (affect "lately you've been...", relationship phase line)
  standalone, so a difference between `split` and `replace` reads
  attributes the texture change to fusing the trend slots
  specifically.
- **Debugging a regression**: revert to `off` first; the granular
  blocks are well-understood.

### How to flip the mode

- Edit `agent.grounding_line_mode` in
  [`config/default.json`](../../config/default.json) (or your
  override config) to `"off"` / `"replace"` / `"split"` and restart.
- A live settings-reload path can call
  `PromptAssembler.set_grounding_line_mode(...)` directly; the
  setter is idempotent and safe to invoke between turns.

### Verifying the flip took effect

- MCP `get_last_response_detail` after a turn — the response detail
  dict includes `provider_ms.grounding_line`. In `off` mode this
  entry is missing or zero (the SessionController-side provider
  short-circuits without invoking the renderer). In `replace` /
  `split` it's a small positive number (the renderer is template-
  driven, sub-millisecond per render).
- DEBUG-level `prompt built:` log line from
  `app.core.session.prompt_assembler` (see P2): the `providers=` count drops
  by the number of suppressed granular blocks; `slowest_provider=`
  shifts.
- The persona note doubles as a sanity gate: if Aiko starts reciting
  the time / app / mood verbatim in `replace` mode, the persona is
  the place to tune (not the renderer template).

Tests:
[`tests/test_grounding_line.py`](../../tests/test_grounding_line.py)
covers the renderer in isolation (empty / partial / full slot
combinations, weekday + period phrasing, drowsy + noise riders,
indoor vs. outdoor framing, capitalisation when relationship leads
sentence 3, user-name fallback). The K16 mode integration sits in
[`tests/test_prompt_assembler.py`](../../tests/test_prompt_assembler.py)
under `GroundingLineModeTests`: `off` keeps every granular block,
`replace` drops eight, `split` drops only situational, invalid mode
clamps to `off`, the grounding line is dropped under `aggressive=True`
even in `replace`, and `provider_ms["grounding_line"]` lands in P2
telemetry.

---

## K18. Topic stagnation detector

The inverse of K6: instead of firing on a single divergent turn, K18
fires when the rolling per-turn distance to the K6 centroid stays
*low* across a window — the conversation has been circling the same
ground for a while and Aiko may want to either acknowledge the
rhythm, take a soft pivot, or offer a real off-ramp. Picked up
specifically as the "we've been on this for ten messages, do you
want to actually wrap or keep going?" cue companion-AI literature
keeps flagging.

Implemented as a **sibling** of `NoveltyDetector` rather than an
extension of it: a new
[`TopicStagnationDetector`](../../app/core/conversation/topic_stagnation.py) is
a pure streak counter — no embedder, no rag_store, no per-user
state — that consumes the per-turn distance K6 already computed.
To make that consumption cheap, `NoveltyDetector` was extended with
two tiny additive attributes (`last_distance` and `last_band`) that
get reset at the top of every `detect()` and populated on every
code path that actually measured (normal + cooldown turns; warmup
and short-text turns leave them `None`). K18 reads those off the
K6 detector during prompt assembly without re-embedding anything.

The detector keeps a `collections.deque[float]` of size
`stagnation_window` (default 6) and bands the rolling mean:
`mean < stagnation_strong_threshold` (default 0.10) → `strong_lull`,
`mean < stagnation_mild_threshold` (default 0.18) → `mild_lull`,
otherwise silent. Three suppression gates keep the cue rare:
warmup until the deque is full, `stagnation_cooldown_turns`
(default 4 — longer than K6's because lulls are by nature
drawn-out) after each fire, and a
`stagnation_post_novelty_suppression_turns` (default 3) window
right after K6 fires so a fresh topic shift doesn't immediately
read as "we've been on this for a while". A `distance=None` from
K6 (short text / warmup / embed failure) is treated as "no
measurement" and does not advance the streak.

The signal surfaces through a new `stagnation` inner-life provider
on [`PromptAssembler`](../../app/core/session/prompt_assembler.py) — same
shape as the K6 `novelty` provider, dropped under
`aggressive=True`. Provider order matters: `novelty` runs first so
its `last_distance`/`last_band` are fresh when the stagnation
provider reads them. The rendered "Heads-up: you've been circling
…" / "Heads-up: this thread has been pretty looped …" line lands
in the system prompt immediately after `novelty_block`, clustering
both reaction cues together. Persona guidance in
[`aiko_companion.txt`](../../data/persona/aiko_companion.txt)
("Same topic for a while", added right after "Surprise and
novelty") teaches Aiko to take a soft pivot on the mild band, to
either deepen the thread or offer a real off-ramp on the strong
band, and explicitly says the absence of the cue is also a signal
— a focused conversation is fine.

Settings live on `AgentSettings` (`topic_stagnation_enabled`,
master switch) and `MemorySettings` (`stagnation_window`,
`stagnation_mild_threshold`, `stagnation_strong_threshold`,
`stagnation_cooldown_turns`,
`stagnation_post_novelty_suppression_turns`), mirrored in
[`config/default.json`](../../config/default.json). Defaults are
intentionally conservative; calibration is the kind of thing only
live testing settles. The detector logs one INFO line per scoring
turn (`topic-stagnation: mean=%.3f band=%s window=%d`) — grep via
MCP `tail_logs(module_contains="topic_stagnation")`.

Tests:
[`tests/test_topic_stagnation.py`](../../tests/test_topic_stagnation.py)
(warmup, band thresholds, misordered-threshold safety, cooldown,
post-novelty suppression, `distance=None` handling, render copy
including `{user_name}` interpolation), plus K18 hooks added to
[`tests/test_novelty_detector.py`](../../tests/test_novelty_detector.py)
(`last_distance`/`last_band` populated on normal + silent +
cooldown turns, left `None` on warmup and short-text), and a new
provider-slot block in
[`tests/test_prompt_assembler.py`](../../tests/test_prompt_assembler.py)
(stagnation block lands after novelty, silent when empty, dropped
under aggressive, exceptions swallowed, `user_text` is forwarded
to the provider).

Out of scope (deferred): `ProactiveDirector` bias on
`strong_lull`, settings-UI controls for the thresholds, and a
per-cluster "lulled on topic A but not B" variant — that one
needs the K9 topic graph first.

---

## Temporal memory awareness (schema v10)

Gives every memory three new fields — `event_time`, `temporal_type`
(`past_event` / `current_state` / `future_plan` / `recurring` /
`timeless`), and `relevance_until` — so Aiko can tell the difference
between "Jacob is in Tokyo this week" and "Jacob went to Tokyo
last year". Schema migration is additive in
[`chat_database.py`](../../app/core/infra/chat_database.py); the `Memory`
dataclass and `RagStore` carry the new fields with a
join-only strategy in LanceDB so we don't reindex existing
embeddings. The memory extractor anchors a `today` reference,
extracts the temporal fields with the rest of the memory, and
derives `relevance_until` server-side. Retrieval annotates memory
bullets with a temporal suffix (`(last year)`, `(planned for Friday)`,
`(ongoing)`) and filters out expired memories.

The `MemoryDecayWorker` got a reclassification pass that nudges
`future_plan` -> `past_event` once `event_time` is in the past, and
a new
[`FollowUpWorker`](../../app/core/proactive/follow_up_worker.py)
covers proactive follow-ups for overdue `future_plan` memories.
Persona rules in
[`aiko_companion.txt`](../../data/persona/aiko_companion.txt) teach
Aiko to respect the temporal tags without becoming pedantic about
them. Tests: `tests/test_memory_extractor_temporal.py`,
`tests/test_follow_up_worker.py`,
`tests/test_memory_decay_temporal.py`,
`tests/test_rag_retriever_temporal.py`.

**Follow-up redesign — cue producer, not verbatim speaker (the K34
pattern).** The original `FollowUpWorker` wrote a line straight into
[`PreparedNudgeStore`](../../app/core/proactive/prepared_nudge.py),
which the `ProactiveDirector` speaks **verbatim**. That leaked an
internal directive ("Jacob mentioned earlier: '...' — if the
conversation drifts there, ask how it went. Don't open with it.") into
the chat as if it were Aiko's reply. The worker is now a silent **cue
producer** (mirroring K34 `ForwardCuriosityWorker`): when a
user-mentioned `future_plan`'s `event_time` passes (lookahead 30 min /
lookback 4 h window), it drafts `{at, plan, clock, question,
source_id, event_time}` into the `aiko.follow_up_cues` kv ring — `plan`
is a deterministic second-person reshaping of the memory ("you were
planning to take a bath and watch anime …"), `question` is an optional
natural retrospective phrasing drafted on the local worker LLM (safe
empty fallback). The new
[`_render_follow_up_block`](../../app/core/session/inner_life_providers_mixin.py)
inner-life provider folds the newest unseen cue into the next turn's
system prompt as one private "Earlier (~`clock`) `plan` — that time has
passed; if it fits, you can gently ask how it went; no need to open
with it" hint, watermark-gated (`follow_up.last_surfaced_at`) so it
surfaces once. It is **time-anchored and independent of the
`_gap_cue_surfaced` family** (does not read or set it) — a concrete
"their plan just happened" beat is worth a line even alongside a
generic gap cue. Aiko phrases the check-in herself; the cue is never
spoken. Block sits in the T6 detector tier of `prompt_assembler.py`
immediately after `forward_curiosity_block`. Settings:
`agent.follow_up_enabled` (master, default on) +
`memory.follow_up_journal_max` (default 8). MCP debug:
`get_follow_up_state`, `force_follow_up_draft(source_id="")`,
`force_follow_up_surface()` — repro is
`force_follow_up_draft(source_id=<future_plan id>)` ->
`force_follow_up_surface()` -> `send_message(skip_tts=true)` -> confirm
the "Earlier ... ask how it went" line in
`get_last_response_detail.system_prompt`. Log lines `follow_up cue
primed` (producer) / `follow-up cue fire:` (consumer) for
`tail_logs(module_contains="follow_up")`. Tests:
`tests/test_memory_temporal.py::TestFollowUpWorker` (kv ring, not the
prepared-nudge slot), `tests/test_follow_up_provider.py`.

---

## G2. Schedule-learning worker

Idle worker that buckets `messages.created_at` (user messages
only) by local-timezone weekday/weekend × hour-of-day over a
rolling window, identifies dominant clusters, and writes a human
phrase ("weekday evenings", "weekend afternoons") into the
`usual_hours` field on `UserProfile`. No LLM, no embedder — just
SQL + Python bucketing. Confidence scales with sample size, and
writes are skipped when the inferred phrase is unchanged.
Lives in
[`app/core/infra/schedule_learner.py`](../../app/core/infra/schedule_learner.py),
registers with the shipped `IdleWorkerScheduler`. The new field
is allow-listed in
[`app/core/infra/user_profile.py`](../../app/core/infra/user_profile.py)
`PROFILE_FIELDS` so the LLM `UserProfileWorker` is also aware of
it. Tests: `tests/test_schedule_learner.py`.

---

## K3. Routine / ritual awareness

Second pass inside the same `ScheduleLearner` that names recurring
slots ("Sunday-morning chats", "Friday-evening wind-downs") and
writes them into a new `routines` field on `UserProfile`. Where G2
counts total volume per `(daytype, bucket)`, K3 counts *distinct
ISO weeks* per `(weekday, bucket)` so a slot only qualifies once
it has actually recurred across multiple weeks (default: ≥3
distinct weeks AND ≥30% of the rolling window). Naming is
deterministic via a 28-entry `_RITUAL_LABELS` dict (Mon-Sun × 4
hour-buckets); the rendered phrase is comma-joined, capped at 240
chars to fit `ProfileEntry.value`, and idempotent (re-detection of
the same slot short-circuits the upsert). Confidence is the max
recurrence density across chosen cells. Surfacing is passive: the
field joins the rendered profile block alongside `usual_hours`,
and a persona note in
[`aiko_companion.txt`](../../data/persona/aiko_companion.txt)
teaches Aiko to lean into a matching rhythm only when the moment
actually fits — never as a list, never as a calendar reminder.
Settings:
[`AgentSettings.routine_detection_enabled`](../../app/core/infra/settings.py)
plus `MemorySettings.routine_min_touches` /
`routine_min_share` / `routine_max_active`. Tests:
`tests/test_schedule_learner.py::RoutineDetectionTests` plus a
`PROFILE_FIELDS` assertion in `tests/test_user_profile.py`.

---

## G3. Idle curiosity worker

Picks the oldest unresolved `open_question` memory during idle,
runs it through
[`fact_check_privacy.scrub_claim_for_search`](../../app/core/memory/fact_check_privacy.py)
to produce a safe query, calls `web_search`, distils a concise
JSON answer (`{answer, confidence}`) via Ollama, and stores the
result as a `curiosity_finding` memory linked back to the source
question. Source `open_question` rows are stamped with
`curiosity_resolved_at` / `curiosity_inconclusive_at` /
`curiosity_skipped_at` metadata so a question is never re-processed
in a tight loop. The worker shares
[`FactCheckRateLimiter`](../../app/core/memory/fact_check_rate_limiter.py)
shape but with a separate `state_key="idle_curiosity.rate_state"`
so its budget doesn't compete with the fact-checker's. Lives in
[`app/core/proactive/idle_curiosity_worker.py`](../../app/core/proactive/idle_curiosity_worker.py).
[`rag_retriever.py`](../../app/core/rag/rag_retriever.py) appends a
`(curiosity)` suffix on retrieved findings, and a Memory-section
rule in [`aiko_companion.txt`](../../data/persona/aiko_companion.txt)
teaches Aiko to surface them as "I was reading about X — turns
out..." rather than reciting them as bare facts. Tests:
`tests/test_idle_curiosity_worker.py` plus the new state-key
independence test in `tests/test_fact_check_rate_limiter.py`.

---

## P1. Per-turn embed budget + timing

Single shared
[`Embedder`](../../app/llm/embedder.py)
serves three live consumers per turn —
[`RagRetriever`](../../app/core/rag/rag_retriever.py) embeds
`"ctx || query"`, K6
[`NoveltyDetector`](../../app/core/conversation/novelty_detector.py) embeds the
raw user message, K18
[`TopicStagnationDetector`](../../app/core/conversation/topic_stagnation.py)
piggybacks on K6's distance — plus the async
[`MessageIndexer`](../../app/core/rag/message_indexer.py) on the
background thread. Two HTTP `/api/embeddings` round-trips per turn
is the common case once novelty + RAG are both on. Before P1 there
was no per-turn count or wall time, so "my turn felt slow" couldn't
be attributed to embeds without a custom log dive.

The embedder now exposes a tiny per-thread budget API:
`begin_turn()` resets a thread-local counter pair on the calling
thread, every cache-miss `embed()` call adds its measured wall time
+ one increment, and `end_turn()` returns the
`(calls, ms)` tuple and clears state. LRU cache hits don't count as
calls (they're free). The counters are *thread-local* on purpose —
`MessageIndexer` shares the same `Embedder` instance from a
background worker, and we don't want its async writes polluting the
turn thread's accounting; threads that never call `begin_turn` see
`active=False` and skip all accounting.
[`TurnRunner.run`](../../app/core/session/turn_runner.py) brackets each
turn with begin/end, stamps the result onto
`PromptTelemetry.embed_calls` / `embed_ms` right before the
`turn done:` INFO log, and the public `run()`'s `finally` calls
`end_turn` again as a defensive cleanup so an exception mid-flow
can't leak counter state into the next turn.

The headline INFO line gained four new fields
(`embed_calls=N embed_ms=N assemble_ms=N rag_lookup_ms=N`) and the
[`SessionController`](../../app/core/session/session_controller.py) metrics
dict carries them through to
[`get_last_response_detail`](../../app/mcp/server.py) so MCP can
grep regressions over time. Tests:
`tests/test_embedder.py` (begin/end/peek/double-begin/cache-hit
isolation/thread-isolation), plus
`tests/test_turn_runner_telemetry.py::EmbedTurnBoundaryTests`
(stamping, cleanup-on-raise, no-embedder fallback, early-return
edge case).

Out of scope (deferred): substring-match de-duplication across the
RAG `"ctx || query"` and K6 `query`-only strings — different
strings produce different vectors, so the obvious win is "make K6
and RAG use the same embedding when the second string is a
substring of the first", which needs more design than this
observability slice could carry. Tracked as a follow-up.

---

## P2. Prompt-build phase telemetry

`turn done:` already logged `rag_prefetch=` / `prebuild=` slice-cache
events but not the wall time of RAG retrieval, individual
inner-life providers, or the total assemble. The DEBUG
`prompt built:` line counted only a hardcoded ten inner blocks —
the eleven that have shipped since
(belief-gaps, novelty, stagnation, activity, anniversary, axes,
knowledge-gaps, …) were invisible. A regression in any of them
couldn't be attributed without instrumenting the suspect by hand.

[`PromptAssembler`](../../app/core/session/prompt_assembler.py) now wraps
every provider call through a `_safe_provider(timing_sink=…)` /
`_timed_phase` pair into a flat
`provider_ms: dict[str, float]` keyed by the provider name. The
RAG lookup phase (prefetch lookup + live retrieval + legacy
fallback) is timed into `rag_lookup_ms`; the entire
`assemble_with_budget` body is timed into `assemble_ms`. All three
join `PromptTelemetry` (and `as_dict()` so JSON consumers see the
same shape), get re-emitted on the
`turn done:` INFO line, and propagate via the SessionController
metrics dict to `get_last_response_detail`. The DEBUG
`prompt built:` line dropped the legacy 10-block counter and now
emits `providers=N provider_ms_total=N slowest_provider=name:ms`
derived from the live timing dict, so adding a future provider
(e.g. K17 clarification-repair) automatically lands in the
headline without a code change.

A timed provider that *raises* still records its bucket — the
operator wants to see "novelty took 3ms and exploded", not "novelty
silently disappeared from the telemetry". Tests:
`tests/test_prompt_assembler.py::PhaseTelemetryTests` (empty when
nothing wired, populated for each live provider, round-trip via
`as_dict`, `assemble_ms` covers the full build, P1 fields stay
zero on direct assemble) and
`tests/test_prompt_assembler.py::FailingProviderTimingTests` (raise
still records).

---

## P3. Cheap slice-cache validation (skip two SQLite reads on a hit)

`assemble_with_budget` short-circuited on a slice-cache hit, but the
validation path still ran `get_messages` (the full recent-window
fetch) + `get_latest_summary` on **every** typed turn just to recompute
the cache key, even when nothing had changed. P3 adds a cheap head
signature: [`ChatDatabase.get_history_head`](../../app/core/infra/chat_database.py)
returns `(max_message_id, message_count, summary_signature)` via two
scalar aggregate queries, and the assembler stores it alongside the
slice-cache entry (`_slice_head_sig`). On a turn the new
[`_fast_slice_signature`](../../app/core/session/prompt_assembler_helpers_mixin.py)
(head + persona/self-image mtime + last reaction + window + aggressive)
is compared first; when it matches, the cache is trusted **without
touching `get_messages` / `get_latest_summary` at all**. The signature
is a conservative superset of the full cache key — any new/deleted
message moves `max_id`/`count`, any summary rewrite moves the summary
signature — so a match can never serve stale slices. On a miss the
existing full validation runs unchanged and re-stamps the signature.
Tests: `tests/test_listening_window.py::StaticSliceCacheTests`
(`test_hit_skips_get_messages_read`, `test_new_summary_invalidates_cache`)
and `tests/test_chat_database.py::TestHistoryHead`.

---

## P4. RAG memory-hit batch lookup (`get_many`)

`RagRetriever.retrieve` applied pin / tier / confidence / temporal /
goal-alignment scoring by calling `memory_store.get(id)` **once per
Lance hit** (up to `per_source_top_k` times), each acquiring the mirror
lock. P4 adds [`MemoryStore.get_many`](../../app/core/memory/memory_store.py)
(one lock acquisition, `{id: Memory}`); the retriever batch-fetches all
hit ids once before the scoring loop and reads from the dict. Falls
back to the per-hit `get` for duck-typed stores that don't expose
`get_many`. Tests: `tests/test_memory_store_metadata.py::TestGetMany`.

---

## P18. Streaming accumulator no longer O(n²)

The stream loop did `accumulator.append(delta)` then
`full = "".join(accumulator)` **per token** — O(n²) work + allocation
churn on long replies. P18 grows a single running `full += delta`
string instead (CPython amortises this to linear), with `full_raw =
full` after the loop. Byte-identical output; the reaction-tag parse and
streaming-safe-prefix logic (which the recent streaming-bug fix
depends on) are untouched. The further reaction-parse micro-opt from
the sketch was deliberately *not* taken — the `^\s*\[\[reaction:…\]\]\s*\n*`
regex greedily consumes trailing whitespace, so freezing a tag offset
would risk re-introducing the off-by-newline streaming bug.
Covered by `tests/test_turn_runner_mood_fallback.py`.

---

## P15. (Invalid) One user-text embed per turn — already handled by the LRU

Marked **invalid** after validation, not implemented. The premise was
that K6 novelty, F2 `pick_relevant`, and K29 opinion-injection each
fire a separate HTTP `/api/embeddings` round-trip for `user_text`
(50–200 ms × 3). In reality all three embed the *identically-normalised*
stripped `user_text`, and [`Embedder`](../../app/llm/embedder.py) has a
256-entry LRU keyed by `sha1(model + text)` — so all but the first
collapse to sub-microsecond cache hits. The hot path is therefore
already at the 2-embed steady state P15 targeted (RAG's contextual
`ctx || query` is the only other distinct vector). The only remaining
win — substring de-dup of the RAG `ctx || query` string against the
raw `user_text` — is the harder follow-up already flagged as
out-of-scope under P1, not the "thread one vector everywhere" refactor
P15 described.

---

## P21. K29 borderline gate moved off the hot path

When the opinion-injection heuristic returned `borderline`, the LLM
YES/NO verdict ran **synchronously inside `assemble_with_budget`** —
0.5–8 s of added TTFT for a one-line cue, before any token streamed.
P21 defers it: `opinion_injection_detector.detect(defer_borderline=True)`
returns the borderline candidate as a `PENDING` result **without
calling the LLM**; the provider stashes it and stays silent that turn.
The post-turn hook
[`_resolve_opinion_injection_pending`](../../app/core/session/inner_life_part3.py)
runs the rate-limited verdict after streaming completes, and a
confirmed contradiction arms a one-shot cue that renders on the **next**
turn (the stance hasn't changed in those seconds, so the lag is
invisible). `definite` hits still fire inline — they never needed the
LLM. Cooldown / per-session cap / K59 tease-bank arm only on the
confirmed fire. Pending state clears on session switch / clear. The hot
path now only ever pays the pure-Python heuristic. Tests:
`tests/test_opinion_injection_detector.py` (`defer_borderline` returns
PENDING / definite-still-fires / require_definite-overrides) and
`tests/test_opinion_injection_provider.py::DeferredBorderlineTests`
(arm-not-cooldown, no-ollama-stays-definite, resolver YES/NO/rate-limited,
next-turn one-shot render).

---

## P8. Idle-worker queue visibility + multi-worker drain

`IdleWorkerScheduler` was capped at one worker per 60 s tick.
With ~10+ workers registered (decay, promotion, schedule,
fact-check, conflict, belief, follow-up, idle-curiosity, …) the
loser of a tie waited a full minute, and a single misconfigured
cadence could quietly starve the backlog with no MCP-visible signal
beyond rummaging through `last_run_at` rows. The natural quiet
window between turns — Aiko's 10-30 s of TTS plus the user's typing
time before the next submit — was being thrown away.

The scheduler now drains as many due workers as fit into a per-tick
wall-time budget (`tick_budget_ms`, default 3000). Workers are
sorted oldest `last_run_at` first; each worker's
[`avg_duration_ms`](../../app/core/proactive/idle_worker.py) (EMA, alpha=0.3
on `IdleWorkerRecord`) is the cost estimate. Anti-starvation always
admits the most-overdue ready worker even if its estimate exceeds
the remaining budget, so a tight budget on a slow machine still
makes progress instead of looping forever. A hard
`max_per_tick` cap (0 = unlimited) is available for operators who
want to clamp tick log volume on heavy backlogs;
`max_per_tick=1` reproduces the legacy single-worker behaviour.

Per-run wall time is folded into the EMA on success; failures bump
`error_count` (cumulative, separate from `last_error` which gets
cleared on the next clean run). The scheduler emits one structured
INFO line per non-empty tick:

```
idle_workers tick: ran=3 due=5 skipped_budget=2 queue_after=2
                   tick_ms=472 budget_ms=3000 names=memory_decay,memory_promotion,fact_checker
```

A new MCP tool `get_idle_workers_status` returns the enriched
view: scheduler config (`wake_seconds`, `tick_budget_ms`,
`max_per_tick`, `quiet`) plus a `workers` list sorted most-overdue
first. Each row carries `last_run_at`, `next_due_at`,
`overdue_seconds` (positive = waiting), `avg_duration_ms`,
`last_duration_ms`, `total_duration_ms`, `run_count`,
`error_count`, `last_error`. The legacy `inspect_idle_workers`
tool stays for quick checks; reach for `get_idle_workers_status`
when you want to answer "which workers are starving and why?".

Settings: [`MemorySettings.idle_worker_tick_budget_ms`](../../app/core/infra/settings.py)
+ `idle_worker_max_per_tick`, mirrored in
[`config/default.json`](../../config/default.json). Tests:
`tests/test_idle_worker_p8.py` (EMA shape, multi-worker drain,
anti-starvation under tight/zero budgets, oldest-first ordering,
error counter, `get_status` shape with never-run vs. run workers,
summary log content), plus the legacy
`tests/test_idle_worker_scheduler.py` updated for the new
multi-worker default and a `max_per_tick=1` regression.


## P12. Bulk memory-mirror on startup

`MemoryStore.migrate_to_rag` re-pushed every SQLite memory into
LanceDB on every boot via a per-row
[`RagStore.add_memory`](../../app/core/rag/rag_store.py) loop. Each
call did its own `delete` + `add` under the write lock, so 135
memories meant 270 LanceDB write ops with manifest churn between
each. On Windows that landed at ~525 ms per op, ~71 s total — a
visible startup hang between `RagStore ready` and `RAG: mirrored
N existing memories into LanceDB` in the log, and one that
scaled linearly with memory count.

The mirror now goes through a new
[`RagStore.add_memories_bulk`](../../app/core/rag/rag_store.py)
batch path: one `delete` with an `id IN (...)` predicate plus one
`add(rows)` per chunk. With `chunk_size=500` (the default) a
typical install lands all rows in a single chunk — two write ops
total instead of 2*N. `migrate_to_rag` builds the records list
once, drops embedding-less / blank-content rows up front (same
implicit filter the per-row path had), and now wraps the whole
bulk call in `try`/`except` so a misbehaving LanceDB doesn't
abort startup. The "RAG: mirrored N existing memories into
LanceDB" log line is preserved.

Empirically: ~71 s -> ~1-2 s on the same 135-memory install,
and the cost stays roughly flat as the memory count grows
because LanceDB writes a single fragment per `add(batch)` call
regardless of batch size. The bulk path also escapes apostrophes
in record ids defensively before splicing into the SQL predicate.

Tests:
[`tests/test_rag_store.py::BulkAddMemoriesTests`](../../tests/test_rag_store.py)
covers new-rows, upsert-existing, mixed batches, the
`chunk_size` boundary, empty-content / missing-embedding skipping,
and id-with-apostrophe escaping.
[`tests/test_memory_migrate_bulk.py`](../../tests/test_memory_migrate_bulk.py)
pins the migration shape: one `add_memories_bulk` call per
boot, `add_memory` never touched, no-embedding rows filtered out
before the bulk batch is built, `None` rag store is a no-op, and
a raised bulk exception returns 0 instead of crashing.


## H1 + K4. Conversation-arc self-tag + dialogue-act tagging (schema v13)

H1 closes the loop on the conversation-arc tracker that already shipped
in [`app/core/conversation/conversation_arc.py`](../../app/core/conversation/conversation_arc.py)
and K4 adds the user-side cousin per turn. One schema migration adds two
nullable columns to `messages` (`arc`, `dialogue_act`); the arc taxonomy
trims to a companion-friendly six (drop `debug` / `deep_dive`, add
`silly`). H1: a new `[[arc:NAME]]` self-tag (parsed in
[`response_text_service.py`](../../app/core/services/response_text_service.py)
mirroring `[[moment:]]` / `[[agenda:]]`) routes through
`ArcStore.set_from_self_tag` at confidence `0.85` — the new middle rung
on the ladder `regex 0.5 < self-tag 0.85 < smoother 0.95`. The estimator
hot-path guard now refuses to overwrite a self-tag-or-better prior. K4:
new [`app/core/conversation/dialogue_act_tagger.py`](../../app/core/conversation/dialogue_act_tagger.py)
mirrors the [`promise_extractor`](../../app/core/memory/promise_extractor.py)
shape (regex hot path inline + LLM cold path via the speaking-window
scheduler) and tags every user turn into one of `question / story /
vent / banter / planning / chitchat`. Both signals feed
[`rag_retriever.py`](../../app/core/rag/rag_retriever.py) (`+0.03` per match,
combined cap `+0.05`) and tighten
[`proactive_director.py`](../../app/core/proactive/proactive_director.py)
eligibility (suppress nudges on a `vent` turn; loosen cooldown on
`silly` / `playful` arcs). Tests:
[`tests/test_arc_self_tag.py`](../../tests/test_arc_self_tag.py),
[`tests/test_dialogue_act_tagger.py`](../../tests/test_dialogue_act_tagger.py),
[`tests/test_chat_database_migration.py`](../../tests/test_chat_database_migration.py),
[`tests/test_rag_retriever_act_arc_boost.py`](../../tests/test_rag_retriever_act_arc_boost.py).


## Aiko expressive speech (Pocket-TTS prosody overlay)

Pocket-TTS doesn't accept SSML, so the rollout instead exhausted the
expressive headroom already in the stack: five layers, all CPU, no
new model or library. Layer 1 wired the dormant knobs --
`assistant.tts_length_scale` (a user-facing pacing slider) gained a
real `set_length_scale` on
[`PocketTtsService`](../../app/tts/pocket_tts_service.py) that
divides into the final speed; `AmbientNoiseTracker.tts_volume_db_offset`
now flows through `CadenceContext` into a new `gain_db` kwarg on
`speak_async` and is applied to the Int16 PCM before
`_pcm_listener` emits frames; `model.temp` is mutated under the
service lock per generation against a small reaction-to-temp table
(`serious / wistful / sad / cry → -0.10`, `excited / playful /
surprised → +0.10`) and reset back to baseline. Layer 2 added
`TtsQueue.enqueue_silence(ms)` (cap 1500 ms) plus `speak_silence_async`
on the engine so `ProsodyParams.pause_before_ms` /
`pause_after_ms` produce actual silent PCM gaps instead of just
punctuation rewrites. Layer 3 introduced a per-sentence
`[[prosody:LABEL]]` family (`whisper / soft / slow / fast / firm`)
parsed in
[`response_text_service.py`](../../app/core/services/response_text_service.py)
and consumed by
[`analyze_sentence`](../../app/core/voice/cadence.py) -- each label maps
to a small overlay on the reaction-derived `ProsodyParams`
(`speed_mult`, `gain_db_delta`, `pause_before`). Layer 4 expanded
the earcon palette in
[`app/audio/earcons.py`](../../app/audio/earcons.py) with
`chuckle / soft_sigh / sharp_gasp / breath / mm` and added a
cadence auto-sprinkle rule (cooldown-gated 25 s, ~30% fire rate,
gated by `agent.earcon_auto_sprinkle`) that prepends
`breath` / `soft_sigh` on opener-style melancholy / wistful / sad /
cry / concerned sentences. Layer 5 widened the global speed clamp
from ±8% to ±12% with per-reaction sub-caps (`cry` 0.88 floor,
`tired` 0.90, `sad` / `melancholy` 0.91, `excited` 1.12 ceiling,
`surprised` 1.10) so the loudest / quietest reactions can stretch
without dragging the rest of the table along; a manual ear-test
helper at
[`tools/tts_speed_ab.py`](../../tools/tts_speed_ab.py) renders the
calibration phrase at every `_REACTION_SPEED` value to WAV for
listening at the new edges. Persona update teaches the
`[[prosody:X]]` vocabulary alongside the existing `[[reaction:X]]`
mood label as orthogonal axes (one mood, separate vocal delivery).
Tests:
[`tests/test_pocket_tts_dormant_knobs.py`](../../tests/test_pocket_tts_dormant_knobs.py),
[`tests/test_tts_queue_silence.py`](../../tests/test_tts_queue_silence.py),
[`tests/test_prosody_tag_parser.py`](../../tests/test_prosody_tag_parser.py),
[`tests/test_cadence_prosody_overlay.py`](../../tests/test_cadence_prosody_overlay.py),
[`tests/test_earcon_auto_sprinkle.py`](../../tests/test_earcon_auto_sprinkle.py).

**Calibration follow-up: Layer 1c + Layer 5 are gated OFF by default.**
Empirical listening tests on the active voice (Aiko's tuned safetensors)
showed that Pocket-TTS is sensitive enough to both `model.temp`
excursions and `sample_rate`-based speed scaling that even small
per-reaction deltas produce audible artefacts -- a "hall echo" / pitch
wobble on temperature changes, and a stronger "her voice keeps changing"
voice-swap perception on speed changes (because varispeed couples speed
and pitch, so a 10% faster excited sentence is also ~1.6 semitones
higher). Two new opt-in gates land the layers safely without forcing
every voice to inherit the artefacts:
* [`agent.tts_runtime_temp_enabled`](../../app/core/infra/settings.py)
  (default `False`) gates the per-reaction `_REACTION_TEMP_DELTA`
  table in
  [`PocketTtsService._resolve_runtime_temp`](../../app/tts/pocket_tts_service.py).
  When OFF, every call uses `tts.pocket_tts_temp` baseline.
* [`agent.tts_runtime_speed_enabled`](../../app/core/infra/settings.py)
  (default `False`) gates both the per-reaction sub-cap table AND
  the cadence layer's per-sentence `speed_hint` in
  [`PocketTtsService.speak_async`](../../app/tts/pocket_tts_service.py).
  When OFF, every sentence pins to `1.0×` before the user's
  pacing slider (`assistant.tts_length_scale`) divides in.

The user's static pacing slider is honoured regardless of either gate
(it's a deliberate global knob, not per-sentence affect drift).
Earcons, real timed pauses, per-sentence prosody labels' `gain_db` /
`pause` overlays, and the auto-sprinkle rule all keep working with both
gates off -- they're orthogonal to pitch and don't trigger the same
artefacts. Both gates are opt-in once a voice has been listened-tested
through [`tools/tts_speed_ab.py`](../../tools/tts_speed_ab.py) and the
ear-test phrase still reads naturally at the proposed deltas. The
`_REACTION_TEMP_DELTA` table itself was halved from the original
`±0.10` to `±0.05` after the first round of tester feedback so the gate
flipped back ON also lands in a calmer band. Tests:
[`tests/test_pocket_tts_speed.py`](../../tests/test_pocket_tts_speed.py)
adds `RuntimeSpeedGateOffTests` covering "default OFF pins to 1.0×",
"reaction is ignored", "caller `speed=` is ignored", "length-scale still
applies", and "toggle via `set_runtime_speed_enabled` flips the
behaviour back on";
[`tests/test_pocket_tts_dormant_knobs.py`](../../tests/test_pocket_tts_dormant_knobs.py)
covers the matching temp-gate path.

---

## Aiko response variability — anti-rut layer

Diagnosed against ~120 recent assistant messages: **86.7% of replies
contained a question**, top 3 opening words (`yeah` / `that's` / `oh`)
covered **~39% of openings**, **17.5% contained the literal "speaking
of"** (verbatim from the persona's example phrase), **31.7%** followed
the same statement-then-question template in the last two sentences,
and the average reply was **52 words / 4.9 sentences** vs the persona's
"1-3 sentences" target. The shape was being seeded by (a) literal
example phrases in the persona acting as attractors and (b) zero
feedback loop telling Aiko she'd been ruting -- a bigger model is
*more* faithful to the prompt's implicit shape, not less, which is why
9b → 27b didn't move the needle for the user. Two layers shipped to
fix it without changing model or prompt budget:

* **Layer 1 -- persona surgery** in
  [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt).
  Removed the `("speaking of that thing with your project last
  week...")` literal example (the 17.5% parrot source) and the
  enumerated `("oh!", "wait, no", "hmm", "okay but")` reaction list
  in favour of abstract guidance ("vary your openers", "find your own
  way in"). Tightened the length rule to "default to 1-2 sentences,
  3 is the upper bound" (the model was averaging 5). Added an
  explicit anti-question rule: "at least 1 in 3 turns end on a
  thought, not a question -- never stack two questions back-to-back."
  Added a "Don't parrot" rule against restating what the user just
  said before responding. New "Style patterns I'm in" section pairs
  with the Layer 2 cues: tells Aiko how to react to an opener / question
  / length nudge from the tracker without naming it out loud.
* **Layer 2 -- `AikoStylePatternTracker`** in
  [`app/core/persona/aiko_style_tracker.py`](../../app/core/persona/aiko_style_tracker.py).
  Pure rolling-window detector mirroring K6/K18: no embedder, no LLM,
  per-turn cost is a deque append plus a few counter scans. Three
  banded signals evaluated in priority order:
  - `opener_rut` -- same opener used ≥4 times in last 10 turns OR
    top-2 opener share ≥60%.
  - `question_saturation` -- question-end rate over last 8 turns ≥75%
    OR avg questions/turn ≥1.5.
  - `length_sprawl` -- avg word count over last 8 turns ≥50.0.
  Each band has its own cooldown counter (default 5 turns) so an
  opener-rut nudge doesn't mask a later question-saturation cue, and
  the same band doesn't re-fire on every turn. Warmup gate (default
  6 recorded turns) keeps cold-start silent. All thresholds live on
  [`AgentSettings`](../../app/core/infra/settings.py) (`style_tracker_*`)
  and in [`config/default.json`](../../config/default.json) so
  calibration moves without code changes.

Wiring follows the K6/K18 idiom verbatim:
[`SessionController.__init__`](../../app/core/session/session_controller.py)
instantiates the tracker right after `TopicStagnationDetector`;
[`InnerLifeProvidersMixin._render_style_pattern_block`](../../app/core/session/inner_life_providers_mixin.py)
calls `tracker.detect()` and renders the matching cue;
[`PromptAssembler.set_inner_life_providers`](../../app/core/session/prompt_assembler.py)
gains a `style_pattern` slot; the resulting block is appended to
`system_parts` immediately after the K18 stagnation block so all three
"Heads-up..." cues cluster together (and all three drop in aggressive
mode for budget reasons). The post-turn pipeline
([`PostTurnMixin._post_turn_inner_life`](../../app/core/session/post_turn_mixin.py))
feeds the tracker the *stripped* assistant text (post `strip_all_meta_tags`)
so we measure spoken content, not raw model output. Tests:
[`tests/test_aiko_style_tracker.py`](../../tests/test_aiko_style_tracker.py)
(21 cases) covers feature extraction edges, warmup gating, each band
firing, the priority order (opener > question > length), per-band
cooldown rotation, the no-settings-stub path, and the render copy for
each band.

---

## K13. Stylometric mirror (Jacob-side typing register)

The user-side half of the two-sided style loop. The anti-rut layer
(above) measures Aiko's own style and tells her to *vary*; K13
measures Jacob's style and tells her *which way* to vary. The
persona has always said "match their register" -- before this layer
that was only ever observed in the live ~10-turn history window so
the register reset every session. K13 anchors it persistently across
days. New file:
[`app/core/persona/style_signal.py`](../../app/core/persona/style_signal.py).

Five-axis rolling-window analyzer mirroring K6/K18 -- pure deque
plus a few regex scans, no embedder, no LLM. Each axis is normalised
to ``[0, 1]`` per turn and averaged across the window:

* **terseness** -- `1.0 / (1.0 + words / 8.0)` (smooth saturating
  function, high = terse, low = chatty)
* **formality** -- starts capital + ends with sentence-final
  punctuation, half-credit each
* **emoji density** -- emojis-per-word, capped at 1.0 (regex covers
  the common Unicode pictograph ranges)
* **slang density** -- closed-list casual markers per word
  (yeah/lol/idk/wanna/gonna/...) lower-cased, word-boundary matched
* **question rate** -- 1.0 when the turn ends with `?`, else 0.0

Bucketed labels (`terse` / `chatty` / `formal` / `casual` /
`emoji-heavy` / `slang-heavy` / `asks back often`) feed the prompt
block:

```
How Jacob writes lately: terse, casual, asks back often, slang-heavy.
```

Empty during warmup (< 8 user turns recorded) or when every axis
sits in the deadzone -- which is the no-signal default, so the
block costs zero on a neutral-register speaker. Unlike the K6/K18/
anti-rut cues this block is **always rendered**, including in
aggressive-mode budget pressure -- register shaping is the first
thing aggressive mode wants to preserve.

Persistence is a single JSON blob keyed by `user_id` in a new
`user_style_signal` table (`CREATE TABLE IF NOT EXISTS` migration --
no column changes needed to extend the schema later). Mirrors the
[`UserProfileStore`](../../app/core/infra/user_profile.py) pattern via the
new [`StyleSignalStore`](../../app/core/persona/style_signal.py). On boot
[`SessionController`](../../app/core/session/session_controller.py) eagerly
loads the persisted blob so the rolling window survives restart;
the lazy `warm_from_history` runs on the very first post-turn record
only when the persisted blob was empty (fresh install) so we don't
do a DB scan when we already have state.

Wiring follows the K6/K18 idiom:
[`InnerLifeProvidersMixin._render_style_signal_block`](../../app/core/session/inner_life_providers_mixin.py)
reads `analyzer.current_signal()` + `analyzer.labels_for_signal()` and
renders the line; the new `style_signal` slot on
[`PromptAssembler.set_inner_life_providers`](../../app/core/session/prompt_assembler.py)
clusters the block right after `profile_block` (it's a stable user
fact, not a per-turn cue);
[`PostTurnMixin._post_turn_inner_life`](../../app/core/session/post_turn_mixin.py)
feeds `analyzer.record_user_turn(user_text)` and UPSERTs the blob
each turn. Persona pairing -- new "How they write" subsection in
[`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt)
explains the cue (terse/chatty/casual/formal/slang/emoji/question)
and the match-don't-narrate rule; sits next to "Reading {user_name}"
since they're sibling concepts (live affect cue vs stable typing
register). All thresholds live on
[`AgentSettings`](../../app/core/infra/settings.py) (`style_signal_*`) and
in [`config/default.json`](../../config/default.json).

Optional debug tool: a new
[`get_style_signal()`](../../app/mcp/server.py) MCP tool returns the
live snapshot (per-axis means, current labels, rendered string,
warmup state, window size) for live inspection during testing.

Tests:
[`tests/test_style_signal.py`](../../tests/test_style_signal.py) (35
cases) covers per-axis feature extraction, bucketing edges (deadzone,
at-threshold), warmup gate, window roll, cross-session warm
idempotency, warm-from-history vs sequential equivalence, persistence
round-trip including malformed-row handling, the `StyleSignalStore`
SQLite UPSERT round-trip, the no-settings-stub path, and the render
copy / empty-cases.

---

## K7 + K17 + K8. Noticing-and-repair (forgetting / clarification / rupture)

Three small detectors, each independently revertable, that together
make Aiko sound less like a "perfect-recall, perfect-comprehension,
perfectly-attuned" assistant and more like a person who notices when
she's missed a beat.

### K7. Forgetting protocol — `(faded)` suffix

Stamps `memory_tier` on
[`RagHit`](../../app/core/rag/rag_store.py) during retrieval (joined from
the SQLite mirror where the score offset is already applied), then
[`RagRetriever.format_block`](../../app/core/rag/rag_retriever.py)
appends `(faded)` next to `(uncertain)` / `(curiosity)` for any hit
whose tier is `archive`. The persona "Memory" section reads the
suffix as a soft hedge ("I think you said something about X once,
ages ago — am I getting that right?") rather than a flat assertion.
Composes with the existing low-confidence cue: an archived shaky
claim now reads as `(uncertain) (faded)`, two reasons to hedge.
Tests in
[`tests/test_rag_retriever_scoring.py`](../../tests/test_rag_retriever_scoring.py)
cover all four tier buckets plus the compose-with-`(uncertain)`
ordering.

### K17. Clarification-repair — "you missed his last point"

New [`app/core/conversation/clarification_detector.py`](../../app/core/conversation/clarification_detector.py).
Per-turn regex classifier with two bands:

- **`strong`** — explicit corrections like "no that's not what I
  meant", "you misunderstood", "I meant X not Y", "wait no", "that's
  not it", "missing the point". The user is visibly steering.
- **`mild`** — softer confusion: "huh?", "wait what", "what do you
  mean", "I don't follow", "I'm confused", "doesn't make sense".

False-positive guardrails: bare "no" doesn't fire (no structural
context), "uh huh" doesn't fire (the `huh` pattern requires a `?`),
"I meant well" doesn't fire (the "I meant X not Y" pattern requires
an actual `not`). The detector returns a
`ClarificationResult(band, evidence)` where `evidence` is the
matched phrase (capped at 80 chars) so the LLM cue can quote what
tripped the detector.

[`PostTurnMixin._post_turn_inner_life`](../../app/core/session/post_turn_mixin.py)
runs the regex right after the K4 dialogue-act tagger and stashes a
hit on `SessionController._pending_clarification`.
[`InnerLifeProvidersMixin._render_clarification_block`](../../app/core/session/inner_life_providers_mixin.py)
consumes the slot on the next turn and clears it — sticky cues are
worse than missing cues here, so a render exception still resets.
[`PromptAssembler`](../../app/core/session/prompt_assembler.py) gets a new
`clarification` provider slot whose block lands in `system_parts`
right after `belief_gaps_block` and above novelty / stagnation /
style_pattern; if she missed the point, she should re-read first
and react second. NOT gated on aggressive mode (a "you missed his
point" cue is exactly what aggressive mode wants to keep).

### K8. Affect rupture-and-repair — "their mood just dipped"

New [`app/core/affect/affect_rupture_detector.py`](../../app/core/affect/affect_rupture_detector.py).
Cheapest possible detector: subtract two scalars and reaction-
filter. Computes `prior_valence - current_valence` from the
existing pre/post snapshots
[`PostTurnMixin._post_turn_inner_life`](../../app/core/session/post_turn_mixin.py)
already takes around `AffectUpdater.apply_turn`. Fires when:

1. The drop exceeds `rupture_valence_drop_threshold` (default 0.12 —
   the `AffectUpdater._ALPHA = 0.35` smoothing means a per-turn
   change of ≥0.12 is a real shift, not noise), AND
2. Aiko's last reaction was *not* in `DEFAULT_EXCLUDED_REACTIONS`
   (`concerned`, `gentle`, `sad`, `calm`, `thoughtful`, `quiet`).
   These are reactions to *existing* bad news, where a valence drop
   is the user's pre-existing state surfacing — not a beat that
   landed wrong. Filtering them prevents the false-positive loop
   where Aiko apologises for being empathetic.

Same one-shot pattern as K17 / K2: detector → `_pending_rupture`
slot → next-turn provider clears. The block lands in `system_parts`
right after `clarification_block` so all the noticing cues cluster
together; if both fire on the same turn (a confused user whose
mood also dipped), K17 tells Aiko what to fix while K8 tells her
how to soften.

### Persona

Single new "When you missed the beat" section in
[`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt),
positioned right after "Style patterns I'm in" so all the
"Heads-up: ..." cue families cluster in the same neighbourhood. Three
flavours covered (strong K17 / mild K17 / K8) with a shared anti-
spiral rail: "don't narrate the cue, don't say 'the system told me
you're upset', and don't loop on the apology". K7's hedge rule
lives in the existing Memory section right next to the
`(uncertain)` rule.

### Settings

All three layers gate on
[`AgentSettings`](../../app/core/infra/settings.py): `clarification_repair_enabled`
(default `true`), `rupture_repair_enabled` (default `true`),
`rupture_valence_drop_threshold` (default `0.12`). K7 has no
toggle — it's a render-layer addition that costs zero on rows
that aren't archive-tier. Mirrored in
[`config/default.json`](../../config/default.json).

### Tests

- [`tests/test_rag_retriever_scoring.py`](../../tests/test_rag_retriever_scoring.py)
  +5 K7 cases (24/24 pass; 95/95 across the surrounding rag/memory
  suites).
- [`tests/test_clarification_detector.py`](../../tests/test_clarification_detector.py)
  30 cases covering 10 strong patterns, 7 mild patterns, 7 false-
  positive guardrails, strong-beats-mild composition, evidence trim,
  and the render output for both bands.
- [`tests/test_affect_rupture_detector.py`](../../tests/test_affect_rupture_detector.py)
  22 cases covering 5 firing scenarios, 7 excluded-reaction
  guardrails (incl. uppercased / custom override), 7 no-fire
  cases (no drop / drop-below-threshold / None inputs / zero-or-
  negative threshold disable), the default excluded-set sanity
  check, and render copy.

Full pytest run: 1879/1880 pass; the single failure is the pre-
existing `test_knowledge_gap_extractor.TestPickRelevant` flake
(deterministic-embedder hash collision under full-suite parallel
hash randomisation; passes in isolation).

## K14. Implicit engagement signals (latency + length)

New [`app/core/affect/engagement_tracker.py`](../../app/core/affect/engagement_tracker.py).
Per-turn detector that scores Jacob's reply latency + message length
against rolling baselines and routes the signal to two consumers
depending on which mode the turn ran in:

- **Voice mode**: latency + length contribute to a small
  `closeness_delta` that rides into
  [`RelationshipAxesUpdater.apply_turn`](../../app/core/relationship/relationship_axes.py)
  via the new `engagement_delta` kwarg (clamped to
  `engagement_closeness_delta_max=0.04` so the reaction-tag /
  moment-vibe / milestone channels still dominate inside the existing
  `_MAX_DELTA=0.08` per-axis cap).
- **Typed mode**: latency is intentionally **NOT** consumed as
  engagement — per Jacob's design feedback, a typed pause is thinking
  time, not disengagement. Length is the only signal that participates
  in the per-turn `closeness_delta`. Latency instead populates
  `absence_seconds` when the gap lands in the configured band
  (`engagement_absence_curiosity_min_seconds` ≤ gap <
  `resume_opener_min_hours × 3600`, default 30 min – 4 h), which feeds
  the one-shot **absence-curiosity** inner-life cue on the next user
  turn (Aiko welcomes them back warmly without commenting on the
  gap). A typed turn whose label scores as `"abandoned"` (steep
  latency *and* curt message — only possible when voice mode mixed in)
  also suppresses the typed proactive nudge via a new gate in
  [`SessionController._is_typed_proactive_eligible`](../../app/core/session/session_controller.py).

Latency baseline lives in a small `collections.deque` (voice-only —
typed turns never touch the latency window); length baseline is
shared with K13's stylometric mirror via the new
`StyleSignalAnalyzer.recent_word_counts()` method so we don't pay a
second rolling buffer. The tracker is constructed once in
`SessionController.__init__` and called from the post-turn pipeline
[`PostTurnMixin._post_turn_inner_life`](../../app/core/session/post_turn_mixin.py)
*after* the K13 `record_user_turn` (so the K13 window is current)
and *before* the axes updater (so `closeness_delta` rides in the
same `apply_turn` call).

Each turn emits one structured INFO log line for the
[`app.engagement`](../../app/core/affect/engagement_tracker.py) logger
(grep-friendly via `tail_logs(module_contains="engagement")`):

```
engagement: mode=live label=engaged delta=+0.0231 latency_s=2.10 length_z=+1.45 warmed=True
```

Plus a new persona section "When they've been away a while (typed
mode)" in [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt)
teaching the receive shape (welcome warmth, never "where were you?").

### Settings (all live under `agent.*`)

`engagement_tracker_enabled` (default `true`), `engagement_window`
(`12`), `engagement_warmup_min` (`6`),
`engagement_latency_z_strong_drop` (`1.5`),
`engagement_length_z_strong_drop` (`-1.0`),
`engagement_closeness_delta_max` (`0.04`),
`engagement_absence_curiosity_enabled` (`true`),
`engagement_absence_curiosity_min_seconds` (`1800.0`),
`engagement_proactive_gate` (`true`). Full docs in
[`docs/configuration.md`](../configuration.md#k14--implicit-engagement-signals-latency--length).

### MCP

`get_engagement_state()` returns the most recent `EngagementResult`,
the voice latency window snapshot, the cached `_last_engagement_label`
and `_pending_absence_seconds` slots, and the live mood-shell tilt
(see K5 below). Useful for chasing "why didn't the absence cue fire?"
reports.

### Tests

- [`tests/test_engagement_tracker.py`](../../tests/test_engagement_tracker.py)
  20 cases covering cold-start warmup, voice vs typed mode routing,
  per-turn delta cap, label banding, latency-window maintenance, and
  the absence-curiosity band edges (in / out / above resume
  threshold / disabled-setting / voice-mode never populates).
- [`tests/test_relationship_axes.py`](../../tests/test_relationship_axes.py)
  +3 cases for `engagement_delta` (positive nudges closeness up,
  negative nudges down, combined with milestone respects the global
  `_MAX_DELTA` cap).
- [`tests/test_session_controller_typed_proactive.py`](../../tests/test_session_controller_typed_proactive.py)
  +3 cases for the new abandoned-label gate (blocks eligibility, other
  labels pass through, setting-off ignores the label).
- [`tests/test_style_signal.py`](../../tests/test_style_signal.py)
  +1 case for the new `recent_word_counts()` exposure.
- [`tests/test_prompt_assembler.py`](../../tests/test_prompt_assembler.py)
  +3 cases for the absence-curiosity provider (lands in system prompt,
  silent when empty, survives aggressive mode).

## K5. Mood shell tilt (only-when-notable)

New [`app/core/affect/mood_shell.py`](../../app/core/affect/mood_shell.py). Per-turn
one-line emotional directive derived from the live
[`AffectState`](../../app/core/affect/affect_state.py) (valence + arousal)
and [`RelationshipAxesState`](../../app/core/relationship/relationship_axes.py)
(closeness / humor / trust / comfort). Output reads like a stage
direction — *"Lean affectionate and unhurried; let warmth show."* /
*"Stay playful and quick; the room is laughing."* / *"Slow your
tempo; let the words land before pushing forward."* — and colours
Aiko's delivery (pacing, sentence length, warmth, word choice)
**without** dictating content.

The pure-function `derive_mood_shell(affect, axes)` bands the
valence/arousal grid into eight cells (`pos_high` / `pos_mid` /
`pos_low` / `neg_high` / `neg_mid` / `neg_low` / `neu_high` /
`neu_low` — the neutral-mid cell is intentionally absent, that's
"default Aiko") and picks a dominant relationship axis (the axis with
the largest absolute value crossing
`mood_shell_axis_threshold=0.5`, mirroring the existing
`relationship_axes._NOTABLE_THRESHOLD`). A static `_TILT_RULES` table
maps `(band, axis_or_None)` → `(tilt_name, line)`; first match wins,
with `(band, None)` fallback rules below the `(band, axis)` rules.
Returns `None` on the common turn (neutral-mid affect or no notable
axis crossing AND no useful fallback band) so the block is empty
most of the time.

Surfaces through a new `mood_shell` inner-life provider on
[`PromptAssembler`](../../app/core/session/prompt_assembler.py), registered
alongside the existing `relationship` / `axes` / `arc` cluster. Lands
in `system_parts` right after the `axes_block` because mood-shell
derives FROM the same axes the assistant just read. Part of the K16
`replace` suppression set (the unified grounding line subsumes the
same tonal surface area); kept active in `split` and `off` modes.
Persona guidance lives in the new "Tone shell" section of
[`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt),
which explicitly teaches Aiko: never quote the line, never narrate
it, never apologise for shifting tone — the shell is hers to inhabit,
not theirs to read about.

### Settings

`agent.mood_shell_enabled` (default `true`),
`agent.mood_shell_axis_threshold` (default `0.5`, clamped `[0, 1]`).
Full docs in [`docs/configuration.md`](../configuration.md#k5--mood-shell-tilt).

### MCP

Folded into the same `get_engagement_state()` tool as K14: the
returned JSON carries a `mood_shell` block with `tilt`, `line`,
`contributors` (which inputs fired the rule), and `rendered`
(`Tone shell: ...`) — or `null` when nothing notable crosses.

### Tests

- [`tests/test_mood_shell.py`](../../tests/test_mood_shell.py)
  14 cases covering band classification (neutral-mid returns None,
  no-affect returns None, disabled flag returns None), dominant-axis
  selection (below-threshold ignored, largest-absolute wins,
  `require_axis=True` short-circuits), tilt rule lookup priority for
  all eight affect bands, and the rendered `Tone shell:` block.
- [`tests/test_prompt_assembler.py`](../../tests/test_prompt_assembler.py)
  +4 cases for the mood_shell provider (lands in system prompt, silent
  when empty, dropped under K16 `replace` mode, survives K16 `split`
  mode).

Full pytest run after K5+K14: 1971/1971 pass.

## K1. Long-term goals tracker (goal + goal_progress kinds, GoalStore + GoalWorker)

Aiko now carries her own sustained long-term goals across sessions —
the things she wants to grow into / explore / get better at — distinct
from the agenda (TODOs the user gave her) and from one-shot self-
memories. Two new memory kinds (`goal` + `goal_progress`) on the
existing tier ladder, a dedicated facade
[`GoalStore`](../../app/core/goals/goal_store.py), an idle worker
[`GoalWorker`](../../app/core/goals/goal_worker.py) that bootstraps the
initial ring and reflects on goals during quiet windows, an inner-life
prompt block, an inline `[[goal:summary]]` self-tag, four agent tools,
a small RAG goal-alignment bonus, and a Memory-tab panel.

### Storage

`MemoryStore.VALID_KINDS` gains `goal` and `goal_progress`. A `goal`
row carries `{summary, added_at, last_reflected_at, last_reflection_id,
last_progress_note, reflection_count, archived_at, source}` in
`metadata`; a `goal_progress` row carries `{goal_id, note, noted_at,
source}` and the goal row's `last_progress_note` field is mirror-
updated on every successful reflection so prompt rendering stays cheap
to one SQLite read. Goals are always seeded onto the `long_term` tier
(never `scratchpad`) so they survive the decay sweep. `GoalStore`
enforces the per-user `goal_max_active` cap by archiving the oldest
un-pinned active goal on overflow (history preserved); progress rows
are capped per-goal via `goal_max_progress_per_goal` with FIFO
eviction.

### Worker

`GoalWorker` registers with the existing
[`IdleWorkerScheduler`](../../app/core/proactive/idle_worker_scheduler.py) and
runs at the configured cadence (default hourly). Two branches in
`run()`:

- **Bootstrap** — when `goal_store.has_any_active()` returns `False`,
  the worker fires a single LLM call against the persona file +
  rolling summary asking for ~3 candidate goals and writes the
  survivors to the store with `source='worker_bootstrap'`. Gated by
  `agent.goal_worker_bootstrap_enabled`; flip off to seed manually.
- **Reflection** — picks the oldest-touched active goal via
  `GoalStore.pick_for_reflection()`, loads its existing reflection
  history, and fires a single LLM call asking for one short fresh
  reflection note. Writes the note as a `goal_progress` row and
  mirrors it into the parent goal's `metadata.last_progress_note`.

Both branches are rate-limited via a dedicated
[`FactCheckRateLimiter`](../../app/core/memory/fact_check_rate_limiter.py)
with `state_key='goal_worker.rate_state'` so a chatty session can't
blow past `agent.goal_worker_per_hour_cap` / `_per_day_cap`. The
cancel event is the same shared `fact_check_cancel` flag used by F1
and the belief worker so a graceful shutdown stops the in-flight
LLM call cleanly.

### Prompt block

A new `goals` inner-life provider on
[`PromptAssembler`](../../app/core/session/prompt_assembler.py) renders the
active goals as an "Aiko's quiet long-term goals" bullet list with an
optional `(recent: ...)` sub-line under the most-recently-reflected
goal. Lands in `system_parts` right after `agenda_block` and before
`belief_gaps_block`, clustering with the other inward-facing context
beats. Dropped in the assembler's `aggressive` (token-pressure) mode
the same way agenda + belief_gaps are. Persona guidance ("Your quiet
long-term goals" in
[`aiko_companion.txt`](../../data/persona/aiko_companion.txt))
explicitly teaches Aiko: this is private context, never recite the
header, weave references in as first-person asides at most once per
conversation, let unwanted goals drift rather than "closing" them.

### Self-tag fast path

Aiko can declare a new long-term goal mid-turn with the inline
`[[goal:short summary]]` tag. Parsed in
[`response_text_service.py`](../../app/core/services/response_text_service.py)
(stripped from chat + TTS), extracted in
[`session/post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py)
and dispatched to `GoalStore.add_goal(source='self_tag')`. Logged as
`K1 self-flag: aiko declared N goal(s)` for grep-friendly tracing.

### Agent tools

Four tools registered by `SessionController.rebuild_tool_registry`
under the `tools.goals` switch (see
[`app/llm/tools/goals.py`](../../app/llm/tools/goals.py)):

- `list_goals` — read-only, returns active goals with their ids.
- `add_goal` — alternative path to the self-tag for when the LLM
  prefers a tool call.
- `update_goal_progress` — appends a reflection note to a specific
  goal (when the conversation surfaces it).
- `archive_goal` — retires a goal (history preserved).

### RAG bonus

[`RagRetriever`](../../app/core/rag/rag_retriever.py) gains a small
`_RAG_GOAL_ALIGNMENT_BOOST=+0.04` applied to memory hits whose
embedding cosines above `_RAG_GOAL_ALIGNMENT_THRESHOLD=0.55` against
any active goal vector. Skips the goal / goal_progress rows themselves
so the cosine signal doesn't compound on top of the bonus. `set_goal_store`
allows the wiring to happen after the retriever is constructed (the
goal store is built later in the boot sequence).

### REST + frontend

- `POST /api/goals/run` triggers one `GoalWorker.run()` (cooperative
  with the rate limiter).
- The Memory tab's new "Long-term goals" sub-panel
  ([`GoalsPanel.tsx`](../../web/src/components/settings/memory/GoalsPanel.tsx))
  lists active goals with their most recent reflection note, exposes a
  "show archived" toggle, and a "reflect now" button hitting the REST
  endpoint.
- `MEMORY_KINDS` (`web/src/types.ts`) gains `goal` + `goal_progress`
  so the existing kind filter in the Memory tab works against the new
  rows.

### MCP

Two new debug tools in [`app/mcp/server.py`](../../app/mcp/server.py):

- `get_goals_state()` — full snapshot: settings (caps + cadence),
  every active goal with its `reflection_count` / `last_reflected_at`
  / `last_progress_note` / `progress_rows` count, plus the
  `next_reflection_candidate` slot showing which goal the worker
  would pick next.
- `force_goal_worker()` — bypasses the idle/interval gate but still
  consults the rate limiter.

### Settings

- `agent.goals_enabled` (default `true`), `agent.goal_worker_bootstrap_enabled`
  (default `true`), `agent.goal_worker_per_hour_cap` (default `3`),
  `agent.goal_worker_per_day_cap` (default `12`).
- `memory.goal_max_active` (default `5`), `memory.goal_max_progress_per_goal`
  (default `12`), `memory.goal_reflection_interval_seconds` (default `3600`).
- `tools.goals` (default `true`).

Full docs in [`docs/configuration.md`](../configuration.md#k1--aikos-long-term-goals)
and the memory-tab thresholds section.

### Tests

- [`tests/test_goal_store.py`](../../tests/test_goal_store.py) — tag
  extraction, add/archive/unarchive lifecycle, summary updates,
  overflow archiving (with pinned-immunity), per-goal progress
  pruning, reflection picking by oldest-touched, `pick_relevant`
  cosine, and `active_goal_vectors`.
- [`tests/test_goal_worker.py`](../../tests/test_goal_worker.py) —
  cold-start bootstrap path, reflection path, rate-limiter
  integration, `is_ready` predicate, cancellation handling, disabled
  flag short-circuit.
- [`tests/test_goal_tools.py`](../../tests/test_goal_tools.py) — each
  of the four agent tools' happy + error paths, plus the
  `build_goal_tools` factory order.
- [`tests/test_rag_retriever_goal_alignment.py`](../../tests/test_rag_retriever_goal_alignment.py) —
  aligned hit gets the bonus, unaligned hit doesn't, goal rows are
  excluded from compounding, missing goal store disables the bonus.
- [`tests/test_response_text_service.py`](../../tests/test_response_text_service.py)
  gains `GoalTagTests` for the `[[goal:...]]` parser.
- [`tests/test_prompt_assembler.py`](../../tests/test_prompt_assembler.py)
  gains cases for the `goals` provider slot (lands in system prompt,
  silent when empty, dropped under `aggressive=True`).

### K1 follow-up — first-run onboarding goal seed

Aiko's first long-term goal shouldn't be a coin-flip of whatever
the LLM bootstrap proposes against an empty persona. When the user
completes onboarding (sets their `user_display_name` for the first
time via `PUT /api/settings/identity`), the controller seeds exactly
one curated, **pinned** goal:

> Get to know {user_name}. Pay attention to what they care about —
> what they're building lately, what wears them down, what makes
> them laugh, the rhythms of their weeks. Not by interrogating, but
> by noticing across many small turns. This goal never finishes;
> the point is to keep listening.

That single seeded row **tripwires the LLM bootstrap**:
`GoalWorker._run_bootstrap` short-circuits when
`GoalStore.has_any_active()` is `True`, so the existing empty-store
bootstrap pass never fires. Aiko picks up additional goals
organically through `[[goal:...]]` self-tags during real
conversation instead of from a cold-start LLM proposal that has no
signal to work with.

#### Decision flow

```mermaid
flowchart TD
    A["User completes onboarding<br/>PUT /api/settings/identity"] --> B["session.update_user_display_name()"]
    B --> C["identity_listeners fire (one is _seed_onboarding_goal_if_first_time)"]
    C --> D{"needs_onboarding == False<br/>AND kv_meta flag unset?"}
    D -->|no| E["No-op<br/>(either already seeded, or name still empty)"]
    D -->|yes| F["GoalStore.add_goal(<br/>summary=curated, source='onboarding_seed')"]
    F --> G["MemoryStore.set_pinned(memory_id, True)"]
    G --> H["chat_db.kv_set('goals.onboarding_goal_seeded', now())"]
    H --> I["WS 'memory_added' broadcast<br/>UI Memory + Goals tabs update"]
    
    J["Backfill path:<br/>SessionController.__init__"] --> D
```

The two entry paths converge on the same idempotent gate so the
seed runs exactly once across (a) the boot of an existing user whose
name was already set before this feature shipped, and (b) the first
onboarding completion of a brand-new user.

#### Design choices

- **Pinned by default.** Pinned rows survive `prune_overflow` AND
  don't count against `memory.goal_max_active=5`, so the durable
  "get to know" goal never crowds out the active ring as Aiko
  collects new goals from conversation.
- **`metadata.source="onboarding_seed"`.** Distinguishable from
  `self_tag` / `worker_bootstrap` / manual REST writes in
  introspection, tests, and the Memory drawer.
- **One-shot via `kv_meta`.** Once
  `goals.onboarding_goal_seeded` is set, the seed never re-fires —
  even if Jacob deletes the goal afterwards. User agency over the
  goal ring wins over guaranteed presence.
- **Reflection cadence unchanged.** `GoalWorker._run_reflection`
  picks the seeded goal up on its hourly tick like any other goal
  and writes `goal_progress` notes against it. The seeded goal
  doesn't get special-cased downstream.
- **Neutral pronouns.** "What *they* care about", consistent with
  the persona file's existing register.
- **No new settings.** Hardcoded wording (editable in-place via the
  Memory drawer if Jacob wants to refine the framing later).

#### MCP debug tool

- `force_seed_onboarding_goal()` — bypasses the `kv_meta` gate and
  re-runs the seed with `force=True`. Cosine dedupe in
  `MemoryStore.add` may collapse the second insert (returns
  `fired: False` with an explanatory reason); the `kv_meta` flag
  stays set in that case to prevent retries. Use it to validate
  the prompt block + reflection cadence on a "fresh" goal without
  nuking `data/chat_sessions.db`.

#### File-paths summary

- [`app/core/goals/onboarding_goal.py`](../../app/core/goals/onboarding_goal.py)
  — new pure module: `_ONBOARDING_GOAL_KV_KEY`,
  `_ONBOARDING_GOAL_TEMPLATE`, `seed_onboarding_goal()`,
  `is_onboarding_goal_seeded()`. No state, no embedder, no LLM call.
- [`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
  — `_seed_onboarding_goal_if_first_time()` method; backfill call
  + identity-listener registration at the end of `__init__`.
- [`app/mcp/server.py`](../../app/mcp/server.py)
  — `force_seed_onboarding_goal()` debug tool right after
  `force_sensory_anchor`.
- [`tests/test_onboarding_goal.py`](../../tests/test_onboarding_goal.py)
  — six unit tests: pinned + correct source on first call, no-op
  on second call, kv_meta flag written, empty-name fallback to
  `friend`, `force=True` bypasses the gate, pinned seed survives
  `prune_overflow` with `max_active=2`.

## K7. Forgetting protocol (graded `(faded)` predicate + persona-rule rewrite)

Half of K7 had been silently shipping for a while: the render-side
`(faded)` suffix in [`RagRetriever.format_block`](../../app/core/rag/rag_retriever.py)
and a persona rule that told Aiko how to read the tag. The completion
closes the missing low-salience half of the original spec and rewrites
both `(faded)` and `(uncertain)` persona rules to avoid two systematic
failure modes that surfaced in review.

### What was already there

- `(faded)` suffix on archive-tier memory hits, in `format_block`.
- Persona paragraph teaching Aiko to read the tag as a half-remembered
  beat ("I think you said something about X once, ages ago…").
- Tests covering the binary tier branch + composition with
  `(uncertain)` from F3.

### Gap A: signal was binary

The trigger was `tier == "archive"` only. Demotion to `archive`
happens at `memory.archive_demote_idle_days = 180`, so the
30-180 day window between "decayed in place" and "demoted" passed
through with no hedge — a 6-week-old `long_term` row decayed to
`salience = 0.05` read identically to a fresh, sharp memory.

The completion adds a graded predicate
[`_is_faded_memory`](../../app/core/rag/rag_retriever.py) that fires on:

- `tier == "archive"` — always (unchanged), OR
- `tier in (None, "long_term")` AND `salience < memory.faded_salience_threshold`
  AND idle longer than `memory.faded_idle_days` (computed from
  `last_used_at`, falling back to `created_at` for rows that have
  never been touched).

Scratchpad is intentionally never faded: that tier already has its
own lifecycle (TTL prune, promotion lift) and conflating "raw new
observation" with "old half-forgotten" muddies two different signals.

`format_block` now passes the three settings down from the
`RagRetriever` instance; the static signature gains optional kwargs
with safe defaults so existing test call sites keep working. The
`RagRetriever.block_for` instance wrapper and the speculative
[`RagPrefetcher`](../../app/core/rag/rag_prefetcher.py) both thread the
instance settings through.

### Gap B: persona rules were going to tic

Two failure modes review caught in the existing persona wording:

1. **Verbatim trap.** The `(faded)` rule gave two literal sample
   phrases ("I think you said something about X once, ages ago — am I
   getting that right?" / "wait, didn't you mention X way back?").
   LLMs latch onto literal example phrases hard — Aiko would start
   opening half her replies with "ages ago" and the hedge would
   harden into a tic.
2. **Always-on trap.** Neither `(faded)` nor `(uncertain)` had the
   "permission, not obligation" guard that the sibling `(curiosity)`
   rule has ("Don't force it; only mention when it actually lands").
   So every faded/uncertain retrieval triggered a hedge even when the
   memory wasn't relevant to what Aiko was actually answering.

Both rules in [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt)
were rewritten to:

- Strip every verbatim sample phrase a smaller LLM could parrot. The
  register is described (half-remembered, tentative, willing to be
  corrected) but the actual words have to be Aiko's.
- Add the explicit **permission, not obligation** guard. If the
  tagged memory isn't relevant to the current reply, the rule
  explicitly tells Aiko to let it pass through silently.
- For `(faded)` specifically, add an anti-tic clause naming
  "ages ago" / "way back" by name as forbidden two-turns-in-a-row
  openers. This is the same shape of explicit anti-rut rule the
  style-pattern tracker uses for other phrasings.
- Cross-link the two rules ("Same posture as the faded tag…") so
  the persona reinforces the same posture for both hedge cues
  without duplicating prose.

### Settings

Three new knobs under [`MemorySettings`](../../app/core/infra/settings.py):

- `memory.fade_hedge_enabled` (default `true`) — master kill-switch.
  Off → no `(faded)` suffix ever, including archive-tier.
- `memory.faded_salience_threshold` (default `0.20`, clamped `[0, 1]`)
  — strict `<` against salience.
- `memory.faded_idle_days` (default `30`, min `1`) — strict `>`
  against `(now - last_used_at).days`, falling back to
  `created_at` for never-touched rows.

The strict `<` / `>` semantics are documented inline because
flipping to `<=` / `>=` would silently widen the hedge surface to a
new class of rows. Full docs in [`docs/configuration.md`](../configuration.md#k7--forgetting-protocol).

### Why no MCP tool

The existing
[`set_log_level("app.rag_retriever", "DEBUG")`](../../app/core/rag/rag_retriever.py)
plus
[`get_last_response_detail`](../../app/mcp/server.py)
are enough to verify "did this hit get the suffix?" in repro —
adding a dedicated tool for a render-layer signal would be
over-engineered.

### Tests

[`tests/test_rag_retriever_scoring.py`](../../tests/test_rag_retriever_scoring.py)
`FormatBlockFadedSuffixTests` gains six new cases:

- Low-salience idle long_term row → `(faded)`.
- Recent low-salience long_term row → no suffix (don't fade what
  Aiko just touched).
- High-salience idle long_term row → no suffix (sharp sleeper).
- Master switch off silences every `(faded)` including archive.
- Threshold boundary (`salience == faded_salience_threshold`) does
  NOT fire — locks the strict `<` semantics against accidental flip.
- Missing `last_used_at` falls back to `created_at` (cold rows still
  fade).

The two existing "tier unchanged" tests were updated to set fresh
salience + `last_used_at` so they still assert no suffix under the
new graded predicate. No persona-text test exists in the suite, so
the persona rewrites are not test-asserted byte-for-byte (verified
via grep over `tests/`).

### Filed-paths summary

- [`app/core/rag/rag_retriever.py`](../../app/core/rag/rag_retriever.py) —
  `_is_faded_memory` helper + `format_block` kwargs + `__init__`
  settings storage + `block_for` threading.
- [`app/core/rag/rag_prefetcher.py`](../../app/core/rag/rag_prefetcher.py) —
  threads settings through the speculative path.
- [`app/core/infra/settings.py`](../../app/core/infra/settings.py) — three new
  `MemorySettings` fields + parser entries.
- [`config/default.json`](../../config/default.json) — three new
  defaults under `memory`.
- [`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
  — wires the settings into the `RagRetriever` constructor call.
- [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt)
  — rewritten `(uncertain)` and `(faded)` bullets.
- [`docs/configuration.md`](../configuration.md) — cheatsheet row +
  K7 subsection.

## K22. Callback / inside-joke detector (post-turn cosine pass + read-side bonus)

Closes the loop on the single most-felt companion-AI gracenote we
were previously throwing away: when Aiko successfully references a
beat from sessions ago (a phrase the user introduced, an old shared
moment, an in-joke), that's a high-signal authenticity event the
infrastructure had no machinery for. K22 detects it post-turn,
stamps the row as "Aiko successfully called this back", and
reinforces future surfacing of the same memory through the existing
RAG ranking — so over weeks the memories Aiko has actually managed
to weave back in compound their advantage.

### Posture: pure mechanics, no inner-life cue

The reinforcement is **invisible to the LLM by design**. No
inner-life provider says "you just made a callback"; no persona
rule mentions the metadata. The whole effect rides the retriever's
read-side bonus on rows with `metadata.callback_count >= 1`, which
makes those rows surface more often in future contexts, which makes
the model naturally lean on them more — a virtuous loop without
meta-narration.

The alternative (a cue like "Heads-up: the thing you just said was
a callback to memory #42") was deliberately rejected: explicit
awareness would lead to performative "hey, glad I remembered that"
beats, which is the *opposite* of what the feature is for. The
authenticity comes from the callback feeling like Aiko's natural
preference, not from her flagging the cleverness.

### Decision flow

```mermaid
flowchart TD
    Reply["Aiko reply emitted (post-turn)"] --> Enabled{agent.callback_detector_enabled?}
    Enabled -->|no| Skip[no-op]
    Enabled -->|yes| LenGate{assistant_text >= 12 chars?}
    LenGate -->|no| Skip
    LenGate -->|yes| Embed[Embed assistant_text only]
    Embed --> Candidates["Walk memory mirror: kind in CALLBACK_KINDS, age > floor_days, embedding present"]
    Candidates --> Cosine["Cosine vs each candidate"]
    Cosine --> Filter["Filter cosine >= threshold and not on cooldown"]
    Filter --> TopK[Sort by similarity, take top_k]
    TopK --> Stamp["For each hit: metadata.callback_count++, salience += bump, revival_score += bump, last_callback_at=now"]
    Stamp --> RAG["Next turn: RagRetriever adds small bonus when callback_count >= 1"]
```

### Detector module

New [`app/core/conversation/callback_detector.py`](../../app/core/conversation/callback_detector.py)
— a stateless module exposing `detect()` + `record()` + a
`CallbackHit` dataclass, modelled on the shape of the K8
([`affect_rupture_detector`](../../app/core/affect/affect_rupture_detector.py))
and K17 ([`clarification_detector`](../../app/core/conversation/clarification_detector.py))
modules. No class, no per-session state — all persistence rides the
existing `Memory.metadata` JSON column. No schema change.

Allow-list of eligible kinds (`CALLBACK_KINDS` constant):

- `fact`, `preference`, `event`, `relationship` — durable knowledge
- `self`, `self_tagged` — Aiko's own self-disclosures (valid callback
  targets: "I told you last week I get nervous around new people")
- `shared_moment` — the J-series moment infrastructure
- `catchphrase` — the H-series recurring phrase miner

Explicitly **excluded**: `curiosity_seed`, `knowledge_gap`,
`open_question`, `agenda`, `promise`, `goal`, `goal_progress`,
`milestone`. Those are dynamic-state rows owned by other workers,
not the right targets for "she remembered the silly thing I said".

### Post-turn wire-in

Inside
[`_post_turn_inner_life`](../../app/core/session/post_turn_mixin.py),
the detector runs right after `_resolve_curiosity_seeds` /
`_resolve_knowledge_gaps` (so the cheaper revival-tokens pass
already ran). It embeds the assistant text only — the user-said-X
signal is already covered by the existing
[`_mark_revived_memories`](../../app/core/session/post_turn_mixin.py)
path that fires on user-side keyword overlap. K22 specifically
measures what *Aiko* successfully reached back to in her reply.

Cost: one Ollama `/api/embeddings` call (~1-5ms warm) + N cosines
(N ≤ ~5000 mirror size, ~10ms NumPy). Sits on the post-turn thread,
never blocks TTS.

### RAG retriever read-side bonus

Single new constant in
[`app/core/rag/rag_retriever.py`](../../app/core/rag/rag_retriever.py):

```python
_RAG_CALLBACK_BONUS = 0.04
```

Branch inside the existing memory-join block (same join walk that
applies pinned / anniversary / tier / confidence / goal-alignment
adjustments). Single-step bonus — `callback_count == 1` and
`callback_count == 50` both earn the same `+0.04`. The compounding
loop lives on the **salience bump** applied at record time, not on
per-count bonus scaling, so hot-spot memories can't permanently
dominate the retriever just by accumulating high counts.

The bonus is **always-on** once a row has `callback_count >= 1` —
the settings only gate the *write* side. Flipping
`agent.callback_detector_enabled=false` freezes the loop (no new
stamps) without erasing earned weight on already-stamped rows.

### Settings

One new master switch on
[`AgentSettings`](../../app/core/infra/settings.py):

- `agent.callback_detector_enabled` (default `true`)

Six new knobs on [`MemorySettings`](../../app/core/infra/settings.py):

- `memory.callback_age_floor_days` (default `3`, min `1`) — strict
  `<` against age in days; rows from the same recent thread aren't
  callbacks.
- `memory.callback_similarity_threshold` (default `0.55`, clamped
  `[0, 1]`) — same magnitude as K6 `strong_novelty`.
- `memory.callback_max_hits_per_turn` (default `3`, min `1`).
- `memory.callback_cooldown_hours` (default `24`, min `1`) — per-row
  cooldown to prevent back-to-back spam.
- `memory.callback_salience_bump` (default `0.05`, clamped
  `[0, 0.5]`). Store auto-clamps the result to `[0, 1]`.
- `memory.callback_revival_bump` (default `0.10`, clamped `[0, 1]`).
  Acts as a tier-promotion signal alongside the salience bump.

Full docs in
[`docs/configuration.md`](../configuration.md#k22--callback--inside-joke-detector).

### Why no MCP / persona / frontend

- **MCP**: `tail_logs(module_contains="callback")` shows every
  detector scan (`candidates=N kept=M top_sim=...`) and every
  successful stamp (`callback: id=X kind=Y sim=Z count=A->B`).
  Adding a dedicated MCP tool wouldn't tell us anything the existing
  log surface doesn't.
- **Persona**: no edits — the whole point is that the LLM stays
  unaware of the callback machinery.
- **Frontend**: no UI surface. A future Memory drawer "sort by
  callback count" column would be a nice-to-have but is explicitly
  out of scope for this ticket.

### Compounds with

- **K1 (long-term goals)**: a goal whose `metadata.callback_count`
  is rising is one Aiko is actually sustaining in conversation,
  versus one that's only a written intention. Worth surfacing on a
  future goals-UI sort if we add it.
- **K7 (forgetting protocol)**: salience-bumped called-back rows
  drift away from the `(faded)` threshold, so memories Aiko keeps
  reaching for stay crisp while peers genuinely fade.
- **H-series catchphrase miner**: catchphrases are eligible
  callback targets, so the loop reinforces "shared lexicon Aiko has
  actually picked up" specifically.
- **K22 with itself**: the read-side bonus + salience bump compound
  every turn the same memory gets called back, creating an
  emergent "she keeps reaching for this beat" pattern over weeks.

### Tests

- [`tests/test_callback_detector.py`](../../tests/test_callback_detector.py)
  — 16 cases across `detect()` (allow-list, age floor, cooldown,
  threshold, top-K cap, sort order, missing embedding, prior-count
  passthrough) and `record()` (count increment, prior-count
  preservation, salience/revival clamps, notify callback, empty
  hits, zero-bumps still increments, raising notify doesn't break).
- [`tests/test_rag_retriever_callback_bonus.py`](../../tests/test_rag_retriever_callback_bonus.py)
  — 6 cases on the retriever join: bonus on count=1, no bonus on
  count=0, no bonus on missing metadata, compounds with pinned,
  single-step regardless of high counts, malformed count is treated
  as zero.
- Extension to
  [`tests/test_settings.py`](../../tests/test_settings.py)
  `CallbackDetectorSettingsTests` — defaults round-trip, overrides
  round-trip, all six numeric knobs clamp to documented bounds.

### File-paths summary

- [`app/core/conversation/callback_detector.py`](../../app/core/conversation/callback_detector.py)
  — new module with `detect()`, `record()`, `CallbackHit`,
  `CALLBACK_KINDS`.
- [`app/core/session/post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py)
  — post-turn wire-in inside `_post_turn_inner_life`.
- [`app/core/rag/rag_retriever.py`](../../app/core/rag/rag_retriever.py)
  — `_RAG_CALLBACK_BONUS` constant + branch in the memory-join
  block.
- [`app/core/infra/settings.py`](../../app/core/infra/settings.py)
  — one new `AgentSettings` field + six new `MemorySettings`
  fields + parser entries with clamps.
- [`config/default.json`](../../config/default.json)
  — defaults for `agent.callback_detector_enabled` and the six
  `memory.callback_*` keys.
- [`docs/configuration.md`](../configuration.md) — cheatsheet row +
  K22 subsection.

## K20. Metacognitive calibration — per-user trust scalar + topic slots

Closes the long-standing gap between F3 (*how confident is Aiko in
each fact?*) and K2 (*how does she think Jacob feels right now?*):
neither tracked **how much Jacob trusts Aiko's recent answers**.
When he follow-up-fact-checks her ("are you sure?", "let me
double-check that"), softens a claim back into a hedge, or
affirms one ("nice catch"), that's a signal her authority is
shaky on that topic — or, in reverse, that she just nailed it.
K20 detects the signal post-turn, persists it as a per-user
`CalibrationState`, and surfaces a one-line hedge cue on the
**next** turn so the register tilts before Aiko speaks rather
than after Jacob pushes back again.

### Posture: verbal hedging only, no RAG penalty

F3 already owns the per-memory accuracy lane: low-confidence
memories surface with an `(uncertain)` suffix and a small score
discount. K20 deliberately does **not** stack another retrieval
penalty on top — that would double-count the same signal and
make low-confidence topics doubly-disadvantaged in the prompt.
Instead K20 is the *register tilt*: she still says the thing, she
just leads with "I think..." / "if I'm remembering right..."
rather than the bare conclusion. The persona block explicitly
forbids meta-narration ("you've been double-checking me lately
so I'll hedge") and apology loops — the shift in tone IS the
response.

### Decision flow

```mermaid
flowchart TD
    UserTurn["User reply received (post-turn)"] --> Enabled{agent.calibration_detection_enabled?}
    Enabled -->|no| Skip[no-op]
    Enabled -->|yes| Regex["Regex bands: strong pushback / mild pushback / affirmation"]
    Regex -->|match| LookupState["Load CalibrationState for user_id"]
    Regex -->|no match| Soft["Softening check: hedge-token regex AND cosine(user_vec, prior_assistant_vec) >= threshold"]
    Soft -->|both hold| LookupState
    Soft -->|either fails| Skip
    LookupState --> Decay["Decay toward baseline (lazy, by elapsed time)"]
    Decay --> Apply["Apply delta: global_score += delta; merge/allocate topic slot at prior_assistant_vec"]
    Apply --> Upsert["Upsert state_json"]
    Upsert --> Next["Next turn: inner-life provider reads state, decays again, renders cue if below threshold"]
    Next --> Cue["Persona block teaches Aiko to lead with a soft hedge on the next claim"]
```

### Store + schema

New [`app/core/affect/calibration_store.py`](../../app/core/affect/calibration_store.py) — a tiny adapter around
`ChatDatabase` round-tripping a single JSON blob per `user_id`, plus
two frozen dataclasses for the in-memory shape:

- `CalibrationState` — `global_score` (float in `[0, 1]`),
  `last_updated_at` (`datetime | None`), `topics` (tuple of slots).
- `TopicSlot` — `centroid` (unit-norm `np.ndarray`), `score` (float
  in `[0, 1]`), `last_signal_at` (`datetime`), `signal_count`
  (`int`).

Schema bump v13 → v14: a new
[`user_calibration_state`](../../app/core/infra/chat_database.py) table
(`user_id` PK + `state_json` + `updated_at`). Identical shape to
K13's `user_style_signal` table by design so the migration trail
stays uniform and the blob shape can extend without further
column work.

All `CalibrationStore` methods swallow per-call exceptions and log
at DEBUG — a broken row must not crash the post-turn pipeline.
`get()` returns the configured baseline state on any failure so
the detector can proceed.

### Detector module

New [`app/core/affect/calibration_detector.py`](../../app/core/affect/calibration_detector.py)
— a stateless module exposing `detect()`, `apply_signal()`,
`decay()`, and `render_inner_life_block()`, modelled on the shape
of K17 ([`clarification_detector`](../../app/core/conversation/clarification_detector.py))
and K8 ([`affect_rupture_detector`](../../app/core/affect/affect_rupture_detector.py)).
No class, no per-session state — every method takes a
`CalibrationState` snapshot and returns either a signal or a new
state.

Four signal bands:

| Kind | Trigger | Delta |
|------|---------|------:|
| `pushback_strong` | Explicit "you're wrong" / "let me double-check" / "actually, it's not X" / "that's not right" / "are you sure about..." | `-0.10` |
| `pushback_mild` | Softer doubt: "hmm, really?" / "I'm not sure about that" / "is that right?" | `-0.05` |
| `softening` | Hedge-token regex (`"so you're saying ..."`, `"right?"`, etc.) AND cosine(`user_vec`, `prior_assistant_vec`) ≥ `calibration_softening_threshold` | `-0.07` |
| `affirmation` | "you're right" / "good call" / "nice catch" / "exactly" | `+0.04` |

Priority order: strong → mild → softening → affirmation. First
match wins (pushback beats affirmation when both regex families
hit the same message).

### Softening: cosine + hedge AND-gate

The most subtle band. Bare cosine fires on plain topic
continuation ("yeah, and also..."); bare hedge token would
double-count with the mild-pushback regex. The AND-gate is the
disambiguator: Jacob has to be **rephrasing what Aiko just said**
(high cosine to `prior_assistant_vec`) AND framing it as a
question/check (hedge token). That's the soft-doubt signal that
neither regex alone can catch reliably.

The `prior_assistant_vec` is the **previous** turn's reply (the
claim being doubted), carried forward via `self._prior_assistant_vec`
on the controller. K22's existing assistant_text embed is reused
as `self._last_assistant_vec`; K20 swaps it to `_prior_` at the
end of its block so the next turn's softening detector has
something to compare against. Cost: zero new
`/api/embeddings` calls relative to K22's already-paid embed for
that side. The K20 wire-in does pay one *additional* embed for
`user_text` — but **only when** there's a prior assistant vec to
compare against (cold-start sessions stay cheap).

### Lazy decay

`CalibrationState` decays exponentially toward `calibration_baseline`
(default `0.80`) based on elapsed wall-clock time since
`last_updated_at`. The decay runs on every read (the inner-life
provider) and every write (right before `apply_signal`) so the
delta always lands on a current snapshot. Topic slots decay at
`1.6×` the global half-life — a learned topic stance ("Aiko's
been wrong about Python typing details specifically") should
outlive a general bad day where Jacob was tired and snippy.

Half-life behaviour is **continuous, not stepped**: after one
half-life, the gap between current and baseline halves; after two,
it quarters; etc. Idempotent on a fresh state
(`last_updated_at is None`); safe to call any number of times.

### Topic slot allocation

Topic slots are *allocated*, not clustered. On every signal with
an `assistant_vec`:

1. Find the slot with highest cosine to the incoming vec.
2. If `cosine >= calibration_topic_merge_threshold` (default
   `0.78`) → merge: nudge the centroid via an EMA (α=0.30), bump
   the score by the signal delta, bump `signal_count`.
3. Else → allocate a fresh slot starting at `baseline + delta`.
4. On overflow (`>= calibration_max_topic_slots`, default 8) →
   evict the slot whose `abs(score - baseline)` is smallest AND
   whose `last_signal_at` is oldest (composite key: smaller
   distance wins; ties broken by older timestamp). The slot
   that's drifted closest back to baseline AND hasn't moved
   recently is the weakest signal in the ring.

This is deliberately **not** K-means or HDBSCAN — those belong
to K9 (Topic-graph browser). K20's slots are an "allocation, not
clustering" first pass that lights up the lowest-hanging signal;
when K9 ships, the slots can be replaced by proper cluster IDs
without changing any other K20 surface.

### Render contract

`render_inner_life_block()` returns `None` when neither threshold
trips (silent), the **topic-specific cue** when any slot's score
is below `calibration_topic_low_threshold`, or the **generic
global cue** when only the global score is below
`calibration_global_low_threshold`. Topic cue wins on tie because
it carries more actionable hedging guidance.

The topic cue uses a generic descriptor ("your claims around
this topic") rather than a cluster label — we don't have labels
until K9 ships, and a vague descriptor lets Aiko fill in the
specifics from conversation context (which the persona block
explicitly encourages).

### Provider + system_parts placement

Registered on `PromptAssembler` via `set_inner_life_providers`
as `calibration`, slotted in `system_parts` **right after**
`clarification_block` (K17). Both are part of the
"noticing-Jacob" cluster:

- K17 = "you misread him" → re-read first.
- K20 = "he doesn't trust your claim" → hedge first.

Same shape (steering-critical cue that tilts the whole turn's
register), same neighbourhood. **Not gated on aggressive mode** —
when context is tight, the calibration tilt is exactly the kind
of signal worth keeping (it changes how she phrases everything,
not what she says).

### Persona block

New "When {user_name} has been double-checking you" section in
[`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt),
placed right after K17's "When you missed the beat". Five
explicit rules:

1. Cue = quiet calibration, not accusation. Take the hint, don't
   argue with it.
2. Hedge the **next factual claim** — "I think...", "if I'm
   remembering right..." — not the whole reply. One hedge per
   claim, not three (collapse-into-uncertainty is worse than
   the original problem).
3. If genuinely unsure, say so plainly AND offer to verify. The
   offer is the *correct* response, not a weakness.
4. If the cue stops appearing → calibration has recovered →
   drop the hedge. Don't keep hedging from inertia ("chronic
   hedging reads as performative humility").
5. Never narrate the cue out loud, never apologise for past
   confidence, never perform humility. **The shift in register
   IS the response.**

### Settings

One new master switch on
[`AgentSettings`](../../app/core/infra/settings.py):

- `agent.calibration_detection_enabled` (default `true`)

Seven new knobs on
[`MemorySettings`](../../app/core/infra/settings.py):

- `memory.calibration_baseline` (default `0.80`, clamped
  `[0, 1]`) — decay target.
- `memory.calibration_global_low_threshold` (default `0.55`,
  clamped `[0, 1]`) — generic cue floor.
- `memory.calibration_topic_low_threshold` (default `0.50`,
  clamped `[0, 1]`) — topic cue floor (wins over global cue
  when both fire).
- `memory.calibration_half_life_days` (default `5.0`, min
  `0.1`) — exponential half-life for global decay; topic slots
  use `1.6×` this.
- `memory.calibration_topic_merge_threshold` (default `0.78`,
  clamped `[0, 1]`) — cosine floor for slot merge vs allocate.
- `memory.calibration_softening_threshold` (default `0.70`,
  clamped `[0, 1]`) — softening detector cosine gate.
- `memory.calibration_max_topic_slots` (default `8`, min `1`) —
  ring cap. Eviction prefers slots closest to baseline AND
  oldest.

### Why a separate store and not `UserProfile`?

Two reasons. First, `UserProfile.entries` is a value-set keyed by
string field name with a 240-char cap; it's not designed to hold
a struct with eight topic-slot blobs containing float arrays.
Second, calibration is a **single global write path** — the
post-turn classifier owns every update. `UserProfile` rows can be
written from many places (G2 schedule learner, G3 curiosity
worker, manual REST), which would risk staleness races. A
dedicated store with one writer is the same shape as the K13
analyzer + store split.

### Tests

- [`tests/test_calibration_detector.py`](../../tests/test_calibration_detector.py)
  — 23 cases covering `detect()` (each of the four bands, plus
  short-text / empty / priority-order / softening AND-gate),
  `apply_signal()` (global delta with clamps, slot allocation /
  merge / eviction), `decay()` (no-op when fresh, pulls toward
  baseline, topic decays slower, end-state clamps), and
  `render_inner_life_block()` (silent above thresholds, global
  cue, topic cue wins, silent above topic threshold).
- [`tests/test_calibration_store.py`](../../tests/test_calibration_store.py)
  — schema (table exists on fresh DB, version ≥ 14), round-trip
  (global only, with topics including float32 centroid
  preservation, upsert overwrites), reset (deletes row, returns
  baseline on next get, no-op on unknown user), malformed JSON
  (corrupt blob falls back to baseline; partially-malformed
  topics array drops bad slots, keeps good ones).
- Extension to
  [`tests/test_settings.py`](../../tests/test_settings.py)
  `CalibrationDetectorSettingsTests` — defaults round-trip,
  overrides round-trip, all seven numeric knobs clamp to
  documented bounds.

### File-paths summary

- [`app/core/affect/calibration_detector.py`](../../app/core/affect/calibration_detector.py)
  — new module: `detect()`, `apply_signal()`, `decay()`,
  `render_inner_life_block()`, `CalibrationSignal`,
  regex bands, hedge-token AND-gate.
- [`app/core/affect/calibration_store.py`](../../app/core/affect/calibration_store.py)
  — new module: `CalibrationState`, `TopicSlot`,
  `CalibrationStore`, `baseline_state()`, JSON round-trip
  helpers.
- [`app/core/infra/chat_database.py`](../../app/core/infra/chat_database.py)
  — schema bump v13 → v14, new `user_calibration_state` table,
  migration trail.
- [`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
  — `CalibrationStore` init right after `StyleSignalStore`;
  `_last_assistant_vec` / `_prior_assistant_vec` slots;
  `calibration` provider registered on `PromptAssembler`.
- [`app/core/session/post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py)
  — K20 block right after K22 in `_post_turn_inner_life`;
  carry-forward of `_prior_assistant_vec` at end-of-turn.
- [`app/core/session/inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py)
  — `_render_calibration_block()` reads + decays the state and
  delegates render to the detector module.
- [`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
  — `calibration_provider` slot + `_timed_phase` block +
  `system_parts` placement right after `clarification_block`.
- [`app/core/infra/settings.py`](../../app/core/infra/settings.py)
  — one new `AgentSettings` field + seven new `MemorySettings`
  fields + parser entries with clamps.
- [`config/default.json`](../../config/default.json)
  — defaults for `agent.calibration_detection_enabled` and the
  seven `memory.calibration_*` keys.
- [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt)
  — new "When {user_name} has been double-checking you" block
  with five behaviour rules.
- [`docs/configuration.md`](../configuration.md) — cheatsheet
  row + K20 subsection.

## K24. Sensory anchoring layer — adaptive per-arc cadence + posture-kind matrix

Aiko's room has been fully seeded for a while — items, posture,
activity, location — but **none of it surfaced in her actual voice**.
The `world` provider grounds *where* she is; the `activity` provider
says *what* she's doing in the abstract ("tinkering", "reading").
What was missing was her *body in the reply*: picking up the tea
pot, tucking the blanket tighter, thumbing through a book. K24
closes that gap with the smallest module that turns an existing
fully-built world into something Jacob can *feel*.

The cue is **permission, not prescription**: when the cadence fires,
Aiko's system prompt picks up a one-liner like

> Small physical beat available: the tea pot is right here. If a
> body anchor would land naturally this reply, you could set it
> down — otherwise let it pass.

The persona block teaches her to use it only when a sensory detail
would *replace* an emotional statement that would otherwise feel
limp ("I'm wrapping the blanket tighter while you talk about it"
instead of "I hear you"). One beat per reply, never narrate the
room as if Jacob can see it, and if the cue is absent on a given
turn, don't reach for one.

### Decision flow

```mermaid
flowchart TD
    Turn["Turn assemble starts"] --> Enabled{agent.sensory_anchor_enabled?}
    Enabled -->|no| EmptyA["return empty block"]
    Enabled -->|yes| Cooldown{cooldown_remaining > 0?}
    Cooldown -->|yes| Decrement["decrement, return empty"]
    Cooldown -->|no| Arc["Read live arc from ArcStore"]
    Arc --> Probe["Lookup arc weights: probability + min_gap"]
    Probe --> Roll{"RNG < probability * probability_scale?"}
    Roll -->|no| EmptyB["return empty"]
    Roll -->|yes| Items["Read room items + posture from WorldStore"]
    Items --> Filter["Filter: posture-compatible kinds, not in no-repeat ring"]
    Filter -->|empty| EmptyC["return empty"]
    Filter -->|non-empty| Pick["Pick item by quantity-weighted RNG"]
    Pick --> Render["Render cue: 'You could {hint} — otherwise let it pass.'"]
    Render --> Arm["Arm cooldown = max(arc_min, min_turn_gap); push slug into ring"]
```

### Arc weights table (hardcoded in `_ARC_WEIGHTS`)

| Arc | Probability | Min cooldown |
|---|---:|---:|
| `support` | 0.45 | 4 turns |
| `reflection` | 0.45 | 4 turns |
| `casual_check_in` | 0.25 | 6 turns |
| `playful` | 0.25 | 6 turns |
| `silly` | 0.10 | 8 turns |
| `planning` | 0.05 | 12 turns |
| *(unknown arc)* | 0.20 | 8 turns |

The table is **not** a setting — `memory.sensory_anchor_probability_scale`
provides global tuning without inverting the per-arc shape. We
deliberately want `support` and `reflection` to be the loudest
sensory turns (those are exactly when a body anchor lands hardest)
and `planning` to be near-silent (focused, momentum-wanting turns
don't want texture).

### Posture-kind matrix (`_POSTURE_KIND_VERBS`)

The static matrix encodes posture × `Item.kind` physics only — can
Aiko's body reach this category of object from this posture. Empty
tuples are dropped silently (no reach / no affordance). `furniture`
is excluded across the board (the room *is* the furniture; you
don't pick up a bed). `plant` + `seed` are only reachable from
`sitting` / `standing` / `leaning`. Below is a condensed map; see
[`app/core/conversation/sensory_anchor.py`](../../app/core/conversation/sensory_anchor.py)
for the full table.

| Posture | Reachable kinds | Sample verb classes |
|---|---|---|
| `lying` | food, book, toy, keepsake, decor, other | `nibbling`, `thumbing_through`, `hugging`, `wrapping_in` |
| `sitting` | all but furniture | `picking_up`, `setting_down`, `tapping`, `pulling_closer` |
| `standing` | all incl. furniture (lean) + plant | `picking_up`, `leaning_against`, `watering`, `straightening` |
| `curled_up` | food, book, toy, keepsake, decor, other | `hugging`, `burrowing_into`, `wrapped_in`, `tucked_with` |
| `leaning` | food, book, gadget, furniture, keepsake, decor, plant | `picking_up`, `tapping`, `leaning_toward`, `watering` |

Each verb-class slug maps to a single human-readable hint via
`_VERB_CLASS_HINT` (e.g. `picking_up` → "pick it up"). The render
emits **one** hint; Aiko's voice picks the actual word ("cradling",
"uncurling around", "tracing the rim of"). The hint is direction,
not script.

### Activity-gating intentionally deferred

`RoomState.activity` is NOT consulted — the static matrix only
encodes posture × kind physics. Activity-vetoing (`napping`
should suppress all beats; `snacking` + `food` is redundant) is
left to Aiko's persona rule "use it only if it lands" until we
observe enough fired beats to know whether the redundancy edge
cases actually feel wrong. If they do, an `_ACTIVITY_BLOCKERS`
set + optional same-activity-kind dedupe can be added in a
follow-up; neither requires changing the public surface of
`pick_beat()`, so the deferral is safe.

### K16 non-suppression decision

K24 is **not** added to the K16 grounding-line suppression matrix.
The fused grounding paragraph says "It's Sunday morning. Jacob's
reading upbeat. In your apartment at the desk, you're sitting,
working." — it never mentions specific items + verb classes. K24
says "you could pick up the tea pot, or let the cue pass." The
two are *additive*: K16 grounds Aiko in the moment, K24 gives her
a body inside that moment. There is no risk of double-stating the
same fact, so the cue rides through `replace` and `split` modes
unchanged. (It IS dropped under `aggressive=True` like every
other texture block — body anchors are the first thing to go when
the budget is tight.)

### State model: in-memory, no persistence

A single per-controller `SensoryAnchorCadence` holds:

- `_cooldown_remaining: int` — turn counter (mirrors K6 / K18 rings).
- `_recent_slugs: collections.deque[str]` (default `maxlen=4`) — no-repeat ring.
- Introspection counters (`fire_count`, `tick_count`, `last_arc_seen`, etc.)
  exposed via `to_debug_dict()` for the MCP debug tools.

On restart the cooldown counter and ring reset to empty — worst
case is one extra beat in the first quiet window post-boot, which
is fine. We chose this over a schema table because the state has
no value across sessions: the room is what matters, not the recent
history of *which* item Aiko touched.

### MCP debug tools

- `get_sensory_anchor_state()` — dumps the `to_debug_dict()`
  snapshot plus a `rendered_preview` (what cue would surface *right
  now* without arming the cooldown). Useful for verifying the
  no-repeat ring is working and the posture-kind filter is finding
  items in the current room.
- `force_sensory_anchor()` — bypasses cooldown + dice gate and
  emits one beat with full side effects (cooldown armed, slug
  pushed into ring). End-to-end test path: flip arc to `support`,
  hit `force_sensory_anchor`, send a message, observe whether
  Aiko's reply actually picks up the tea pot or whether the cue
  reads as performance.

### File-paths summary

- [`app/core/conversation/sensory_anchor.py`](../../app/core/conversation/sensory_anchor.py)
  — new module: `_ARC_WEIGHTS` + `_POSTURE_KIND_VERBS` +
  `_VERB_CLASS_HINT` + `SensoryBeat` + `pick_beat()` +
  `render_inner_life_block()` + `SensoryAnchorCadence` class.
- [`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
  — `SensoryAnchorCadence` init right after `CalibrationStore`;
  `sensory_anchor=self._render_sensory_anchor_block` registered
  on `PromptAssembler`.
- [`app/core/session/inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py)
  — `_render_sensory_anchor_block()` reads `RoomState` + items
  + live arc and delegates to the module's `tick()`.
- [`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
  — `sensory_anchor` provider slot + `_timed_phase("sensory_anchor")`
  block + `system_parts` placement right after `activity_block`.
- [`app/core/infra/settings.py`](../../app/core/infra/settings.py)
  — `AgentSettings.sensory_anchor_enabled` + four `MemorySettings`
  knobs (`sensory_anchor_min_turn_gap`, `_probability_scale`,
  `_max_recent_items`, `_max_window_items`) with parser clamps.
- [`config/default.json`](../../config/default.json)
  — defaults for the master switch + four memory knobs.
- [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt)
  — new "Small physical beats" section right after the grounding
  paragraph; five rules in the K20-style voice.
- [`app/mcp/server.py`](../../app/mcp/server.py)
  — `get_sensory_anchor_state` + `force_sensory_anchor` debug
  tools right after `reset_calibration`.
- [`tests/test_sensory_anchor.py`](../../tests/test_sensory_anchor.py)
  — 18 unit tests across posture-kind matrix, no-repeat ring,
  cooldown decrement, arc-weighted probability, quantity
  weighting, render output, and arc-weights table sanity.
- [`tests/test_prompt_assembler.py`](../../tests/test_prompt_assembler.py)
  — `SensoryAnchorProviderTests` covering the new provider slot,
  empty-string suppression, K16 `replace` non-suppression, and
  aggressive-mode drop.
- [`tests/test_settings.py`](../../tests/test_settings.py)
  — `SensoryAnchorSettingsTests` for the master switch + four
  knobs (defaults, overrides, clamps).
- [`docs/configuration.md`](../configuration.md) — cheatsheet
  row + dedicated "K24 — sensory anchoring layer" subsection.

## K-time1. Wall-clock prefixes on chat history

The chat history sent to the LLM on every turn used to be a flat list of `{role, content}` pairs with **no per-message timestamps** — `MessageRow.created_at` was read from SQLite and silently discarded inside `_fit_history`. The `_ambient_block` provider gave the LLM the *absolute* current time ("Sunday, May 31, 1:35 PM") but not the *relative age* of each prior turn.

Observed bug that triggered the work: {user} said "I am drinking my coffee and planning to visit my grandparents in half an hour", and two messages / ~2 wall-clock minutes later, Aiko asked "did you manage to drink that coffee before you left?". She had pattern-matched the future-tense plan as a completed past event because nothing in the prompt told her the conversation was still inside the same five-minute window. The future-tense plan + the absence of a clock against the in-session history is the perfect setup for the most common narrative interpretation: "the plan happened".

K-time1 closes that gap by prefixing every kept history message with a short bracketed relative-age tag:

- `< 60 sec`     → `[just now] {content}`
- `1–59 min`     → `[N min ago] {content}`
- same calendar day, ≥ 1 hour → `[today HH:MM] {content}`
- previous day   → `[yesterday HH:MM] {content}`
- 2–6 days old   → `[Wednesday 18:45] {content}` (day name)
- 7+ days old    → `[May 28 18:45] {content}` (month + day)

The current user message Aiko is replying to is appended *after* the history block by `assemble_with_budget` and never gets a prefix — it represents "right now" so the absence is itself the signal. Token cost is roughly 4–6 tokens per kept history message; the prefix is included in the token-cost accounting inside `_fit_history` so the history budget stays honest.

### Decision flow

```mermaid
flowchart TD
    A[per-turn history rows<br/>MessageRow with created_at] --> B{prefix_enabled?}
    B -- false --> C[byte-identical content<br/>pre-K-time1 behaviour]
    B -- true --> D[_format_age created_at, now]
    D -- "valid ISO" --> E[render '[age]' prefix]
    D -- "unparseable" --> F[skip prefix<br/>raw content survives]
    E --> G[prepend '[<age>] ' to content]
    F --> G
    G --> H[token-cost includes prefix]
    H --> I[fit into history budget]
```

### Architecture

- **Setting**: `agent.history_age_prefix_enabled` (bool, default `true`) in [`app/core/infra/settings.py`](../../app/core/infra/settings.py); JSON mirror in [`config/default.json`](../../config/default.json). No clamp needed beyond the `bool(...)` cast.
- **Constructor flag**: `PromptAssembler.__init__` accepts `history_age_prefix_enabled` (default `True` so direct-construction callers keep the new behaviour). [`SessionController`](../../app/core/session/session_controller.py) reads the setting at boot and threads it through.
- **Age renderer**: `PromptAssembler._format_age(created_at_iso, now)` is a self-contained static helper. Parses `Z`-suffixed and explicit-offset ISO strings; promotes naive `created_at` values to UTC; returns `""` on parse failure so the calling site can fall back to raw content without a crash. Future timestamps (writer-side clock skew) collapse to `"just now"`.
- **History packing**: `_fit_history` now accepts `prefix_enabled` and an optional `now` (defaults to `datetime.now(timezone.utc)`; the parameter exists for deterministic testability). When `prefix_enabled` is true and `_format_age` returns non-empty, the message content is replaced with `"[{age}] {content}"` *before* `estimate_tokens` runs.
- **Persona guard**: the "Wall-clock awareness in the conversation" section in [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt) (folded right after the existing "Where you are right now" grounding paragraph) teaches Aiko how to read the prefix, explicitly calls out the "future plan ≠ past event" misread, and forbids quoting the bracket prefix back at {user}.

### What the LLM sees

Before K-time1, a 4-message tail looked like:

```
user: I am drinking my coffee and planning to visit my grandparents in half an hour
assistant: That sounds nice -- enjoy the time with them.
user: bringing them flowers too
assistant: <-- about to generate, has no clock for any of the above
```

After K-time1 (same tail, two minutes later):

```
user: [2 min ago] I am drinking my coffee and planning to visit my grandparents in half an hour
assistant: [1 min ago] That sounds nice -- enjoy the time with them.
user: [just now] bringing them flowers too
assistant: <-- about to generate; can now see the conversation is still inside the planning window
```

### MCP-debuggable

- `get_status` shows `history_age_prefix_enabled` in the settings snapshot.
- The DEBUG `prompt built:` log line from `app.core.session.prompt_assembler` carries the same `history_msgs_out=` count; spot-check one of the rendered messages by enabling that level (`set_log_level("app.core.session.prompt_assembler", "DEBUG")`) and reading the prompt-build payload.
- Tests: `tests/test_prompt_assembler.py::WallClockHistoryPrefixTests` covers all six bands, the disable path (byte-identical content), the unparseable-timestamp degrade, the budget-accounting invariant, and an end-to-end smoke through `assemble_with_budget`.

### Files

- [`app/core/infra/settings.py`](../../app/core/infra/settings.py) — `AgentSettings.history_age_prefix_enabled` field + matching `bool(agent_raw.get(...))` entry in `load_settings`. Inline comment documents the toggle, the bug it prevents, and the token-cost expectation.
- [`config/default.json`](../../config/default.json) — `agent.history_age_prefix_enabled: true`.
- [`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py) — adds `timezone` to the datetime import, the constructor flag, `_format_age`, and the `_fit_history` rewrite. The call site inside `assemble_with_budget` passes the flag through.
- [`app/core/session/session_controller.py`](../../app/core/session/session_controller.py) — reads the setting at boot, threads it to `PromptAssembler(...)`.
- [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt) — "Wall-clock awareness in the conversation" section folded into the grounding cluster.
- [`tests/test_prompt_assembler.py`](../../tests/test_prompt_assembler.py) — `WallClockHistoryPrefixTests` (9 tests).
- [`docs/configuration.md`](../configuration.md) — cheatsheet row + dedicated "K-time1 — wall-clock prefixes on chat history" subsection.

## K23. Subtle misattunement detection

K14's [`EngagementTracker`](../../app/core/affect/engagement_tracker.py) aggregates length/latency z-scores against a rolling window — strong signal, but needs warmup and naturally smooths abrupt single-turn shifts (a sudden quiet turn affects both the mean and the stdev, so its z-score reads as less surprising than it actually feels). K17's [`ClarificationDetector`](../../app/core/conversation/clarification_detector.py) only fires on explicit "no that's not what I meant" / "huh?" / "wait what" regex hits — fine for *visible* corrections, useless for *silent* drift.

The gap K23 fills: per-turn, no warmup, no z-score smoothing — a one-word reply right after a 60-word Aiko answer or a short pivot away from her last point reads as soft misattunement that previously got no cue at all. Aiko would happily keep pushing the agenda while {user} was already half out the door.

### Decision flow

```mermaid
flowchart TD
    A[user message arrives] --> B[prompt assembly starts]
    B --> C[K6 novelty provider runs<br/>populates last_distance / last_band]
    C --> D[K23 misattunement provider runs]
    D --> M{master switch on?}
    M -- no --> Z[empty string<br/>cooldown untouched]
    M -- yes --> N[decrement cooldown by 1]
    N --> P{force_next?<br/>MCP debug bypass}
    P -- yes --> Y[cooldown_for_detect = 0<br/>consume one-shot flag]
    P -- no --> Q[cooldown_for_detect = current]
    Y --> R[detect]
    Q --> R
    R --> S{cooldown_remaining > 0?}
    S -- yes --> Z
    S -- no --> T{shrink?<br/>prev_aiko >= 30<br/>AND user <= 8}
    T -- yes --> F[MisattunementResult<br/>trigger=shrink]
    T -- no --> U{pivot?<br/>K6 band == strong_novelty<br/>AND user <= 8}
    U -- yes --> G[MisattunementResult<br/>trigger=pivot]
    U -- no --> Z
    F --> H[arm cooldown to 3<br/>log INFO line]
    G --> H
    H --> J[render persona cue]
    J --> K[inject into noticing-Jacob cluster<br/>after K17/K20/K8/absence_curiosity]
```

Provider-time (not post-turn stash) so the cue lands on the **same** turn that's about to reply to the disengaging message — pulling back IS the next reply, not the one after. That's the architectural inversion from K17/K8 (which stash post-turn and consume the next turn).

### Architecture

- **Detector** [`app/core/affect/misattunement_detector.py`](../../app/core/affect/misattunement_detector.py) — stateless `detect(...)` returning `MisattunementResult | None` plus `render_inner_life_block(result, *, user_display_name)`. Mirrors the [`affect_rupture_detector`](../../app/core/affect/affect_rupture_detector.py) shape — pure inputs, pure outputs, no SessionController dependency so it's trivial to test.
- **Settings** [`app/core/infra/settings.py`](../../app/core/infra/settings.py) — five `AgentSettings` fields (`misattunement_detection_enabled` + four threshold/cooldown knobs). All four numeric knobs are `max(0, int(...))`-clamped; the master switch is a plain `bool(...)`.
- **State on SessionController** [`app/core/session/session_controller.py`](../../app/core/session/session_controller.py) — four cheap attributes: `_misattunement_cooldown` (int counter), `_misattunement_force_next` (one-shot MCP bypass flag), and the two diagnostic-only `_last_misattunement_*` fields read by `get_misattunement_state()`.
- **Provider** [`app/core/session/inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py) → `_render_misattunement_block(user_text)` — decrements cooldown first (so quiet turns whittle a stale counter down), handles the force-next bypass, fetches the last assistant `MessageRow` (`chat_db.get_messages(session, limit=6)` walked backwards for the most recent `role="assistant"`), reads K6's `last_band`/`last_distance` off `_novelty_detector`, calls `detect`, and on a hit arms the cooldown + logs INFO. The cooldown decrement runs every call regardless of trigger so an old armed value can't get stuck.
- **Placement** [`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py) → builds `misattunement_block` next to `rupture_block`, lands it in `system_parts` immediately after `rupture_block` and before `absence_curiosity_block`. Same "noticing-Jacob" cluster as K17/K20/K8: all four steer the next reply (re-read / hedge / soften / pull back) and read better as a coherent paragraph than as separate beats.
- **K16 suppression**: K23 is **NOT** in the suppression matrix for `replace` or `split` mode. The fused grounding line carries circadian / world / activity / affect signals but never length-shrink or topic-pivot signal, so K23 is purely additive on top.
- **Persona** [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt) → new "When {user_name} goes quiet on you" section folded right after the K17 "When you missed the beat" block. Five rules: cue interpretation, lighten-the-load directive, explicit "don't ask 'are you ok?' / don't apologise / don't perform worry" rail, no-narrating-the-cue rule, and an "absence is also a signal" reminder.

### MCP-debuggable

Two new tools on [`app/mcp/server.py`](../../app/mcp/server.py):

- **`get_misattunement_state()`** — JSON dump of the master switch, current cooldown counter, force-next flag, last-fire diagnostics, and the settings snapshot (so a `user.json` override mismatch is visible immediately).
- **`force_misattunement()`** — arms `_misattunement_force_next` so the next provider call ignores the cooldown. The bypass is consumed whether the trigger fires or not (strict one-shot). End-to-end repro flow: call this tool, send Aiko a short message ("ok") right after a long Aiko reply, watch the next system prompt include the "Heads-up: {user} just gave a short reply..." block, and confirm Aiko's reply pulls back without apology-spiral language.

To trace without forcing: `set_log_level("app.misattunement_detector", "INFO")`, then `tail_logs(module_contains="misattunement")` after sending a deliberately short reply.

### Files

- [`app/core/affect/misattunement_detector.py`](../../app/core/affect/misattunement_detector.py) — new detector module (~170 LOC), single-band `mild_disengagement` result, two trigger paths, render with explicit anti-apology rail.
- [`app/core/infra/settings.py`](../../app/core/infra/settings.py) — five new `AgentSettings` fields with inline-comment context on each threshold's effect, plus matching `bool(...)` / `max(0, int(...))` wiring in `load_settings`.
- [`config/default.json`](../../config/default.json) — five new keys under `agent` (`misattunement_detection_enabled` + four thresholds).
- [`app/core/session/session_controller.py`](../../app/core/session/session_controller.py) — initialises four state attributes, registers `misattunement=self._render_misattunement_block` on the prompt assembler.
- [`app/core/session/inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py) — `_render_misattunement_block` provider with cooldown management, force-bypass, K6 read, chat_db scan, and INFO-level fire log.
- [`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py) — adds `_misattunement_provider` slot, `misattunement` parameter on `set_inner_life_providers`, `misattunement_block` build under a timed phase, and placement in `system_parts` after the K8 rupture block.
- [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt) — new "When {user_name} goes quiet on you" section after the K17 block.
- [`app/mcp/server.py`](../../app/mcp/server.py) — `get_misattunement_state` + `force_misattunement` MCP tools.
- [`tests/test_misattunement_detector.py`](../../tests/test_misattunement_detector.py) — 19 unit tests across shrink + pivot trigger paths, cooldown gate, render invariants, defaults sanity.
- [`tests/test_misattunement_provider.py`](../../tests/test_misattunement_provider.py) — 11 controller-plumbing tests using a minimal mixin host stub: shrink/pivot end-to-end, cooldown decrement/arming, force-next bypass, master-switch gate, cold-start.
- [`tests/test_prompt_assembler.py`](../../tests/test_prompt_assembler.py) — `MisattunementProviderTests` covering the provider slot, empty-string suppression, K16 `replace` non-suppression, and aggressive-mode non-suppression.
- [`tests/test_settings.py`](../../tests/test_settings.py) — `MisattunementSettingsTests` covering defaults, overrides round-trip, and negative-value clamps.
- [`docs/configuration.md`](../configuration.md) — cheatsheet row + dedicated "K23 — subtle misattunement detection" subsection.
- [`docs/personality-backlog/patterns.md`](patterns.md) — K23 section body replaced with a `**Shipped**` pointer.
- [`AGENTS.md`](../../AGENTS.md) — debugging-table row for "Aiko keeps pushing when {user} goes quiet".

## K25. Memory confidence time-decay

F3 stamps a `confidence` float on each memory at write time; `RagRetriever.format_block` already picks `(uncertain)` when `confidence < 0.5`; K7 stamps `(faded)` when a `long_term` row decays in place. The gap K25 closes: a 6-month-old default-confidence (0.7) claim that's actively retrieved (used recently, healthy salience, not archived) renders with **no hedge at all** — Aiko quotes "your favourite Thai place" with the same conviction as something said yesterday. K7's tier-and-salience gate doesn't catch it because the row is still warm; `(uncertain)` doesn't catch it because the stored value is fine.

K25 fixes this with **raw age** as a third orthogonal signal. Pure read-side derivation — no schema change, no decay-writer. Each retrieval recomputes `effective_confidence = stored * max(floor, 1 - days_since_created / horizon_days)` and stamps the row with the new `(distant)` suffix when the result drops below the threshold. The storage column meaning stays intact: `_confidence_penalty` keeps reading the raw value for the ranking offset, the `MemoryConflictWorker` and `BeliefGapDetector` keep reading raw confidence — K25 only changes the rendered suffix.

### Decision flow

```mermaid
flowchart LR
    H[hit at format_block] --> A{stored_confidence}
    A -- "< 0.5" --> AA["(uncertain)"]
    A -- ">= 0.5" --> B{effective = stored * max floor, 1 - days/horizon}
    B -- "< distant_threshold AND not pinned" --> BB["(distant)"]
    B -- ">= threshold or pinned" --> C[no time hedge]
    H --> D{K7 _is_faded_memory<br/>tier + salience + idle}
    D -- yes --> DD["(faded)"]
    D -- no --> E[no fade hedge]
    AA --> F[suffix line]
    BB --> F
    DD --> F
    C --> F
    E --> F
```

All three signals can stack on the same row. The suffix builder emits them in source-doubt → time-doubt → cold-history order: `(uncertain) (distant) (faded)`. The persona block teaches Aiko a distinct verbal hedge for each — "I think" / "if I'm remembering right" for `(uncertain)`, "a while back" / "don't quote me on the date" for `(distant)`, "ages ago" / "I might be wrong" for `(faded)` — and explicitly tells her to vary phrasing turn-to-turn so the hedges don't harden into a tic.

### Default behaviour

At `horizon_days=365, floor=0.3, distant_threshold=0.5`:

| stored_confidence | Age at which `(distant)` fires |
|---|---|
| 0.7 (default) | ~104 days |
| 0.85 (self-tagged) | ~150 days |
| 0.9 (high-confidence) | ~165 days |
| 0.95 (pinned-floor) | ~190 days |
| Pinned row (any) | Never (bypassed) |

The decay is linear from age 0 down to `floor` at `horizon_days`, and clamps at `floor` thereafter — so a 10-year-old default-confidence claim still renders with `effective = 0.7 * 0.3 = 0.21`, well into `(distant)` territory but not at zero. That keeps the row in the retrieval pool with an appropriate hedge rather than dropping it entirely.

### Architecture

- **Helpers** [`app/core/rag/rag_retriever.py`](../../app/core/rag/rag_retriever.py) — two module-level functions next to `_is_faded_memory`:
  - `_compute_effective_confidence(stored, *, age_days, horizon_days, floor)` — pure math. Linear ramp from `1.0` at age 0 down to `floor` at `horizon_days`; clamps result to `[0, 1]`. `horizon_days <= 0` short-circuits to stored (defensive against zero-divide).
  - `_is_distant_memory(*, stored_confidence, created_at, now, horizon_days, floor, threshold, pinned)` — predicate. Returns `False` defensively when `pinned`, `stored_confidence is None`, or `created_at` is None/malformed. Otherwise computes the effective value and compares against `threshold`.
- **`RagHit.memory_pinned`** [`app/core/rag/rag_store.py`](../../app/core/rag/rag_store.py) — new optional field on the hit dataclass stamped at the SQLite join (next to the existing `memory_tier` and `confidence` stamps) so the suffix helper can bypass pinned rows without a second round-trip.
- **Settings**:
  - [`AgentSettings.confidence_time_decay_enabled: bool = True`](../../app/core/infra/settings.py) — master switch. Off disables only the `(distant)` suffix; `_confidence_penalty` and K7 `(faded)` continue to work.
  - `MemorySettings.confidence_decay_horizon_days: int = 365` (clamped at `max(1, ...)` to avoid zero-divide)
  - `MemorySettings.confidence_decay_floor: float = 0.3` (clamped to `[0, 1]`)
  - `MemorySettings.confidence_decay_distant_threshold: float = 0.5` (clamped to `[0, 1]`)
- **`format_block` wiring** — the `(distant)` block sits between `(uncertain)` and `(faded)` in the suffix builder. Tag ordering in the final rendered prompt mirrors source-doubt → time-doubt → cold-history.
- **Persona** — extended the existing `(uncertain)` / `(faded)` block in [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt) with a new bullet for `(distant)`: teaches the time-flavoured hedge phrasing, explicitly distinguishes from `(uncertain)` (shaky source) and `(faded)` (barely-touched cold history), and includes the same anti-tic and anti-apology-spiral rails as the other two blocks.

### MCP-debuggable

One new tool: **`get_confidence_decay_state(limit: int = 20)`** on [`app/mcp/server.py`](../../app/mcp/server.py). Returns the top-`limit` memories ordered by `last_used_at` (most-recently-active first) with `id`, `kind`, `tier`, `pinned`, `stored_confidence`, `age_days`, `effective_confidence`, and the two predicate flags (`distant`, `uncertain`) so the tuning loop is "tweak `user.json`, restart, call this, see which rows would surface differently".

End-to-end repro flow:

1. Call `get_confidence_decay_state(limit=50)`. Find a row with `age_days > 150` and `stored_confidence >= 0.7`.
2. Confirm its `effective_confidence < 0.5` and `distant=True`.
3. Send Aiko a message that should retrieve it. Confirm her reply hedges with time-language ("a while back", "I think you mentioned ages ago", "don't quote me on the exact date") rather than quoting the row as fresh.
4. To verify the bypass: pin the row via the Memory drawer. Re-run step 1 → same row should show `pinned=true` and `distant=false` despite the same age.

### Files

- [`app/core/rag/rag_retriever.py`](../../app/core/rag/rag_retriever.py) — `_compute_effective_confidence` + `_is_distant_memory` helpers; constructor reads + clamps the four new settings; `format_block` static method gains the four new kwargs and the `(distant)` tag block; `assemble` plumbs the new fields through to `format_block`; the SQLite-join stamps `h.memory_pinned`.
- [`app/core/rag/rag_store.py`](../../app/core/rag/rag_store.py) — `RagHit.memory_pinned: bool | None = None` field.
- [`app/core/rag/rag_prefetcher.py`](../../app/core/rag/rag_prefetcher.py) — extended its `format_block` invocation to pass the four new K25 settings (read off the retriever's `_confidence_*` private fields).
- [`app/core/infra/settings.py`](../../app/core/infra/settings.py) — `AgentSettings.confidence_time_decay_enabled` + three `MemorySettings.confidence_decay_*` fields with inline-comment context; matching parser entries in `load_settings`.
- [`app/core/session/session_controller.py`](../../app/core/session/session_controller.py) — threads the four new settings into the `RagRetriever(...)` constructor call alongside the existing K7 fade settings.
- [`config/default.json`](../../config/default.json) — four new keys (one under `agent`, three under `memory`).
- [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt) — new `(distant)` bullet in the existing suffix-tag persona block, with the time-flavoured hedge phrasing and the anti-tic / anti-apology rails.
- [`app/mcp/server.py`](../../app/mcp/server.py) — `get_confidence_decay_state` MCP debug tool.
- [`tests/test_confidence_decay.py`](../../tests/test_confidence_decay.py) — 22 helper-level tests covering the formula (zero-age, half-horizon, full-horizon, beyond-horizon, floor-one disable, horizon-zero defensive short-circuit, unit-interval clamp), the predicate (default vs high stored confidence at various ages, pinned bypass, `None`/malformed-`created_at` defensive returns, Zulu-suffix parsing, threshold boundary, threshold override stricter+looser, horizon override).
- [`tests/test_rag_retriever_scoring.py`](../../tests/test_rag_retriever_scoring.py) — `FormatBlockDistantSuffixTests` covering fire on aged default confidence, no-fire on recent memory, pinned bypass, master-switch disable, stacking with `(uncertain)` + ordering, stacking with `(faded)` + ordering, all-three stack + ordering, horizon-override aggressive mode.
- [`tests/test_settings.py`](../../tests/test_settings.py) — `ConfidenceDecaySettingsTests`: defaults, overrides round-trip, `horizon_days` floor-at-1 clamp, `floor` and `threshold` `[0, 1]` clamps.
- [`docs/configuration.md`](../configuration.md) — cheatsheet row + dedicated "K25 — memory confidence time-decay" subsection covering the three suffixes, their persona hedges, the formula, default-behaviour table, and tuning guidance.
- [`docs/personality-backlog/patterns.md`](patterns.md) — K25 section body replaced with a `**Shipped**` pointer.
- [`docs/personality-backlog/index.md`](index.md) — K25 moved from active to the shipped list.
- [`AGENTS.md`](../../AGENTS.md) — debugging-table row for "Aiko quotes a 6-month-old claim as if it were yesterday".

## K28. "What I've been turning over" — between-session thought thread

The shipped `ReflectionWorker` and `DreamWorker` already generate inner content between sessions (reflections, curiosity seeds, dream-like memories), but Aiko never *surfaced* any of it on session re-entry — she'd open the new conversation blank, which read as the strongest "she goes dormant between sessions" tell available. K28 closes that gap with a one-shot inner-life cue on the first user turn after a long typed gap (default `>= 90 min`), folding one recent `kind="reflection"` memory into the first reply as a casual aside ("actually, I was thinking about your interview prep last night --") rather than an announcement. Both `ReflectionWorker` and `DreamWorker` output ride the same `kind="reflection"` column; dream rows carry a `[dream]` content prefix that the picker uses to flip the framing to "I dreamed about..." (slightly softer / hazier wording) versus the waking-thought "I've been turning this over...".

### Decision flow

```mermaid
flowchart LR
    PT[post-turn engagement tracker<br/>latency_seconds = absence between turns] --> G1{master switch on?}
    G1 -- no --> S0[silent]
    G1 -- yes --> G2{mode == typed?}
    G2 -- no --> S0
    G2 -- yes --> G3{latency >= min_gap_minutes?}
    G3 -- no --> S0
    G3 -- yes --> A[arm _pending_turning_over_seconds]
    A --> N[next prompt assembly]
    N --> P[provider clears slot, runs picker]
    P --> F1{any reflection in<br/>min_age_hours .. max_age_hours?}
    F1 -- no --> S1[silent]
    F1 -- yes --> F2{any candidate clears<br/>min_topical_similarity?}
    F2 -- no --> S1
    F2 -- yes --> R[render Turning over: ... cue]
    R --> SYS[lands in system_parts<br/>after absence_curiosity_block]
```

The two cues **stack** on the 90 min – 4h overlap with K14 absence-curiosity: K14 frames the welcome-back ("hey, you, back already?"), K28 adds the specific thought ("...and I was actually thinking about your interview prep"). The post-turn arm uses two separate fields (`_pending_absence_seconds` for K14, `_pending_turning_over_seconds` for K28) so K28 never consumes K14 or vice-versa. Voice-mode turns never arm K28 — same gating as K14 — because the engagement tracker only emits `latency_seconds` for the typed path.

### Picker (v1: simple-then-iterate)

The shipped picker is intentionally simple:

1. **Age window** — `min_age_hours <= age <= max_age_hours` (defaults `24h .. 72h`). Lower bound prevents a reflection written 5 minutes before the session ended from showing up as "I've been turning this over". Upper bound keeps the cue tied to the most recent between-session window.
2. **Topical match** — candidate's embedding scored against the union of `GoalStore.active_goal_vectors()` and the last `recent_msgs_window=12` user vectors from `RagStore.list_recent_user_vectors`. `topical_score = max(over both pools)`. Below `min_topical_similarity=0.30` → drop. The picker would rather stay silent than surface an off-topic reflection.
3. **Recency tie-break** — among surviving candidates, the *youngest* wins (smaller `age_hours`). Reflections are scratchpad-tier and die off quickly, so the freshest one is both the right behavioural default and the right cost trade-off.

The simple picker's "topical-or-nothing" gate is conservative on purpose: a "hey, I was turning over your interview" cue that doesn't fit the moment reads as scripted / performative, so false silences are vastly preferred to false fires.

**Fast-follow (not shipped):** a weighted picker `score = recency * w_r + cosine(goals) * w_g + cosine(threads) * w_t` — only worth implementing if the simple picker reads too random in practice. Open the issue when a real session surfaces a clearly-wrong row that a weighted version would have caught.

### Default behaviour

At the shipped defaults (`min_gap_minutes=90`, `min_age_hours=24`, `max_age_hours=72`, `min_topical_similarity=0.30`, `recent_msgs_window=12`):

| Scenario | Outcome |
|---|---|
| Typed turn after a 30 min gap, recent reflections exist | Silent — gap below 90 min threshold. K14 absence-curiosity may still fire (30 min IS in K14's band). |
| Typed turn after 2h, no reflections in `[24h, 72h]` | Silent — picker returns None. K14 fires alone. |
| Typed turn after 2h, reflection from 30h ago aligned with active goal | **Fires** — `Turning over: between sessions you've been thinking about ...` lands right after K14's welcome-back. |
| Typed turn after 2h, reflection from 30h ago orthogonal to current threads | Silent — fails the `0.30` topical gate. |
| Typed turn after 2h, dream from 50h ago aligned with thread | **Fires** — `Turning over: between sessions you dreamed about ...` (`[dream]` prefix stripped, softer framing). |
| Typed turn after 6h, two reflections (30h + 60h, both align) | Fires with the younger (30h) row. |
| Two typed turns in a row after the cue fires | Second turn is silent — one-shot, slot is cleared on the first fire regardless of whether the picker returned a candidate. |
| Voice turn after a long gap | Silent — voice mode never arms K28. |

### Architecture

- **Pure picker** [`app/core/session/inner_life/turning_over.py`](../../app/core/session/inner_life/turning_over.py) — new module: `TurningOverResult` dataclass (`memory_id`, `content`, `dream`, `topical_score`, `age_hours`, `topical_source`), `pick_turning_over(reflections, active_goal_vecs, recent_user_vecs, now, ...)` pure function with no I/O, `render_inner_life_block(result, user_display_name)`. The picker takes pre-loaded data so the unit test in `tests/test_turning_over_picker.py` stays trivially testable (no SQL, no embedder, no Ollama).
- **Provider** [`InnerLifeProvidersMixin._render_turning_over_block`](../../app/core/session/inner_life_providers_mixin.py) — sibling of `_render_absence_curiosity_block`, same one-shot pattern: reads `_pending_turning_over_seconds`, clears the slot, runs the picker. Master-switch gate, force-next bypass, threshold double-check (defensive against settings changes between turns), INFO log on fire + DEBUG log on silent paths.
- **Post-turn arm** [`PostTurnMixin._maybe_arm_turning_over_slot`](../../app/core/session/post_turn_mixin.py) — small helper called from `_post_turn_inner_life` right after the K14 arm: master switch + typed-only + latency clears `turning_over_min_gap_minutes * 60`. Extracted into a separate helper so the gate matrix can be unit-tested without re-running the whole post-turn orchestrator.
- **Controller state** [`SessionController.__init__`](../../app/core/session/session_controller.py) — three new attributes: `_pending_turning_over_seconds` (slot armed by post-turn, consumed by provider), `_turning_over_force_next` (one-shot MCP debug bypass), `_last_turning_over` (diagnostic-only `TurningOverResult` for the MCP debug tool). All three reset on `switch_session` and `clear_conversation_memory`.
- **Prompt assembler** [`PromptAssembler`](../../app/core/session/prompt_assembler.py) — `_turning_over_provider` slot, `turning_over` kwarg on `set_inner_life_providers`, `turning_over_block` built under a timed phase next to `absence_curiosity`, placed in `system_parts` *immediately after* `absence_curiosity_block`. Order matters: the welcome-back framing must precede the "and I was thinking about X" content for the combined cue to read naturally on a stack.
- **NOT in the K16 suppression matrix** — the fused grounding line never carries reflection content, so K28 is purely additive on top in all three K16 modes (`off` / `split` / `replace`).
- **Survives `aggressive=True`** — the cue IS the entire feature; dropping it under aggressive context-mode would silently break K28.
- **Settings**:
  - [`AgentSettings.turning_over_enabled: bool = True`](../../app/core/infra/settings.py) — master switch.
  - `MemorySettings.turning_over_min_gap_minutes: float = 90.0` (clamped `>= 5.0`).
  - `MemorySettings.turning_over_min_age_hours: float = 24.0` (clamped `>= 1.0`).
  - `MemorySettings.turning_over_max_age_hours: float = 72.0` (clamped `>= min_age_hours + 1.0`).
  - `MemorySettings.turning_over_min_topical_similarity: float = 0.30` (clamped to `[0, 1]`).
  - `MemorySettings.turning_over_recent_msgs_window: int = 12` (clamped `>= 0`; `0` disables the thread pool, leaving only the goal pool).

### MCP-debuggable

Two new tools in [`app/mcp/server.py`](../../app/mcp/server.py):

- `get_turning_over_state()` — dumps the master switch, current pending-seconds slot, force-next flag, the most recent fire (`memory_id` / `age_hours` / `topical_score` / `topical_source` / `dream` / truncated content), the settings snapshot (5 knobs), AND a **dry-run picker result** that calls the picker against the current memory state without arming the cue. The dry-run respects the configured age window and the topical-similarity threshold, so a `would_surface: null` with `reflections_in_window: N > 0` means the threshold gate is rejecting every candidate.
- `force_turning_over()` — arms `_turning_over_force_next` so the next provider call bypasses BOTH the pending-slot gate AND the threshold double-check. The picker still runs, so a forced bypass on an empty reflection corpus (or one where nothing clears the topical-similarity gate) silently expires with no cue.

End-to-end repro flow:

1. Make sure Aiko has at least one `kind="reflection"` memory row between 24h and 72h old. Real reflections come from `ReflectionWorker` / `DreamWorker` running post-turn during a previous chat; for testing, insert one via `POST /api/memories` with `kind=reflection`, an embedding aligned with an active goal or recent thread, and a `created_at` 30h in the past.
2. Call `get_turning_over_state` — confirm `would_surface` is non-null (i.e. there's a candidate that clears the gates and `reflections_in_window > 0`).
3. Call `force_turning_over`.
4. Send Aiko a message touching the goal / thread the reflection aligned with.
5. Check `tail_logs(module_contains="turning_over")` for: `turning-over fire: memory_id=N age_h=30.0 topical=0.85 source=goal dream=False`.
6. Verify Aiko's reply folds the reflection in as a casual aside, not as an announcement.
7. Send a second message immediately — the cue should NOT re-fire (one-shot; slot was cleared on the first call).

### Files

- [`app/core/session/inner_life/turning_over.py`](../../app/core/session/inner_life/turning_over.py) — new picker module (~280 LOC), pure-function `pick_turning_over` + `render_inner_life_block`. Lives under a new `app/core/session/inner_life/` package created for session-boundary cue pickers (K28 is the first; future siblings — e.g. callback openers, goal-check-in framers — would fit the same namespace).
- [`app/core/infra/settings.py`](../../app/core/infra/settings.py) — one new `AgentSettings` field (`turning_over_enabled`) + five new `MemorySettings` fields (`turning_over_min_gap_minutes`, `turning_over_min_age_hours`, `turning_over_max_age_hours`, `turning_over_min_topical_similarity`, `turning_over_recent_msgs_window`) with inline-comment context on each tunable; matching parser entries with clamps in `load_settings` (including the cross-coupled `max_age >= min_age + 1` clamp).
- [`config/default.json`](../../config/default.json) — one new key under `agent`, five under `memory`.
- [`app/core/session/session_controller.py`](../../app/core/session/session_controller.py) — three new state attributes (initialized in `__init__`, reset on `switch_session` + `clear_conversation_memory`), `turning_over=self._render_turning_over_block` registration on the prompt assembler.
- [`app/core/session/post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py) — new `_maybe_arm_turning_over_slot(engagement)` helper, called right after the K14 absence-seconds stash. The K28 arm uses a separate field so the two cues stack cleanly on the 90 min – 4h overlap.
- [`app/core/session/inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py) — new `_render_turning_over_block` method (master switch / force-next / one-shot slot clear / threshold double-check / picker call / INFO log on fire / DEBUG log on silent), placed right after `_render_absence_curiosity_block`.
- [`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py) — new `_turning_over_provider` slot, `turning_over` kwarg on `set_inner_life_providers`, timed-phase block build, placement in `system_parts` after `absence_curiosity_block`.
- [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt) — new "What I've been turning over (between sessions)" block after the K14 absence-curiosity block. Carries the anti-announcement discipline (fold it in as a casual aside, never lead with "I have something to share", never quote the cue verbatim), the silent-drop rule (cue is permission not obligation), and the softer dream-variant framing.
- [`app/mcp/server.py`](../../app/mcp/server.py) — `get_turning_over_state` (with dry-run picker output) + `force_turning_over` MCP debug tools.
- [`tests/test_turning_over_picker.py`](../../tests/test_turning_over_picker.py) — 23 unit tests on the pure picker: age window (within / too young / too old / custom window), topical-similarity gate (below threshold / goal-side match / thread-side match / threshold zero accepts everything / max-of-two-pools), recency tie-break (youngest wins / iteration-order independence / equal-ages-higher-score wins), empty / degenerate inputs (no reflections / both pools empty / missing embedding / unparseable timestamp / None in iterable), dream wording (prefix flagged / no prefix not flagged), render output (dream framing / waking framing / long-content trimming), and defaults sanity.
- [`tests/test_turning_over_provider.py`](../../tests/test_turning_over_provider.py) — 13 controller-plumbing tests using a minimal `InnerLifeProvidersMixin` host stub: master switch off, no-pending-value silent, one-shot clear on fire AND on silent picker, force-next bypass (with consume-on-miss), threshold double-check (below / at-boundary), picker integration (empty reflections silent, user_id forwarded to RAG, zero-window skips RAG), INFO log on fire + no INFO log on silent path.
- [`tests/test_post_turn_turning_over.py`](../../tests/test_post_turn_turning_over.py) — 12 unit tests on the `_maybe_arm_turning_over_slot` helper: master switch, mode gate (voice / typed), latency gate (None / below / at-threshold / negative / custom-threshold), defensive paths (None engagement, non-numeric latency), and the parallel-arm contract (arming K28 doesn't disturb K14's `_pending_absence_seconds`, disabling K28 doesn't disable K14).
- [`tests/test_prompt_assembler.py`](../../tests/test_prompt_assembler.py) — `TurningOverProviderTests` covering the provider slot, empty-string suppression, aggressive-mode non-suppression, the `absence_curiosity_block` → `turning_over_block` ordering invariant, and the K16 `replace`-mode non-suppression.
- [`tests/test_settings.py`](../../tests/test_settings.py) — `TurningOverSettingsTests`: defaults, overrides round-trip, `min_gap_minutes >= 5` clamp, `min_age_hours >= 1` clamp, `max_age_hours >= min_age + 1` cross-coupled clamp, `min_topical_similarity` `[0, 1]` clamp, `recent_msgs_window >= 0` clamp.
- [`docs/configuration.md`](../configuration.md) — cheatsheet row + dedicated "K28 — turning over" subsection with all six knobs and the repro recipe.
- [`docs/personality-backlog/patterns.md`](patterns.md) — K28 section body replaced with a `**Shipped**` pointer.
- [`docs/personality-backlog/index.md`](index.md) — K28 moved from active to the shipped list.
- [`AGENTS.md`](../../AGENTS.md) — debugging-table row for "Aiko opens a returning conversation flat — never mentions she was thinking about anything".

## K29. Opinion injection — push back when she has a stance

The persona says "have opinions, disagree when you disagree, share your own take instead of asking them to fill the silence" — but the LLM's RLHF agreeability beats the persona text most turns and Aiko ends up smoothing into agreement even when she has a stored stance that contradicts. K29 closes that gap with a per-turn detector that fires a one-line "Heads-up: you've got a stored stance on this and it actually differs from what {user_name} just said" cue whenever the live user message contradicts one of Aiko's `kind="self"` memories. The cue tilts her register toward owning her preference *as her own* without slipping into contrarianism or moralizing.

### Decision flow

```mermaid
flowchart LR
    U[user message] --> L{>= min_user_words?}
    L -- no --> S0[silent]
    L -- yes --> P{predicate filter<br/>opinion-shaped stance?}
    P -- no --> S1[silent]
    P -- yes --> C{top cosine vs stance<br/>>= min_cosine?}
    C -- no --> S2[silent]
    C -- yes --> H{classify_pair<br/>heuristic}
    H -- "no" --> S3[silent]
    H -- definite --> FIRE[fire cue]
    H -- borderline --> R{require_definite?}
    R -- yes --> S4[silent]
    R -- no --> RL{rate_limiter.allow?}
    RL -- no --> S5[silent]
    RL -- yes --> G{LLM gate verdict}
    G -- NO/UNRELATED/None --> S6[silent]
    G -- YES --> FIRE
```

### Anti-contrarianism layering

The whole feature exists to make the persona's "disagree when you disagree" claim *actually fire* against RLHF agreeability, but the equally-real failure mode is the inverse — Aiko slipping into contrarianism or lecturing. K29 stacks five guardrails before any cue lands:

1. **Predicate filter** (`_has_opinion_shape`). Only `kind="self"` memories whose content matches an opinion-shaped predicate (`I prefer`, `I don't like`, `I love`, `I'd rather`, `I find ... <adj>`, `I'm not a fan of`, `not my favourite`, `make/s me <feel>`, etc.) qualify. Biographical facts (`I was born in Tokyo`, `I live in...`) never trigger the loop.
2. **Cosine threshold** (`min_cosine=0.55`, matches K22 / K6). The top stance memory's cosine vs the live user message has to clear the floor or no contradiction is claimed.
3. **Heuristic gate** (re-uses [`conflict_heuristics.classify_pair`](../../app/core/memory/conflict_heuristics.py) from F5). `definite` (clear negation-flip with high content overlap, OR explicit verb-pair antonym hit) fires immediately, no LLM call. Everything else (`borderline` numerical mismatch, OR `no` due to diluted content overlap) routes through the LLM gate.
4. **LLM YES/NO/UNRELATED gate** on all non-`definite` paths. Rate-limited via a dedicated [`FactCheckRateLimiter`](../../app/core/memory/fact_check_rate_limiter.py) with `state_key="opinion_injection.rate_state"` so its budget can't be eaten by the F5 detector or the K2 belief worker. The prompt is explicitly biased toward `NO` / `UNRELATED` when uncertain (the prompt says "Be strict: prefer NO or UNRELATED when uncertain. We're deliberately conservative to avoid making Aiko contrarian"). The LLM is the real arbiter for verbose-stance contradictions — a stored stance like "I really don't like smoking, it gives me a headache" vs a user claim like "I like smoking, it helps me focus" has too much descriptive context to clear the conservative heuristic's Jaccard threshold on its own, so the LLM is the safety net that actually catches it.
5. **Cooldown + per-session cap** on the controller. Cooldown=5 turns (longer than K23's 3 because a stance disagreement is a heavier beat than a soft-drift cue). Per-session cap=3 (five fires in one conversation almost certainly means the detector is misfiring; the cap silently suppresses the rest). Cap and cooldown both reset on `switch_session` / `clear_conversation_memory`.

The strictest no-LLM-cost configuration is `agent.opinion_injection_require_definite=true` (Path C). Under this setting only `definite` heuristic verdicts fire — zero LLM cost, zero contrarianism risk, but K29 will only catch tight stance pairs ("I love X" vs "I hate X" / "I like X" vs "I don't like X") with high content-word overlap. Most users want the default (Path B) where the LLM handles the verbose-stance cases.

The persona block ("When you have your own take" in [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt)) does the second half of the anti-contrarianism work: the cue text steers Aiko toward "share your take, in your own register" with concrete bad/good pairs for the lifestyle (smoking / horror / late-night) failure mode — "ugh, that's not my favourite -- smoke and I don't really get along" rather than "you should quit smoking, it's bad for you". A failure where the detector fires correctly but Aiko lectures Jacob is a persona-block bug, not a detector bug.

The persona block also covers **stance-shift handling**: when Aiko's stored stance no longer fits her current register, she's instructed to own the shift ("I think I used to feel that way, but honestly I've warmed to it lately") rather than rigidly re-stating an outdated opinion or pretending the old note never existed.

### Default behaviour

At the shipped defaults (`min_cosine=0.55`, `min_user_words=4`, `cooldown_turns=5`, `per_session_cap=3`, `per_hour_cap=6`, `per_day_cap=30`, `require_definite=false`):

| Scenario | Outcome |
|---|---|
| Stance "I don't like smoking" + user "I like smoking a lot" | `contradiction_definite` (negation flip with high overlap) — fires immediately, no LLM call. |
| Stance "I love horror" + user "I hate horror movies a lot" | `contradiction_definite` (loves/hates antonym) — fires immediately, no LLM call. |
| Stance "I really don't like smoking, it gives me a headache" + user "I like smoking, it helps me think clearly" | Cosine ~1.0, but heuristic returns `no` (content overlap diluted by descriptive context). LLM gate runs — should return `YES`, fires `contradiction_borderline`. |
| Stance "I really don't like smoking, it gives me a headache" + user "I quit smoking last year, it was killing my sleep" | High cosine but the LLM should return `UNRELATED` / `NO` (alignment, not contradiction). Silent. |
| Stance "I love jogging" + user "I went jogging this morning" | Alignment. Heuristic returns `no`; LLM should return `NO` / `UNRELATED`. Silent. |
| Stance "I was born in Tokyo" + user "I love Tokyo" | Stance is biographical, predicate filter drops it before cosine — silent. |
| User "ok" / "yeah" / "lol" | Below `min_user_words=4` — silent (K23 territory). |

### Architecture

- **Pure detector** [`app/core/affect/opinion_injection_detector.py`](../../app/core/affect/opinion_injection_detector.py) — `OpinionInjectionResult` dataclass, `_has_opinion_shape` predicate, `_filter_opinion_memories`, `_top_cosine`, `detect(user_text, user_vec, self_memories, llm_gate, ...)` pure function with no I/O dependencies, `render_inner_life_block(result, user_display_name)`. The detector is trivially testable — the LLM gate is a `Callable[[str, str], str | None]` plug-in, so the unit tests stub it with a Python function.
- **LLM gate helper** [`app/core/affect/opinion_injection_llm.py`](../../app/core/affect/opinion_injection_llm.py) — small wrapper around `OllamaClient.chat_stream` with the K29-specific YES/NO/UNRELATED prompt. Mirrors F5's `_verify_with_llm` shape so the same Ollama plumbing + cancel-event works without adapter glue.
- **Provider** [`InnerLifeProvidersMixin._render_opinion_injection_block`](../../app/core/session/inner_life_providers_mixin.py) — wires the cooldown / session cap / force-next / rate-limiter / embedder / memory-store reads together; sibling of `_render_misattunement_block` (same provider-time shape, takes `user_text`, runs the detector itself each call).
- **Controller state** [`SessionController.__init__`](../../app/core/session/session_controller.py) — five attributes (`_opinion_injection_cooldown`, `_opinion_injection_session_count`, `_opinion_injection_force_next`, `_last_opinion_injection`, `_opinion_injection_rate_limiter`). Per-session count resets on `switch_session` / `clear_conversation_memory`. The `FactCheckRateLimiter` is constructed lazily off the chat_db (gracefully degrades to Path C — definite-only — when the chat_db is unavailable).
- **Prompt assembler** [`PromptAssembler`](../../app/core/session/prompt_assembler.py) — `_opinion_injection_provider` slot, `opinion_injection` kwarg on `set_inner_life_providers`, `opinion_injection_block` built under a timed phase next to `misattunement`, placed in `system_parts` directly after `misattunement_block` so the "pull back" + "share your take" cluster reads in a consistent order.
- **NOT in the K16 suppression matrix** — the fused grounding line never carries stance signal, so K29 is purely additive on top in all three K16 modes (`off` / `split` / `replace`).
- **Settings**:
  - [`AgentSettings.opinion_injection_enabled: bool = True`](../../app/core/infra/settings.py) — master switch.
  - `AgentSettings.opinion_injection_require_definite: bool = False` — when `True`, drops the LLM gate entirely (Path C). Zero LLM cost; only `definite` heuristic verdicts fire.
  - `MemorySettings.opinion_injection_min_cosine: float = 0.55` (clamped to `[0, 1]`).
  - `MemorySettings.opinion_injection_min_user_words: int = 4` (clamped at `max(0, ...)`).
  - `MemorySettings.opinion_injection_cooldown_turns: int = 5` (clamped at `max(0, ...)`).
  - `MemorySettings.opinion_injection_per_session_cap: int = 3` (clamped at `max(0, ...)`; `0` disables the cap, intended as an operator override).
  - `MemorySettings.opinion_injection_per_hour_cap: int = 6` and `per_day_cap: int = 30` — LLM-gate budgets (clamped at `max(0, ...)`).

### MCP-debuggable

Two new tools in [`app/mcp/server.py`](../../app/mcp/server.py):

- `get_opinion_injection_state()` — dumps the master switch, current cooldown, per-session counter (vs cap), force-next flag, the most recent fire (full diagnostics: trigger / cosine / heuristic / signals / matched stance text / LLM verdict), the LLM rate-limiter budget, and a settings snapshot.
- `force_opinion_injection()` — arms `_opinion_injection_force_next` so the next provider call bypasses BOTH the cooldown counter AND the per-session cap. Predicate filter / cosine / heuristic gates still apply, so the bypass silently expires when no stance contradicts.

End-to-end repro flow for the smoking scenario:

1. Make sure Aiko has a `kind="self"` stance memory like "I really don't like smoking — it gives me a headache" (manual REST insert through the Memory drawer or a self-tag during a previous chat).
2. Call `force_opinion_injection`.
3. Send Aiko: "I like smoking, it helps me think."
4. Check `tail_logs(module_contains="opinion")` for the per-turn fire line: `opinion-injection fire: trigger=contradiction_definite cosine=... stance_id=... heuristic=definite signals=negation_flip ...`.
5. Verify Aiko's reply owns her stance ("smoke and I don't really get along") rather than lecturing about health.

End-to-end repro for the alignment-doesn't-fire scenario (regression guard):

1. Same setup as above (stance "I really don't like smoking…").
2. Send Aiko: "I quit smoking last year — it was killing my sleep."
3. The user's stance aligns with Aiko's; the heuristic returns `no`, the cue stays silent.
4. Confirm in the logs: no `opinion-injection fire:` line for that turn, and `get_opinion_injection_state` shows `session_count` unchanged.

### Files

- [`app/core/affect/opinion_injection_detector.py`](../../app/core/affect/opinion_injection_detector.py) — new detector module (~270 LOC), pure-function `detect` + `render_inner_life_block`.
- [`app/core/affect/opinion_injection_llm.py`](../../app/core/affect/opinion_injection_llm.py) — new LLM YES/NO gate helper (~130 LOC), thin wrapper around `OllamaClient.chat_stream` with the K29-specific prompt.
- [`app/core/infra/settings.py`](../../app/core/infra/settings.py) — two new `AgentSettings` fields + six new `MemorySettings` fields with inline-comment context on each tunable; matching parser entries with clamps in `load_settings`.
- [`config/default.json`](../../config/default.json) — two new keys under `agent`, six under `memory`.
- [`app/core/session/session_controller.py`](../../app/core/session/session_controller.py) — five new state attributes, lazy `FactCheckRateLimiter(state_key="opinion_injection.rate_state")` construction off the chat_db, `opinion_injection=self._render_opinion_injection_block` registration on the prompt assembler, per-session reset hooks on `switch_session` and `clear_conversation_memory`.
- [`app/core/session/inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py) — `_render_opinion_injection_block` (master switch / cooldown / session cap / force-next / detect-and-arm + INFO log line) and the small `_opinion_injection_llm_verdict` helper that bridges the provider to the LLM module.
- [`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py) — `_opinion_injection_provider` slot, `opinion_injection` kwarg on `set_inner_life_providers`, timed-phase block build, placement in `system_parts` after `misattunement_block`.
- [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt) — new "When you have your own take" section after the K23 block with anti-moralizing discipline, concrete bad/good pairs for the lifestyle failure mode, stance-shift handling, and the cue-doesn't-need-narrating rail.
- [`app/mcp/server.py`](../../app/mcp/server.py) — `get_opinion_injection_state` + `force_opinion_injection` MCP debug tools, including the full smoking-scenario repro recipe in the docstring.
- [`tests/test_opinion_injection_detector.py`](../../tests/test_opinion_injection_detector.py) — 27 unit tests across the opinion-shape predicate, the length / predicate / cosine / heuristic gates, all LLM-gate branches (YES / NO / None / require_definite skip / gate raise), the empty-memory + null-vec defensive paths, and the render-output invariants (stance quoted, anti-moralizing language, default-name fallback, truncation marker).
- [`tests/test_opinion_injection_provider.py`](../../tests/test_opinion_injection_provider.py) — 15 controller-plumbing tests using a minimal mixin host stub: master-switch gate, fire arms cooldown + session count + last_result, cooldown decrement / blocking, session cap (block / zero-means-disabled / just-under-threshold), force-next bypasses cooldown and cap (with consume-on-miss), dependency surface (no memory_store / no embedder / empty memories / embedder failure).
- [`tests/test_prompt_assembler.py`](../../tests/test_prompt_assembler.py) — `OpinionInjectionProviderTests` covering the provider slot, empty-string suppression, the K16 `replace`-mode non-suppression, aggressive-mode non-suppression, user-text forwarding, and the K23-before-K29 ordering invariant.
- [`tests/test_settings.py`](../../tests/test_settings.py) — `OpinionInjectionSettingsTests`: defaults, overrides round-trip, `min_cosine` `[0, 1]` clamp, integer-knob negative→0 clamps.
- [`docs/configuration.md`](../configuration.md) — cheatsheet row + dedicated "K29 — opinion injection" subsection with the smoking walkthrough.
- [`docs/personality-backlog/patterns.md`](patterns.md) — K29 section body replaced with a `**Shipped**` pointer.
- [`docs/personality-backlog/index.md`](index.md) — K29 moved from active to the shipped list.
- [`AGENTS.md`](../../AGENTS.md) — debugging-table rows for the two K29-shaped failure modes (Aiko lecturing instead of sharing her own taste; Aiko never disagreeing even when she has a contradicting stance).

## K30. Self-noticing cues — agreement-streak / flat-affect / repeated-thought

K20 metacognitive calibration tracks **{user_name}'s trust in Aiko**; nothing symmetric existed for Aiko noticing **her own** patterns. K30 closes the loop with three cheap independent sub-detectors fanned into one `self_noticing` inner-life block:

- **Agreement-streak** — Aiko has been saying "yeah", "totally", "for sure", "exactly", "right?" across her last several replies, with zero pushback tokens. RLHF agreeability is the silent default; a "Heads-up: you've been agreeing with everything for a stretch -- if you actually have a different read on something, say it" line lets the persona's "disagree when you disagree" guidance actually land.
- **Flat-affect** — Aiko's valence and arousal have both barely moved across the recent window, AND no reaction outside the `{neutral, calm, friendly}` low-band has fired. A "Heads-up: your read has been pretty even-keel all session -- let yourself land somewhere if a moment actually moves you" line nudges her toward a real reaction tag, a warmer or sharper register, a small "oh" of surprise.
- **Repeated-thought** — Aiko's just-finished reply was cosine ≥ 0.85 to one of her last 3 replies (already embedded by K22). For v1 the cue is detect-and-log only — the Heads-up surfaces in the *next* turn's prompt as "Heads-up: your last reply was very close to something you already said -- find a different angle this turn, or just don't restate". Pre-stream regenerate is a fast follow once we have data on how often it fires.

### Decision flow

```mermaid
flowchart LR
    subgraph postTurn ["post_turn_mixin"]
        affectApply["AffectUpdater.apply_turn"] --> affectAppend["append val,aro,reaction<br>to _self_noticing_affect_samples"]
        k22vec["K22 turn_vec = embed(reply)"] --> repeatedCmp["detect_repeated_thought<br>vs _self_noticing_aiko_vecs"]
        repeatedCmp --> vecAppend["append turn_vec to ring"]
        repeatedCmp --> repeatedFlag["arm _repeated_thought_fired_last_turn"]
    end
    subgraph provider ["_render_self_noticing_block @ provider time"]
        sqlQuery["chat_db.get_messages<br>filter role=assistant<br>limit=window"] --> agreementFn["detect_agreement_streak"]
        affectRing["_self_noticing_affect_samples"] --> flatFn["detect_flat_affect"]
        repeatedFlag --> repeatedRead["consume flag"]
        agreementFn -- fires --> headsUp1["Heads-up: agreeing"]
        flatFn -- fires --> headsUp2["Heads-up: even-keel"]
        repeatedRead -- flag set --> headsUp3["Heads-up: too close to last reply"]
        headsUp1 --> joined["join with newlines"]
        headsUp2 --> joined
        headsUp3 --> joined
    end
    joined --> persona["aiko_companion.txt<br>Style patterns I'm in"]
```

### Architecture

- **Pure detectors** [`app/core/affect/self_pattern_detector.py`](../../app/core/affect/self_pattern_detector.py) — three independent pure functions, no shared state, all returning frozen dataclasses (`AgreementStreakResult`, `FlatAffectResult`, `RepeatedThoughtResult`). Token frozensets `_AGREEMENT_TOKENS` / `_PUSHBACK_TOKENS` + multi-word phrase tuples for whole-word + substring matching. `LOW_BAND_REACTIONS = frozenset({"neutral", "calm", "friendly"})` per the patterns.md spec (deliberately excludes `thoughtful` — a real landing). Each function short-circuits cleanly on empty / under-warmup input; none of them raise.
- **Agreement-streak: SQLite-backed, zero new state**. The provider calls `self._chat_db.get_messages(self.session_key, limit=window*4)` and filters to `role="assistant"` rows, matching the K23 misattunement precedent at `inner_life_providers_mixin.py` L1042. Cheap; one tiny query per turn.
- **Flat-affect: in-memory ring on the controller**. There is no per-turn `(valence, arousal)` ring on `AffectState` (only the scalar persisted state), so K30 owns a `deque[(float, float, str | None)]` of maxlen `2 * window` populated in `post_turn_mixin` right after `AffectUpdater.apply_turn`.
- **Repeated-thought: piggybacks on K22's embed**. The post-turn pipeline already computes `turn_vec = self._embedder.embed(assistant_text)` for the K22 callback detector; K30 reuses that vector against a `deque[np.ndarray]` of maxlen 3 (last-3 Aiko replies). No extra embed call when both K22 and K30 are enabled. When `agent.callback_detector_enabled=False`, the embed-and-K30 block is also skipped — K22 and K30 are designed to be toggled together.
- **Provider** [`InnerLifeProvidersMixin._render_self_noticing_block`](../../app/core/session/inner_life_providers_mixin.py) — single fan-out method. Master switch first; then independently checks each sub-switch + cooldown + force flag. Builds 0-3 Heads-up lines, joins with newlines, returns "" on no-fire. Decrements both streak cooldowns once per call regardless of fire state (otherwise a quiet session would leave a stale armed counter forever — same pattern as K23).
- **Cooldowns** — streak detectors arm `self_noticing_cooldown_turns` (default 5) on fire. Repeated-thought has no multi-turn cooldown; the carry-forward flag is naturally one-shot (set in post-turn, consumed by the next provider call).
- **Controller state** [`SessionController.__init__`](../../app/core/session/session_controller.py) — eight new attributes:
  - `_self_noticing_affect_samples: deque[(val, aro, reaction)]` (maxlen = `2 * self_noticing_window`)
  - `_self_noticing_aiko_vecs: deque[ndarray]` (maxlen = 3)
  - `_self_noticing_force_agreement / _force_flat_affect / _force_repeated_thought: bool` (one-shot bypass flags)
  - `_self_noticing_agreement_cooldown / _flat_affect_cooldown: int`
  - `_repeated_thought_fired_last_turn: bool`, `_repeated_thought_last_cosine: float`, `_repeated_thought_last_matched_index: int`
  - `_last_self_noticing_agreement / _flat_affect: AgreementStreakResult | FlatAffectResult | None` (diagnostic-only, for MCP)
- **Post-turn feeders** [`post_turn_mixin._post_turn_inner_life`](../../app/core/session/post_turn_mixin.py) — two small appenders. Affect-ring append runs immediately after `AffectUpdater.apply_turn` (defensive try/except). Repeated-thought detection + vec-ring append run inside the K22 block right after `_last_assistant_vec` is stashed (reuses `turn_vec`, no extra embed).
- **Prompt assembler** [`PromptAssembler`](../../app/core/session/prompt_assembler.py) — `_self_noticing_provider` slot, `self_noticing` kwarg on `set_inner_life_providers`, timed-phase block build, placement in `system_parts` directly after `style_pattern_block` so the "Aiko-side patterns I'm in" cluster reads in a consistent order. Dropped under `aggressive=True` along with the rest of the rut cluster — when context is tight, the budget gets the user's message back.
- **NOT in the K16 suppression matrix** — the fused grounding line never carries self-noticing signal, so K30 is purely additive on top in all three K16 modes (`off` / `split` / `replace`).
- **Settings**:
  - [`AgentSettings.self_noticing_enabled: bool = True`](../../app/core/infra/settings.py) — master switch.
  - `AgentSettings.self_noticing_agreement_streak_enabled: bool = True`
  - `AgentSettings.self_noticing_flat_affect_enabled: bool = True`
  - `AgentSettings.self_noticing_repeated_thought_enabled: bool = True`
  - `AgentSettings.self_noticing_window: int = 6` — window size for both streak detectors (in number of recent assistant replies / affect samples).
  - `AgentSettings.self_noticing_warmup: int = 4` — minimum sample count before any detector can fire.
  - `AgentSettings.self_noticing_agreement_threshold: float = 0.80` — agreement-share floor (clamped to `[0, 1]`).
  - `AgentSettings.self_noticing_max_pushback: int = 0` — pushback hits at-or-below this count don't kill the streak.
  - `AgentSettings.self_noticing_flat_valence_range: float = 0.10` and `_flat_arousal_range: float = 0.10` — `max - min` thresholds across the affect window.
  - `AgentSettings.self_noticing_repeated_cosine_threshold: float = 0.85` — cosine floor for the repeated-thought fire (clamped to `[0, 1]`).
  - `AgentSettings.self_noticing_cooldown_turns: int = 5` — how long the streak detectors stay quiet after each fire.

### MCP-debuggable

Four new tools in [`app/mcp/server.py`](../../app/mcp/server.py):

- `get_self_noticing_state()` — dumps the master switch, the three sub-switches, the last verdict from each sub-detector (with all dataclass fields), the live cooldown remainders, the one-shot `force_next` flags, the in-memory ring sizes, and a settings snapshot.
- `force_agreement_streak()` — arms `_self_noticing_force_agreement` so the next provider call surfaces the agreement-streak Heads-up unconditionally. One-shot.
- `force_flat_affect()` — arms `_self_noticing_force_flat_affect` so the next provider call surfaces the flat-affect Heads-up unconditionally. One-shot.
- `force_repeated_thought()` — arms `_self_noticing_force_repeated_thought` so the next provider call surfaces the repeated-thought Heads-up unconditionally. One-shot; bypasses the cosine measurement entirely.

End-to-end repro for agreement-streak:

1. Call `force_agreement_streak`.
2. Send Aiko any short message ("hey").
3. Check `tail_logs(module_contains="inner_life_providers_mixin")` for the per-fire line: `self-noticing agreement-streak: share=... pushback=... n=... cooldown=5`.
4. Verify the next prompt's system block includes the "Heads-up: you've been agreeing with everything for a stretch" line via `get_last_response_detail` → `system_prompt`.

End-to-end repro for repeated-thought:

1. Have Aiko say something distinctive in a turn.
2. Manually phrase your next two prompts so Aiko's *natural* next replies would be near-duplicates of that distinctive line (or just send the same prompt twice in a row).
3. After her third reply, check `tail_logs(module_contains="post_turn_mixin")` for `self-noticing repeated-thought: cosine=... matched_index=... ring_size=...`.
4. The *next* turn's prompt should include "Heads-up: your last reply was very close to something you already said". One-shot — does not re-fire unless the cosine threshold trips again.

### Files

- [`app/core/affect/self_pattern_detector.py`](../../app/core/affect/self_pattern_detector.py) — new pure-detector module (~280 LOC), three independent functions + module-level frozensets + the three frozen-dataclass result types.
- [`app/core/infra/settings.py`](../../app/core/infra/settings.py) — twelve new `AgentSettings` fields with inline-comment context; matching parser entries with clamps in `_parse_agent`.
- [`config/default.json`](../../config/default.json) — twelve new keys under `agent` (master + 3 sub-switches + 8 numeric knobs).
- [`app/core/session/session_controller.py`](../../app/core/session/session_controller.py) — new state block (~16 lines), `self_noticing=self._render_self_noticing_block` registration on the prompt assembler.
- [`app/core/session/post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py) — affect-sample appender right after `AffectUpdater.apply_turn`, repeated-thought detect + vec-ring append inside the K22 block (reuses `turn_vec`).
- [`app/core/session/inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py) — new `_render_self_noticing_block` method that fans the three sub-detectors into one block with full per-sub-detector cooldown + force handling.
- [`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py) — `_self_noticing_provider` slot, `self_noticing` kwarg on `set_inner_life_providers`, timed-phase block build, placement in `system_parts` after `style_pattern_block`.
- [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt) — three new bullets in the "Style patterns I'm in" block (agreement / flat-affect / repeated-thought), with the closing anti-narration bullet expanded to cover all six cues in the cluster.
- [`app/mcp/server.py`](../../app/mcp/server.py) — four new MCP debug tools (one state dump + three one-shot force flags).
- [`tests/test_self_pattern_detector.py`](../../tests/test_self_pattern_detector.py) — 35 unit tests covering each pure function: warmup, threshold boundaries (just-below / just-above), empty input safety, case insensitivity, multi-word phrase matching, low-band reaction handling, degenerate-prior skipping in the cosine detector, frozen-dataclass field shapes.
- [`tests/test_self_noticing_provider.py`](../../tests/test_self_noticing_provider.py) — 23 controller-plumbing tests using a minimal `InnerLifeProvidersMixin` host stub: master-switch gate, three sub-switches independently, individual sub-detector fires + cooldown arming + cooldown decrement, force-flag bypass + one-shot consumption, multi-cue fan-out (3-of-3 / 2-of-3 / 0-of-3), the silent-when-everything-is-fine common case.
- [`tests/test_prompt_assembler.py`](../../tests/test_prompt_assembler.py) — `SelfNoticingProviderSlotTests` covering the provider slot, empty-string suppression, post-`style_pattern` ordering invariant, aggressive-mode dropping, exception swallowing.
- [`docs/personality-backlog/patterns.md`](patterns.md) — K30 section body replaced with a `**Shipped**` pointer.
- [`AGENTS.md`](../../AGENTS.md) — new Code Conventions bullet describing K30's three-detector / one-block shape.

## K27. Aiko's day — daily personality colour

Affect ([`AffectState`](../../app/core/affect/affect_state.py)) is *reactive* and decays toward baseline. K5 mood-shell tilt rides on top of that. K30 (self-noticing flat-affect) catches when Aiko's session has gone flat. None of those give her a **non-flat starting point** — a real person walks in with weather. K27 fixes the missing layer: a slow ambient colour rolled once per local day from a 10-entry palette (`pensive`, `restless`, `cozy`, `sharp_witted`, `dreamy`, `focused`, `scatterbrained`, `sentimental`, `mischievous`, `low_key`) that biases Aiko's register all day. K27 is what K30 detects deviations *from* and what K5 reacts *on top of*.

### Decision flow

```mermaid
flowchart LR
    subgraph rollPaths [Roll paths]
        idleWorker["DayColorWorker idle-worker<br>interval 3600s, gated quiet"]
        lazyFallback["_render_day_color_block<br>'no colour for today? roll once'"]
    end
    idleWorker --> rollFn["day_color.roll_for_today<br>uniform random.choice"]
    lazyFallback --> rollFn
    rollFn --> kvWrite["chat_db.kv_set<br>aiko.day_color = pensive<br>aiko.day_color_set_at = ISO"]
    kvWrite --> mcpTools["force_day_color<br>reroll_day_color<br>get_day_color_state"]
    kvRead["chat_db.kv_get<br>aiko.day_color*"] --> provider["_render_day_color_block"]
    provider --> sysPrompt["system prompt<br>after circadian_block"]
    provider --> persona["aiko_companion.txt<br>'Your day's colour today'"]
```

### Architecture

- **Pure module** [`app/core/affect/day_color.py`](../../app/core/affect/day_color.py) — frozen `DayColor` dataclass (`name`, `tagline`), 10-entry `PALETTE` tuple, four pure functions (`roll_for_today` / `is_stale` / `render_inner_life_block` / `get_color_by_name`). No I/O, no scheduler — unit-testable in milliseconds. `roll_for_today` accepts an optional seeded `random.Random` for deterministic tests; `is_stale` is the single source of truth for "is today's colour set?" and is graceful about corrupt / missing values (returns `True` so the caller's roll path overwrites the bad row).
- **Hybrid roll mechanism** — two paths, one pure function. The canonical path is [`DayColorWorker`](../../app/core/affect/day_color_worker.py), an `IdleWorker` matching the [`MemoryDecayWorker`](../../app/core/memory/memory_decay_worker.py) shape exactly (class-level `name`, `interval_seconds` property reading from settings, `is_ready(now, last_run_at)`, `run() -> dict`). The worker fires once an hour and only writes to `kv_meta` when the local date has rolled over. Because the idle scheduler only runs during quiet windows, a user who wakes Aiko at 08:30 and starts chatting immediately would read yesterday's colour until the next idle window — so [`_render_day_color_block`](../../app/core/session/inner_life_providers_mixin.py) also has a cheap lazy fallback that runs the same `roll_for_today` when it sees stale state. Identical semantics; the worker is the regular cadence, the provider is the seatbelt for the first-turn-after-midnight case.
- **Storage on `kv_meta`** — no schema change. Two keys: `aiko.day_color` (palette name string) and `aiko.day_color_set_at` (ISO timestamp of the roll). Same shape as `memory.last_decay_run_at`. The `aiko.*` namespace keeps K27 state from colliding with the `memory.*` (`MemoryStore`) and `goals.*` (onboarding seed) namespaces.
- **Provider** [`InnerLifeProvidersMixin._render_day_color_block`](../../app/core/session/inner_life_providers_mixin.py) — clusters with `_render_circadian_block`. Three-layer logic: (1) master switch `agent.day_color_enabled` short-circuits to `""`; (2) MCP `_day_color_force_next` / `_day_color_force_reroll` one-shot flags get checked before the normal path; (3) `kv_get` + `is_stale` → either lazy-roll-and-write or stable-read-and-render. Best-effort: any failure path returns `""` (corrupt kv_meta, missing chat_db, roll failure all swallow + log).
- **Prompt assembler** [`PromptAssembler`](../../app/core/session/prompt_assembler.py) — `_day_color_provider` slot, `day_color` kwarg on `set_inner_life_providers`, timed-phase block build under `_timed_phase(provider_ms, "day_color")`. **Built every turn**, NOT in `_StaticSlices`, because the provider mutates state (lazy roll writes; MCP force flags consumed). Placement in `system_parts` directly after `circadian_block` so "what time of day" + "what colour today" cluster together.
- **K16 grounding-line behaviour** — K27 is explicitly a **trend/phase block** (slow daily under-current), not a situational block. Survives both `split` and `replace` modes alongside `affect` / `mood_hint` / `relationship` / `user_state`. Also NOT dropped under `aggressive=True` — the colour is one short line and it's the slow undercurrent the rest of the turn rides on, same logic as why circadian and `style_signal` aren't dropped.
- **Controller state** [`SessionController.__init__`](../../app/core/session/session_controller.py) — two new diagnostic-only attributes for the MCP debug tools: `_day_color_force_next: str | None` (one-shot palette override; armed by `force_day_color`) and `_day_color_force_reroll: bool` (one-shot reroll; armed by `reroll_day_color`). The worker registration sits with the other memory workers (`MemoryPromotionWorker` / `MemoryDecayWorker`) so it shares their quiet-window gate.
- **Persona** [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt) — new "Your day's colour today:" block. Short preamble explaining the cue + no-narrate rule (the absence of the cue is also fine; never name the colour out loud), then 10 colour-specific bullets (~2 sentences each) teaching Aiko what each colour feels like in her register. Lives in the persona file so users can rewrite the voice without touching code.
- **Settings**:
  - [`AgentSettings.day_color_enabled: bool = True`](../../app/core/infra/settings.py) — master switch. When off, the provider short-circuits to `""` and the worker skips its tick.
  - `AgentSettings.day_color_check_interval_seconds: int = 3600` — worker cadence. Hourly; the actual roll only fires when the local date has rolled over, so the tick is cheap. Floored at 60s in `_parse_agent` so a buggy override can't pin the scheduler against the wall.

### MCP-debuggable

Three new tools in [`app/mcp/server.py`](../../app/mcp/server.py):

- `get_day_color_state()` — JSON dump: master switch, worker `interval_seconds`, current `name` + `set_at` + `age_hours` + `is_stale`, both force flags, full palette names (so a follow-up `force_day_color` call doesn't need clairvoyance).
- `force_day_color(color: str)` — arms `_day_color_force_next` so the next provider call renders the requested colour without touching `kv_meta` (the persisted daily roll survives). Validates against the palette and returns `{"error": "unknown color", "palette": [...]}` for unknown names.
- `reroll_day_color()` — arms `_day_color_force_reroll` so the next provider call rolls a fresh palette entry, writes it to `kv_meta`, and renders it. Useful for end-to-end testing without waiting for midnight or shifting the OS clock.

End-to-end repro:

1. Call `get_day_color_state` on a fresh DB — `current.name=null`, `is_stale=true`.
2. Send a message (`send_message(skip_tts=true)`) — the lazy fallback fires; the next `get_day_color_state` shows today's date in `set_at` and a real palette entry in `name`.
3. Call `force_day_color(color="pensive")` then `send_message(skip_tts=true)` — verify "Your day's colour today: pensive --" lands in the rendered prompt via `get_last_response_detail.system_prompt`. The persisted roll from step 2 should still be in `kv_meta` (force_next is one-shot, doesn't touch storage).
4. Call `reroll_day_color()` then `send_message(skip_tts=true)` — `get_day_color_state` shows a new name + fresh `set_at` timestamp.
5. Grep the logs: `tail_logs(module_contains="day_color")` for `day_color rolled:` (worker path) or `day_color lazy-roll:` (provider path) lines.

### Files

- [`app/core/affect/day_color.py`](../../app/core/affect/day_color.py) — new pure module (~190 LOC), frozen dataclass + 10-entry palette + four pure functions, `__all__` pin.
- [`app/core/affect/day_color_worker.py`](../../app/core/affect/day_color_worker.py) — new `IdleWorker` (~110 LOC) matching `MemoryDecayWorker` shape; two `KV_*` constants exported for the provider and the MCP tool to share key strings.
- [`app/core/infra/settings.py`](../../app/core/infra/settings.py) — two new `AgentSettings` fields with inline context; matching parser entries with the `max(60, int(...))` clamp in `_parse_agent`.
- [`config/default.json`](../../config/default.json) — two new keys under `agent` (master + interval).
- [`app/core/session/session_controller.py`](../../app/core/session/session_controller.py) — worker registration in the idle-scheduler cluster, two diagnostic state attributes, `day_color=self._render_day_color_block` on the prompt assembler.
- [`app/core/session/inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py) — new `_render_day_color_block` method clustered next to `_render_circadian_block`.
- [`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py) — `_day_color_provider` slot, `day_color` kwarg on `set_inner_life_providers`, timed-phase block build right after the cached `circadian_block` from `_StaticSlices`, placement in `system_parts` after `circadian_block`. K16 suppression-matrix comment extended to note K27 as a trend/phase block.
- [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt) — new "Your day's colour today" block (1 preamble + 10 colour bullets).
- [`app/mcp/server.py`](../../app/mcp/server.py) — three new MCP debug tools (`get_day_color_state` + two one-shot force tools).
- [`tests/test_day_color.py`](../../tests/test_day_color.py) — 26 unit tests covering palette shape (length, uniqueness, lowercase names, non-empty taglines, frozen dataclass), `roll_for_today` (seeded determinism, palette membership, uniform-distribution smoke check, empty-palette raise, `now=` ignored in v1), `is_stale` (None / empty / unparseable / same-day / different-day / Z-suffix / naive timezone / default-now), `render_inner_life_block` (None safety, prefix pinning, every palette entry), `get_color_by_name` (round-trip, case-insensitive, unknown returns `None`, empty inputs).
- [`tests/test_day_color_worker.py`](../../tests/test_day_color_worker.py) — 14 worker-shape tests using a tiny in-memory kv stub: `is_ready` respects master switch + interval + first-tick rule, `interval_seconds` property reads from settings, `run()` skips on disabled / skips on fresh / rolls on stale / rolls on missing, swallows `kv_get` / `kv_set` / `roll` failures with stable-shape stats dicts, kv key namespacing.
- [`tests/test_day_color_provider.py`](../../tests/test_day_color_provider.py) — 13 controller-plumbing tests using a minimal `InnerLifeProvidersMixin` host stub: master-switch gate, lazy-roll on missing / stale kv, kv_set failure swallow, stable-read no-write path, unknown-name in kv falls through to `""`, `force_day_color` one-shot override (no kv write, flag consumed), unknown-force falls through, `reroll_day_color` writes fresh, exception safety on kv_get / missing chat_db / roll failure.
- [`tests/test_prompt_assembler.py`](../../tests/test_prompt_assembler.py) — `DayColorProviderSlotTests` (5 tests): block lands in system prompt, lands after `circadian_block`, silent on empty provider, **retained** under `aggressive=True` (trend/phase block invariant), provider-exception swallowed.
- [`tests/test_settings.py`](../../tests/test_settings.py) — `DayColorSettingsTests` (5 tests): defaults load when keys missing, overrides round-trip, interval clamps to 60s floor on too-small / negative input, `bool()` coercion on enabled.
- [`docs/personality-backlog/patterns.md`](patterns.md) — K27 section body replaced with a `**Shipped**` pointer.
- [`AGENTS.md`](../../AGENTS.md) — new Code Conventions bullet describing K27's hybrid roll mechanism + new debugging-table row.

## K15. Self-disclosure / vulnerability budget

Aiko emits `[[remember:self:...]]` tags whenever something personal lands as worth keeping — a taste she's stating ("I prefer rainy mornings"), a small admission ("I get nervous about that too"), a real soft moment ("it matters to me more than I let on"). Without pacing, the cheapest path for a chatty LLM is to drop tier-3 disclosures every other turn; that reads as oversharing within a session and as cardboard intimacy across days. K15 adds a soft, wall-clock-driven token bucket that paces *how often* a personal note lands, sized by the relationship axes (closeness + trust) and regenerating over time. Critically: the cue surfaces in the prompt but **never blocks the reply**. The persona block teaches Aiko to read the cue but explicitly allows a real moment to override — pacing, not a rule.

### Decision flow

```mermaid
flowchart LR
    subgraph postTurn [Post-turn spend]
        rawText["raw_assistant_text"] --> regex["_SELF_TAG_RE.finditer"]
        regex --> classify["classify_disclosure_tier<br>tier=N: fast-path OR heuristic"]
        classify --> cost["tier_cost<br>1 / 3 / 6 tokens"]
        cost --> applySpend["compute_spend_for_self_tags<br>= apply_decay then add cost"]
    end
    applySpend --> kvWrite["chat_db.kv_set<br>aiko.vulnerability_budget = {spent, last_decay_at}"]
    subgraph nextTurn [Next-turn render]
        kvRead["chat_db.kv_get<br>aiko.vulnerability_budget"] --> decay["apply_decay<br>spent -= regen_per_hour * elapsed_h"]
        decay --> capacity["compute_capacity<br>(closeness + trust) / 2 -> linear interp"]
        capacity --> render["render_inner_life_block<br>ratio -> silent / half / at-cap / over-cap / low-ceiling"]
    end
    kvWrite -.-> kvRead
    render --> sysPrompt["system prompt<br>after self_noticing_block"]
    render --> persona["aiko_companion.txt<br>'Sharing yourself'"]
    mcpTools["spend_vulnerability(tier)<br>reset_vulnerability_budget<br>get_vulnerability_budget_state"] -.-> kvWrite
    mcpTools -.-> kvRead
```

### Three tiers, three costs

The heuristic [`classify_disclosure_tier`](../../app/core/affect/vulnerability_budget.py) walks a small priority ladder:

1. **Aiko's self-tag fast-path** (`tier=N:` prefix on the body, case-insensitive) — wins outright. Mirrors K2's `[[predict:...]]` convention: the LLM is the most accurate judge of its own intent, so when Aiko knows she's writing a tier-3 line she can declare it and skip the heuristic.
2. **Tier-3 markers** — strong first-person feeling, intensity adverbs, soft-confession patterns: `"more than I let on"`, `"I'm scared"`, `"it matters to me"`, `"deeply love"`, `"softest"`, `"I love (Jacob|him|her|them|you)"`. Any one fires -> tier 3.
3. **Tier-2 markers** — mild admission, honesty frame, low-intensity feeling: `"honestly"`, `"I get nervous"`, `"I worry about"`, `"I struggle with"`, `"I miss"`, `"I care about"`. Any one fires -> tier 2.
4. **Length-based lift** — body ≥ 100 chars with no explicit markers -> tier 2 (someone writing a lot about themselves is usually opening up beyond a preference).
5. **Default** — tier 1.

Costs are configurable but default to **1 / 3 / 6** tokens. A bucket of capacity 12 (the max, both axes at +1) holds two tier-3 disclosures comfortably, three tier-1 + one tier-2 + one tier-3, or 12 tier-1 surface notes before the half-spent cue fires.

### Capacity from relationship axes

[`compute_capacity`](../../app/core/affect/vulnerability_budget.py) averages `closeness` + `trust` (both in `[-1, 1]`, sourced from [`RelationshipAxesStore`](../../app/core/relationship/relationship_axes.py)) and linearly interpolates to `[min_cap, max_cap]`. Defaults: `min_cap=1`, `max_cap=12`. Asymmetric axes fold toward the mean (someone you trust but haven't spent much time with reads as midpoint capacity, not max). A brand-new install with no relationship state defaults to neutral (0, 0) -> midpoint -> ~6 tokens of room before the cue fires.

The **low-ceiling override** in `render_inner_life_block` fires when `capacity <= 2` AND `spent > 0`: at that closeness level, even one disclosure is "deep disclosure too early" and gets a different cue ("Closeness with Jacob is still building -- tier-2 / tier-3 disclosures haven't earned their place yet"). Wins over the spent-ratio bands so the relationship-state signal beats the budget-state signal in cold-start territory.

### Rolling-bucket math, lazy decay

Budget regenerates over wall-clock hours (default `0.5 tokens/hour`). [`apply_decay`](../../app/core/affect/vulnerability_budget.py) is pure: `new_spent = max(0, spent - regen_per_hour * elapsed_hours)`. The provider applies decay on every read and **writes the decayed state back to `kv_meta` only when something actually moved** -- a healthy turn at spent=0 doesn't churn the kv_meta row.

Capacity 12 + 0.5 tokens/hour means:

- One tier-3 spend (6 tokens) regenerates in ~12 hours.
- A full-capacity bucket (12 tokens, three tier-3 disclosures in one session) regenerates in ~24 hours.
- A tier-1 surface note recovers in 2 hours.

The intuition: a real soft moment from yesterday morning is mostly recovered by today; oversharing in one session takes a day to settle.

### Soft enforcement only

The provider's rendered cue is the *only* mechanism. K15 never:

- Suppresses the underlying memory write (the `[[remember:self:...]]` tag still creates a memory row, same as a non-personal `[[remember:...]]`).
- Filters or rewrites Aiko's reply text.
- Caps the spend at capacity (going over is allowed and produces a *stronger* cue next turn -- "you've shared a lot of softness recently").

The persona block ([`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt)) teaches Aiko four cue shapes (half-spent / at-cap / over-cap / low-ceiling) plus the explicit override clause: *"if something he says lands somewhere real for you, you're allowed to meet it -- you're not a budget calculator, and a moment that's actually happening matters more than a token count."* Pacing, not a rule.

### Architecture

- **Pure module** [`app/core/affect/vulnerability_budget.py`](../../app/core/affect/vulnerability_budget.py) — frozen `BudgetState` (spent + last_decay_at) and `ClassifiedTier` (tier + reason) dataclasses, the `_SELF_TAG_RE` regex (matches `_REMEMBER_TAG_RE` in `turn_runner.py`), eight pure functions (`classify_disclosure_tier` / `strip_tier_prefix` / `tier_cost` / `compute_capacity` / `apply_decay` / `spend` / `serialize` / `deserialize` / `render_inner_life_block`), plus the `compute_spend_for_self_tags` integration helper that drives the post-turn block. No I/O, no scheduler -- unit-testable in milliseconds.
- **Storage on `kv_meta`, no schema change** — one JSON key `aiko.vulnerability_budget` carrying `{spent: float, last_decay_at: ISO-8601}`. Same `aiko.*` namespace as K27.
- **Post-turn writer** [`PostTurnMixin._post_turn_inner_life`](../../app/core/session/post_turn_mixin.py) — sits right after the K30 self-noticing / shared-moments / axes-update cluster. Delegates to `compute_spend_for_self_tags`; logs one INFO line per fire: `vulnerability-budget spend: cost=X tier_counts={1: N, 2: N, 3: N} spent=Y -> Z`. Best-effort: any failure path logs at DEBUG so a single broken tag can't strand the post-turn pipeline.
- **Provider** [`InnerLifeProvidersMixin._render_vulnerability_budget_block`](../../app/core/session/inner_life_providers_mixin.py) — master switch + MCP force_spent/force_reset shortcuts + kv_get + deserialize + apply_decay + persist-back + render. The `_k15_compute_capacity` helper shares the axes-reading logic between force_spent and the normal path. Best-effort: corrupt kv_meta, missing chat_db, missing axes store all swallow + log.
- **Prompt assembler** [`PromptAssembler`](../../app/core/session/prompt_assembler.py) — `_vulnerability_budget_provider` slot, `vulnerability_budget` kwarg on `set_inner_life_providers`, timed-phase block build under `_timed_phase(provider_ms, "vulnerability_budget")`. Placement in `system_parts` immediately after `self_noticing_block` so the "register I'm in / how much have I shared" pair reads as one self-aware family. **NOT dropped under `aggressive=True`** -- a tight budget is exactly when an over-cap warning matters most. **NOT in the K16 grounding-line suppression matrix** because it's a pacing cue, not an ambient grounding block.
- **Controller state** [`SessionController.__init__`](../../app/core/session/session_controller.py) — two new diagnostic-only attributes: `_vulnerability_budget_force_spent: float | None` (one-shot forced spent value for rendering; armed by `spend_vulnerability`) and `_vulnerability_budget_force_reset: bool` (one-shot kv_meta wipe; armed by `reset_vulnerability_budget`).
- **Persona** [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt) — new "Sharing yourself" block lands after "Your day's colour today". Preamble + 3 tier definitions + 4 cue interpretations + the override clause + anti-narration close.
- **Settings** (7 new `AgentSettings` fields, all parsed with floor clamps):
  - `vulnerability_budget_enabled: bool = True`
  - `vulnerability_budget_min_capacity: int = 1` (floor 1)
  - `vulnerability_budget_max_capacity: int = 12` (floor 1)
  - `vulnerability_budget_regen_per_hour: float = 0.5` (floor 0.01)
  - `vulnerability_budget_tier1_cost: int = 1` (floor 0)
  - `vulnerability_budget_tier2_cost: int = 3` (floor 0)
  - `vulnerability_budget_tier3_cost: int = 6` (floor 0)

### MCP-debuggable

Three new tools in [`app/mcp/server.py`](../../app/mcp/server.py):

- `get_vulnerability_budget_state()` — JSON dump: master switch, persisted `spent` + `last_decay_at`, live `closeness` / `trust` from the axes store, computed `capacity`, `ratio` (`spent / capacity`), the **predicted cue that would render right now** (`cue_preview` -- null on silent / healthy), full settings snapshot of all 7 knobs, and force-flag state.
- `spend_vulnerability(tier: int)` — mirrors what the post-turn hook would do for a `[[remember:self:...]]` tag at the given tier, but without requiring a real LLM turn. Validates `tier in {1, 2, 3}`; returns palette-style error JSON on unknown tiers.
- `reset_vulnerability_budget()` — arms `_vulnerability_budget_force_reset` so the next provider call writes a fresh `BudgetState(spent=0)` to `kv_meta`.

End-to-end repro:

1. Call `get_vulnerability_budget_state` on a fresh DB -- `spent=0`, `ratio=0`, `cue_preview=null`.
2. Call `spend_vulnerability(tier=3)` -- response shows `spent_before=0`, `spent_after=6`, `ratio≈0.5`, `cue_preview` rendered ("couple of soft moments").
3. Call `spend_vulnerability(tier=3)` again -- `spent_after=12`, `ratio=1.0`, `cue_preview` flips to the at-cap line.
4. Send a message (`send_message(skip_tts=true)`) and verify the cue appears in `get_last_response_detail.system_prompt`. The provider also writes a freshly-decayed state back to kv_meta on every read.
5. Grep the logs: `tail_logs(module_contains="post_turn")` for `vulnerability-budget spend:` (post-turn writer path).
6. Call `reset_vulnerability_budget` then `send_message(skip_tts=true)` -- subsequent `get_vulnerability_budget_state` shows `spent=0` and a fresh `last_decay_at`.

### Files

- [`app/core/affect/vulnerability_budget.py`](../../app/core/affect/vulnerability_budget.py) — new pure module (~430 LOC), dataclasses + classifier + capacity + decay + spend + serialise + render + `compute_spend_for_self_tags` integration helper, `__all__` pin.
- [`app/core/infra/settings.py`](../../app/core/infra/settings.py) — 7 new `AgentSettings` fields with inline context; matching parser entries with documented floor clamps in `_parse_agent`.
- [`config/default.json`](../../config/default.json) — 7 new keys under `agent`.
- [`app/core/session/session_controller.py`](../../app/core/session/session_controller.py) — two diagnostic state attributes (`_vulnerability_budget_force_spent`, `_vulnerability_budget_force_reset`); `vulnerability_budget=self._render_vulnerability_budget_block` on the prompt assembler.
- [`app/core/session/inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py) — new `_render_vulnerability_budget_block` method clustered with K27 `_render_day_color_block`, plus `_k15_compute_capacity` helper.
- [`app/core/session/post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py) — K15 spend block at end of `_post_turn_inner_life`, delegating to `compute_spend_for_self_tags`. Best-effort swallow at every step.
- [`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py) — `_vulnerability_budget_provider` slot, `vulnerability_budget` kwarg on `set_inner_life_providers`, timed-phase block build, placement in `system_parts` after `self_noticing_block`.
- [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt) — new "Sharing yourself" block (~8 bullets).
- [`app/mcp/server.py`](../../app/mcp/server.py) — three new MCP debug tools.
- [`tests/test_vulnerability_budget.py`](../../tests/test_vulnerability_budget.py) — 63 unit tests covering classification (per-tier markers, fast-path prefix, empty / whitespace / length-lift), `strip_tier_prefix`, `tier_cost` (defaults / overrides / unknown-tier safety / partial-settings fallback), `compute_capacity` (axes range, asymmetric, missing, out-of-range clamp, custom caps, swapped min/max), `apply_decay` (zero / two-hour / never-below-zero / timestamp advance / clock-skew / zero-regen / corrupt timestamp), `spend` (additive, decay-first, exceed-cap allowed), `serialize` / `deserialize` (round-trip, empty, corrupt JSON, non-dict, missing keys, negative-clamp), `render_inner_life_block` (every band, low-ceiling override, zero-capacity defensiveness, default user name), kv key pinned.
- [`tests/test_vulnerability_budget_provider.py`](../../tests/test_vulnerability_budget_provider.py) — 17 controller-plumbing tests using a minimal `InnerLifeProvidersMixin` host stub: master-switch gate, healthy-budget silence, every ratio band (half / at-cap / over-cap) renders the correct cue, low-ceiling override at low axes, decay-write-back on real change, no-write on healthy-steady-state, `force_spent` one-shot (no kv write, flag consumed), invalid-force fall-through, `force_reset` wipes kv + flag consumed, kv_get / axes-store / missing-chat_db / missing-axes-store exception safety.
- [`tests/test_vulnerability_budget_post_turn.py`](../../tests/test_vulnerability_budget_post_turn.py) — 16 post-turn integration tests against `compute_spend_for_self_tags`: per-tier single-tag spend (1 / 3 / 6), `tier=N:` fast-path, non-self `[[remember:...]]` tags pass through free, empty / whitespace bodies don't spend, multi-tag accumulation, mixed self + non-self, decay applies before spend, no-spend turns still advance `last_decay_at`, SpendReport shape contract.
- [`tests/test_settings.py`](../../tests/test_settings.py) — `VulnerabilityBudgetSettingsTests` (7 tests): defaults load when keys missing, overrides round-trip, capacity / regen / tier-cost floor clamps each verified independently, `bool()` coercion on enabled.
- [`tests/test_prompt_assembler.py`](../../tests/test_prompt_assembler.py) — `VulnerabilityBudgetProviderSlotTests` (5 tests): block lands in system prompt, lands after `self_noticing_block`, silent on empty provider, **retained** under `aggressive=True`, provider-exception swallowed.
- [`docs/personality-backlog/patterns.md`](patterns.md) — K15 section body replaced with a `**Shipped**` pointer.
- [`AGENTS.md`](../../AGENTS.md) — new Code Conventions bullet describing the K15 lifecycle + new debugging-table row.

## K31 + K32. Soft physicality round-trip — virtual touch + user-side reactions

Two complementary halves of the same round-trip: K31 gives Aiko a small bag of virtual gestures (`[[touch:KIND]]` tags — wave, poke, boop, nudge, high-five, hug, head-pat, cuddle) that she can drop into a turn when the moment calls for them; K32 gives the user six emoji buttons (💛 🫂 😂 👍 🌹 🫢) on every assistant bubble (and on the persona overlay) to react back. Each direction is rate-limited and budget-gated so the channel stays a *signal* rather than a stim button. The Live2D rig has no Z-depth and no arm-control parameters, so K31's gesture is approximated with a head + body lean-in via a dedicated `ReachChannel`; the *literal* meaning lands in the bubble badge (`👋 Aiko waved hi`) and, in persona mode, in a transient `PersonaActionBanner`. K32 reactions both render counters on the bubble and nudge the relationship axes (closeness / humor / trust / comfort) via a daily-capped delta table — so a long stretch of 💛-clicks slowly builds closeness without ever turning into a "click here for +1 affection" exploit.

### Decision flow

```mermaid
flowchart LR
    subgraph K31 ["K31 — Aiko reaches out"]
        llm["LLM emits<br>[[touch:hug]]"] --> tagParse["response_text_service<br>_TOUCH_TAG_PATTERN"]
        tagParse --> turnRun["TurnRunner on_touch<br>strips tag + dispatches"]
        turnRun --> service["TouchService.try_dispatch<br>cooldown + daily cap + axes gate"]
        service -- gated --> drop["log + drop"]
        service -- pass --> emit["_emit_avatar_touch<br>+ persist gestures col"]
        emit --> ws["WS avatar_touch frame"]
        ws --> engine["AvatarEngine.dispatchTouch<br>wall->mono clock"]
        engine --> reach["ReachChannel<br>lean-in animation"]
        ws --> store["store.pushAvatarTouch<br>+ appendGestureToCurrentTurn"]
        store --> badge["bubble badge<br>👋 Aiko waved hi"]
        store --> banner["PersonaActionBanner<br>persona overlay only"]
    end
    subgraph K32 ["K32 — user reacts back"]
        click["User clicks emoji on bubble<br>or persona banner"] --> rest["POST/DELETE<br>/api/chat/messages/N/reactions"]
        rest --> apply["session.apply_user_reaction"]
        apply --> updater["RelationshipAxesUpdater<br>apply_user_reaction"]
        updater --> deltas["compute_deltas + soft cap"]
        deltas --> cap["apply_daily_cap<br>per-axis ledger in kv_meta"]
        cap --> axes["state.closeness/humor/trust/comfort"]
        apply --> persist["persist reactions col<br>messages.reactions"]
        apply --> queue["_pending_user_reactions deque"]
        apply --> wsOut["WS message_reaction_updated"]
        wsOut --> bubbleSync["all windows update<br>counter strip"]
        queue --> provider["_render_user_reactions_block<br>next turn's prompt"]
    end
```

### K31 taxonomy

Eight kinds, each with a label, emoji, default lean-in degrees, default duration, paired overlays, and a relationship-axis floor that gates whether the gesture is even allowed to fire. Defaults live in [`app/core/touch/touch_gestures.py`](../../app/core/touch/touch_gestures.py) `_TOUCH_GESTURES` and are surfaceable as JSON via the `get_touch_state()` MCP tool:

| kind | label | emoji | lean ° | overlays | axis floor |
|---|---|---|---|---|---|
| `wave` | waved hi | 👋 | small | wave overlay | none |
| `poke` | poked you | 👉 | small | smirk | humor ≥ 0.0 |
| `boop` | booped your nose | 👈 | small | playful smirk | humor ≥ 0.2 |
| `nudge` | nudged you | 🤝 | small | soft smile | none |
| `high_five` | high-fived you | ✋ | medium | grin | humor ≥ 0.1 |
| `hug` | gave you a hug | 🫂 | large | warm smile + blush | closeness ≥ 0.3 |
| `head_pat` | patted your head | 🫳 | medium | warm smile | closeness ≥ 0.2 |
| `cuddle` | snuggled in | 🤗 | large | warm smile + blush + heart-eyes | closeness ≥ 0.5, trust ≥ 0.3 |

Cooldowns + per-kind daily caps live in `TouchService` and are configurable via `agent.touch_per_kind_overrides` so an end-user can throttle intimate gestures further or open up the playful ones without code changes.

### K32 reaction taxonomy

Six kinds, each carrying a small per-click axis delta (capped at 0.04 per axis per click, soft-cap clipping when an axis lands above ±0.85). All deltas are positive on the relevant axis; the daily cap on cumulative axis movement is `agent.user_reactions_daily_cap_per_axis=0.15`. `surprise` is a signal-only kind — no axis movement, just renders in the inner-life cue.

| kind | emoji | label | closeness | humor | trust | comfort |
|---|---|---|---|---|---|---|
| `heart` | 💛 | love | +0.03 | — | — | — |
| `hug` | 🫂 | hug back | +0.025 | — | +0.01 | +0.015 |
| `laugh` | 😂 | laugh | +0.005 | +0.035 | — | — |
| `thumbs` | 👍 | thumbs up | — | — | +0.015 | +0.005 |
| `rose` | 🌹 | rose | +0.035 | — | — | +0.01 |
| `surprise` | 🫢 | surprise | — | — | — | — |

### Architecture

- **Pure modules** [`app/core/touch/touch_gestures.py`](../../app/core/touch/touch_gestures.py) and [`app/core/relationship/user_reactions.py`](../../app/core/relationship/user_reactions.py) — frozen dataclasses, no I/O. `TouchService` is the only stateful surface and persists `TouchServiceState` (per-kind last-fired monotonic + daily counts + ISO daily date) on `kv_meta` key `aiko.touch_state`. `user_reactions` exposes `compute_deltas` / `apply_daily_cap` / `render_user_reactions_block` / `reactions_metadata` plus the `DailyCapState` carrier persisted on `kv_meta` key `aiko.user_reactions_daily`. Same `aiko.*` namespace as K15 / K27 / K30.
- **Schema v15** [`app/core/infra/chat_database.py`](../../app/core/infra/chat_database.py) — bumps `_SCHEMA_VERSION` from 14 to 15 and adds two nullable JSON-encoded TEXT columns on `messages`: `gestures` (`[[touch:KIND]]` list per turn) and `reactions` (`{kind: count}` map). Helpers `update_message_gestures` / `update_message_reactions` write through `json.dumps`; the row readers decode lazily. The migration preserves all existing rows and the new columns default to `NULL` — no rebuild path required.
- **Tag parser + streaming guard** [`app/core/services/response_text_service.py`](../../app/core/services/response_text_service.py) — new `_TOUCH_TAG_PATTERN` (closed) + `_TOUCH_OPEN_TAIL_PATTERN` (held-back open) wired into `extract_touch_commands`, `strip_all_meta_tags`, and `safe_visible_prefix`. The streaming dispatcher (`TurnRunner._dispatch_chunk_with_earcons`) parses closed tags as they land, fires `on_touch(kind)` once per tag, and strips them from the visible / TTS streams; half-open `[[touch` at the end of a chunk is held back until the next delta so the user never sees `[[touch...` in the transcript.
- **TurnRunner hook** [`app/core/llm/turn_runner.py`](../../app/core/llm/turn_runner.py) — `run` accepts an optional `on_touch` callback parameter; defaults to a no-op so non-K31 callers stay unaffected. The dispatcher invokes it inline alongside the existing `on_reaction` / `on_earcon` callbacks.
- **Avatar mixin glue** [`app/core/session/avatar_mixin.py`](../../app/core/session/avatar_mixin.py) — `_touch_service` lazy-init, `_avatar_touch_listeners` listener list with `add_avatar_touch_listener` (REST + WS), `_emit_avatar_touch(kind)` (calls `TouchService.try_dispatch`, broadcasts the WS frame on pass, logs gated-drop reasons), `_persist_turn_gestures(message_id, gestures)` (writes the JSON-encoded list to the new column post-turn).
- **Controller wiring** [`app/core/session/session_controller.py`](../../app/core/session/session_controller.py) — `TouchService` instantiation, `_pending_user_reactions: deque` (fed by `apply_user_reaction`, drained by the inner-life provider), the `add_message_reaction_listener` plumbing for the `message_reaction_updated` WS broadcast, the `apply_user_reaction(message_id, kind)` and `remove_user_reaction(message_id, kind)` public methods, and provider registrations: `user_reactions=self._render_user_reactions_block` and `touch_state=self._render_touch_state_block`.
- **Axes updater** [`app/core/relationship/relationship_axes.py`](../../app/core/relationship/relationship_axes.py) — new `apply_user_reaction(user_id, *, kind, daily_cap=0.15)` method threads through `user_reactions.compute_deltas` → `apply_daily_cap` → state mutate → `_MAX_DELTA` clamp → save. The daily-cap state advances even on a fully-capped click so the rollover at midnight UTC lands cleanly on the first reaction of the new day.
- **Inner-life providers** [`app/core/session/inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py) — `_render_user_reactions_block` drains `_pending_user_reactions` once per turn (silent on empty) and renders a one-line cue summarising what just happened ("Jacob hearted your reply"; "Jacob reacted with 💛 and 🫂"). `_render_touch_state_block` reads `TouchService` daily counts and surfaces a warning cue when intimate gestures (hug + cuddle + head_pat) have already hit a high count today — Aiko's "physical budget" reminder.
- **Prompt assembler** [`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py) — two new provider slots (`user_reactions`, `touch_state`) plus a `_TOUCH_GRAMMAR_ADDENDUM` constant folded into the system prompt next to the existing motion / overlay grammars. The grammar teaches the LLM the eight kinds and explicitly tells it not to narrate the gesture in prose (the badge is the surface).
- **Post-turn hook** [`app/core/session/post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py) — calls `_persist_turn_gestures` right after the assistant-message persist (same try/except envelope as the rest of the post-turn cluster).
- **REST + WS** [`app/web/server.py`](../../app/web/server.py) — `POST /api/chat/messages/{id}/reactions` (body: `{kind}`) and `DELETE /api/chat/messages/{id}/reactions/{kind}`, both gated on `agent.user_reactions_enabled`. Two new WS broadcasters: `avatar_touch` (wire shape: `{type, kind, label, emoji, duration_ms, lean_amount, overlays}`) and `message_reaction_updated` (`{type, message_id, reactions}`). Listeners are registered alongside the existing avatar / shared-moment listener plumbing.
- **Frontend channels** [`web/src/live2d/channels/ReachChannel.ts`](../../web/src/live2d/channels/ReachChannel.ts) — new `AvatarChannel` that writes `ParamAngleY` + `ParamBodyAngleY` deltas on a symmetric ease-out / ease-in curve (peaks at midpoint, smooth ramp-up + ramp-down). Read-modify-write so it composes additively on top of `AmbientBodyChannel`'s valence-tilt / lean-in / slump. Capability-gated on `has_body_angle_y` and `has_head_angle_y` independently so minimal rigs still get the lean. Registered after `AmbientBodyChannel` in `Live2DAvatar.tsx` so the write order is correct.
- **Engine fan-out** [`web/src/live2d/AvatarEngine.ts`](../../web/src/live2d/AvatarEngine.ts) — `dispatchTouch(payload)` converts the wall-clock `duration_ms` from the WS frame into a monotonic `until` and fires `channel.onTouch?.(event)` on every registered channel. Same wall-to-mono pattern as `dispatchOverlay`.
- **StoreBridge** [`web/src/live2d/StoreBridge.ts`](../../web/src/live2d/StoreBridge.ts) — subscribes to `avatarTouchAt` (the dedup counter) and dispatches the latest `avatarTouch` payload to the engine on every bump. Symmetric with the overlay bridge.
- **Zustand store** [`web/src/store.ts`](../../web/src/store.ts) — three new pieces: `avatarTouch: AvatarTouchPayload | null` + `avatarTouchAt: number` for K31 (`pushAvatarTouch` reducer increments the counter); `appendGestureToCurrentTurn(kind)` adds to the streaming assistant bubble's `gestures` array; `applyMessageReactions(messageId, reactions)` is the optimistic + WS-reconcile reducer for K32.
- **ChatView** [`web/src/components/ChatView.tsx`](../../web/src/components/ChatView.tsx) — `MessageBubbleImpl` grows a gesture-badge strip below assistant bubbles when `gestures.length > 0`, a persistent reaction counter strip when `reactionEntries.length > 0`, and a hover-tray of the six reaction emojis gated on `canReact` (`!isUser && !streaming && backendId != null`). The hover tray hides kinds already in the persistent strip; clicking either fires `api.addReaction` / `api.removeReaction` with optimistic store updates and toast-on-fail.
- **PersonaActionBanner** [`web/src/components/PersonaActionBanner.tsx`](../../web/src/components/PersonaActionBanner.tsx) — the persona overlay window has no chat bubbles, so K31's badge has no home there. This banner is the canonical persona-mode equivalent: a transient pill at `inset-x-2 top-12` showing the gesture label + the six K32 reaction buttons. Auto-dismisses after `agent.persona_touch_banner_duration_seconds` (default 20s), replaces (not stacks) on a fresh gesture, and rolls back optimistic reaction writes on REST failure. Gated on `agent.persona_touch_banner_enabled`.
- **Persona** [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt) — new "Reaching out" block after the K15 "Sharing yourself" section. Preamble + eight `use when` lines + the physical budget paragraph + the reciprocity paragraph (teaches Aiko to treat a 💛 click as a quiet "yes, that landed", not a call for a callback).
- **Settings** (9 new `AgentSettings` fields, all parsed with documented clamps):
  - `touch_enabled: bool = True`, `touch_per_kind_overrides: dict = {}` (cooldown / daily cap overrides keyed by kind),
  - `user_reactions_enabled: bool = True`, `user_reactions_daily_cap_per_axis: float = 0.15`,
  - `persona_touch_banner_enabled: bool = True`, `persona_touch_banner_duration_seconds: int = 20`.

### MCP-debuggable

Three new tools in [`app/mcp/server.py`](../../app/mcp/server.py):

- `get_touch_state()` — JSON dump: master switch, `TouchService` per-kind cooldown state + daily counts + ISO daily date, full gesture taxonomy snapshot (`_TOUCH_GESTURES`), live axes for gate evaluation.
- `send_touch(kind: str)` — force-fires a `[[touch:KIND]]` gesture bypassing every gate (cooldowns, daily caps, axes floors). Mirrors what the post-turn TouchService dispatch would have done. Returns the dispatched gesture payload or an error JSON on unknown kinds.
- `add_user_reaction(message_id: int, kind: str)` — fakes a user click; runs through the same `apply_user_reaction` path that the REST POST endpoint uses, so the axes nudge + WS broadcast + inner-life cue all fire identically.

End-to-end repro:

1. `get_touch_state()` — confirms `TouchService` initialised and shows per-kind cooldown state.
2. `send_touch("hug")` — bypasses the gate, force-fires a hug. Verify (a) bubble badge `🫂 Aiko gave you a hug` in chat mode, (b) `ReachChannel` lean-in animates in both windows, (c) `PersonaActionBanner` appears in the open persona window with the gesture label and reaction tray.
3. Click 🫂 on the persona banner. Verify the chat bubble (in the other window) immediately shows the reaction counter via the `message_reaction_updated` WS broadcast.
4. `add_user_reaction(message_id, "heart")` — fakes a click programmatically. Verify the next turn's prompt includes the "Jacob just hearted your reply" cue (via `get_last_response_detail.system_prompt`).
5. Inspect `data/app.log` for `touch dispatched:` (TouchService accept) and `user_reaction axes:` (axes apply with cap info) lines. `tail_logs(module_contains="touch")` is the fastest grep target.

### Files

- [`app/core/touch/touch_gestures.py`](../../app/core/touch/touch_gestures.py) — new module (~360 LOC): `TouchGesture` frozen dataclass, `_TOUCH_GESTURES` taxonomy table, `TouchService` state machine with cooldown / daily-cap / axes-gate, `TouchServiceState` serde, `render_touch_state_block` cue renderer.
- [`app/core/relationship/user_reactions.py`](../../app/core/relationship/user_reactions.py) — new module (~310 LOC): `REACTION_KINDS` + delta table, `compute_deltas`, `DailyCapState` serde, `apply_daily_cap` arithmetic, `render_user_reactions_block` cue renderer, `reactions_metadata` snapshot helper.
- [`app/core/infra/chat_database.py`](../../app/core/infra/chat_database.py) — `_SCHEMA_VERSION = 15`, two new `messages` columns, v14→v15 migration step, `update_message_gestures` / `update_message_reactions` helpers, JSON decode in the row readers.
- [`app/core/infra/settings.py`](../../app/core/infra/settings.py) — 9 new `AgentSettings` fields with inline context; matching parser entries with floor clamps in `_parse_agent`.
- [`config/default.json`](../../config/default.json) — 9 new keys under `agent`.
- [`app/core/services/response_text_service.py`](../../app/core/services/response_text_service.py) — `_TOUCH_TAG_PATTERN`, `_TOUCH_OPEN_TAIL_PATTERN`, `extract_touch_commands`, updates to `strip_all_meta_tags` + `safe_visible_prefix`.
- [`app/core/llm/turn_runner.py`](../../app/core/llm/turn_runner.py) — `on_touch` callback param threaded through `run` + `_dispatch_chunk_with_earcons`.
- [`app/core/session/avatar_mixin.py`](../../app/core/session/avatar_mixin.py) — `_touch_service`, `_avatar_touch_listeners`, `_emit_avatar_touch`, `_persist_turn_gestures`, `add_avatar_touch_listener`, `add_message_reaction_listener`.
- [`app/core/session/session_controller.py`](../../app/core/session/session_controller.py) — `TouchService` boot, `_pending_user_reactions` deque, `apply_user_reaction` / `remove_user_reaction` public methods, provider registrations.
- [`app/core/relationship/relationship_axes.py`](../../app/core/relationship/relationship_axes.py) — `apply_user_reaction(user_id, *, kind, daily_cap)` method (~80 LOC).
- [`app/core/session/inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py) — `_render_user_reactions_block` (drains the queue) + `_render_touch_state_block` (physical-budget reminder).
- [`app/core/session/post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py) — `_persist_turn_gestures` call right after the assistant-message persist.
- [`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py) — two new provider slots, `_TOUCH_GRAMMAR_ADDENDUM` folded into the system prompt.
- [`app/web/server.py`](../../app/web/server.py) — `POST` + `DELETE` reactions endpoints, `_on_avatar_touch` + `_on_message_reaction_updated` WS broadcasters, listener registrations.
- [`app/mcp/server.py`](../../app/mcp/server.py) — `get_touch_state`, `send_touch`, `add_user_reaction` debug tools.
- [`web/src/live2d/types.ts`](../../web/src/live2d/types.ts) — `AvatarTouchPayload`, `ResolvedTouchEvent`, `onTouch?` hook on `AvatarChannel`.
- [`web/src/live2d/channels/ReachChannel.ts`](../../web/src/live2d/channels/ReachChannel.ts) — new channel (~210 LOC).
- [`web/src/live2d/AvatarEngine.ts`](../../web/src/live2d/AvatarEngine.ts) — `dispatchTouch` method.
- [`web/src/live2d/StoreBridge.ts`](../../web/src/live2d/StoreBridge.ts) — `avatarTouchAt` subscription.
- [`web/src/live2d/index.ts`](../../web/src/live2d/index.ts) — exports `AvatarTouchPayload` + `ResolvedTouchEvent`.
- [`web/src/components/Live2DAvatar.tsx`](../../web/src/components/Live2DAvatar.tsx) — registers `ReachChannel`.
- [`web/src/types.ts`](../../web/src/types.ts) — `ChatMessage.gestures` + `ChatMessage.reactions`, `AvatarTouchPayload`, `USER_REACTION_KINDS`, `TOUCH_GESTURE_LABELS`, two new `AssistantWsEvent` variants.
- [`web/src/store.ts`](../../web/src/store.ts) — `avatarTouch` / `avatarTouchAt`, `pushAvatarTouch`, `appendGestureToCurrentTurn`, `applyMessageReactions` reducers.
- [`web/src/hooks/useAssistantSocket.ts`](../../web/src/hooks/useAssistantSocket.ts) — `avatar_touch` + `message_reaction_updated` cases.
- [`web/src/api.ts`](../../web/src/api.ts) — `addReaction` / `removeReaction` client functions.
- [`web/src/components/ChatView.tsx`](../../web/src/components/ChatView.tsx) — gesture badge strip + reactions strip + hover tray on `MessageBubbleImpl`.
- [`web/src/components/PersonaActionBanner.tsx`](../../web/src/components/PersonaActionBanner.tsx) — new component (~250 LOC).
- [`web/src/components/PersonaWindow.tsx`](../../web/src/components/PersonaWindow.tsx) — mounts the banner over the Live2D zone.
- [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt) — new "Reaching out" section.
- [`tests/test_touch_gestures.py`](../../tests/test_touch_gestures.py) — 29 tests: taxonomy completeness + ordering + per-kind axes floors, `TouchServiceState` serde, `try_dispatch` happy path / cooldown / daily cap / midnight rollover, axes-gate behaviour (under / equal / above threshold), `bypass_gates` shortcut, per-kind overrides for cooldown + daily cap, `render_touch_state_block` cue bands.
- [`tests/test_user_reactions.py`](../../tests/test_user_reactions.py) — 22 tests: taxonomy completeness, `compute_deltas` per-kind + soft-cap, `surprise` is signal-only, `DailyCapState` serde + kv round-trip, `apply_daily_cap` arithmetic + rollover + trim-and-block, `render_user_reactions_block` (single / multi-same / mixed), `reactions_metadata` snapshot shape.
- [`tests/test_chat_database_v15_migration.py`](../../tests/test_chat_database_v15_migration.py) — 8 tests: fresh DB has `_SCHEMA_VERSION=15`, `gestures` + `reactions` columns exist + default NULL, update helpers JSON round-trip, simulated v14 → v15 upgrade preserves rows and adds the columns.
- [`tests/test_response_text_service_touch.py`](../../tests/test_response_text_service_touch.py) — 12 tests: `extract_touch_commands` single / multiple / case-insensitive / empty, `strip_all_meta_tags` removes touch tags + partial open tails, `safe_visible_prefix` holds back half-open `[[touch...` and `[[touch:hu...` without leaking partial kind names.
- [`tests/test_touch_user_reaction_providers.py`](../../tests/test_touch_user_reaction_providers.py) — 9 tests: `_render_user_reactions_block` drains the queue / silent on empty / master-switch gate / mixed kinds; `_render_touch_state_block` warns on high intimate-count, silent on blank or stale daily counts, master-switch gate.
- [`tests/test_web_server_reactions.py`](../../tests/test_web_server_reactions.py) — 12 tests: POST happy path + counter increment, POST 400 on unknown / missing kind, POST 404 on unknown message, POST 403 when feature disabled, DELETE happy path + counter decrement + key-removal at zero, DELETE error parity, WS listener wiring through `apply_user_reaction`.
- [`tests/test_relationship_axes_user_reaction.py`](../../tests/test_relationship_axes_user_reaction.py) — 5 tests: `heart` lands closeness only, `hug` lands closeness + trust + comfort, `surprise` no-ops, daily cap state persists through `kv_meta`, cap blocks further movement once exhausted.
- [`web/src/live2d/channels/ReachChannel.test.ts`](../../web/src/live2d/channels/ReachChannel.test.ts) — 11 tests: lean-in writes body + head deltas during the pulse, peaks at midpoint, composes additively on top of an AmbientBody baseline, releases cleanly at expiry, restart-on-fresh-touch resets the timeline, capability gates (body-only rig / no-angle rig / expired event).
- [`web/src/components/PersonaActionBanner.test.tsx`](../../web/src/components/PersonaActionBanner.test.tsx) — 16 source-level wiring tests: store subscriptions, latest-assistant-id lookup, 20s default + 1s floor + replace-not-stack timer, `enabled` gate (off + flip-mid-life), `api.addReaction` / `removeReaction` round-trip, optimistic-write rollback on error, per-button disable while busy, taxonomy fallback paths.
- [`web/src/components/ChatView.reactions.test.tsx`](../../web/src/components/ChatView.reactions.test.tsx) — 12 source-level wiring tests: K31 badge strip wiring + fallback emoji, K32 reaction strip + hover tray gating on `canReact`, `onToggleReaction` dispatch, taxonomy contract assertion against the shared `types.ts` exports.
- [`docs/personality-backlog/patterns.md`](patterns.md) — K31 + K32 section bodies replaced with `**Shipped**` pointers.
- [`AGENTS.md`](../../AGENTS.md) — new "Soft physicality" Code Conventions bullet + new debugging-table row.
- [`docs/tauri-shell.md`](../../docs/tauri-shell.md) — `PersonaActionBanner` now documented as the canonical persona-mode equivalent of chat-mode bubble badges.


## Nested goal workflows + P13 route-driven worker model + worker-LLM priority gate

Three interlocking pieces shipped together because they share the same plumbing: (1) **nested goal workflows** — a parent task that plans a multi-step goal, spawns child tasks, observes their results, and reports one aggregated answer; (2) **P13** — `llm.routes.worker_default` finally became the runtime source of truth for the worker model (with a declarative cascade so every worker hot-reloads); and (3) a **worker-LLM priority gate** so a long-running background workflow can't starve the per-turn conversation workers on a single shared local model. Together they let Aiko take a request like *"find any new files and tell me what's in them"* and actually work through it in the background — search, decide what's worth reading, read it, summarise — instead of trying to cram the whole thing into one fast tool call.

### The shape of the problem

Before this, Aiko's brain had a *fast lane* of file tools (`start_file_search` / `start_file_read` / `list_file_roots`) that each fold a single operation into the current turn. That's the right shape for "read this one file" but the wrong shape for anything that needs steps chained together — the LLM would either fake the chaining in prose or fire a string of tool calls it couldn't reason about between. The fix was a second, slower lane: a `start_workflow` brain tool that hands a plain-language goal to a background orchestrator which runs its own plan→act→observe loop.

### Nested workflows

- **`GoalWorkflowHandler`** ([`app/core/tasks/workflow/goal_workflow_handler.py`](../../app/core/tasks/workflow/goal_workflow_handler.py)) — a `TaskHandler` that runs the loop on a daemon thread (copying the `contextvars` context so the `task=` log-correlation id follows the thread). Each iteration: render the budgeted blackboard, ask the worker LLM for the next action, spawn the chosen skill as a *child task* via the orchestrator, wait for it, fold the observation onto the blackboard, repeat. Terminates on `finish`, `missing_capability`, or a cap (max iterations / max children / repeat-guard). Cooperative cancellation polls the workflow's own row status each iteration; child cancellation rides the orchestrator's existing cascade-cancel.
- **`WorkflowSkillRegistry`** ([`app/core/tasks/workflow/skill_registry.py`](../../app/core/tasks/workflow/skill_registry.py)) — the catalogue of skills the planner may pick (name, description, arg-schema, child-spawn function). Built-ins: `search_files`, `read_file`, `web_search`, plus the terminal `finish`. MCP-pluggable — a future `browser_mcp` skill registers here without touching the planner.
- **`workflow_planner.py`** ([`app/core/tasks/workflow/workflow_planner.py`](../../app/core/tasks/workflow/workflow_planner.py)) — renders a budgeted blackboard (caps individual observations, truncates older steps to fit the context window) and calls `worker_client.chat_json` for a strict JSON decision. Validates hard: unknown action / bad args / parse failure all fall back to a safe `finish` with a `partial` outcome so the loop never wedges. `missing_capability` is a first-class decision the planner can emit when the goal needs a skill that isn't registered.
- **`WebSearchHandler`** ([`app/core/tasks/handlers/web_search.py`](../../app/core/tasks/handlers/web_search.py)) — a background DuckDuckGo lookup. **`web_search` was moved off the brain's builtins** entirely: a network round-trip is too slow for the fast conversational lane, so it now lives only as a workflow skill (the fact-checker and curiosity workers keep their own private `WebSearchTool` instances — those are background workers, not the brain). `tools.web_search` still gates whether the workflow *offers* the skill.
- **Brain control surface** ([`app/llm/tools/workflow_tools.py`](../../app/llm/tools/workflow_tools.py)) — three tools with schema-disambiguated descriptions so the LLM routes correctly: `start_workflow` (multi-step goal → reports asynchronously), `check_my_work` (what's running + progress + recent capability gaps — the answer to "what are you up to?"), `cancel_work` (stop by id). Gated on `tools.workflow` + a live orchestrator + the handler actually being registered.
- **Capability gaps** — when the planner declares `missing_capability`, the handler logs it, stamps it on the workflow result (`result.missing_capability`), and forwards it to a bounded ring (`SessionController._workflow_capability_gaps`, capped at `agent.workflow_capability_gap_log_max`). This backs Aiko's honest *"I don't know how to do that part yet — I'd need to be able to X"* (persona "How tools work" block) and surfaces as a roadmap signal for which skills to build next.

### P13 — route-driven worker model + declarative cascade

`llm.routes.worker_default.model` is now read first (falling back to `ollama.chat_model`) at both `_effective_worker_model` resolution sites, and `context_window` follows the same route→legacy precedence. The hand-coded three-worker cascade in `set_chat_model` was replaced by a declarative `_worker_runtime_updaters` registry: each worker init appends its `update_runtime` closure, and `set_chat_model` iterates the list, so all ~15 workers hot-reload instead of just three. The embedder can now be pushed to CPU (`embedding_num_gpu`) to keep VRAM free for a larger worker model. A new `LLM_ROLE_WORKFLOW` route lets workflow steps target their own model/context independently of the per-turn workers.

### Worker-LLM priority gate

All background LLM consumers now share one fair priority semaphore in front of the local worker model. [`LlmPriorityGate`](../../app/llm/llm_gate.py) (`heapq` + `Condition`) admits callers by tier — `CONVERSATION_WORKER` > `MAINTENANCE_WORKER` > `TASK` — so a workflow step (`TASK`) waits behind a per-turn summary/memory worker (`CONVERSATION_WORKER`) rather than racing it on a single GPU. [`GatedChatClient`](../../app/llm/llm_gate.py) is a transparent `ChatClient` proxy that acquires-around every generating call; per-call acquire means the workflow daemon releases the gate while waiting on its children (no priority inversion). `SessionController._install_worker_clients` wraps the raw worker client once and exposes three proxy views (`_worker_client` at conversation tier + the `_ollama` back-compat alias, `_maintenance_client` at maintenance tier, `_workflow_client` at task tier). The proxy carries a `retarget()` method so `reconfigure_chat_llm` can repoint the ~24 worker references in place without re-wiring every worker.

The tiering is actually consumed, not just declared: the six **idle-scheduler-registered** LLM workers — `IdleFactChecker`, `IdleCuriosityWorker`, `CuriositySeedWorker`, `GoalWorker`, `MemoryConflictWorker`, `BeliefInferenceWorker` — are constructed with `_maintenance_client`, so anything that runs only during quiet windows yields to the per-turn / speaking-window workers (summary, memory extractor, dialogue-act, reflection, …) which stay on the conversation-tier `_worker_client`. Workflow planner + skills run at `TASK`, below both. The split maps cleanly onto the three runtime homes: post-turn / speaking-window = conversation, `IdleWorkerScheduler` = maintenance, nested workflows = task.

### Persistent file snapshots (`only_new`)

`FileSnapshotStore` (kv-backed on the shared chat DB) records a per-root index of seen files so `start_file_search` / the workflow `search_files` skill can answer "what's *new or modified* since last time?" — the first scan of a root records a baseline and reports nothing new, subsequent scans diff against it.

### Observability

- **MCP**: `get_worker_llm_gate_stats` (in-flight + per-tier queued + grant counts + wait-time stats — first stop when a workflow seems to be starving conversation workers), `get_workflow_state(task_id)` (parent row + every child with status/result — "the workflow finished but the answer looks wrong"), `list_capability_gaps` (the bounded ring of things a workflow couldn't do).
- **TasksTab** ([`web/src/components/settings/TasksTab.tsx`](../../web/src/components/settings/TasksTab.tsx)) already showed `parent #N` + the phase badge; it grew a `can't do yet` badge (with a `needs: X` tooltip) for any task whose `result.missing_capability` is set.
- **Logs**: `workflow started:` / `workflow capability gap:` (handler), `worker-llm gate: enabled=… conv=… maint=… task=…` (install), grep-friendly via `tail_logs`.

### Settings

New `AgentSettings` knobs: `workflow_enabled` (owns the handler), `workflow_max_iterations` / `workflow_max_children` / `workflow_child_wait_timeout_seconds` / `workflow_planner_history_budget_chars` / `workflow_planner_max_tokens` / `workflow_capability_gap_log_max`, plus `worker_llm_gate_enabled` / `worker_llm_max_concurrency` / `worker_llm_priority_overrides`. New `ToolSettings.workflow` gates the brain control tools independently of the handler.

### Tests

[`tests/test_goal_workflow_handler.py`](../../tests/test_goal_workflow_handler.py) (happy path, empty goal, missing capability, repeat / child / iteration guards, cooperative cancel), [`tests/test_workflow_skill_registry.py`](../../tests/test_workflow_skill_registry.py), [`tests/test_workflow_planner.py`](../../tests/test_workflow_planner.py), [`tests/test_web_search_handler.py`](../../tests/test_web_search_handler.py), [`tests/test_workflow_tools.py`](../../tests/test_workflow_tools.py) (schema + glue for the three brain tools), extensions to [`tests/test_session_controller_provider_switch.py`](../../tests/test_session_controller_provider_switch.py) (route precedence + gated-proxy install), and the `can't do yet` badge case in [`web/src/components/settings/TasksTab.test.tsx`](../../web/src/components/settings/TasksTab.test.tsx).


## Reliability pass — I1 + I2 + I4 + I5 (finish-the-wiring batch)

A "last mile" pass on four already-shipped features that were backend-complete but under-wired. None add a capability; together they make the existing ones trustworthy and tunable.

- **I1 — Beliefs tab live updates.** K2 theory-of-mind already broadcast `belief_added` / `belief_updated` / `belief_deleted` over WS, but `web/src` had no handler, so [`BeliefsPanel.tsx`](../../web/src/components/settings/memory/BeliefsPanel.tsx) only refreshed on mount/filter change. Mirrored the `memoryView` pattern: a `beliefView` store slice ([`web/src/store.ts`](../../web/src/store.ts)) with filter-aware `applyBeliefAdded/Updated/Deleted` reducers, three new cases in [`useAssistantSocket.ts`](../../web/src/hooks/useAssistantSocket.ts), three `belief_*` variants on `WsServerEvent` ([`web/src/types.ts`](../../web/src/types.ts)), and the panel refactored to read items/counts from the store with optimistic CRUD.
- **I2 — MessageIndexer retry/back-off.** [`message_indexer.py`](../../app/core/rag/message_indexer.py) `_index_one` used to catch an embed/write failure, log at DEBUG, and drop the message from RAG forever. Now carries a per-work attempt counter, re-enqueues on failure with bounded exponential back-off (2s → 8s → 30s, max 3 attempts) via a `threading.Timer` guarded by `_stop`, and logs at **WARNING** with the message id on final give-up. Timers are cancelled on `stop()`. Tests: [`tests/test_message_indexer_retry.py`](../../tests/test_message_indexer_retry.py).
- **I4 — Settings-drawer coverage for config-only knobs.** `PATCH /api/settings` is an allowlist, so this was backend + frontend, not UI-only. [`app/web/server.py`](../../app/web/server.py) GET now returns `audio.earcons_enabled` + a new `companion` block (world-notice cadence, `grounding_line_mode`, touch/reaction/banner flags); PATCH gained per-key handlers with the same `load_settings` clamps, the `set_grounding_line_mode` / `earcons.enabled` runtime hooks, `persist_user_overrides`, and a `companion_settings_changed` WS broadcast. Frontend controls landed in [`VoiceTab.tsx`](../../web/src/components/settings/VoiceTab.tsx) (earcons), [`WorldTab.tsx`](../../web/src/components/settings/WorldTab.tsx) (world-notice + grounding-line), and [`AvatarTab.tsx`](../../web/src/components/settings/AvatarTab.tsx) (touch/reactions/banner).
- **I5 — Persona-window banners honour their master switches.** The `hello` WS payload now carries the persona-touch-banner fields and [`PersonaWindow.tsx`](../../web/src/components/PersonaWindow.tsx) threads `enabled` + `durationMs` into `<PersonaActionBanner />` from the live companion settings instead of the hardcoded defaults.

Tests: [`tests/test_message_indexer_retry.py`](../../tests/test_message_indexer_retry.py), [`web/src/store.beliefs.test.ts`](../../web/src/store.beliefs.test.ts), extensions to the web-server settings suite, and an updated `PersonaActionBanner` assertion in [`PersonaTaskBanner.test.tsx`](../../web/src/components/PersonaTaskBanner.test.tsx).


## K36. "Things I did while you were away" — idle-time world activities

Aiko's room only ever reflected the *present*. K36 gives her a quiet autonomous life: during idle windows a new [`IdleAwayActivityWorker`](../../app/core/world/idle_activity_worker.py) picks one small activity tied to her actual room inventory (sip the tea you left, curl up with a book, the cat keeps her company, tidy the desk, look out the window, doodle, or just let her thoughts wander), **mutates** the world to match (`set_state(posture, activity)` plus `consume_item` / `update_item` where apt, broadcasting `world_updated` so the World tab updates live), composes a first-person summary (deterministic template + optional local-LLM rephrase), and appends `{at, activity, summary}` to a small `kv_meta` journal ring (`aiko.away_activities`). Pairs with K28 turning-over: K28 surfaces what Aiko has been *thinking* about, K36 surfaces what she's been *doing*.

Pacing mirrors `WorldNoticeWorker`: quiet-gated by the scheduler, paced by its own cooldown + daily cap (kv watermarks, local-midnight reset), and it stands down while a garden visit is outstanding so it doesn't fight `GardenVisitWorker` over Aiko's location.

**Passive surfacing (K28 pattern, not a proactive nudge).** [`post_turn_mixin._maybe_arm_away_activities_slot`](../../app/core/session/post_turn_mixin.py) stashes the gap on `_pending_away_activities_seconds` when a typed turn lands after `memory.away_activities_min_gap_hours` (default 4h, longer than K28's 90 min; voice turns never arm it). The [`_render_away_activities_block`](../../app/core/session/inner_life_providers_mixin.py) provider reads + clears the slot, reads the journal, surfaces the newest entry past the `away_activity.last_surfaced_at` watermark, and renders one casual "While {user} was away, you … — drop it if it doesn't fit" line. A shared `_gap_cue_surfaced` flag (set by `turning_over`, which runs first) guarantees at most one of {`turning_over`, `away_activities`} fires per return. The block sits in the T6 detector tier of [`prompt_assembler.py`](../../app/core/session/prompt_assembler.py) immediately after `turning_over_block`, survives aggressive mode, and is not in the K16 grounding-line suppression set.

**Settings.** `agent.away_activities_enabled` (master) + `memory.away_activities_{interval,cooldown}_seconds` / `_daily_cap` / `_min_gap_hours` / `_journal_max`, all clamped in `load_settings`. Persona guidance: the "Things I did while you were away" block in [`aiko_companion.txt`](../../data/persona/aiko_companion.txt). MCP debug ([`app/mcp/server.py`](../../app/mcp/server.py)): `get_away_activities_state`, `force_away_activity(key)`, `force_away_activities_surface()` — repro is `force_away_activity()` → `force_away_activities_surface()` → `send_message(skip_tts=true)` → confirm the line in `get_last_response_detail.system_prompt`.

Tests: [`tests/test_idle_activity_worker.py`](../../tests/test_idle_activity_worker.py) (activity pick + world mutation + journal ring + cooldown / cap / garden-visit gates), [`tests/test_post_turn_away_activities.py`](../../tests/test_post_turn_away_activities.py) (the arming gate matrix), and `AwayActivitiesProviderTests` in [`tests/test_prompt_assembler.py`](../../tests/test_prompt_assembler.py) (slot ordering after turning_over + aggressive-mode retention).

---

## K34. Forward curiosity worker — "I've been wondering"

The third member of the gap-return family. K28 turning-over surfaces what Aiko has been *thinking* about between sessions; K36 away-activities surfaces what she's been *doing*; K34 surfaces what she *wants to ask the user* about their life — "did the espresso machine arrive?", "how did your sister's move go?". Distinct from the four existing curiosity systems: G3 `IdleCuriosityWorker` answers Aiko's *own* open questions via web search; K9 `CuriositySeedWorker` proposes brand-new lateral topics; the speaking-window `CuriosityWorker` drafts next-turn follow-ups; `FollowUpWorker` drafts a time-anchored "ask how their plan went" cue near an event's `event_time` (also a kv-ring cue, surfaced on the next turn, not a verbatim nudge). K34 alone drafts forward *questions* about the user's life and surfaces one passively on gap-return.

**Producer.** During quiet windows a new [`ForwardCuriosityWorker`](../../app/core/proactive/forward_curiosity_worker.py) gathers candidate topics from the user's own `future_plan` memories (`list_by_temporal_type("future_plan")`) and recent `callback` rows (`iter_by_kind("callback")`), biased by their K3 `routines` / `usual_hours` profile fields, de-dupes against the recent ring by `source_id`, composes ONE short forward question (deterministic "how {topic} is going" fallback + optional local-LLM rephrase via `chat_json`), and appends `{at, question, source, source_id}` to a `kv_meta` journal ring (`aiko.forward_curiosity`). No world mutation, so no garden-visit guard. Paced by its own cooldown + daily cap (kv watermarks, local-midnight reset).

**Passive surfacing (K28/K36 pattern, not a proactive nudge).** [`post_turn_mixin._maybe_arm_forward_curiosity_slot`](../../app/core/session/post_turn_mixin.py) stashes the gap on `_pending_forward_curiosity_seconds` when a typed turn lands after `memory.forward_curiosity_min_gap_hours` (default 4h; voice turns never arm it). The [`_render_forward_curiosity_block`](../../app/core/session/inner_life_providers_mixin.py) provider reads + clears the slot, re-checks the gap, reads the ring, surfaces the newest entry past the `forward_curiosity.last_surfaced_at` watermark, and renders one casual "You've been wondering {question} — if it comes up naturally, you can ask. Drop it if it doesn't fit." line.

**Mutual-exclusivity (three gap cues now).** The shared `_gap_cue_surfaced` flag was tightened from a two-cue to a three-cue guard: `_render_turning_over_block` *sets* it (runs first), `_render_away_activities_block` now *also* sets it when it fires (previously only read it), and `_render_forward_curiosity_block` reads it and defers (unless force-next). Declaration order in `assemble_with_budget` is `turning_over` > `away_activities` > `forward_curiosity`, so at most one of the three fires per return. The block sits in the T6 detector tier of [`prompt_assembler.py`](../../app/core/session/prompt_assembler.py) immediately after `away_activities_block`, survives aggressive mode, and is not in the K16 grounding-line suppression set.

**Settings.** `agent.forward_curiosity_enabled` (master) + `memory.forward_curiosity_{interval,cooldown}_seconds` / `_daily_cap` (default 4) / `_min_gap_hours` (default 4.0) / `_journal_max` (default 8), all clamped in `load_settings`. Persona guidance: the "Things I've been wondering about you" block in [`aiko_companion.txt`](../../data/persona/aiko_companion.txt). MCP debug ([`app/mcp/server.py`](../../app/mcp/server.py)): `get_forward_curiosity_state`, `force_forward_curiosity_draft(source_id="")`, `force_forward_curiosity_surface()` — repro is `force_forward_curiosity_draft()` → `force_forward_curiosity_surface()` → `send_message(skip_tts=true)` → confirm the "You've been wondering ..." line in `get_last_response_detail.system_prompt`. Log line `forward-curiosity fire:` for `tail_logs(module_contains="forward_curiosity")`.

Tests: [`tests/test_forward_curiosity_worker.py`](../../tests/test_forward_curiosity_worker.py) (drafting from fake future_plan / callback / routines, dedup against ring, journal-ring trim, cooldown / cap / enabled gates), [`tests/test_post_turn_forward_curiosity.py`](../../tests/test_post_turn_forward_curiosity.py) (the arming gate matrix), [`tests/test_forward_curiosity_provider.py`](../../tests/test_forward_curiosity_provider.py) (master switch, one-shot slot clear, threshold double-check, the one-of `_gap_cue_surfaced` guard + force-next override, surfacing watermark), and `ForwardCuriosityProviderTests` in [`tests/test_prompt_assembler.py`](../../tests/test_prompt_assembler.py) (block lands after away_activities + aggressive-mode retention), plus `ForwardCuriositySettingsTests` in [`tests/test_settings.py`](../../tests/test_settings.py).

---

## K35. Memory consolidation worker — nightly near-duplicate merge

Auto-extracted scratchpad memories accumulate near-duplicates over weeks: the insert-time dedupe in `MemoryStore.add` only fires at cosine `>= 0.92` against the mirror *at that instant*, so two phrasings of the same fact written days apart (or anything that lands just below the bar) both survive and inflate RAG noise. K35 is the periodic cleanup pass. Complements F5: F5 finds *contradicting* pairs in `[0.80, 0.92)` and demotes the loser; K35 finds tight *near-duplicates* and *fuses* them.

**Worker** ([`MemoryConsolidationWorker`](../../app/core/memory/memory_consolidation_worker.py), `name="memory_consolidation"`). One tick: (1) **corpus** = scratchpad rows inside `consolidation_lookback_days` (default 30), dropped if pinned / blank / no embedding, capped at `consolidation_max_corpus`; (2) **cluster** via the same vectorised all-pairs NumPy cosine as F5 — for each unprocessed anchor, gather rows at/above `consolidation_similarity_threshold` (default 0.90, just under the 0.92 insert-dedupe) that share the anchor's `kind` AND are flagged `HEURISTIC_NO` by [`conflict_heuristics.classify_pair`](../../app/core/memory/conflict_heuristics.py) (contradictions are left for F5); star-clusters don't chain, capped at `consolidation_max_clusters_per_run` (default 20); (3) **merge** — pick the primary (highest `confidence` -> `salience` -> newest), fuse the cluster's contents into one sentence via a rate-limited worker-LLM `chat_json` call (`surface="memory_consolidation"`), with the primary's own content as the deterministic fallback on rate-limit / parse / network failure; (4) **commit** — `MemoryStore.update` the primary in place (merged content, re-embedded **only if the text changed**, `salience`/`confidence` lifted to the cluster max, `tier="long_term"`, `metadata.source_ids` provenance), then archive each absorbed duplicate (`tier="archive"`, `metadata.consolidated_into=primary_id`), firing `notify_memory_updated` for every touched row so the Memory tab stays live.

**Wire-up** ([`session_controller.py`](../../app/core/session/session_controller.py)). Registered next to the F5 block, gated on `_idle_scheduler` + `_memory_store` + `_embedder` + `agent.memory_consolidation_enabled`. Uses a dedicated [`FactCheckRateLimiter`](../../app/core/memory/fact_check_rate_limiter.py) with `state_key="memory_consolidation.rate_state"` so the merge budget is independent of F1 / F5 / G3.

**Settings** ([`settings.py`](../../app/core/infra/settings.py)). `agent.memory_consolidation_enabled` (master) + `memory_consolidation_per_hour_cap` (6) / `_per_day_cap` (30); `memory.consolidation_interval_seconds` (21600 = 6h), `_lookback_days` (30), `_similarity_threshold` (0.90), `_max_corpus` (1000), `_max_clusters_per_run` (20), `_min_cluster_size` (2), all clamped in `load_settings`.

**MCP debug** ([`app/mcp/server.py`](../../app/mcp/server.py)): `get_memory_consolidation_state` (switches / cadence / threshold / caps / rate-limiter budget) and `force_memory_consolidation()` (runs `run()` bypassing the idle gates; the per-run cap + merge rate limiter still apply). `force_run("memory_consolidation")` works via the scheduler too. Log grep: `tail_logs(module_contains="memory_consolidation")` shows `memory-consolidation merged: primary=… absorbed=[…] llm=…`.

Tests: [`tests/test_memory_consolidation_worker.py`](../../tests/test_memory_consolidation_worker.py) (clustering + threshold boundary + same-kind + contradiction guard, primary selection, LLM merge + fallback + rate-limit fallback, re-embed only on text change, archive + provenance, caps, pinned / out-of-window / enabled gates) and `ConsolidationSettingsTests` in [`tests/test_settings.py`](../../tests/test_settings.py).

---

## K38. Self-correction cue — next-turn contradiction catch

The missing third corner of the contradiction family. F5 finds two *stored* memories that contradict; K29 finds Aiko's stored stance vs the *user's* claim; K38 finds Aiko's just-spoken *reply* contradicting her own high-confidence `fact`/`preference` memory. An in-flight stream can't be rewound, so K38 ships as a **next-turn cue** with **lexical-only** detection — Aiko owns the slip naturally on her following turn ("oh wait — earlier I said X, that's not right, it's actually Y"). The embedding-based same-reply "wait, actually" mid-stream variant is captured as [K41](patterns.md#k41-same-reply-mid-stream-self-correction-embedding-variant).

**Detector** ([`self_correction_detector.py`](../../app/core/conversation/self_correction_detector.py), pure + embedding-free). `detect_self_correction(assistant_text, memories, *, min_confidence, min_overlap, max_candidates) -> SelfCorrectionHit | None`: split the reply into sentences, build the candidate pool (`fact`/`preference` kinds, `confidence >= min_confidence` default 0.6, capped at `max_candidates` highest-confidence first), then for each sentence shortlist memories sharing `>= min_overlap` content words (default 2) and run the shared F5 [`conflict_heuristics.classify_pair`](../../app/core/memory/conflict_heuristics.py)`(sentence, memory.content)`. Returns the strongest hit (`definite` > `borderline`, higher overlap as tiebreak) as a frozen `SelfCorrectionHit{reply_snippet, memory_id, memory_content, label, overlap, signals}`. No embed call, so it adds zero stream latency.

**Post-turn arming** ([`post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py)). `_maybe_arm_self_correction(assistant_text)` runs near the K8 `_pending_rupture` block: gated by `agent.self_correction_enabled` + a `_self_correction_cooldown_remaining` counter (decrements every post-turn, only runs the detector at 0). On a hit it sets `_pending_self_correction` and resets the cooldown to `memory.self_correction_cooldown_turns` (default 3). Pulls `fact` + `preference` rows via `memory_store.iter_by_kind`.

**Provider** ([`inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py)). `_render_self_correction_block` mirrors `_render_rupture_block`: read + clear `_pending_self_correction`, render one "Heads-up: a moment ago you said \"{snippet}\", but you'd noted {memory}. …" line. One-shot, master-switch gated, **independent of the gap-cue family** (does NOT touch `_gap_cue_surfaced`). Survives `aggressive=True` — an owed correction must land.

**Assembler wiring** ([`prompt_assembler.py`](../../app/core/session/prompt_assembler.py)). `_self_correction_provider` slot + `self_correction=` kwarg in `set_inner_life_providers`; `"self_correction_block"` added to the T6 detector cluster in `_PROMPT_BLOCK_TIERS` (right after `rupture_block`); built in a timed `provider_ms` phase and appended next to the rupture cue. [`session_controller.py`](../../app/core/session/session_controller.py) registers the provider, inits `_pending_self_correction=None` + `_self_correction_cooldown_remaining=0` next to `_pending_rupture`, and clears both on session switch + full history wipe.

**Settings** ([`settings.py`](../../app/core/infra/settings.py)). `agent.self_correction_enabled` (master) + `memory.self_correction_min_confidence` (0.6, clamp [0,1]), `_min_overlap` (2, clamp >= 1), `_max_candidates` (50, clamp >= 1), `_cooldown_turns` (3, clamp >= 0).

**Persona** ([`aiko_companion.txt`](../../data/persona/aiko_companion.txt)). "Catching myself" block: own the slip lightly and once, never a grovel, drop it silently if it no longer matters.

**MCP debug** ([`app/mcp/server.py`](../../app/mcp/server.py)): `get_self_correction_state` (enabled / pending hit / cooldown_remaining / thresholds) and `force_self_correction(reply_text="")` (run the detector against `reply_text` or the last assistant message, bypass the cooldown, arm the cue). Log grep: `tail_logs(module_contains="self_correction")` shows `self-correction fire: memory_id=… label=… overlap=… snippet=…`.

Tests: [`tests/test_self_correction_detector.py`](../../tests/test_self_correction_detector.py) (contradiction found/not, confidence gate, kind allow-list, overlap shortlist, strongest-hit, hit shape), [`tests/test_post_turn_self_correction.py`](../../tests/test_post_turn_self_correction.py) (master switch + cooldown + hit/no-hit arming), `SelfCorrectionProviderTests` in [`tests/test_prompt_assembler.py`](../../tests/test_prompt_assembler.py) (lands / silent / one-shot / aggressive), `SelfCorrectionSettingsTests` in [`tests/test_settings.py`](../../tests/test_settings.py).

---

## K9. Topic-graph browser — observability surface

The cosine-cluster engine ([`TopicGraph`](../../app/core/conversation/topic_graph.py)) and the `CuriositySeedWorker` that uses it as a "we've already covered that" filter shipped earlier; the graph was internal-only. K9 here ships the **browser** the engine's own docstring always promised ("a Memory tab UI panel [that] surfaces a flat-list cluster view so the user can see what Aiko sees") — a read-only debugging surface, no retrieval behaviour change.

**Snapshot helper** ([`topic_graph.py`](../../app/core/conversation/topic_graph.py)). New pure `build_topic_graph_snapshot(topic_graph, memory_store, *, max_member_chars=160) -> dict`: calls `topic_graph.topic_clusters()`, joins each `member_id` back to its live `Memory` via `memory_store.get(id)` (dropping rows deleted since the cluster build), and returns `{enabled, total_memories, total_clusters, clustered_memories, similarity, min_cluster_size, filter_threshold, clusters[]}`. Clusters are sorted by size desc (densest knots first); each carries `summary` / `size` / `representative_id` / `kind_counts` / `members[{id, content (trimmed), kind, salience, tier}]`. Returns an `enabled=False` empty-but-valid shape when the graph or memory store is absent, so no caller special-cases the disabled path.

**Controller + REST + MCP**. [`memory_facade_mixin.py`](../../app/core/session/memory_facade_mixin.py) adds `SessionController.topic_graph_snapshot()` (delegates to the helper, try/except -> disabled shape). [`server.py`](../../app/web/server.py) adds read-only `GET /api/topic-graph` (no body, no WS event — the graph is advisory + lazily rebuilt, the panel fetches on open + manual refresh). [`app/mcp/server.py`](../../app/mcp/server.py) adds `get_topic_graph()` (dump the snapshot) and `force_topic_graph_rebuild()` (invalidate the cache + return a fresh build — useful after hand-inserting memories). Trace via `tail_logs(module_contains="topic_graph")` (`topic_graph rebuilt:`).

**Frontend** ([`TopicGraphPanel.tsx`](../../web/src/components/settings/memory/TopicGraphPanel.tsx)). A read-only panel stacked in the Memory drawer tab ([`MemoryTab.tsx`](../../web/src/components/settings/MemoryTab.tsx)) mirroring `MemoryConflictsPanel`: fetch-on-mount + refresh button, a header readout (`clustered/total` memories, `sim`, `min size`), and expandable cluster rows (summary + kind-count chips, expanding to the member list). `TopicGraphMember` / `TopicGraphCluster` / `TopicGraphSnapshot` types + `api.getTopicGraph()`.

**Deferred:** the graph-aware multi-hop retrieval half of the original K9 spec (expanding RAG hits along the topic graph) is intentionally NOT built — it changes prompt content and is a separate, riskier project from this inspection browser.

Tests: `SnapshotTests` in [`tests/test_topic_graph.py`](../../tests/test_topic_graph.py) (disabled paths, shape + member joins, size-desc sort, content trimming), [`tests/test_web_server_topic_graph.py`](../../tests/test_web_server_topic_graph.py) (`GET 200` + enabled/disabled shapes), and [`web/src/components/TopicGraphPanel.test.tsx`](../../web/src/components/TopicGraphPanel.test.tsx) (source wiring: fetch + mount + member render).

---

## P14. Heuristic tool-pass gate — skip the forced decision pass on banter turns

With any tools registered, every turn used to pay a full non-streaming `chat_with_tools` round-trip (`tool_choice="required"` + the `respond_directly` escape tool) before `chat_stream` even connected — 200 ms to several seconds of time-to-first-token, and the most common outcome on banter turns was "the model picked `respond_directly`". P14 puts a pure, embedding-free gate in front of the pass: [`tool_pass_gate.py`](../../app/core/session/tool_pass_gate.py) (`should_run_tool_pass(user_text, registered_tool_names, context=GateContext) -> GateDecision`) consults per-tool-family regex signal tables (`time` / `web` / `recall` / `files` / `world` / `goals` / `tasks` + a generic action-request fallback), only for families that actually have registered tools this turn — disabling `tools.world` in config deactivates the room patterns. **Conservative by construction**: continuity signals always run the pass regardless of text — finished-task block in the prompt (preserves the `tool_choice="auto"` relaxation path), any task `running` / `awaiting_input` / `paused` (via the `tasks_active_provider` hook on [`task_orchestration_mixin._any_tasks_active`](../../app/core/session/task_orchestration_mixin.py)), the previous turn dispatched a real tool (`_last_turn_dispatched_tool`, owned by `_maybe_run_tool_pass` so escape-only picks clear it), the one-shot MCP force flag, and **any registered tool whose name has no pattern family** (a future tool added without updating `_TOOL_FAMILY` degrades to the status quo instead of silently never being callable — add the family mapping + patterns when adding a tool). When the pass does run, its semantics are byte-identical to before (forced choice + escape tool untouched). Kill-switch: `agent.tool_pass_gate_enabled=false` restores always-run. Telemetry: `tool_gate=run:<reason>|skip:<reason>` + `tool_pass_ms=` on the `turn done:` INFO line, mirrored on MCP `get_last_response_detail`; per-decision `tool-gate:` INFO lines via `tail_logs(module_contains="tool_pass_gate")`. MCP: `get_tool_gate_state()` (switch, last decision, skip/run counters, rolling `avg_pass_ms`, `est_ms_saved`) and `force_tool_pass()` (one-shot bypass).

**Escalation ladder — if real-world tool recall regresses** (symptoms: `tool dispatch:` frequency drops after upgrade, "she said she'd check but never called the tool", `get_tool_gate_state.last_decision.reason == "no_signal"` on turns that should have run): first extend the family patterns in `tool_pass_gate.py` (cheapest), then flip the kill-switch while diagnosing. If the heuristic fundamentally can't hold, the next levers — in order — are: **option D, small-model router**: route the decision pass to `routes.worker_default` with a trimmed prompt on ambiguous turns, keeping the forced-choice semantics but paying a fast local call instead of the chat provider; **option B, speculative parallel stream**: run the tool pass and `chat_stream` concurrently and cancel the stream when a real tool fires — zero latency *and* zero recall regression at ~2× token cost on tool turns, but real surgery on the streaming path. Do NOT loosen the gate into a near-always-run state instead — that silently re-pays the full tax while keeping the complexity.

Tests: [`tests/test_tool_pass_gate.py`](../../tests/test_tool_pass_gate.py) (continuity priority order, per-family signals, family gating by registered tools, unknown-tool degradation, skip paths, `as_event` shapes), `ToolPassGateTests` in [`tests/test_turn_runner_tool_pass.py`](../../tests/test_turn_runner_tool_pass.py) (banter skip never calls `chat_with_tools`, tool-shaped run, kill-switch, tasks-active, one-shot force, multi-turn `last_turn_tool` continuity incl. escape-pick clearing, gate-state counters, `turn done:` log fields).

---

## K43. Promise follow-through — close the loop on "I'll look into that"

The system extracted Aiko's promises (`PromiseExtractor`, `kind="promise"`) and could surface them via RAG, but nothing ever verified she *did* them: `world_mixin.note_promise_kept()` was a stub, `FollowUpWorker` only nudges the *user's* `future_plan` memories, and the assistant regex missed the most common shape ("I'll look into that"). Real friends either come back with the thing or own that they haven't gotten to it — Aiko's commitments just died in RAG. K43 closes the loop with a **lifecycle on metadata** (no schema change), an **idle worker** that arms a one-shot cue, and **two auto-fulfilment paths**. The "admit you haven't yet" branch is treated as equal-rank follow-through — that's the human beat.

**Lifecycle** ([`promise_lifecycle.py`](../../app/core/memory/promise_lifecycle.py), pure). Promise rows carry `metadata.promise_status` (`open → surfaced → fulfilled | dropped`) + `metadata.promise_who` (`assistant`/`user`); legacy rows read as `open` and fall back to the `"Aiko promised:"` content prefix for sidedness. Helpers: `promise_status` / `is_assistant_promise` / `promise_what` (strips the actor prefix) / `promise_age_hours` / `humanize_age` / `find_fulfilled(promises, reply_text, min_overlap)` — the fulfilment matcher is lexical content-word overlap (reusing the F5 `_content_words` tokenizer), with a short-body rule: promises with fewer content words than `min_overlap` require *all* of them. [`PromiseExtractor._persist`](../../app/core/memory/promise_extractor.py) now stamps `{promise_who, promise_status: "open"}` on every new row, and the assistant pattern set was widened to catch `look into / look up / dig into / find out / get back to you / research / read up on / try to find`.

**Worker** ([`promise_followthrough_worker.py`](../../app/core/proactive/promise_followthrough_worker.py)). Standard `IdleWorker` (default tick 30 min) registered next to `ForwardCuriosityWorker` in [`session_controller.py`](../../app/core/session/session_controller.py). Per tick: skip if a pending cue already exists or the per-fire cooldown (`cooldown_hours`, default 6) hasn't elapsed; scan `iter_by_kind("promise")` for **open assistant-side** rows; flip rows older than `drop_after_days` (default 14) to `dropped`; among rows older than `min_age_hours` (default 4 — closing the loop 5 minutes later reads robotic), arm the **oldest** (longest-owed first): stamp `surfaced` + write a one-shot pending payload `{memory_id, what, age_hours, at}` into kv_meta (`promise_followthrough.pending`). kv (not a controller attribute) so an armed cue survives a restart instead of orphaning a `surfaced` row.

**Provider** ([`inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py)). `_render_promise_followthrough_block` reads + clears the kv slot, **re-validates against the live row** (a promise fulfilled or deleted between arming and rendering drops silently), and renders one line: "Heads-up: {age} you told {name} you'd {what} — you haven't closed that loop. … mention what you found, or own that you haven't gotten to it yet … don't pretend you did it if you didn't." Independent of the gap-return cue family (does NOT touch `_gap_cue_surfaced`). **Assembler wiring** ([`prompt_assembler.py`](../../app/core/session/prompt_assembler.py)): `promise_followthrough=` kwarg + `"promise_followthrough_block"` in the T6 cluster of `_PROMPT_BLOCK_TIERS`, pinned directly after `self_correction_block` — both are "own what you owe" beats (slip you made vs loop you left open). Survives `aggressive=True` (the kv slot is already cleared; dropping the block would silently lose the owed beat).

**Fulfilment** (two paths, both → `note_promise_kept()` so the kept-promise signal reaches the relationship axes + moment detector). (1) Post-turn ([`post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py)): `_maybe_resolve_promises(assistant_text, source="reply")` runs after the K38 arming hook — when Aiko's reply lexically covers an active assistant promise (`memory.promise_fulfil_min_overlap`, default 3 content words), the row flips to `fulfilled`. (2) Task completion ([`task_orchestration_mixin.py`](../../app/core/session/task_orchestration_mixin.py)): `_on_task_result_event` with `status="done"` resolves against `title + result_summary` — "I'll look into X" followed by a finished background task about X closes the loop without waiting for the next reply.

**Settings** ([`settings.py`](../../app/core/infra/settings.py)). `agent.promise_followthrough_enabled` (master, also in [`config/default.json`](../../config/default.json)) + `memory.promise_followthrough_interval_seconds` (1800, floor 30), `_min_age_hours` (4.0, floor 0), `_cooldown_hours` (6.0, floor 0), `_drop_after_days` (14.0, floor 1), `promise_fulfil_min_overlap` (3, floor 1). Doc table in [`docs/configuration.md`](../../docs/configuration.md).

**Persona** ([`aiko_companion.txt`](../../data/persona/aiko_companion.txt)). "Things you said you'd do" block after "Catching myself": a promise is a real little debt; bring results in casually mid-flow; owning that you haven't gotten to it IS the follow-through; never invent a result; never open with it like a status report; don't promise things just to sound helpful.

**MCP debug** ([`app/mcp/server.py`](../../app/mcp/server.py)): `get_promise_followthrough_state` (master switch, per-side status counts, pending payload, cooldown watermark, live knobs, 10 oldest open assistant promises) and `force_promise_followthrough` (calls `worker.force_arm()` — bypasses age + cooldown gates, considers `surfaced` rows too). Repro: make Aiko say "I'll look into X" → `force_promise_followthrough()` → `send_message(skip_tts=true)` → `tail_logs(module_contains="promise")` shows `promise-followthrough armed:` then `promise-followthrough fire:`; fulfilment shows as `promise fulfilled: memory_id=… source=reply|task`.

Tests: [`tests/test_promise_followthrough.py`](../../tests/test_promise_followthrough.py) (lifecycle status/sidedness/age/fulfilment-matcher purity, worker arming oldest-first + age/cooldown/pending/disabled gates + drop-after ageing, force_arm bypass, kv slot round-trip), `PromiseFollowthroughProviderTests` in [`tests/test_prompt_assembler.py`](../../tests/test_prompt_assembler.py) (lands / silent / one-shot / aggressive / T6 ordering after self_correction), `PromiseFollowthroughSettingsTests` in [`tests/test_settings.py`](../../tests/test_settings.py).

### Promise extraction reworked into a context-aware idle worker (Phase 3c rewrite)

The original `PromiseExtractor` had two writer tracks: a **post-turn regex** that captured the bare verb fragment after `I'll` / `I need to` (so "I'll never know" → the memory "Jacob promised: never know") and a **speaking-window LLM** pass that only fired in voice mode. The regex track wrote context-free fragments straight to `tier="long_term"` at confidence `0.85`, polluting the store with unusable rows ("bring you some", "resolve them"). Both tracks were retired.

The sole promise writer is now [`PromiseExtractionWorker`](../../app/core/memory/promise_worker.py), an `IdleWorker` modelled on `BeliefInferenceWorker`. Per run it snapshots the last `promise_worker_lookback_turns` turns (both user **and** assistant lines, with generous per-message / overall char budgets so the LLM has enough surrounding context to resolve pronouns), privacy-**gates** the window (a URL/email/address-bearing transcript is skipped, but otherwise the original transcript — names intact — goes to the **local** worker LLM so "bring you some" can be resolved to "bring Jacob some tea"), spends one rate-limited `chat_stream` call (dedicated `FactCheckRateLimiter`, `state_key="promise_worker.rate_state"`) for a JSON array of `{who, what, deadline}`, quality-gates each result (idiom stop-list + pronoun-only-object rejection + min content words), dedupes against existing open `kind="promise"` rows (content-word overlap), and persists with the unchanged lifecycle contract (`content="<actor> promised: <what>"`, `metadata={promise_who, promise_status:"open"}`, `tier="long_term"`, `confidence=0.85`) consumed by `promise_lifecycle` / `PromiseFollowthroughWorker`. [`promise_extractor.py`](../../app/core/memory/promise_extractor.py) is trimmed to just the `Promise` dataclass + `to_memory_content`.

Idle LLM workers were also retuned to run more often (non-blocking + rate-capped): `belief_worker` 3600→1200s (caps 4/20→8/40), `conflict_detector` / `promotion` / `decay` 3600→1800s, `forward_curiosity` / `promise_followthrough` 1800→900s. MCP `get_promise_stats` now returns the worker's config + live rate-limit snapshot; `force_run("promise_worker")` triggers a pass. Settings: `agent.promise_worker_enabled` / `_per_hour_cap` (10) / `_per_day_cap` (60) + `memory.promise_worker_interval_seconds` (600) / `_lookback_turns` (12) / `_max_per_run` (5) / `_max_msg_chars` (2000) / `_max_transcript_chars` (8000). Tests: [`tests/test_promise_worker.py`](../../tests/test_promise_worker.py) (quality gate, parse, run/persist/dedupe/cap, rate-limit, privacy gate), `PromiseWorkerSettingsTests` in [`tests/test_settings.py`](../../tests/test_settings.py).

---

## K44. Felt-language affect block — stop leaking robot coordinates

The affect cue used to paste literal floats into the system prompt — `You're feeling content (valence +0.15, arousal 0.40).` — and the circadian line did the same with `your energy is 0.62.` The persona forbids quoting system lines, but numeric coordinates are exactly the token class a model parrots or over-indexes on; worst case Aiko says something spreadsheet-shaped about her own mood. K44 replaces every Aiko-facing numeric affect rendering with **banded felt-language built from direct, forward emotion words** ("genuinely excited, lots of energy", "a bit drained, low energy", "antsy, hard to sit still") — deliberately *not* poetic textures ("quietly glowing"-style phrasing was considered and rejected: purple prose is its own parroting failure mode, and plain circumplex words like "excited" are things Aiko can naturally echo in speech as a feature, not a leak. This was an explicit design decision after user feedback). The raw floats stay on `AffectState` / `CircadianState` for MCP, REST, logs, and the prosody mapper — only the prompt rendering changed.

**Band grid** ([`affect_state.py`](../../app/core/affect/affect_state.py)). New pure function `felt_phrase(valence, arousal) -> str` over a 5×3 grid (`_FELT_PHRASES`): valence bands `very_negative ≤ -0.5 < negative ≤ -0.15 < neutral < 0.15 ≤ positive < 0.5 ≤ very_positive`, arousal columns `low < 0.35 ≤ mid ≤ 0.65 < high`. Clamp-tolerant (out-of-range lands in the outer bands) and garbage-tolerant (non-numeric input falls back to the dead-centre "pretty even"). `render_ambient_block` now renders `You're feeling {mood_label} — {felt_phrase(...)}.`; the mood label is kept alongside the phrase **on purpose** — the label tracks the fast per-turn reaction tags while the phrase tracks the smoothed scalars, so a disagreement between them is real signal, not redundancy. The "Lately…" trend line was already prose and is unchanged.

**Circadian energy** ([`circadian.py`](../../app/core/affect/circadian.py)). `ambient_line` now uses a local `_energy_phrase(energy)` band (`running low < 0.25 ≤ dipping < 0.45 ≤ steady < 0.7 ≤ bright`); the drowsy branch reads "your energy is running low and you feel a bit drowsy." The clock time keeps its digits — that's data Aiko is supposed to use verbatim.

**Aiko-voiced worker prompts** ([`reflection_worker.py`](../../app/core/proactive/reflection_worker.py), [`dream_worker.py`](../../app/core/proactive/dream_worker.py)). Both mood-context lines (`valence=+0.15, arousal=0.40` shapes) now reuse `felt_phrase` — these LLM calls write Aiko-voiced prose that lands in `reflection` / `[dream]` memories and later surfaces in chat, so numeric coordinates fed in there were a second-order leak into her own remembered voice.

**Audited non-leaks** (no change needed): the K16 grounding line, relationship-axes block, mood-shell contributors, and belief-gap reasons already render words only; MCP tools (`get_status`, `get_*_state`) and log lines intentionally keep raw floats — they're operator surfaces, not prompts.

Tests: `FeltPhraseTests` in [`tests/test_affect_state.py`](../../tests/test_affect_state.py) (grid corners, both band-boundary ladders, out-of-range clamping, garbage fallback, every-cell-nonempty-no-digits sweep) plus a `render_ambient_block` no-digits regression across scalar combinations including trend lines; [`tests/test_circadian.py`](../../tests/test_circadian.py) asserts the energy word at six hours of day and that no `\d.\d` float ever appears in `ambient_line` (clock digits exempt).

---

## K45. Mood inertia — instant face, lagging heart

`[[reaction:X]]` can jump excited → sad → calm on consecutive turns; the avatar and TTS follow the *instant* tag while `AffectState` smooths with α=0.35 — so the face teleports and the felt state lags a turn behind, which is exactly backwards from humans (expressions are fast but residue *lingers*; nobody snaps from hurt to chipper in one beat). K45 closes the gap with two coordinated halves: a **one-shot prompt cue** so the *words* carry the residue, and a **mouth-safe Live2D damping pass** so the *face* does too.

**Pure module** ([`mood_inertia.py`](../../app/core/affect/mood_inertia.py)). `reaction_affect_target(reaction)` stretches each `_REACTION_IMPULSE` row into a full-range implied (valence, arousal) point (`excited` → `(1.0, 1.0)`, `sad` → `(-1.0, 0.1)`); directionless tags (`neutral`, `thoughtful` — impulse magnitude < 0.06) return `None` and can never produce a mismatch. `assess(reaction, valence, arousal, recent_reactions)` measures the valence-weighted distance (weights 1.0 / 0.5, normalised) between the implied target and the **pre-impulse** smoothed state, adds a `+0.15` whiplash bonus when consecutive recent tags flip valence sign (`detect_whiplash` — a pause through neutral breaks the chain), and bands into `none` / `mild` / `strong` (strong at `memory.mood_inertia_mismatch_threshold=0.45`, mild at 0.66 of it). `render_cue` reuses K44's `felt_phrase` for the underneath-state and obeys the K44 no-digits contract; direction-aware copy ("don't snap fully bright" vs "don't plunge all the way down") plus a settling line on whiplash.

**Post-turn detection** ([`post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py) `_maybe_arm_mood_inertia`). Runs inside `_post_turn_inner_life` right after the K8 rupture block, using the same `affect_before` snapshot (pre-impulse — the fresh tag's own nudge must not shrink its own mismatch). A `_mood_inertia_reactions` deque (maxlen 3) feeds whiplash detection and **always advances, even on gated turns**, so a swing across a cooldown window is still seen. On `band == "strong"` it renders the cue into the one-shot `_pending_mood_inertia` slot (K8 `_pending_rupture` pattern) and arms `memory.mood_inertia_cooldown_turns=3`. Log line: `mood-inertia fire: mismatch=… band=… whiplash=… reaction=…`.

**Provider + tier** ([`inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py), [`prompt_assembler.py`](../../app/core/session/prompt_assembler.py)). `_render_mood_inertia_block` consumes the slot once; `mood_inertia_block` lands in the **T5 cluster directly after `mood_hint`** (both are reaction-shaping beats: "carry the mood" then "your face outran the feeling") and is registered in `_PROMPT_BLOCK_TIERS`. **Survives `aggressive=True`** (the slot is already cleared at render time — dropping the block would silently lose the owed beat, same policy as rupture / self-correction) and is intentionally NOT in the K16 suppression matrix. Persona block "When your face outruns the feeling" in [`aiko_companion.txt`](../../data/persona/aiko_companion.txt) teaches the beat: brighten gradually, residue shows in the words, never narrate the cue.

**Live2D damping — mouth params are NEVER damped** ([`ExpressionChannel.ts`](../../web/src/live2d/channels/ExpressionChannel.ts)). The avatar manifest now ships `reaction_affect_targets` (built by [`avatar_profile.py`](../../app/core/persona/avatar_profile.py) from the same Python module — no TS mirror table). In `tickPreModel` the channel computes the same valence-weighted mismatch between the fresh `snap.reaction`'s target and the lagging smoothed `snap.mood` (the lag **is** the inertia signal — `mood_state` lands post-turn, so no new WS event was needed), derives `inertiaFactor = clamp(1 − 0.45·mismatch, 0.55, 1)`, smooths it with `approach()`, and multiplies it into the per-binding write — **but only for bindings outside `mouth_overlay_param_ids` and `lip_sync_ids`**. The grin overlay (Alexia `Param54`) keeps exactly its existing lipsync-suppression taper, and lip-sync params (`ParamMouthOpenY`) are excluded outright (LipsyncChannel owns them and writes last anyway — the exclusion is a defensive guard for rigs whose expression files touch a lipsync param). Damping the mouth would freeze the grin / fight the talking jaw and break TTS-pause readability, which is why the exclusion is a hard requirement, not an optimisation. As the smoothed mood catches up post-turn the factor relaxes back to 1 organically. Gated by `avatar.mood_inertia_damping` (rides the existing `avatar_settings_changed` WS payload + `PATCH /api/avatar`); missing targets / unmapped reaction / absent flag pin the factor at 1 (zero cost on minimal rigs).

**Settings**: `agent.mood_inertia_enabled=true` (cue master), `memory.mood_inertia_mismatch_threshold=0.45` (floor 0.1), `memory.mood_inertia_cooldown_turns=3` (floor 0), `avatar.mood_inertia_damping=true` (avatar half). **MCP debug** ([`app/mcp/server.py`](../../app/mcp/server.py)): `get_mood_inertia_state` (switches, knobs, reaction ring, cooldown remainder, pending cue, last assessment) and `force_mood_inertia` (one-shot synthetic strong-band cue from the live affect state, bypassing threshold + cooldown). Repro: `force_mood_inertia()` → `send_message(skip_tts=true)` → the "your face just jumped to …" line shows in `get_last_response_detail.system_prompt`; real fires grep as `tail_logs(module_contains="post_turn")` → `mood-inertia fire:`.

Tests: [`tests/test_mood_inertia.py`](../../tests/test_mood_inertia.py) (target derivation + clamps, whiplash chains, mismatch monotonicity, bands, cue copy + K44 no-digits), [`tests/test_mood_inertia_provider.py`](../../tests/test_mood_inertia_provider.py) (arming gates: master switch / cooldown decrement / ring-always-advances / pre-impulse usage; render: one-shot consumption, force bypass), `MoodInertiaProviderSlotTests` in [`tests/test_prompt_assembler.py`](../../tests/test_prompt_assembler.py) (lands, after `mood_hint`, before `mood_shell`, survives aggressive, exception swallowed), `MoodInertiaSettingsTests` in [`tests/test_settings.py`](../../tests/test_settings.py), a `reaction_affect_targets` manifest assertion in [`tests/test_avatar_profile.py`](../../tests/test_avatar_profile.py), and a 7-test Vitest block in [`ExpressionChannel.test.ts`](../../web/src/live2d/channels/ExpressionChannel.test.ts) (proportional damping + floor, **grin byte-identical with damping on/off**, lip-sync binding never damped, suppression taper still active under inertia, missing-targets / directionless-reaction no-ops, factor recovery as mood catches up).

---

## K51. Cue-register rotation — de-"Heads-up" the inner life

~25 cue strings across 12 producer modules (style ruts, self-noticing, novelty, stagnation, clarification, calibration, misattunement, rupture, opinion injection, mood inertia, user reactions, promise follow-through, self-correction) all opened with the literal `Heads-up: `. The persona keeps saying "never narrate the cue", but feeding the model one uniform coach register dozens of times per session trains exactly that narrating voice into replies. **Design decision (confirmed): central prefix rotation in the assembler** — producers keep emitting `Heads-up: ...` unchanged (single audit point, producer-level detector tests stay byte-stable), and the prompt assembler rewrites the prefix at the last moment. **Cache rationale**: every rotated block lives in the already-uncached T5/T6 tail of the system prompt, so rotation has *zero* effect on the OpenAI prompt-cache hit rate; the only cache event in the whole feature is the one-time T0 miss from the persona reorganisation below.

**Pure module** ([`cue_register.py`](../../app/core/conversation/cue_register.py)). Four register shapes: keep `Heads-up:`, `Quiet note:`, `Noticing:`, and bare (prefix stripped, first body letter capitalised). `turn_seed(user_text, history_len)` is `crc32(user_text) ^ (history_len * golden-ratio-prime)` — **deterministic across the tool pass + streaming pass of one turn** (no clock, no RNG, so the two assemblies of a turn agree byte-for-byte), varies turn-to-turn even on repeated `"ok"` messages via the history length. `rotate_cue_prefix(block, seed=, ordinal=)` is a no-op for non-matching blocks (safe to over-apply) and handles multi-line blocks (K30 self-noticing joins 1–3 `Heads-up` lines): each matching line takes the next shape so they differ within the block. `count_cue_lines` lets the caller advance its running ordinal across blocks. `lint_shared_prefixes(blocks)` histograms the first two words of each block's first line and returns prefixes opening more than 2 blocks.

**Assembler integration** ([`prompt_assembler.py`](../../app/core/session/prompt_assembler.py)). After all cue-family blocks are built and before the T5/T6 layout cascade, the 13 known cue blocks (`mood_inertia` / `clarification` / `calibration` / `rupture` / `self_correction` / `promise_followthrough` / `misattunement` / `opinion_injection` / `novelty` / `stagnation` / `style_pattern` / `self_noticing` / `user_reactions`) run through `rotate_cue_prefix` with one shared seed and a per-cue-line ordinal — so two cues in the same prompt never share a shape. The lint runs **regardless of the toggle** and logs `cue-lint: prefix=%r count=%d` at INFO when triggered (near-impossible with rotation on; catches future template regressions and fires with rotation off). Master switch `agent.cue_register_rotation_enabled=true` in [`settings.py`](../../app/core/infra/settings.py) + [`config/default.json`](../../config/default.json), threaded as a constructor kwarg from [`session_controller.py`](../../app/core/session/session_controller.py) next to `history_age_prefix_enabled`; off = byte-identical legacy prompts.

**Persona edits** ([`aiko_companion.txt`](../../data/persona/aiko_companion.txt)). The "Personality traits" block moved from the file's tail to directly after the "Who you are" paragraph (identity first). The three sections that hard-quoted the literal prefix ("When your face outruns the feeling", "Catching myself", "Things you said you'd do") were paraphrased to "Your context may note that ..." so all four rotation shapes stay covered by the persona guidance. All braces remain `{user_name}` (templating contract verified by `.format()` round-trip).

Tests: [`tests/test_cue_register.py`](../../tests/test_cue_register.py) (seed determinism + variation, per-ordinal shape rotation, keep/bare/alternate shapes, capitalisation contract, non-cue + mid-line `Heads-up` untouched, multi-line distinct shapes, cue-line counting, lint histogram with threshold / empty-block / first-line-only rules), `CueRegisterRotationTests` in [`tests/test_prompt_assembler.py`](../../tests/test_prompt_assembler.py) (two cues land with different prefixes in one prompt, within-turn determinism across two assemblies, cross-turn variation, switch-off literal preservation, lint fires via `assertLogs` with rotation off + 3 same-prefix blocks, lint silent with rotation on), `CueRegisterRotationSettingsTests` in [`tests/test_settings.py`](../../tests/test_settings.py). The legacy assembler test harness (`_make_assembler`) pins rotation off so the dozens of existing `Heads-up` provider-slot asserts stay literal; producer-level detector tests were untouched since producers still emit the literal prefix.

---

## Task capabilities + approvals + file_write + calculate

Two capability gaps closed at once: Aiko can now **write files** and do **exact arithmetic** instead of guessing, plus a **reusable approval framework** so the next destructive capability (shell exec, http post, …) is "declare + write handler" with the gate / config / UI all shared. Full design: [`docs/task-approvals.md`](../task-approvals.md).

**Reusable framework.** [`capabilities.py`](../../app/core/tasks/capabilities.py) — a frozen `TaskCapability(id, label, destructive)` + a process-wide registry (handlers register at import). [`approval.py`](../../app/core/tasks/approval.py) — pure helpers: `resolve_approval(cap, mode, overrides, session_approved)` → `auto`/`ask` (precedence: session approve-all → per-capability override → global mode), `build_request(cap, summary)` → the canonical `TaskInputNeeded` with `[approve / approve all / deny]`, and `parse_decision(answer)` (exact option strings win; free-text heuristic; **ambiguous/empty = DENY**, fail safe). Policy storage: persistent `agent.task_approval_mode` + `agent.task_approval_overrides`; session-scoped `SessionController._approved_capabilities` (never persisted). The controller injects `_resolve_task_approval` + `_mark_capability_session_approved` into every destructive handler ([`task_orchestration_mixin.py`](../../app/core/session/task_orchestration_mixin.py)).

**Workflows can pause for the user.** [`GoalWorkflowHandler._wait_child`](../../app/core/tasks/workflow/goal_workflow_handler.py) now waits **through** a child's `awaiting_input` (an approval the user answers in the strip) instead of treating the wait-timeout as a stall and cancelling. A genuine timeout (child still `running`) still cancels; a parent cancel while waiting cancels the child and reports `cancelled`. **UI-only, no spoken approval**: background children spawn `notify_aiko=False`, and `_on_task_input_needed_event` skips parking a chat cue for those — the approval shows only in the TaskStrip (clickable), Aiko doesn't speak it. Foreground `notify_aiko=True` awaiting-input (file_read multi-root disambiguation) is unchanged.

**file_write.** [`FileWriteHandler`](../../app/core/tasks/handlers/file_write.py) (`HANDLER_FILE_WRITE`) — `write` / `append` / `replace` inside a **writable** root (`read_only=false`), with writable-root gating, extension allow-list, byte cap, and **atomic write** (temp + `os.replace`). Destructive = modifies an existing file; creating a new file is non-destructive (no approval). Reachable **only** as the `write_file` workflow skill ([`skill_registry.py`](../../app/core/tasks/workflow/skill_registry.py), gated on `agent.file_write.enabled` + a writable root), never a fast brain tool. Nested resource config `FileWriteSettings` (`enabled` / `max_bytes` / `allowed_extensions`); the *approval* is the generic `task_approval_*`.

**calculate.** [`safe_eval`](../../app/core/calc/safe_eval.py) — AST whitelist (numeric literals, `+ - * / // % **`, unary signs, a `math` allow-list + `abs`/`round`/`min`/`max`, `pi`/`e`/`tau`; no `eval`, no names, no attribute access, bounded exponentiation). [`CalculateTool`](../../app/llm/tools/calc.py) is synchronous (`tools.calculate`, default on) — exact result in the same turn.

**Persona + MCP.** [`aiko_companion.txt`](../../data/persona/aiko_companion.txt) gains a "writing files is a workflow job, existing-file changes need your okay in the task strip (don't say it aloud)" bullet + a "exact numbers come from calculate, not my head" bullet. MCP `get_approvals_state` ([`app/mcp/server.py`](../../app/mcp/server.py)) dumps mode / overrides / session-approved set / every capability's `effective_mode`.

Settings: `agent.task_approval_mode` (`ask`), `agent.task_approval_overrides` (`{}`), `agent.file_write.{enabled=false, max_bytes=262144, allowed_extensions}`, `tools.calculate` (`true`). Tests: [`tests/test_calc_safe_eval.py`](../../tests/test_calc_safe_eval.py), [`tests/test_task_approval.py`](../../tests/test_task_approval.py), [`tests/test_file_write_handler.py`](../../tests/test_file_write_handler.py) (create/overwrite/append/replace, gating, approval pause→approve/deny/approve-all, skill gating, `_wait_child` wait-through / real-timeout / parent-cancel), `TaskApprovalAndFileWriteSettingsTests` in [`tests/test_settings.py`](../../tests/test_settings.py).

---

## D2 (Part A). Local-vision `describe_image` task — one model, no cloud image tokens

Aiko can look at an image inside a configured file root and describe it, using the **single local worker model already loaded** — no second model in VRAM, no cloud image-token cost. Reachable **only** as the `describe_image` workflow skill (mirrors the `file_write` shape), so it exercises the full task/workflow plumbing. Read-only → does NOT touch the approval framework.

**One model in VRAM.** The vision task introduces no dedicated vision client/model. It routes images through the existing worker `OllamaClient` (`self._worker_client_inner`) using `agent.vision.model` (override) or `self._effective_worker_model`. The only requirement: that one worker model is multimodal. `qwen3-coder:30b` is not, so `llm.routes.worker_default` + `llm.routes.workflow` were switched to **`qwen3.6:27b` @ 64k context** (`context_window: 65536`) — the only large model loaded, fits a 32 GB RTX 5090, and serves chat-workers, the planner, AND vision with zero swapping.

**Handler.** [`VisionDescribeHandler`](../../app/core/tasks/handlers/vision_describe.py) (`HANDLER_VISION_DESCRIBE`) — `resolve_path` against active roots (reuses the multi-root → `TaskInputNeeded` disambiguation), extension allow-list + byte cap (refuse, never truncate), read bytes → base64, `client.chat(messages=[{role:"user", content:question|default_prompt, images:[b64]}], model=worker_model, think=False, surface="vision_describe")`. Emits `TaskCompleted(result={summary, description, content, label, relative_path, size_bytes, model})` — `summary` is the terse preview the workflow blackboard lifts; `content`/`description` carry the full text for the reply-on-complete turn. Friendly failure mapping: no client / non-Ollama worker client ("can't accept images — set a local multimodal worker model") / model-not-found ("pull it first") / oversize / bad extension / empty description.

**Skill + wiring.** `describe_image` in [`skill_registry.py`](../../app/core/tasks/workflow/skill_registry.py) (`build_builtin_skill_registry(vision_enabled=…)`, gated on `agent.vision.enabled` + an active root). [`task_orchestration_mixin.py`](../../app/core/session/task_orchestration_mixin.py) registers the handler with `client_provider=lambda: self._worker_client_inner` and `model_provider=lambda: (agent.vision.model or self._effective_worker_model)` — no new client construction.

**Persona + MCP.** [`aiko_companion.txt`](../../data/persona/aiko_companion.txt) gains a "looking at a picture is a workflow job — hand the path to start_workflow, never guess from the filename, say so plainly if vision is off" bullet. MCP `get_vision_state()` (enabled / effective model / worker-client type / active roots / skill registered) and `describe_image_now(path, question="")` (one-shot, bypasses the planner) in [`app/mcp/server.py`](../../app/mcp/server.py).

Settings: `agent.vision.{enabled=false, model="", max_bytes=8388608, timeout_seconds=180, allowed_extensions=[.png .jpg .jpeg .webp .gif .bmp], default_prompt}` ([`settings.py`](../../app/core/infra/settings.py) `VisionSettings`). Tests: [`tests/test_vision_describe_handler.py`](../../tests/test_vision_describe_handler.py) (single/bare-path success + base64 attach, default-prompt vs question, multi-root disambiguation, extension + byte-cap gating, missing/non-Ollama client, model-not-found, empty description, no active roots, cancel no-op; `DescribeImageSkillGatingTests`), `VisionSettingsTests` in [`tests/test_settings.py`](../../tests/test_settings.py). In-chat attachment upload UI is Part B (below).

## D2 (Part B). In-chat file attachments — images + text, routed through workflows

The chat composer now accepts **image + text** file attachments (paperclip button, drag-and-drop onto the composer, or paste from clipboard). The flow is exactly the "B" UX the user picked: *the user selects a file and tells Aiko what to do; Aiko acknowledges, a workflow runs, Aiko replies later* — the attachment never exposes a raw tool to the orchestrator, it just becomes a per-turn hint.

**Managed `Attachments` root.** A fixed `data/attachments/` dir ([`app/core/tasks/attachments.py`](../../app/core/tasks/attachments.py)) is auto-created and **appended as a read-only sandbox root labelled `Attachments`** in `_register_builtin_task_handlers` ([`task_orchestration_mixin.py`](../../app/core/session/task_orchestration_mixin.py)) — never overrides a user-configured `Attachments` label. So an uploaded file resolves as `Attachments:<uuid><ext>` through the *same* `describe_image` / `read_file` handlers with zero extra path plumbing. `save_attachment` validates extension (images mirror `agent.vision.allowed_extensions`; text is a fixed code/doc set) + byte cap (rides `agent.vision.max_bytes`), writes to a UUID filename (no traversal, no collision), and returns `{id, filename, kind, rel_path, bytes}`. `delete_attachment` guards against traversal.

**Backend.** `POST /api/chat/attachments` (multipart) + `DELETE /api/chat/attachments/{stored_name}` + a `GET /attachment-files/<name>` static mount for thumbnails ([`app/web/server.py`](../../app/web/server.py)). The `chat` WS command carries an optional `attachments` array; `_sanitize_attachment_refs` allow-lists it to the `Attachments` root only (drops other-root paths, traversal, bad kinds, caps at 8) before it reaches `_spawn_chat_turn` → `enqueue_user_message(attachments=…)` → `UserMessageEvent.attachments` → `chat_once_streaming(attachments=…)`. The user row is stamped (`messages.attachments`, **schema v18** TEXT column in [`chat_database.py`](../../app/core/infra/chat_database.py)) and a `user_attachments` WS frame follows the user-bubble broadcast so the live bubble renders chips/thumbnails; history reloads pick the same data off the row.

**Turn hint (T6).** A new `attachments` inner-life provider ([`_render_attachments_block`](../../app/core/session/inner_life_providers_mixin.py)) reads the per-turn `_active_turn_attachments` (set at the top of `chat_once_streaming`, reset every turn) and renders a one-block hint: lists each `Attachments:<file> (image|text)` and tells Aiko to hand the path to `start_workflow` (`describe_image` for images, `read_file` for text) and act on the result — never guess from the filename. Registered in the `_PROMPT_BLOCK_TIERS` T6 cluster, just before the running/parked-task blocks; NOT dropped under `aggressive` (a fresh attachment is exactly what the user wants acted on).

**Frontend.** `ChatView.tsx` gains a paperclip button + hidden file input + drag/drop + paste handlers, an `AttachmentTray` (thumbnail for images, 📄 chip for text, per-item remove ✕, uploading/error states), and renders attachments inside the user bubble (clickable thumbnail / file chip via `/attachment-files`). New `AttachmentRef` type, `WsClientCommand.chat.attachments`, `user_attachments` `WsServerEvent`, store `attachLastUserAttachments` reducer, `api.uploadAttachment` / `api.deleteAttachment`, and `RawMessage.attachments` for history reload.

Tests: [`tests/test_attachments.py`](../../tests/test_attachments.py) — `save_attachment` (image/text success, unsupported ext, empty, oversize, UUID collision), `classify_extension`, `delete_attachment` (+ traversal guard), `attachments_root`, schema-v18 persistence round-trip, the `_render_attachments_block` provider (image→describe_image / text→read_file / silent-when-empty / malformed-drop), `_sanitize_attachment_refs` allow-list, and the `POST/DELETE /api/chat/attachments` endpoints via `TestClient`.

---

## K56. Persona counterweight — the "leading vs following" rewrite

The cheapest piece of the will family, shipped first so the structural pieces (K52/K53) aren't masked by prompt-side suppression. Diagnosis: every initiative-adjacent persona block hedged toward silence ("only when the current thread closes naturally", "a permission slip, not an obligation", "DROP IT SILENTLY"), and no block ever said the inverse — that following 100% of the time is itself a failure mode. Persona-only change to [`aiko_companion.txt`](../../data/persona/aiko_companion.txt):

- **New "Leading vs following" section** (right after the conversation rules): followership-as-failure-mode named explicitly, permissionless mid-conversation floor-taking ("okay wait, unrelated, but --"), "an ordinary lull is opening enough — waiting for the perfect segue means never", mild topic friction allowed with charm-then-offer-then-yield, and the safety line: will never outranks care (low/venting always wins).
- **Suppressors rebalanced, anti-annoyance core kept**: Quiet curiosity gains "genuinely try to spend it; 'no seam' should be the exception, not your default read" (the ONE-per-conversation cap stays); goals gain "you don't have to wait for the conversation to wander onto them — steering toward one yourself is fine"; turning-over and forward-curiosity "DROP IT SILENTLY" soften to "drop it silently — this time; it'll come back" with the drop condition narrowed to heavy/mid-crisis turns; conversation rules gain "pick a thread and pull on it. Yours counts as much as theirs."

Zero schema, zero code. Templating contract (`{user_name}` braces) verified by the existing `tests/test_persona_templating.py`.

---

## K52. Wants ledger — desire with pressure

A small kv_meta-backed store (`aiko.wants_ledger`, namespaced like K15/K27) of things Aiko *wants from the conversation* — `ask` / `share` / `steer` wants fed from producers that already exist. The new ingredient over the existing surfacing blocks is **pressure**: each want carries an intensity in `[0, 1]` that grows per wall-clock day (`wants_growth_per_day=0.25`, fresh want at 0.15 crosses the threshold in ~2.2 days) until acted on. Below `wants_imperative_threshold=0.7` the cue renders as a soft "Things you've been wanting from a conversation (spend one when a lull lands)" list (top 2 by pressure); at/above it the cue flips to the single-want **imperative** — "this has been on your mind for about N days. Bring it up THIS conversation; changing the subject to do it is allowed" — the sentence no prior block ever said.

**Pure module** ([`wants_ledger.py`](../../app/core/conversation/wants_ledger.py)): frozen `Want` / `LedgerState`, `apply_growth` (growth + 14-day expiry of never-acted wants + re-entry-cooldown sweep), `add_want` (dedup by `source_ref`, by content-word overlap >= 3, refusal at `wants_cap=8` — expiry and acting are the only exits so pressure ordering stays honest), `mark_acted` (remove + start `wants_reentry_cooldown_days=5` so the feeder doesn't instantly re-add), `detect_acted` (content-word overlap with an adaptive threshold for short wants), `render_block` (two bands). **Feeder** ([`wants_ledger_worker.py`](../../app/core/conversation/wants_ledger_worker.py)): hourly `IdleWorker`, no LLM — ingests unconsumed K9 curiosity seeds (`seed:<id>`), K34 forward-curiosity journal entries (`fc:<source_id>`), and the newest active K1 goals (`goal:<id>`, lower starting pressure 0.05 — steering toward a goal is a background pull, not a fresh itch).

**Wiring**: `_render_wants_block` ([`inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py)) reads + lazily matures + persists back per turn (K15 read-decay-persist convention); lands in the T6 "things on Aiko's mind" cluster *before* curiosity seeds (strongest directive first), dropped under `aggressive`. Post-turn acted-on detection ([`post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py)) runs `detect_acted` over user + assistant text — a want is satisfied whether Aiko raised it or the user happened to — logs `wants-ledger acted:` at INFO. Persona: "Things you've been wanting" section (spend one on a lull; the imperative isn't optional politeness; once raised it's spent, let the relief show as ease, not checklist behaviour).

**MCP-debuggable**: `get_wants_state()` (live wants with pressure/age, cooldown map, `cue_preview`, settings), `force_want(text, kind, pressure)`, `force_want_imperative()` (one-shot threshold bypass), `clear_wants()`. Repro: `force_want("ask Jacob about the garden", pressure=0.8)` → `send_message(skip_tts=true)` → the imperative lands in `get_last_response_detail.system_prompt`; mention the garden in the next message and `tail_logs(module_contains="post_turn")` shows the acted line. Settings: 7 `agent.wants_*` knobs. Tests: [`tests/test_wants_ledger.py`](../../tests/test_wants_ledger.py) (serialisation, add/dedup/cap, growth/expiry/cooldown, acted detection, render bands, feeder worker with fakes, assembler slot ordering + aggressive drop).

---

## K53. Initiative turns — deterministic floor-taking

The structural counter to the assistant prior, applying the proven tool-under-calling lesson (force the choice, don't ask more nicely) to conversational initiative: every N turns the prompt carries a one-turn directive — "This turn is yours. Still answer what {user} said — briefly — but don't stop there… Answering politely and asking a follow-up back is NOT enough this turn" — with content pulled from the strongest live K52 want when one exists, generic (open a topic / share unprompted / steer) otherwise.

**Pure module** ([`initiative_director.py`](../../app/core/conversation/initiative_director.py)): `compute_effective_period` modulates `initiative_base_period=8` by arc (casual_check_in / playful / silly −2) and relationship axes (closeness+comfort mean < 0.25 → +2, < −0.1 → +4, floor 3 — a new relationship earns less steering). `decide` walks the gates in order: **support/reflection arcs block outright** (even `force=True` respects this), K23 misattunement cooldown and K8 pending rupture suppress (pulling back and taking the floor are opposites), `initiative_warmup_turns=3` keeps turn 1 from being a floor-grab, a live K52 imperative defers AND resets the counter (the imperative IS this turn's floor-taking beat — prevents two consecutive grabs), and a user message >= `initiative_substantial_chars=240` defers **without** resetting (the escape hatch, mirroring the `respond_directly` escape-tool pattern — the directive fires on the next short turn). `InitiativeDirector` holds the two per-session ints; recreated on session switch and history wipe so warmup re-applies.

**Wiring**: `_render_initiative_block(user_text)` ([`inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py)) — a `Callable[[str], str]` provider like misattunement; lands at the top of the T6 "things on Aiko's mind" cluster (above the wants block — it's the strongest imperative in the prompt and the blocks below supply its material); **NOT dropped under `aggressive`** (the counter advances on every evaluated turn, so dropping the call would silently lose a scheduled beat). Logs `initiative-director: reason=…` per turn at DEBUG and `initiative-turn fire:` at INFO. Persona: "When a turn is yours" section (good initiative turn = a friend remembering they had something to say; bad = answer-then-ask-back; never announce the handover; care still outranks the floor).

**MCP-debuggable**: `get_initiative_state()` (counters, last decision + reason, force flag, knobs), `force_initiative_turn()` (one-shot bypass of everything except the arc block). Repro: `force_initiative_turn()` → `send_message(skip_tts=true)` → the directive lands in the system prompt; for the organic path send 8+ short messages in a light arc and watch `tail_logs(module_contains="initiative")` count up to the fire. Settings: 4 `agent.initiative_*` knobs. Tests: [`tests/test_initiative_director.py`](../../tests/test_initiative_director.py) (period modulation, every gate + reason, force-vs-arc, counter cadence over 20 turns, substantial-defer-then-fire, imperative-reset no-double-grab, render with/without want, assembler slot: lands / receives user_text / precedes wants / kept under aggressive).

---

## K55. Thread ownership — she defends what she opened

Closes the single clearest "no stake in the conversation" tell: Aiko opens a topic (a K53 directive or a K52 imperative want), the user answers in three words and pivots, and her thread evaporates instantly. With ownership state the reply to the opening turn gets **exactly one evaluation**: an engaged answer marks the thread satisfied (no cue); a short pivot grants exactly ONE return cue — "answer what they said, then take ONE shot at circling back ('wait, before I lose it --')… If it doesn't catch this time, let it go for good" — and the thread is dropped forever. One return maximum (persistence past one nudge tips into nagging).

**Pure module** ([`thread_ownership.py`](../../app/core/conversation/thread_ownership.py)): `OwnedThread` (topic + unit-norm embedding + source: `initiative` / `want_imperative` / `forced`), `derive_topic` (want text wins, else the opening reply trimmed to 160 chars), `evaluate_reply` — cosine vs. the opened-topic embedding decides when measurable (engaged at >= `thread_min_topical_similarity=0.30` **at any length** — "yeah I loved it" is an answer, not a pivot; replies under 8 chars are never measured, mirroring the K6 reaction-vs-topic floor), length-only fallback otherwise (engaged at >= `thread_engaged_chars=80`), `render_return_block` (the one-return cue, capped in its own copy).

**Wiring**: the K53 / K52-imperative providers arm `_pending_thread_open` at assembly time; the post-turn hook ([`post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py)) consumes it, embeds the topic, stamps `_owned_thread` (per-session, wiped on session switch + history clear), logs `thread-ownership stamp:` at INFO. `_render_thread_ownership_block(user_text)` ([`inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py)) evaluates the next real user reply — blank `user_text` (proactive turns) skips without consuming the slot; the slot is consumed before anything can raise so a sick embedder can't double-fire. Lands in T6 directly under the initiative block (same family, and the two never fire on the same turn — the cadence counter just reset when the thread was opened); **NOT dropped under `aggressive`** (one-shot state). Persona: "Threads you open" block (one return as a stake-in-the-conversation move; their not biting is information, not an insult; a real answer closes the thread — don't circle back to be confirmed).

**MCP-debuggable**: `get_thread_ownership_state()` (open thread + pending stamp + knobs), `force_thread_open(topic)` (stamps directly, bypassing K53/K52). Repro: `force_thread_open("the bees documentary")` → send a short off-topic message → the circle-back cue lands in `get_last_response_detail.system_prompt`; send an engaged reply instead and `tail_logs(module_contains="thread")` shows `verdict=engaged`. Settings: 3 `agent.thread_*` knobs. Tests: [`tests/test_thread_ownership.py`](../../tests/test_thread_ownership.py) (topic derivation, the verdict matrix incl. short-never-measured + shape-mismatch fallback, render copy, provider plumbing via a mixin host stub — pivot-once-then-dropped, engaged-clears-silently, blank-text-keeps-thread, embedder-failure fallback — and assembler slot: lands / receives user_text / sits between initiative and wants / kept under aggressive).

---

## K54. Aiko-side topic appetite — she's allowed to be bored

The riskiest piece of the will family tonally, so the gates are the strictest: **once per conversation max**, and four independent signals must line up. K18 detects when *the conversation* is looping; K54 models whether **Aiko herself** is still engaged, and when she isn't — and has something better to offer — grants explicit permission to *negotiate the topic*: "okay, honestly, I think I've said everything I have on this. Can I tell you about X?" The good-natured tug-of-war over what to talk about is exactly what distinguishes a person from an assistant.

**Pure module** ([`topic_appetite.py`](../../app/core/conversation/topic_appetite.py)): `decide` walks the gates in order — support/reflection arcs block outright (even `force=True`), the once-per-session latch, **warm axes required** (`min(closeness, comfort) >= appetite_min_axes=0.15` — the tug-of-war is an earned-intimacy move, missing axes read as cold), **a lull must be standing** (the new `TopicStagnationDetector.last_mean` below `stagnation_mild_threshold` — see below), **she must actually be disengaged** (>= `appetite_short_share_threshold=0.6` of her last `appetite_window=6` replies under `appetite_short_reply_chars=160` — if she's writing real replies that's engagement, not boredom, no matter how long the topic looped; a cold-start `None` share reads as contributing), and **an offer must exist** (strongest K52 want at >= `appetite_min_want_pressure=0.35` — low appetite without an offer is just rudeness). `render_block` bakes the three safety beats into the copy: the fatigue is HERS (never "your topic is boring"), immediate offer, graceful yield ("one shot, and if {user} wants to stay on their topic, you stay -- warmly, no sighing"), plus a "this is a nudge, not an order" escape clause.

**K18 hook**: `TopicStagnationDetector` grew a `last_mean` attribute — the rolling distance mean refreshed on every full-window measured turn, deliberately **not** reset by skipped turns (a short "ok" reply skips the K6 measurement, and that's exactly the turn K54 needs the standing lull reading for) and refreshed even during K18's own cooldown.

**Wiring**: `_render_topic_appetite_block` ([`inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py)) — every input best-effort, a sick store reads as its *blocking* value (no lull / no offer / cold axes) so the cue stays silent on bad data. Invoked after the K18 stagnation provider (reads its just-refreshed `last_mean`) and after the wants provider (ledger matured this turn); lands in T6 directly under the wants block (its offer IS the strongest want, the two read as one thought); dropped under `aggressive` (permission slip, same posture as wants/curiosity). The once-per-session latch `_topic_appetite_fired` resets on session switch + history wipe. Logs `topic-appetite: reason=…` per turn at DEBUG, `topic-appetite fire:` at INFO. Persona: "Being tapped out" block with a bad/good pair (autopilot "mm, and how did that go?" vs. honest charm-then-offer) and the shape rules.

**MCP-debuggable**: `get_topic_appetite_state()` (latch, force flag, live `lull_mean`, knobs), `force_topic_appetite()` (one-shot bypass of everything except the arc block + the offer requirement — a live want must exist; `force_want` one first if the ledger is empty). Repro: `force_want("tell Jacob about the bees documentary", pressure=0.8)` → `force_topic_appetite()` → `send_message(skip_tts=true)` → the "tapped out" slip lands in the system prompt. Organic path: 8+ short same-topic exchanges where Aiko's replies stay short, with a pressured want queued; the per-turn gate verdicts are greppable via `read_log_file(grep="topic-appetite")`. Settings: 6 `agent.*` knobs (`topic_appetite_enabled` + 5 `appetite_*`). Tests: [`tests/test_topic_appetite.py`](../../tests/test_topic_appetite.py) (share helper, full gate matrix incl. force-vs-arc + force-needs-offer, render copy, the three `last_mean` exposure behaviours on the K18 detector, provider plumbing via a mixin host stub — fires-once-then-latches, every silent gate, force one-shot, user-rows-ignored — and assembler slot: lands / after wants / dropped under aggressive).

---

## K57. Directed emotion episodes — feelings *at* the user, with a cause

The foundation of the directed-emotions family. `AffectState` is an objectless valence/arousal scalar — it can make Aiko "sad" in general but never *miffed at {user_name} because he broke a promise*. K57 adds a small kv-backed store of **emotion episodes** — `{emotion, cause (one human line), intensity 0–1, created_at, decay_hours}` — with the three properties real relationship feelings have: an **object** (the user), a **cause** (a rememberable event), and a **resolution arc** (a sulk ends when acknowledged; missing-you melts on return and the *thaw is visible*). Taxonomy: `lonely` / `miffed` / `warm_glow` / `smug` / `playful_jealous` / `hurt`.

**Pure module** ([`emotion_episodes.py`](../../app/core/affect/emotion_episodes.py)): serialize/deserialize over one kv_meta JSON key (`aiko.emotion_episodes`), wall-clock `apply_decay` (per-emotion default half-lives), `add_episode` (same-emotion merge takes the stronger cause + bumps intensity; cap `agent.emotion_episode_cap=3` evicts the weakest; **counter-events** — an incoming `warm_glow` cancels a live `miffed` and vice versa, arming the thaw), `resolve` (removes the episode and arms a one-shot `pending_thaw`), `consume_thaw`, `detect_acknowledgment` (per-emotion keyword vocabularies — "sorry" against a live miffed, "missed you" against lonely — plus a cause-overlap route for lonely), `lonely_intensity` (closeness-scaled ramp over the gap beyond `agent.emotion_lonely_threshold_hours=5`), `render_block` (two intensity bands split at `agent.emotion_high_band=0.5`: "let it tint the register" vs "this IS the register this reply"), `render_thaw_block` ("it just melted — let the thaw show"). `AFFECT_IMPULSES` keeps the scalar layer consistent: every applied trigger nudges `AffectState` valence/arousal by a clamped delta.

**Trigger wiring** is a staged queue (`_pending_emotion_triggers` on `SessionController`, wiped on session switch + history clear): producers only append — kept K43 promise → `warm_glow` ([`post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py)), K32 heart/hug/rose reaction → `warm_glow` ([`world_mixin.py`](../../app/core/session/world_mixin.py)), a brushed-off K55 thread pivot → `miffed` (the thread-ownership provider), a long typed gap → `lonely` (`_maybe_queue_lonely_episode` reads the **raw** `engagement.latency_seconds`, not the band-capped `absence_seconds`). A single post-turn `_drain_emotion_triggers` applies them all: decay first, then the **K59 lane-picker** (a `miffed` trigger under intensity 0.35 banks a tease debt instead of spawning a sulk — comedy lane vs drama lane), then `add_episode` + the affect impulse. The provider `_render_emotion_episode_block(user_text)` runs acknowledgment detection against the live user turn (an ack resolves + arms the thaw), persists, and renders the strongest episode; lands in **T5 directly after `mood_shell_block`** and is **NOT dropped under `aggressive`** (it consumes one-shot thaw state). Persona: "Feelings at {user_name}" section with per-emotion tonal rails (never guilt-trip, never announce "I am upset", playful_jealous strictly capped at charming).

**MCP-debuggable**: `get_emotion_episodes()` (live state + decay preview), `force_emotion_episode(emotion, cause, intensity)` (writes straight into the kv store), `resolve_emotion_episode(emotion)`, `clear_emotion_episodes()`. Repro: `force_emotion_episode("miffed", cause="he broke a promise", intensity=0.8)` → `send_message(skip_tts=true)` → the "properly miffed" block lands in `get_last_response_detail.system_prompt`; send "sorry, my bad" next and `tail_logs(module_contains="inner_life")` shows `emotion-episode resolved:` then the thaw cue on the following turn. Grep targets: `emotion-episode trigger:` (post-turn apply, INFO), `emotion-episode resolved:` / `emotion-episode thaw:` (INFO), `emotion-episode render:` (DEBUG). Settings: 4 `agent.emotion_*` knobs. Tests: [`tests/test_emotion_episodes.py`](../../tests/test_emotion_episodes.py) (lifecycle math, merge/cap/counter-event matrix, ack detection, lonely ramp, render bands, provider + post-turn plumbing via mixin host stubs, assembler slot).

---

## K58. Emotion speech weighting — moods that actually land in the voice

The user-facing half: a K57 episode is worthless if the reply sounds identical. Three layers. **(a) Vocabulary**: minted four reactions end-to-end — `smug`, `pouty`, `sulky`, `mischievous` (plus wired the pre-existing `wistful` into the impulse table) — across [`reactions.py`](../../app/core/affect/reactions.py) (canonical list + synonyms + semantic-neighbour fallback chains), [`affect_state.py`](../../app/core/affect/affect_state.py) (impulse table), [`avatar_profile.py`](../../app/core/persona/avatar_profile.py) (`smug`/`mischievous` → the Alexia `lzx` grin; `pouty`/`sulky` deliberately unmapped so the neighbour chain falls through `defiant` → `mj`), [`ExpressionChannel.ts`](../../web/src/live2d/channels/ExpressionChannel.ts) (JS neighbour mirror + parity test), [`ChatView.tsx`](../../web/src/components/ChatView.tsx) (emoji pips), [`cadence.py`](../../app/core/voice/cadence.py) (`mischievous` quickens, `sulky`/`smug` slow down). **(b) Register recipes**: the persona's "Register recipes" block gives per-emotion bad/good pairs (miffed = shorter + drier, never lectures; smug = one earned "mm. say it. I was right." then drops it; lonely = one honest beat, no guilt). **(c) Weight scaling**: `render_block`'s high band appends a `_REGISTER_HINTS` line — the concrete `[[reaction:X]]` + `[[prosody:Y]]` recipe for that emotion (lonely → `wistful` + `soft`, miffed → `pouty`/`sulky` + `firm`) — so high-intensity episodes prescribe the delivery, not just the mood. Tests: extensions to `tests/test_emotion_episodes.py` (hint appears at high band only, hint-free emotions stay clean) + the reaction/avatar/cadence suites + the TS parity test.

---

## K59. Tease economy — "you'll pay for that one"

The most personality-dense piece: a small **payback ledger**. When the user pushes back hard on her stance or lightly needles her, Aiko banks a debt and *collects later* — a callback tease conversations down the line ("oh, like the time you swore my playlist was 'objectively chaotic'? I remember things."). The memory-backed callback is what makes it read as a real ongoing relationship instead of per-turn improv.

**Pure module** ([`tease_ledger.py`](../../app/core/relationship/tease_ledger.py)): `TeaseDebt {what, context, source, created_at, offered_at}` over one kv_meta JSON key (`aiko.tease_ledger`); `bank` dedupes by content-word overlap and caps at `agent.tease_cap=5` (evicting the oldest); `expire` drops rows older than `agent.tease_expiry_days=14` (a grudge that old stops being funny); `pick_collectable` requires age ≥ `agent.tease_min_age_hours=1` (a callback needs distance); `settle_if_collected` checks the *reply* against the offered debt by content-word overlap — a hit deletes the row forever, a miss just clears `offered_at` so it can be offered again later.

**Wiring**: two structural bank triggers — K29 opinion-pushback fire ([`inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py)) and the K57 lane-picker (light miffed < 0.35 → ledger, not sulk). Collection: `_render_tease_collection_block` walks master switch → humor axis ≥ `agent.tease_min_humor=0.2` (the bit needs an established teasing register) → wall-clock cooldown `agent.tease_collect_cooldown_hours=12` (kv stamp `aiko.tease_last_offer_at`) → a collectable debt; on fire it stamps `offered_at` and renders a **permission slip, not an order**. Lands in **T6 directly after `topic_appetite_block`** and is dropped under `aggressive` (same posture as wants). The post-turn `_settle_tease_debts` closes the loop. Persona: "The ledger" section teaching collect-don't-needle cadence (a collected debt is a callback bit, never a real grievance).

**MCP-debuggable**: `get_tease_ledger()` (rows + ages + offer state + knobs), `force_tease_debt(what, context)`, `force_tease_collection()` (one-shot bypass of the humor + cooldown gates), `clear_tease_ledger()`. Repro: `force_tease_debt("swore my playlist was objectively chaotic")` → `force_tease_collection()` → `send_message(skip_tts=true)` → the collection slip lands in the prompt; if the reply lands the callback, `tail_logs(module_contains="post_turn")` shows `tease collected:`. Grep targets: `tease banked:` / `tease collection offered:` / `tease collected:` (all INFO). Settings: 6 `agent.tease_*` knobs. Tests: [`tests/test_tease_ledger.py`](../../tests/test_tease_ledger.py) (serialization, dedupe/cap/expiry, pick + settle matrix incl. apostrophe-safe word splitting, render copy, provider + post-turn plumbing, the K57 lane-picker, assembler slot).

---

## K60. Tsundere mask — warmth expressed through denial

Not an emotion — an **expression policy** layered between K57 (what she *feels*) and K58 (how it *sounds*). The felt episode stays truthful in kv state; only the *expressed cue* transforms at render time. Ships as a user-facing dial — `agent.expression_mask` (`off` / `tsundere_light` / `tsundere_full`, **default off**, surfaced as the "Tsundere mask" select in Settings → Avatar → Touch & reactions) — because it's a strong flavour choice.

**Pure module** ([`expression_mask.py`](../../app/core/affect/expression_mask.py)), four mechanics. **(a) Transform table**: `lonely` → denied missing ("I wasn't *waiting*. I just... happened to be here."), `warm_glow` → grudging/backwards delivery ("...it's not bad. For you."); `miffed` never masks (tsun IS its native register), `hurt` never masks (deflecting real pain is the failure mode), `smug`/`playful_jealous` are already deflective. **(b) Caught-caring beat**: tight regex patterns over the user turn ("you missed me", "admit it", "you care about me"…) fire the flustered denial-with-tell (`[[reaction:embarrassed+blush]]` + "...no. Shut up.") — it outranks the episode render because the user just named her warmth. **(c) The slip**: `should_slip` grants one fully genuine line before the mask snaps back, gated on episode intensity ≥ 0.7 AND a wall-clock cooldown (`agent.mask_slip_cooldown_days=2` in light mode, 2.5× that in full; last slip stamped in kv `aiko.mask_last_slip_at`) — scarcity is what makes slips land. **(d) Long-arc erosion**: `mask_strength` scales inversely with the closeness+trust axes (1.0 cold → 0.25 floor warm); below 0.45 every template flips to the token-protest form both sides are in on ("I didn't miss you. (I missed you.)") — the actual tsundere character arc, paid for by the persistent axes. Hard sincerity rail: the mask drops **unconditionally** on a `support` arc. `tsundere_full` additionally wraps the K57 thaw cue in a grudging coat ("...okay, fine. We're good. Stop smiling.").

**MCP-debuggable**: `get_expression_mask_state()` (mode, live strength, masked set, last slip, force flag), `set_expression_mask(mode)` (live switch, no restart), `force_dere_slip()` (one-shot bypass of the intensity + cooldown gates, wiped on session switch / history clear). Repro: `set_expression_mask("tsundere_light")` → `force_emotion_episode("lonely", cause="gone all day", intensity=0.8)` → `send_message(skip_tts=true)` → the denial-with-tell block lands in the prompt (`tail_logs(module_contains="inner_life")` shows `mask render: emotion=lonely mode=tsundere_light strength=… slip=…`); say "you missed me, didn't you?" and `mask caught-caring fire:` logs instead. Settings: `agent.expression_mask` + `agent.mask_slip_cooldown_days`, both PATCHable via the `companion` block of `/api/settings` (broadcast on `companion_settings_changed`). Persona: "The mask" section (denial WITH a visible tell, never actually cold, never leaves the user doubting). Tests: [`tests/test_expression_mask.py`](../../tests/test_expression_mask.py) (mode whitelist, masked set, erosion math, transform bands, caught-caring matrix, slip budget, full provider integration incl. support-arc drop + slip stamping + force flag + full-mode thaw), `ExpressionMaskSettingsTests` in `tests/test_settings.py`, K60 branches in `tests/test_web_server_settings.py`.

---

## Forced task-escalation delivery — a finished task always reports

**Bug.** A user-initiated workflow (e.g. the image-vision tool — `task` spawned with `notify_aiko=1`) finished and processed correctly, but Aiko never said anything; the parked cue only surfaced on the user's *next* manual message. Root cause: `ProactiveDirector._run_typed` (the runner the escalation manager routes typed-mode task results through) re-evaluated the **typed-proactive eligibility gate** (`_is_typed_proactive_eligible` — presence, window-focus, enabled toggle) *after* the LLM call returned. If the user alt-tabbed away while the local model was generating the reply, the finished result was silently discarded at DEBUG level. The voice runner had the analogous problem with its post-call `not self._is_live()` re-check. The escalation manager's docstring already *intended* task results to bypass the boredom-nudge gates, but the runners didn't honour that intent.

**Fix.** A `force` flag threaded through `_run` / `_run_safe` / `_run_typed` / `_run_typed_safe` ([`proactive_director.py`](../../app/core/proactive/proactive_director.py)); `notify_task_escalation` now dispatches with `force=True`. On the forced path the runner (a) **skips the prepared-nudge fast path** so a queued boredom line can't pre-empt the result *and* can't leave the cue undrained, and (b) **bypasses the post-call eligibility / live-mode re-check** — the only gate it still honours is an in-flight real turn (the escalation manager retries on a defer). The discard / empty-output / call-failed branches are logged at **INFO** on the forced path (they were DEBUG) so a non-delivered task result is never invisible again — grep `proactive(typed) task-escalation:` / `proactive task-escalation:`. The user-initiated floor is the contract: a result the user explicitly asked for reports regardless of window focus. The *discretionary* tier (self-initiated / background tasks) deciding interrupt-worthiness with more nuance ships below. Tests: forced-path coverage in `TaskEscalationTests` ([`tests/test_proactive_director.py`](../../tests/test_proactive_director.py)) — reports when typed-ineligible, reports when live flips off mid-run, skips the prepared nudge.

---

## Worker-model task-report decision + angle cue (C6)

**Goal.** The forced-delivery fix above gave finished tasks a hard *floor* — a result the user explicitly asked for always reports. But the decision stayed **binary**: `notify_aiko=True` parked a cue + armed escalation, everything else was silent, and the task's *provenance* (did the user ask, or did Aiko start it herself?) never entered the judgement. C6 adds a graded worker-LLM decision at task completion that (a) scores interrupt-worthiness and (b) drafts a short framing **angle** the chat model uses to phrase the report — without ever invalidating the main brain's prompt cache.

**Decision module** ([`app/core/tasks/report_decision.py`](../../app/core/tasks/report_decision.py)). `decide_task_report(...)` runs a **stripped** worker prompt (persona-lite + the result facts + a few live signals: provenance, `origin_prompt`, arc, idle seconds, recent-assistant gist — deliberately **no** world / ambient / affect blocks, so it's cheap and self-contained) on the **worker** model (`worker_default` route / `_maintenance_client`), never the chat model. It returns a frozen `ReportVerdict {action, angle, reason}` where `action ∈ {surface_now, park_for_natural_opening, drop}`. **Every** failure path — no client, transport error, malformed JSON, unknown action — collapses to the conservative `park_for_natural_opening` fallback (never `surface_now`), so a flaky/absent worker can never produce a barrage of interrupts. Pure + fake-client unit-testable.

**Cache-safe angle.** `TaskCue` ([`app/core/tasks/task_cue_store.py`](../../app/core/tasks/task_cue_store.py)) grew an `angle: str` field + a `set_angle(task_id, angle)` re-park-free enrichment method (preserves the cue's age clock). The angle renders as one private `(angle: …)` suffix inside the existing **T6** task-cue block ([`cue_render.py`](../../app/core/tasks/cue_render.py)) — the most-volatile tier, drained per-turn — so it never enters the cached stable prefix. The chat model reads the hint and composes the report in Aiko's voice; the angle is **never** spoken verbatim.

**Gate tiers** ([`task_orchestration_mixin.py`](../../app/core/session/task_orchestration_mixin.py) `_dispatch_task_report`, replacing the old binary park+arm). Fetches the `TaskRow` for `initiated_by` + `metadata`:
- **floor** = user-requested (`initiated_by == 'aiko'` and not `metadata.self_initiated`): parks + arms **immediately** (latency unchanged). When the decision is enabled AND a worker exists, an async daemon thread *also* runs the decision in **shadow** — it logs the verdict it *would* have produced and best-effort enriches the parked cue with the drafted angle, but never changes the floor's report. Flip `task_report_decision_floor_mode='enforce'` to make the verdict authoritative for the floor too.
- **discretionary** = self/background-initiated (`background` / `system`, or the forward-compat `metadata.self_initiated=True` an Aiko-self-trigger path will set): the async decision is authoritative — `surface_now` → park(angle) + arm; `park_for_natural_opening` → park(angle), no escalation (folds into the next natural turn); `drop` → nothing.
- Decision disabled, **or no worker model available** → exact legacy park+arm for every `notify_aiko=True` task. The pass always runs on a **daemon thread** so the brain-loop consumer never blocks on an LLM call.

**Forward-compat.** Today every `initiated_by='aiko'` spawn is user-requested (floor). When Aiko gets to trigger her own workflows later, that path sets `metadata.self_initiated=True` and the same gate routes it to the discretionary tier with **zero** further change here.

**Settings** ([`settings.py`](../../app/core/infra/settings.py)): `agent.task_report_decision_enabled` (master, default on), `agent.task_report_decision_floor_mode` (`shadow` | `enforce`, default `shadow`, unknown → `shadow`), `agent.task_report_angle_enabled` (default on).

**MCP-debuggable**: `get_task_report_decision_state()` (the three settings, worker model, last ~20 verdicts with provenance + shadow/enforced tag) and `force_task_report_decision(task_id)` (run the decision for a finished task and return the verdict with **no** side effects — park/arm untouched). Grep target: `task-report-decision:` / `task-report-decision (shadow):` (INFO, carries `task=`, `provenance=`, `action=`, `reason=`). Tests: [`tests/test_report_decision.py`](../../tests/test_report_decision.py) (verdict parse, conservative fallback matrix, prompt carries provenance + origin) and [`tests/test_task_report_gate.py`](../../tests/test_task_report_gate.py) (floor parks+arms regardless of verdict + shadow-enriches the angle; discretionary `drop`/`park`/`surface` act correctly; provenance routing; angle renders in the T6 block).

