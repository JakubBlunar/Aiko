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
[`avatar_profile.py`](../../app/core/avatar_profile.py) (supported
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
fallback in [`reactions.py`](../../app/core/reactions.py) /
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
[`LoggingSettings`](../../app/core/settings.py)) gates the feature;
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

[`WorldStore`](../../app/core/world_store.py) backs a small persistent
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
([`WorldStore.ensure_garden_seed`](../../app/core/world_store.py))
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
[`IdleWorkerScheduler`](../../app/core/idle_worker_scheduler.py) wakes
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
[`app/core/idle_fact_checker.py`](../../app/core/idle_fact_checker.py)
and registers with the shipped `IdleWorkerScheduler`. Privacy is
enforced by [`fact_check_privacy.py`](../../app/core/fact_check_privacy.py)
which blocks personal claims at classification time and scrubs the
search query (drops emails, phone numbers, names, addresses) before
it ever leaves the box. Per-hour and per-day budgets live in
[`fact_check_rate_limiter.py`](../../app/core/fact_check_rate_limiter.py)
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
[`app/core/knowledge_gap_extractor.py`](../../app/core/knowledge_gap_extractor.py)
(regex + the inline `[[gap:topic:question]]` self-tag, mirroring
the promise extractor shape). Storage reuses `MemoryStore` via the
`knowledge_gap` kind in [`memory_store.py`](../../app/core/memory_store.py).
Resolved gaps gain a `resolved_at` metadata stamp from the F1
worker and the original gap row is kept for audit. Surfacing in the
prompt is gated on cosine similarity to the current turn so only
relevant gaps re-enter the conversation.

---

## F3. Confidence column on memories

`confidence REAL NOT NULL DEFAULT 0.7` added to the `memories`
table; `Memory` dataclass + `MemoryStore.add` / `update` / mirror
plumbing all carry it now. Defaults: extractor `0.7`,
`[[remember:self:...]]` self-tags `0.85`, `[[remember:...]]`
user-confirmed tags `0.9`, tool-result memories (RAG / web) `0.95`,
manual memory-tab creates `1.0`. F1 pushes confidence up toward
`0.95` on positive verification and down to `0.4` on contradiction.
[`rag_retriever.py`](../../app/core/rag_retriever.py) penalises
hits with `confidence < 0.5` and appends an `(uncertain)` suffix
in the rendered memory block so the LLM hedges. Memory tab in
[`SettingsDrawer.tsx`](../../web/src/components/SettingsDrawer.tsx)
gained a confidence column + filter. Pinned rows clamp to `>= 0.9`.

---

## F5. Conflicting-memory detector (schema v11)

Periodic background worker that scans pairs of allow-listed memories
(`fact` / `preference` / `relationship` / `event`) with high cosine
similarity but lexically contradicting content. New
[`memory_conflicts`](../../app/core/chat_database.py) table (schema
v11) records each detected pair with the heuristic signals,
optional LLM verdict, and a status of `open` / `auto_resolved` /
`user_resolved` / `dismissed`. The
[`MemoryConflictStore`](../../app/core/memory_conflict_store.py)
wraps it with `record` / `list_open` / `mark_user_resolved` /
`dismiss` / `delete_for_memory` (cascade-cleanup hook on
`MemoryStore.delete`).

Detection is hybrid: a cheap heuristic gate in
[`conflict_heuristics.py`](../../app/core/conflict_heuristics.py)
(negation flip, antonym table, numerical mismatch) labels each
candidate pair `definite` (skip LLM, resolve immediately),
`borderline` (LLM verifies via a `YES` / `NO` / `UNRELATED` JSON
prompt, rate-limited through a dedicated
[`FactCheckRateLimiter`](../../app/core/fact_check_rate_limiter.py)
with `state_key="conflict_detector.rate_state"`), or `no` (drop
without LLM cost). Confirmed conflicts with `|conf_a - conf_b| >=
0.30` (default) auto-demote the loser to `tier=archive`,
`confidence=0.20`, with `metadata.superseded_by` stamped — the rest
surface in the new Conflicts sub-tab on the Memory drawer for the
user to resolve via Keep-this / dismiss buttons. The worker
[`MemoryConflictWorker`](../../app/core/memory_conflict_worker.py)
registers with the shipped `IdleWorkerScheduler` on an hourly
cadence and respects per-tick caps (`max_corpus=1000`,
`max_pairs_per_run=50`) so an O(n²) sweep can never tank a tick.

