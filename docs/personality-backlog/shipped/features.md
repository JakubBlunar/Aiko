# Shipped — Standalone features

Part of the [shipped log index](../shipped.md). One paragraph per entry; full detail lives in the linked implementation files.

---

## User-facing memory editor

Dedicated "Memory" tab in
[`web/src/components/SettingsDrawer.tsx`](../../../web/src/components/SettingsDrawer.tsx).
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

[`WorldStore`](../../../app/core/world/world_store.py) backs a small persistent
SQLite world (locations, items with consume semantics, a singleton
state row holding posture / activity / location). A default rich
room is seeded once on first boot. The room flows into the LLM via
a `world` inner-life provider, five new agent tools
(`look_around`, `move_to`, `change_posture`, `inspect_item`,
`consume_item`), and a `world_updated` WS event. "Give Aiko a
cookie" is intentionally silent. Schema v6 added `world_locations`
/ `world_items` / `world_state`. See
[`docs/aiko-room.md`](../../aiko-room.md).

---

## Aiko's living garden — outdoor plot + plant growth loop

Extends the world model with a `garden` location seeded
idempotently on every boot
([`WorldStore.ensure_garden_seed`](../../../app/core/world/world_store.py))
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
single location. See [`docs/aiko-room.md`](../../aiko-room.md) under
"Garden". H5 (second scene / travel semantics) in
[`immersion.md`](../immersion.md) is the natural follow-up.

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
nudges) live in [`moments.md`](../moments.md). See
[`docs/shared-moments-and-relationship.md`](../../shared-moments-and-relationship.md).

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
[`IdleWorkerScheduler`](../../../app/core/proactive/idle_worker_scheduler.py) wakes
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
[`docs/memory-tiers.md`](../../memory-tiers.md).

---

## Temporal memory awareness (schema v10)

Gives every memory three new fields — `event_time`, `temporal_type`
(`past_event` / `current_state` / `future_plan` / `recurring` /
`timeless`), and `relevance_until` — so Aiko can tell the difference
between "Jacob is in Tokyo this week" and "Jacob went to Tokyo
last year". Schema migration is additive in
[`chat_database.py`](../../../app/core/infra/chat_database.py); the `Memory`
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
[`FollowUpWorker`](../../../app/core/proactive/follow_up_worker.py)
covers proactive follow-ups for overdue `future_plan` memories.
Persona rules in
[`aiko_companion.txt`](../../../data/persona/aiko_companion.txt) teach
Aiko to respect the temporal tags without becoming pedantic about
them. Tests: `tests/test_memory_extractor_temporal.py`,
`tests/test_follow_up_worker.py`,
`tests/test_memory_decay_temporal.py`,
`tests/test_rag_retriever_temporal.py`.

**Follow-up redesign — cue producer, not verbatim speaker (the K34
pattern).** The original `FollowUpWorker` wrote a line straight into
[`PreparedNudgeStore`](../../../app/core/proactive/prepared_nudge.py),
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
[`_render_follow_up_block`](../../../app/core/session/inner_life_providers_mixin.py)
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

## H1 + K4. Conversation-arc self-tag + dialogue-act tagging (schema v13)

H1 closes the loop on the conversation-arc tracker that already shipped
in [`app/core/conversation/conversation_arc.py`](../../../app/core/conversation/conversation_arc.py)
and K4 adds the user-side cousin per turn. One schema migration adds two
nullable columns to `messages` (`arc`, `dialogue_act`); the arc taxonomy
trims to a companion-friendly six (drop `debug` / `deep_dive`, add
`silly`). H1: a new `[[arc:NAME]]` self-tag (parsed in
[`response_text_service.py`](../../../app/core/services/response_text_service.py)
mirroring `[[moment:]]` / `[[agenda:]]`) routes through
`ArcStore.set_from_self_tag` at confidence `0.85` — the new middle rung
on the ladder `regex 0.5 < self-tag 0.85 < smoother 0.95`. The estimator
hot-path guard now refuses to overwrite a self-tag-or-better prior. K4:
new [`app/core/conversation/dialogue_act_tagger.py`](../../../app/core/conversation/dialogue_act_tagger.py)
mirrors the [`promise_extractor`](../../../app/core/memory/promise_extractor.py)
shape (regex hot path inline + LLM cold path via the speaking-window
scheduler) and tags every user turn into one of `question / story /
vent / banter / planning / chitchat`. Both signals feed
[`rag_retriever.py`](../../../app/core/rag/rag_retriever.py) (`+0.03` per match,
combined cap `+0.05`) and tighten
[`proactive_director.py`](../../../app/core/proactive/proactive_director.py)
eligibility (suppress nudges on a `vent` turn; loosen cooldown on
`silly` / `playful` arcs). Tests:
[`tests/test_arc_self_tag.py`](../../../tests/test_arc_self_tag.py),
[`tests/test_dialogue_act_tagger.py`](../../../tests/test_dialogue_act_tagger.py),
[`tests/test_chat_database_migration.py`](../../../tests/test_chat_database_migration.py),
[`tests/test_rag_retriever_act_arc_boost.py`](../../../tests/test_rag_retriever_act_arc_boost.py).