Aiko can also self-flag mid-turn with `[[conflict:short reason]]`
(parsed in
[`response_text_service.py`](../../app/core/services/response_text_service.py),
stripped from chat/TTS, dispatched in
[`SessionController._post_turn_inner_life`](../../app/core/session_controller.py)
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
[`beliefs`](../../app/core/chat_database.py) table (schema v12) holds
two shapes in one store, distinguished by the `kind` column:
`mood` beliefs carry numeric `valence` / `arousal` so the gap
detector can compare directly against the live
[`AffectState`](../../app/core/affect_state.py), and `opinion`
beliefs hold a free-text predicted state ("rust is overhyped"). The
[`BeliefStore`](../../app/core/belief_store.py) wraps it with
`upsert` (dedupes by `(user_id, kind, topic)` plus topic-embedding
cosine ≥ 0.88) / `list_active` / `mark_contradicted` /
`mark_confirmed` / `mark_stale` / `delete` / `count_by_status`,
mirroring the F5 store shape.

Two write paths feed the store. The self-tag fast path adds a new
`[[predict:kind:topic:state:confidence]]` grammar to
[`response_text_service.py`](../../app/core/services/response_text_service.py)
(parsed alongside `[[conflict:...]]`, stripped from chat/TTS,
dispatched in `_post_turn_inner_life`); the
[`BeliefInferenceWorker`](../../app/core/belief_worker.py) mines
recent user turns once an hour, privacy-scrubs the transcript via
[`fact_check_privacy.scrub_claim_for_search`](../../app/core/fact_check_privacy.py),
spends one rate-limited LLM call through a dedicated
[`FactCheckRateLimiter`](../../app/core/fact_check_rate_limiter.py)
(`state_key="belief_worker.rate_state"`) to extract a JSON array of
`{kind, topic, predicted_state, confidence}` tuples, then upserts
with `source="worker"`. Self-tagged beliefs at higher confidence are
preserved over worker rewrites.

The
[`BeliefGapDetector`](../../app/core/belief_gap_detector.py) runs
each post-turn and surfaces mismatches: for each active mood belief
younger than `belief_recent_window_hours` (default 24h), it
flips the row to `contradicted` when
`|val_pred - val_obs| > belief_gap_valence_threshold` (default 0.30),
`|aro_pred - aro_obs| > belief_gap_arousal_threshold` (default 0.25),
or the recomputed valence band lands in opposing territory. Opinion
beliefs use
[`conflict_heuristics.classify_pair`](../../app/core/conflict_heuristics.py)
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

The [`NoveltyDetector`](../../app/core/novelty_detector.py) keeps an
in-memory `collections.deque[np.ndarray]` of size `novelty_window`
(default 12) on each `SessionController`. On the first `detect()`
call per session it lazily warms the ring from
[`RagStore.list_recent_user_vectors`](../../app/core/rag_store.py)
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
[`PromptAssembler`](../../app/core/prompt_assembler.py) (same shape
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
[`GroundingLineRenderer`](../../app/core/grounding_line.py) that
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
in [`AgentSettings`](../../app/core/settings.py) (default `"off"`,
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
  `app.core.prompt_assembler` (see P2): the `providers=` count drops
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
[`TopicStagnationDetector`](../../app/core/topic_stagnation.py) is
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
on [`PromptAssembler`](../../app/core/prompt_assembler.py) — same
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
[`chat_database.py`](../../app/core/chat_database.py); the `Memory`
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
[`FollowUpWorker`](../../app/core/follow_up_worker.py)
generates proactive nudges for overdue `future_plan` memories,
queueing them in
[`PreparedNudgeStore`](../../app/core/prepared_nudge_store.py)
for the typed-proactive fast path. Persona rules in
[`aiko_companion.txt`](../../data/persona/aiko_companion.txt) teach
Aiko to respect the temporal tags without becoming pedantic about
them. Tests: `tests/test_memory_extractor_temporal.py`,
`tests/test_follow_up_worker.py`,
`tests/test_memory_decay_temporal.py`,
`tests/test_rag_retriever_temporal.py`.

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
[`app/core/schedule_learner.py`](../../app/core/schedule_learner.py),
registers with the shipped `IdleWorkerScheduler`. The new field
is allow-listed in
[`app/core/user_profile.py`](../../app/core/user_profile.py)
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
[`AgentSettings.routine_detection_enabled`](../../app/core/settings.py)
plus `MemorySettings.routine_min_touches` /
`routine_min_share` / `routine_max_active`. Tests:
`tests/test_schedule_learner.py::RoutineDetectionTests` plus a
`PROFILE_FIELDS` assertion in `tests/test_user_profile.py`.

---

## G3. Idle curiosity worker

Picks the oldest unresolved `open_question` memory during idle,
runs it through
[`fact_check_privacy.scrub_claim_for_search`](../../app/core/fact_check_privacy.py)
to produce a safe query, calls `web_search`, distils a concise
JSON answer (`{answer, confidence}`) via Ollama, and stores the
result as a `curiosity_finding` memory linked back to the source
question. Source `open_question` rows are stamped with
`curiosity_resolved_at` / `curiosity_inconclusive_at` /
`curiosity_skipped_at` metadata so a question is never re-processed
in a tight loop. The worker shares
[`FactCheckRateLimiter`](../../app/core/fact_check_rate_limiter.py)
shape but with a separate `state_key="idle_curiosity.rate_state"`
so its budget doesn't compete with the fact-checker's. Lives in
[`app/core/idle_curiosity_worker.py`](../../app/core/idle_curiosity_worker.py).
[`rag_retriever.py`](../../app/core/rag_retriever.py) appends a
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
[`RagRetriever`](../../app/core/rag_retriever.py) embeds
`"ctx || query"`, K6
[`NoveltyDetector`](../../app/core/novelty_detector.py) embeds the
raw user message, K18
[`TopicStagnationDetector`](../../app/core/topic_stagnation.py)
piggybacks on K6's distance — plus the async
[`MessageIndexer`](../../app/core/message_indexer.py) on the
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
[`TurnRunner.run`](../../app/core/turn_runner.py) brackets each
turn with begin/end, stamps the result onto
`PromptTelemetry.embed_calls` / `embed_ms` right before the
`turn done:` INFO log, and the public `run()`'s `finally` calls
`end_turn` again as a defensive cleanup so an exception mid-flow
can't leak counter state into the next turn.

The headline INFO line gained four new fields
(`embed_calls=N embed_ms=N assemble_ms=N rag_lookup_ms=N`) and the
[`SessionController`](../../app/core/session_controller.py) metrics
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

[`PromptAssembler`](../../app/core/prompt_assembler.py) now wraps
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
[`avg_duration_ms`](../../app/core/idle_worker.py) (EMA, alpha=0.3
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

Settings: [`MemorySettings.idle_worker_tick_budget_ms`](../../app/core/settings.py)
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
[`RagStore.add_memory`](../../app/core/rag_store.py) loop. Each
call did its own `delete` + `add` under the write lock, so 135
memories meant 270 LanceDB write ops with manifest churn between
each. On Windows that landed at ~525 ms per op, ~71 s total — a
visible startup hang between `RagStore ready` and `RAG: mirrored
N existing memories into LanceDB` in the log, and one that
scaled linearly with memory count.

The mirror now goes through a new
[`RagStore.add_memories_bulk`](../../app/core/rag_store.py)
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