## Aiko expressive speech (Pocket-TTS prosody overlay)

Pocket-TTS doesn't accept SSML, so the rollout instead exhausted the
expressive headroom already in the stack: five layers, all CPU, no
new model or library. Layer 1 wired the dormant knobs --
`assistant.tts_length_scale` (a user-facing pacing slider) gained a
real `set_length_scale` on
[`PocketTtsService`](../../../app/tts/pocket_tts_service.py) that
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
[`response_text_service.py`](../../../app/core/services/response_text_service.py)
and consumed by
[`analyze_sentence`](../../../app/core/voice/cadence.py) -- each label maps
to a small overlay on the reaction-derived `ProsodyParams`
(`speed_mult`, `gain_db_delta`, `pause_before`). Layer 4 expanded
the earcon palette in
[`app/audio/earcons.py`](../../../app/audio/earcons.py) with
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
[`tools/tts_speed_ab.py`](../../../tools/tts_speed_ab.py) renders the
calibration phrase at every `_REACTION_SPEED` value to WAV for
listening at the new edges. Persona update teaches the
`[[prosody:X]]` vocabulary alongside the existing `[[reaction:X]]`
mood label as orthogonal axes (one mood, separate vocal delivery).
Tests:
[`tests/test_pocket_tts_dormant_knobs.py`](../../../tests/test_pocket_tts_dormant_knobs.py),
[`tests/test_tts_queue_silence.py`](../../../tests/test_tts_queue_silence.py),
[`tests/test_prosody_tag_parser.py`](../../../tests/test_prosody_tag_parser.py),
[`tests/test_cadence_prosody_overlay.py`](../../../tests/test_cadence_prosody_overlay.py),
[`tests/test_earcon_auto_sprinkle.py`](../../../tests/test_earcon_auto_sprinkle.py).

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
* [`agent.tts_runtime_temp_enabled`](../../../app/core/infra/settings.py)
  (default `False`) gates the per-reaction `_REACTION_TEMP_DELTA`
  table in
  [`PocketTtsService._resolve_runtime_temp`](../../../app/tts/pocket_tts_service.py).
  When OFF, every call uses `tts.pocket_tts_temp` baseline.
* [`agent.tts_runtime_speed_enabled`](../../../app/core/infra/settings.py)
  (default `False`) gates both the per-reaction sub-cap table AND
  the cadence layer's per-sentence `speed_hint` in
  [`PocketTtsService.speak_async`](../../../app/tts/pocket_tts_service.py).
  When OFF, every sentence pins to `1.0×` before the user's
  pacing slider (`assistant.tts_length_scale`) divides in.

The user's static pacing slider is honoured regardless of either gate
(it's a deliberate global knob, not per-sentence affect drift).
Earcons, real timed pauses, per-sentence prosody labels' `gain_db` /
`pause` overlays, and the auto-sprinkle rule all keep working with both
gates off -- they're orthogonal to pitch and don't trigger the same
artefacts. Both gates are opt-in once a voice has been listened-tested
through [`tools/tts_speed_ab.py`](../../../tools/tts_speed_ab.py) and the
ear-test phrase still reads naturally at the proposed deltas. The
`_REACTION_TEMP_DELTA` table itself was halved from the original
`±0.10` to `±0.05` after the first round of tester feedback so the gate
flipped back ON also lands in a calmer band. Tests:
[`tests/test_pocket_tts_speed.py`](../../../tests/test_pocket_tts_speed.py)
adds `RuntimeSpeedGateOffTests` covering "default OFF pins to 1.0×",
"reaction is ignored", "caller `speed=` is ignored", "length-scale still
applies", and "toggle via `set_runtime_speed_enabled` flips the
behaviour back on";
[`tests/test_pocket_tts_dormant_knobs.py`](../../../tests/test_pocket_tts_dormant_knobs.py)
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
  [`data/persona/aiko_companion.txt`](../../../data/persona/aiko_companion.txt).
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
  [`app/core/persona/aiko_style_tracker.py`](../../../app/core/persona/aiko_style_tracker.py).
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
  [`AgentSettings`](../../../app/core/infra/settings.py) (`style_tracker_*`)
  and in [`config/default.json`](../../../config/default.json) so
  calibration moves without code changes.

Wiring follows the K6/K18 idiom verbatim:
[`SessionController.__init__`](../../../app/core/session/session_controller.py)
instantiates the tracker right after `TopicStagnationDetector`;
[`InnerLifeProvidersMixin._render_style_pattern_block`](../../../app/core/session/inner_life_providers_mixin.py)
calls `tracker.detect()` and renders the matching cue;
[`PromptAssembler.set_inner_life_providers`](../../../app/core/session/prompt_assembler.py)
gains a `style_pattern` slot; the resulting block is appended to
`system_parts` immediately after the K18 stagnation block so all three
"Heads-up..." cues cluster together (and all three drop in aggressive
mode for budget reasons). The post-turn pipeline
([`PostTurnMixin._post_turn_inner_life`](../../../app/core/session/post_turn_mixin.py))
feeds the tracker the *stripped* assistant text (post `strip_all_meta_tags`)
so we measure spoken content, not raw model output. Tests:
[`tests/test_aiko_style_tracker.py`](../../../tests/test_aiko_style_tracker.py)
(21 cases) covers feature extraction edges, warmup gating, each band
firing, the priority order (opener > question > length), per-band
cooldown rotation, the no-settings-stub path, and the render copy for
each band.

---

## Reliability pass — I1 + I2 + I4 + I5 (finish-the-wiring batch)

A "last mile" pass on four already-shipped features that were backend-complete but under-wired. None add a capability; together they make the existing ones trustworthy and tunable.

- **I1 — Beliefs tab live updates.** K2 theory-of-mind already broadcast `belief_added` / `belief_updated` / `belief_deleted` over WS, but `web/src` had no handler, so [`BeliefsPanel.tsx`](../../../web/src/components/settings/memory/BeliefsPanel.tsx) only refreshed on mount/filter change. Mirrored the `memoryView` pattern: a `beliefView` store slice ([`web/src/store.ts`](../../../web/src/store.ts)) with filter-aware `applyBeliefAdded/Updated/Deleted` reducers, three new cases in [`useAssistantSocket.ts`](../../../web/src/hooks/useAssistantSocket.ts), three `belief_*` variants on `WsServerEvent` ([`web/src/types.ts`](../../../web/src/types.ts)), and the panel refactored to read items/counts from the store with optimistic CRUD.
- **I2 — MessageIndexer retry/back-off.** [`message_indexer.py`](../../../app/core/rag/message_indexer.py) `_index_one` used to catch an embed/write failure, log at DEBUG, and drop the message from RAG forever. Now carries a per-work attempt counter, re-enqueues on failure with bounded exponential back-off (2s → 8s → 30s, max 3 attempts) via a `threading.Timer` guarded by `_stop`, and logs at **WARNING** with the message id on final give-up. Timers are cancelled on `stop()`. Tests: [`tests/test_message_indexer_retry.py`](../../../tests/test_message_indexer_retry.py).
- **I4 — Settings-drawer coverage for config-only knobs.** `PATCH /api/settings` is an allowlist, so this was backend + frontend, not UI-only. [`app/web/server.py`](../../../app/web/server.py) GET now returns `audio.earcons_enabled` + a new `companion` block (world-notice cadence, `grounding_line_mode`, touch/reaction/banner flags); PATCH gained per-key handlers with the same `load_settings` clamps, the `set_grounding_line_mode` / `earcons.enabled` runtime hooks, `persist_user_overrides`, and a `companion_settings_changed` WS broadcast. Frontend controls landed in [`VoiceTab.tsx`](../../../web/src/components/settings/VoiceTab.tsx) (earcons), [`WorldTab.tsx`](../../../web/src/components/settings/WorldTab.tsx) (world-notice + grounding-line), and [`AvatarTab.tsx`](../../../web/src/components/settings/AvatarTab.tsx) (touch/reactions/banner).
- **I5 — Persona-window banners honour their master switches.** The `hello` WS payload now carries the persona-touch-banner fields and [`PersonaWindow.tsx`](../../../web/src/components/PersonaWindow.tsx) threads `enabled` + `durationMs` into `<PersonaActionBanner />` from the live companion settings instead of the hardcoded defaults.

Tests: [`tests/test_message_indexer_retry.py`](../../../tests/test_message_indexer_retry.py), [`web/src/store.beliefs.test.ts`](../../../web/src/store.beliefs.test.ts), extensions to the web-server settings suite, and an updated `PersonaActionBanner` assertion in [`PersonaTaskBanner.test.tsx`](../../../web/src/components/PersonaTaskBanner.test.tsx).
