# Shipped — Awareness & memory grounding (F/G-series)

Part of the [shipped log index](../shipped.md). One paragraph per entry; full detail lives in the linked implementation files.

---

## F1. Background fact-checker worker

Idle worker that fact-checks recently surfaced claims in the
background and updates the originating memory's `confidence` (and
optionally its content) when the search clearly corrects a number /
date. Lives in
[`app/core/memory/idle_fact_checker.py`](../../../app/core/memory/idle_fact_checker.py)
and registers with the shipped `IdleWorkerScheduler`. Privacy is
enforced by [`fact_check_privacy.py`](../../../app/core/memory/fact_check_privacy.py)
which blocks personal claims at classification time and scrubs the
search query (drops emails, phone numbers, names, addresses) before
it ever leaves the box. Per-hour and per-day budgets live in
[`fact_check_rate_limiter.py`](../../../app/core/memory/fact_check_rate_limiter.py)
backed by `kv_meta`. Each phase logs at INFO with timing + previews
(`start`, `scrubbed`, `search done`, `distil done`, `apply done`)
so [`data/app.log`](../../../data/app.log) is the audit trail. Tests:
[`tests/test_idle_fact_checker.py`](../../../tests/test_idle_fact_checker.py),
[`tests/test_fact_check_privacy.py`](../../../tests/test_fact_check_privacy.py),
[`tests/test_fact_check_rate_limiter.py`](../../../tests/test_fact_check_rate_limiter.py).

**Known sharp edge: the payload the enqueue hook receives.** For most of this
worker's life it checked nothing at all — one web search in three months — and the
cause was a type mismatch, not a gate. `MemoryFacadeMixin._maybe_enqueue_claims`
read its argument with `getattr(memory, "id", None)`, but `_notify_memory_added` is
called with **both** a `Memory` and its dict form: the turn path and the REST facade
pass the object, while [`idle_knowledge_worker.py`](../../../app/core/proactive/idle_knowledge_worker.py),
[`topic_digest_worker.py`](../../../app/core/conversation/topic_digest_worker.py),
[`pre_thought_worker.py`](../../../app/core/proactive/pre_thought_worker.py) and the
K1 goal path in [`post_turn_mixin.py`](../../../app/core/session/post_turn_mixin.py)
all pass `mem.to_dict()`. A dict has no `.id`, so the hook returned silently on
exactly the impersonal `knowledge` writes that are the only thing the privacy gate
would ever let through. The failure was invisible: the "enqueue done" log line is
guarded on `if enqueued or skipped`, so a silent return logs nothing, and the
symptom was an absence.

Two rules follow. **The dual payload shape is the contract, not a caller bug** —
the WS listener in [`server.py`](../../../app/web/server.py) has always handled
both, so a consumer of `_notify_memory_added` must too; read fields through
`_memory_field`. And **an always-empty queue is a symptom worth alerting on**: the
diagnostic that finally found this was counting rows, not reading logs.
[`scripts/fact_check_backfill.py`](../../../scripts/fact_check_backfill.py) replays
the stored corpus through the real gates and reports what *would* queue, which is
the cheap way to ask "is this subsystem alive?" — it found 46 claims across 34
memories that should have been checked and never were.

---

## F2. Knowledge-gap journal

Captures Aiko's "I don't know" moments as structured
`knowledge_gap` memories so F1 can close them later and the prompt
can resurface them when the topic returns. Extraction lives in
[`app/core/memory/knowledge_gap_extractor.py`](../../../app/core/memory/knowledge_gap_extractor.py)
(regex + the inline `[[gap:topic:question]]` self-tag, mirroring
the promise extractor shape). Storage reuses `MemoryStore` via the
`knowledge_gap` kind in [`memory_store.py`](../../../app/core/memory/memory_store.py).
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
[`KnowledgeGapStore.mark_resolved`](../../../app/core/memory/knowledge_gap_extractor.py)
API:

* **Idle memory-match resolver** — a new
  [`IdleGapResolver`](../../../app/core/conversation/idle_gap_resolver.py) registered
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
  [`PostTurnMixin`](../../../app/core/session/post_turn_mixin.py),
  modeled directly on `_resolve_curiosity_seeds`. After every turn it
  embeds `user_text + assistant_text` once and cosines against every
  open gap's stored embedding. Anything above
  `agent.gap_user_answer_resolve_threshold` (default 0.50) closes
  with `resolved_by="user_answer"`. This catches the answer the
  moment the user speaks it; the idle worker mops up the rest.

Tunables on
[`AgentSettings`](../../../app/core/infra/settings.py):
`gap_resolver_enabled`, `gap_resolver_interval_seconds` (600),
`gap_resolver_threshold` (0.55 — slightly stricter than the seed
resolver's 0.50 because closing a gap is a stronger claim than
consuming a seed), `gap_resolver_per_tick` (5),
`gap_user_answer_resolve_threshold` (0.50).

Tests:
[`tests/test_idle_gap_resolver.py`](../../../tests/test_idle_gap_resolver.py)
(15 cases: backfill happy path, kind filtering, threshold clamps,
per-tick cap, `is_ready` gates, INFO audit log) and
[`tests/test_session_controller_gap_resolver.py`](../../../tests/test_session_controller_gap_resolver.py)
(8 cases mirroring the K9 seed-resolve fixture pattern).

---

## F12. Semantic echo — revival stops only crediting what Aiko quotes

**The problem.** `revival_score` is the memory layer's one durable
quality signal: it earns a per-day salience rebate, it is an
early-promotion path out of `scratchpad`, it protects `long_term` rows
from archive demotion, and it lowers prune priority. It was decided by a
bag-of-words test — tokenise Aiko's reply and the surfaced memory, drop
stopwords and anything under four characters, and count it a citation at
three shared content words.

That test is strict enough to almost never produce a false positive,
which sounds like a virtue until you notice the consequence: **nearly all
of its errors are misses.** Aiko paraphrases constantly — that is the
entire reason to hand a memory to a language model rather than pasting it
— and a paraphrase shares no credit. "You mentioned wanting to get back
into film photography" against a stored "user shot 35mm in college and
misses it" is a perfect use of the memory and scores zero overlap. So the
memories that survived were the ones Aiko happened to *quote*, not the
ones she used well.

**One detector, two consumers.** The tokeniser, stopword list and overlap
test used to live as private helpers on the post-turn mixin, and the L37
ledger had grown a second copy of the same logic. Both now defer to
[`app/core/memory/echo_detector.py`](../../../app/core/memory/echo_detector.py),
which returns an `EchoVerdict` carrying *how* the echo was decided
(`lexical` / `semantic` / `none`) and *how strongly* (overlapping word
count, or cosine). The memory layer and the ledger can no longer drift
apart on what an echo is.

Lexical runs first, because a quote is unambiguous and cheap. On a miss
it falls back to cosine between the reply and the stored memory. Both
vectors were already in hand — memory embeddings live on `MemoryStore`'s
in-process mirror, and K22's callback detector already embedded the
reply — so this costs **no extra embed call**. The fallback is enabled by
supplying a floor rather than a flag, so there is no way to ask for
semantic matching without saying how strict.

**The embed hoist.** The reply embedding was computed *inside* K22's gate,
which meant its other consumer (K30 repeated-thought detection) silently
inherited the callback detector's on/off switch. Semantic revival needed
the same vector and must not inherit it too, so the embed moved to the top
of `_post_turn_inner_life` under the union of its consumers' conditions,
and `_last_assistant_vec` is now **cleared unconditionally first** — a
skipped or failed embed previously left the *previous* turn's vector in
place, which would have been compared against this turn's memories. K20's
carry-forward only ever copies non-None, so it behaves exactly as before.

**Why a semantic hit earns less — and the retention gate that forced the
issue.** The obvious design is to treat both verdicts alike. That would
have been a mistake, for a reason the original backlog entry missed:
**the candidates were already selected for topical similarity.** A
surfaced memory is one of the top-k nearest to the turn, and the reply is
about that same turn, so a high cosine is close to guaranteed and partly
measures "was on topic" rather than "she used it".

Scratchpad TTL deleted an unused row only when `revival_score == 0.0`
*exactly*. Under full credit, nearly every surfaced scratchpad row would
have acquired some score and become permanently exempt — F12 would have
quietly switched scratchpad cleanup off altogether rather than improving
what was retained. So:

- a **lexical** hit earns the full historical `revival_per_hit` (0.15);
- a **semantic** hit earns `semantic_revival_per_hit` (0.05);
- the TTL gate became `revival_score < scratchpad_ttl_min_revival` (0.10),
  which also retires a brittle float-equality test against a value that
  decays.

The thresholds interlock deliberately: a quoted memory is spared exactly
as before, a merely on-topic one is not, and *two* semantic hits clear the
bar — repeated weak evidence is worth more than one instance, decided by
the threshold rather than a special case. A configured `0` keeps meaning
"delete rows with no revival at all" via an epsilon floor, rather than
`< 0.0`, which would match nothing.

**Measuring instead of guessing.** The 0.62 floor is a guess, and schema
**v27** exists so it does not have to stay one: `surfacing_outcomes` gained
`echo_kind` and `echo_score`, recorded on *misses as well as hits* —
a floor cannot be re-derived from a table that kept only the rows clearing
the floor we happened to pick first. Two reads on
[`SurfacingOutcomeStore`](../../../app/core/memory/surfacing_outcome_store.py)
expose it through `get_surfacing_outcomes`:

- `echo_breakdown` — engaged rate split by `echo_kind`. If semantic rows
  engage about as often as lexical ones the discount is unjustified; if
  they engage no better than rows with no echo, it was right.
- `semantic_floor_candidates` — replays each candidate floor over the
  recorded cosines, counting only rows the lexical test did not already
  claim. A floor whose engaged rate is flat all the way up is measuring
  topic, not use.

Pre-v27 rows keep NULL on both columns and report as `"unrecorded"`
rather than being folded into `none`: they were judged by the lexical test
alone, and back-filling `lexical` would claim a semantic comparison had
been made and lost.

**Deferred on purpose.** Whether a semantic hit should earn *full*
retention credit is [F17](../awareness.md#f17-should-a-semantic-echo-earn-full-retention-credit),
to be settled from the data above rather than from intuition now. The
user-side credit half of the original entry — rewarding a memory for the
*user's* engagement rather than Aiko's own verbosity — is also still open
there; L37's ledger already records exactly that join.

**Settings.** `memory.semantic_revival_enabled` (default on),
`memory.semantic_revival_min_cosine` (0.62; `0` disables),
`memory.semantic_revival_per_hit` (0.05),
`memory.scratchpad_ttl_min_revival` (0.10). See
[`docs/memory-tiers.md`](../../memory-tiers.md#revival-drift-e2).

**Tests.**
[`tests/test_semantic_echo.py`](../../../tests/test_semantic_echo.py) —
the detector (quote beats floor, paraphrase caught, sub-floor cosine still
reported, unusable vectors degrade), option-A credit split, the TTL gate
including the configured-zero case, and the calibration reads. Migration
and echo-column coverage in
[`tests/test_surfacing_outcome_ledger.py`](../../../tests/test_surfacing_outcome_ledger.py).

---

## F3. Confidence column on memories

`confidence REAL NOT NULL DEFAULT 0.7` added to the `memories`
table; `Memory` dataclass + `MemoryStore.add` / `update` / mirror
plumbing all carry it now. Defaults: extractor `0.7`,
`[[remember:self:...]]` self-tags `0.85`, `[[remember:...]]`
user-confirmed tags `0.9`, tool-result memories (RAG / web) `0.95`,
manual memory-tab creates `1.0`. F1 pushes confidence up toward
`0.95` on positive verification and down to `0.4` on contradiction.
[`rag_retriever.py`](../../../app/core/rag/rag_retriever.py) penalises
hits with `confidence < 0.5` and appends an `(uncertain)` suffix
in the rendered memory block so the LLM hedges. Memory tab in
[`SettingsDrawer.tsx`](../../../web/src/features/settings/SettingsDrawer.tsx)
gained a confidence column + filter. Pinned rows clamp to `>= 0.9`.

---

## F5. Conflicting-memory detector (schema v11)

Periodic background worker that scans pairs of allow-listed memories
(`fact` / `preference` / `relationship` / `event`) with high cosine
similarity but lexically contradicting content. New
[`memory_conflicts`](../../../app/core/infra/chat_database.py) table (schema
v11) records each detected pair with the heuristic signals,
optional LLM verdict, and a status of `open` / `auto_resolved` /
`user_resolved` / `dismissed`. The
[`MemoryConflictStore`](../../../app/core/memory/memory_conflict_store.py)
wraps it with `record` / `list_open` / `mark_user_resolved` /
`dismiss` / `delete_for_memory` (cascade-cleanup hook on
`MemoryStore.delete`).

Detection is hybrid: a cheap heuristic gate in
[`conflict_heuristics.py`](../../../app/core/memory/conflict_heuristics.py)
(negation flip, antonym table, numerical mismatch) labels each
candidate pair `definite` (skip LLM, resolve immediately),
`borderline` (LLM verifies via a `YES` / `NO` / `UNRELATED` JSON
prompt, rate-limited through a dedicated
[`FactCheckRateLimiter`](../../../app/core/memory/fact_check_rate_limiter.py)
with `state_key="conflict_detector.rate_state"`), or `no` (drop
without LLM cost). Confirmed conflicts with `|conf_a - conf_b| >=
0.30` (default) auto-demote the loser to `tier=archive`,
`confidence=0.20`, with `metadata.superseded_by` stamped — the rest
surface in the new Conflicts sub-tab on the Memory drawer for the
user to resolve via Keep-this / dismiss buttons. The worker
[`MemoryConflictWorker`](../../../app/core/memory/memory_conflict_worker.py)
registers with the shipped `IdleWorkerScheduler` on an hourly
cadence and respects per-tick caps (`max_corpus=1000`,
`max_pairs_per_run=50`) so an O(n²) sweep can never tank a tick.

Aiko can also self-flag mid-turn with `[[conflict:short reason]]`
(parsed in
[`response_text_service.py`](../../../app/core/services/response_text_service.py),
stripped from chat/TTS, dispatched in
[`SessionController._post_turn_inner_life`](../../../app/core/session/session_controller.py)
to `IdleWorkerScheduler.force_run` so the worker runs immediately
instead of waiting for the next hour). REST endpoints
`/api/memory-conflicts` (GET / resolve / dismiss) in
[`app/web/server.py`](../../../app/web/server.py) back the new
Conflicts sub-tab in
[`SettingsDrawer.tsx`](../../../web/src/features/settings/SettingsDrawer.tsx),
which renders a side-by-side card per pair with similarity, both
confidences, the heuristic signals chips, and the LLM reason when
present. A collapsed "Recently auto-resolved" tail provides a
read-only audit log. Tests:
[`tests/test_conflict_heuristics.py`](../../../tests/test_conflict_heuristics.py),
[`tests/test_memory_conflict_store.py`](../../../tests/test_memory_conflict_store.py),
[`tests/test_memory_conflict_worker.py`](../../../tests/test_memory_conflict_worker.py),
plus extensions to `tests/test_response_text_service.py` and
`tests/test_web_server_memories.py`.

---

## F13. The contradiction family's fourth corner — the user corrects Aiko

Closes the hole the K38 docstring named: F5 catches two stored
memories that clash, K29 catches Aiko's stance vs. the user's claim,
K38 catches Aiko's reply vs. her own stored fact — and F13 catches the
**user explicitly correcting a stored fact** ("no, it's my sister, not
my brother", "I never said that", "actually it's Tuesday"). That is the
highest-quality supervisory signal the system ever gets, so instead of
landing beside the wrong memory at equal confidence it now *supersedes*
it. All LLM work and the memory rewrite run off the turn path so latency
is untouched.

A cheap, pure, embedding-free detector
([`user_correction_detector.py`](../../../app/core/conversation/user_correction_detector.py),
sibling of `self_correction_detector.py`) runs post-turn against the
rows RAG surfaced last turn: it requires an explicit correction marker
**and** a content-word overlap with a candidate, and only ever targets
durable-truth kinds (`fact` / `preference` / `relationship` / `event`) —
never a `self` stance row, which keeps the **correction-of-fact vs
disagreement-of-opinion** boundary (that is K29's lane). A hit is stashed
on a bounded `_pending_correction_candidates` queue by
[`post_turn_helpers_mixin.py`](../../../app/core/session/post_turn_helpers_mixin.py);
no LLM, no writes on the turn path.

The off-turn
[`UserCorrectionWorker`](../../../app/core/memory/user_correction_worker.py)
(registered in `idle_workers_init_mixin.py`, modelled on the F5 worker)
drains the queue, confirms each candidate with a `FactCheckRateLimiter`-
gated one-line-JSON LLM call (its own `state_key`), and on `YES` writes
the corrected fact as a new memory at high confidence (0.9) and demotes
the corrected row with the exact F5 supersede stamp (`confidence` 0.20,
`tier="archive"`, `metadata.superseded_by` / `superseded_reason`). It
then propagates the demotion to any backed concept **with no LLM** —
`affected_concepts_for_memory` + `apply_contradiction_penalty` +
`concept_store.update` — and arms a low-key `user_correction` T6 cue so
Aiko owns the slip once, naturally ("ah, I had that backwards"), never
"I've updated my database". Cue policy in
[`cue_accounting.py`](../../../app/core/proactive/cue_accounting.py),
rendered by `_render_user_correction_block`. Settings:
`agent.user_correction_enabled` + caps, `memory.user_correction_*`
(overlap / confidence thresholds, worker cadence + per-tick cap, concept
penalty, supersede confidence). Tests:
[`tests/test_user_correction.py`](../../../tests/test_user_correction.py).

---

## F14. "I was wrong about that" — let fact-check reversals reach the user

The mirror of F13, and the third corner of the "own what you got wrong"
family beside K38 (a slip in her own reply) and F13 (a fact the user set
straight): F14 catches a claim **Aiko's own background research reversed**
after she had already told the user, and brings it back unprompted — "that
thing I mentioned? I looked it up and I had it backwards, it's actually
Y". Previously the F1
[`IdleFactChecker`](../../../app/core/memory/idle_fact_checker.py) would
contradict a stored claim, drop its confidence, even rewrite its content,
and end the loop at a silent `notify_memory_updated` — throwing away the
single most trust-building beat a companion has, and the only visible proof
the background machinery exists.

The reversal gate lives in `_apply_verdict`'s existing detail dict (no
schema change): it fires only on a genuine reversal — `verdict.kind ==
"contradict"`, a confidence drop clearing
`memory.fact_reversal_min_delta` (default 0.25, so `0.7 → 0.65` drift
never qualifies), **and** an actual content rewrite (`|delta| > 0.2`).
Two more bars, both mirroring the F14 backlog's open questions: **she must
actually have said it** — a late-bound `was_surfaced` callable reads the
L37 [`SurfacingOutcomeStore`](../../../app/core/memory/surfacing_outcome_store.py)
so a low-confidence note quietly fixed before it ever surfaced is nothing
to apologise for; and **F13 must not have beaten it** — a row already
`superseded_reason="user_correction"` (or archived) is suppressed, since
the fact-checker arriving after the user already corrected her is redundant
and a little insulting.

On a pass it calls the injected `arm_reversal` →
`PostTurnHelpersMixin.queue_fact_reversal_cue` (subject = the *corrected*
fact so post-turn matching credits her for stating the right thing), which
composes a low-key line — never "I've updated my database" — and queues a
`fact_reversal` T6 cue. Cue policy in
[`cue_accounting.py`](../../../app/core/proactive/cue_accounting.py)
(`inventory_target=0`, `ttl_hours=72`, `max_surfacings=2`, 24h
surface-cooldown, `MATCH_LEXICAL`), rendered by
`_render_fact_reversal_block` right after `user_correction_block` in the
T6 "own what you owe" cluster. It rides along on the next natural turn
(like K38/F13) rather than opening one via `prepared_nudge` — the stronger
unprompted beat is a much higher nag risk and stays a future option. Because
F1's personal-claim privacy filter rarely web-checks user-specific facts,
F14 fires mainly on world claims Aiko asserted; that is accepted, not fixed
here. Settings: `agent.fact_reversal_enabled` (master),
`memory.fact_reversal_min_delta` (clamp [0, 0.3]). Tests:
[`tests/test_fact_reversal.py`](../../../tests/test_fact_reversal.py).

---

## F16. Testimony vs. inference — did he tell her, or did she guess?

Nothing in the memory layer used to distinguish **what the user said** from
**what Aiko concluded**: a `fact` the extractor distilled from an inference
across three conversations and a `fact` the user stated outright were the
same kind at the same default confidence, rendered with the same flat
bullet — so Aiko would assert "you told me you hate meetings" when he never
said that. The honest version ("I get the sense you'd rather skip meetings")
is the same belief correctly attributed, and it *invites* the correction F13
exists to catch instead of foreclosing it.

F16 adds a real `provenance` column (schema **v30**, `stated` / `inferred`,
default `inferred`) to `memories`, mirroring the `temporal_type` precedent
(fresh CREATE + guarded v29→v30 `ALTER` backfilling legacy rows to
`inferred`) in
[`chat_database.py`](../../../app/core/infra/chat_database.py) and
[`memory_store.py`](../../../app/core/memory/memory_store.py)
(`VALID_PROVENANCE` / `_coerce_provenance`, `Memory.provenance`, the `add()`
kwarg, INSERT / read / `_reload_mirror` fallback ladder, `to_dict`). The
default is `inferred` on purpose — over-claiming testimony is the failure
being fixed, so anything unsure lands on the safe side.

The write paths are labelled at their source: the
[`MemoryExtractor`](../../../app/core/memory/memory_extractor.py) prompt asks
the LLM for a per-memory `provenance` (validated + defaulted in
`_validate_entries`); explicit `[[remember:]]` tags
([`turn_runner.py`](../../../app/core/session/turn_runner.py)), F13-confirmed
corrections
([`user_correction_worker.py`](../../../app/core/memory/user_correction_worker.py)),
and manual editor / MCP adds
([`memory_facade_mixin.py`](../../../app/core/session/memory_facade_mixin.py))
all write `stated`. Rendering + ranking live in
[`rag_retriever.py`](../../../app/core/rag/rag_retriever.py): `RagHit`
([`rag_store.py`](../../../app/core/rag/rag_store.py)) gains
`memory_provenance`, stamped in the `retrieve()` join; `format_block` appends
an `(inferred)` suffix on durable user-fact kinds only
(`fact` / `preference` / `relationship` / `event`, never
`self` / `self_tagged` / `knowledge` / `curiosity_finding`), gated by the new
`agent.memory_provenance_enabled` master switch; and a pure
`_provenance_penalty` (−0.03, unconditional like `_confidence_penalty`)
demotes an inferred hit a hair so testimony floats above inference at equal
cosine. Persona gloss lives beside the other trust tags in
[`aiko_companion.txt`](../../../data/persona/aiko_companion.txt). Scope was
deliberately **memory-layer only** — concepts already render tentatively via
`_hedge_for_confidence`, and the concept-voice deepening stays with L41; no
third `confirmed` value; no LanceDB change (provenance is joined from SQLite,
exactly like `confidence` / `temporal_type`). Tests:
[`tests/test_memory_provenance.py`](../../../tests/test_memory_provenance.py).

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
[`app/core/infra/schedule_learner.py`](../../../app/core/infra/schedule_learner.py),
registers with the shipped `IdleWorkerScheduler`. The new field
is allow-listed in
[`app/core/infra/user_profile.py`](../../../app/core/infra/user_profile.py)
`PROFILE_FIELDS` so the LLM `UserProfileWorker` is also aware of
it. Tests: `tests/test_schedule_learner.py`.

---

## G3. Idle curiosity worker

Picks the oldest unresolved `open_question` memory during idle,
runs it through
[`fact_check_privacy.scrub_claim_for_search`](../../../app/core/memory/fact_check_privacy.py)
to produce a safe query, calls `web_search`, distils a concise
JSON answer (`{answer, confidence}`) via Ollama, and stores the
result as a `curiosity_finding` memory linked back to the source
question. Source `open_question` rows are stamped with
`curiosity_resolved_at` / `curiosity_inconclusive_at` /
`curiosity_skipped_at` metadata so a question is never re-processed
in a tight loop. The worker shares
[`FactCheckRateLimiter`](../../../app/core/memory/fact_check_rate_limiter.py)
shape but with a separate `state_key="idle_curiosity.rate_state"`
so its budget doesn't compete with the fact-checker's. Lives in
[`app/core/proactive/idle_curiosity_worker.py`](../../../app/core/proactive/idle_curiosity_worker.py).
[`rag_retriever.py`](../../../app/core/rag/rag_retriever.py) appends a
`(curiosity)` suffix on retrieved findings, and a Memory-section
rule in [`aiko_companion.txt`](../../../data/persona/aiko_companion.txt)
teaches Aiko to surface them as "I was reading about X — turns
out..." rather than reciting them as bare facts. Tests:
`tests/test_idle_curiosity_worker.py` plus the new state-key
independence test in `tests/test_fact_check_rate_limiter.py`.

---

## F6. Privacy-preserving query *reformulation* (not reject) — SHIPPED

**Status: shipped.** Implemented as
[`app/core/memory/query_reformulation.py`](../../../app/core/memory/query_reformulation.py)
(`reformulate_query_for_search` + `make_reformulator`). The local worker
model rewrites a personal claim into a neutral, name-free topic query;
the deterministic `scrub_claim_for_search` runs as a hard **post-filter**
on the LLM output so a hallucinated name can never slip through, and the
deterministic scrub of the original is the fallback when the model
returns `NONE` / fails / fails the post-filter. Threaded into all three
workers' scrub methods (`idle_fact_checker._scrub_claim`,
`idle_curiosity_worker._scrub`, `idle_knowledge_worker._scrub`) via an
optional `query_reformulator` closure built by
`SessionController._build_query_reformulator`. Gated by
`search.query_reformulation_enabled` (default on). Shipped alongside the
LangSearch web-search backend (see
[`docs/configuration.md`](../../configuration.md) `search` block).

**Motivation.** The single biggest reason Aiko "tries to search but it's
blocked": the privacy gate
([`scrub_claim_for_search`](../../../app/core/memory/fact_check_privacy.py))
drops name/pronoun/PII tokens and then **rejects the whole query** if
what survives is too short or has no ≥3-char word. An open question like
*"did {user} ever watch more currently-airing anime"* scrubs down to
nothing and gets stamped `privacy_gate` → never searched, never
answered. The reject is correct (don't leak the name) but the *outcome*
is wrong (the topic was perfectly searchable). The fix is to **rewrite
the personal claim into its searchable topic** instead of token-dropping
it: *"{user} wants more airing anime"* → *"best currently airing anime
summer 2026"*.

**Key files.**
[`app/core/memory/fact_check_privacy.py`](../../../app/core/memory/fact_check_privacy.py)
(`scrub_claim_for_search` — add a reformulation step before the
length/word reject), the two callers that today just skip on `None`:
[`app/core/memory/idle_fact_checker.py`](../../../app/core/memory/idle_fact_checker.py)
(`_scrub_claim`) and
[`app/core/proactive/idle_curiosity_worker.py`](../../../app/core/proactive/idle_curiosity_worker.py)
(`_scrub`).

**Sketched approach.** Add an optional **local-LLM** reformulation
(workers already hold a local `OllamaClient` — zero cloud cost, no
privacy regression since the name never leaves the box). Prompt: "Rewrite
this into a neutral web-search query about the *topic only*, removing any
personal names, pronouns, dates, or private details. If there is no
general topic, output NONE." Keep the existing deterministic
token-scrub + PII hard-reject as a **post-filter on the LLM output** so a
hallucinated name can never slip through. Only fall back to silent-skip
when the reformulation returns `NONE` or fails the post-filter. Cheapest
win in the whole knowledge theme — unblocks F7/F8/F9.

**Effort.** Small-Medium.

---

## F8. `knowledge` memory kind + web→RAG retrieval boost (+ F4 source-citing)

A real, accumulating home for learned facts so Aiko gets *less* generic
over time instead of restarting from parametric knowledge every
informational turn — and crucially a **separate lane** from personal
memory so knowledge never fights `fact`/`event` about the user. A new
`kind="knowledge"` in
[`VALID_KINDS`](../../../app/core/memory/memory_store.py) holds distilled,
impersonal, non-time-sensitive facts (band names in a genre, a studio's
filmography, how a thing works); it mirrors into LanceDB automatically and
dedups through the existing cosine-collapse path so repeat research merges
instead of piling up. Every knowledge row is **source-cited (F4)**:
`metadata` carries `{topic, source_query, source_url, source_urls,
learned_at, cluster_key}`. Retrieval adds a small bonus
([`_RAG_KNOWLEDGE_BONUS = 0.05`](../../../app/core/rag/rag_retriever.py))
to knowledge hits **only on informational turns** — gated on the K4
dialogue-act tag (`_INFORMATIONAL_ACTS = {"question"}`), so a distilled
fact wins over an equally-similar personal memory when the user asks "what
are some good X?" but stays neutral on emotional / banter turns where
reciting a fact would read as a lecture. Knowledge hits surface with a
`(learned)` suffix tag (mirroring the `(curiosity)` tag) so the persona
rule lets Aiko present them naturally. Tests:
[`tests/test_rag_retriever_knowledge_boost.py`](../../../tests/test_rag_retriever_knowledge_boost.py).

---

## F9. Interest-driven knowledge enrichment worker

The engine that *fills* the F8 pool without waiting for a fact-check
trigger:
[`IdleKnowledgeWorker`](../../../app/core/proactive/idle_knowledge_worker.py)
(`name="idle_knowledge"`) reads the **K9 topic graph** on idle ticks,
scores clusters on a coverage-weighted blend of knowledge headroom (0.45)
+ conversational size (0.35) + freshness (0.20) so one big interest can't
monopolise it, then runs a worker-LLM **research planner** that judges
whether a cluster has an evergreen, impersonal subject worth researching
and emits neutral search queries with every personal detail stripped
(purely-personal clusters get a long cooldown). The chosen query is
privacy-scrubbed (the same F6 reformulation / `scrub_claim_for_search`
gate as F1/G3), web-searched, and distilled into ≤2 evergreen impersonal
facts (`think=False` mechanical summarisation, confidence floor `0.6`,
cap `0.9`) written as `knowledge` rows. Extra planner angles are queued
per-cluster for later deepening. **Strictly silent** (never fires a
proactive message) and **off the brain path** (idle scheduler, worker
model). Its own `FactCheckRateLimiter` budget keyed on
`idle_knowledge.rate_state` (per-hour `1`, per-day `4`) keeps it from
grinding. Settings: `agent.knowledge_enrichment_enabled` /
`knowledge_topic_extraction_enabled` /
`knowledge_enrichment_per_{hour,day}_cap`, and `memory.knowledge_*`
(interval `3600`s, `max_clusters_per_run` `3`, `max_per_cluster` `3`,
`cluster_cooldown_hours` `72`, `unresearchable_cooldown_hours` `336`,
`research_queries_per_cluster` `3`). MCP debug:
`force_run("idle_knowledge")` /
[`get_knowledge_worker_state`](../../../app/mcp/server_tools/memory_worker_tools.py).
Grep: `tail_logs(module_contains="idle_knowledge")` (`knowledge start` /
`scrubbed` / `search done` / `distil done` / `apply done`). Tests:
[`tests/test_idle_knowledge_worker.py`](../../../tests/test_idle_knowledge_worker.py),
[`tests/test_worker_query_reformulation.py`](../../../tests/test_worker_query_reformulation.py).

---

## K61. `knowledge_grounding` inner-life block (commit to specifics)

The read-side companion to F8/F9: when knowledge rows are available and
the turn is informational, this nudges Aiko to *commit to the specifics
she's learned* instead of survey-hedging ("there are lots of great
options…"). [`_render_knowledge_grounding_block`](../../../app/core/session/inner_life_part2.py)
surfaces up to `knowledge_grounding_max_items` (default `2`) of the
on-topic `knowledge` memories above
`knowledge_grounding_min_similarity` (default `0.45`), registered as the
`knowledge_grounding` provider and slotted in the T6 detector tier of
[`prompt_assembler.py`](../../../app/core/session/prompt_assembler.py)
(takes `user_text`). Persona copy in
[`aiko_companion.txt`](../../../data/persona/aiko_companion.txt) teaches
her to drop the specifics naturally ("oh — try Slowdive"), never as a
lecture. Settings: `agent.knowledge_grounding_enabled` +
`memory.knowledge_grounding_{min_similarity,max_items}`. Tests:
[`tests/test_knowledge_grounding_provider.py`](../../../tests/test_knowledge_grounding_provider.py).

---

## F10. Topic-graph utilisation (RAG / prompt / knowledge integration)

**Foundation shipped.** The topic graph used to be **one giant
single-link cluster** — useless for anything downstream. It now uses an
**adaptive mutual-k-NN clusterer** ([`topic_graph.py`](../../../app/core/conversation/topic_graph.py)
`_cluster_memories_adaptive`): an edge forms only when two memories are
in each other's top-`k` nearest neighbours (`k ≈ log2(n)+1`, clamped),
so a generic "bridge" memory can't chain two dense families together,
and there's no global threshold to hand-tune. The snapshot now reports
`algorithm` + `neighbors_k`. With the graph carving cleanly into real
topics, these consumers become worth building (today the graph only
feeds K9 curiosity dedup + F9 cluster-pick + the observability browser —
**nothing in RAG or the prompt reads it**).

**Sub-ideas (pick independently).**
- **F10a. LLM-labelled clusters. ✅ SHIPPED.** A cluster's label used to
  be the first sentence of its highest-salience member. The
  [`ClusterLabelWorker`](../../../app/core/conversation/topic_label_worker.py)
  idle worker now names each cluster ("weekend hiking plans") via a tiny
  worker-LLM pass, applied through
  [`TopicGraph.set_cluster_label`](../../../app/core/conversation/topic_graph.py)
  (updates the live `_LiveCluster.label` + persists to `topic_clusters`).
  Labels are cached in `kv_meta` keyed by the cluster representative
  (`aiko.topic_label.<rep>`) with the size-at-label-time, so a batch
  refit doesn't force a re-label: the next tick re-applies the cached
  label for free (no LLM) and only regenerates when the representative is
  new or the size drifted >50%. Per-tick LLM spend bounded by
  `agent.topic_label_max_per_run` (largest-first). Surfaces as the
  cluster `summary` in the snapshot / `GET /api/topic-graph` / Memory
  drawer. Settings: `agent.topic_label_{enabled,interval_seconds,max_per_run,max_tokens}`.
  Tests: [`tests/test_topic_label_worker.py`](../../../tests/test_topic_label_worker.py).
- **F10b. Cluster-aware RAG diversity. ✅ SHIPPED.** In
  [`rag_retriever.py`](../../../app/core/rag/rag_retriever.py), the final
  top-k selection now caps how many hits may come from a single topic
  cluster so a dense knot (e.g. the big "get to know the user" cluster)
  can't monopolise every slot and crowd out other relevant context.
  Implemented as a deterministic MMR-lite: walk the deduped,
  score-descending candidates and defer a memory hit once its cluster
  already holds `rag_max_per_cluster` (default 3) admitted hits, then
  **backfill** from the deferred overflow in score order so the re-rank
  only ever *reorders* the top-k — it never shrinks it. Cluster id comes
  from [`TopicGraph.cluster_id_for`](../../../app/core/conversation/topic_graph.py)
  (O(1) read against the warm assignment map, never forces a rebuild);
  the graph is wired into the retriever via a second-pass `set_topic_graph`
  (mirroring `set_goal_store`). Only `memory` hits with a known cluster are
  capped — message / document hits and unclustered memories are always
  admitted. Note this is **not** about context bloat (the `top_k` cap
  already bounds total context regardless of cluster size); it's about
  *monoculture* — diversifying which topics fill the slots. Gated by
  `agent.rag_cluster_diversity_enabled` (default on) + `rag_max_per_cluster`;
  no-op on the in-memory / non-persistent topic-graph path. Pure retrieval
  re-rank, no prompt-shape change. Tests:
  [`tests/test_rag_retriever_cluster_diversity.py`](../../../tests/test_rag_retriever_cluster_diversity.py).
- **F10c. Topic expansion / multi-hop. ✅ SHIPPED.** When a turn's
  strongest memory hit (score ≥ `agent.rag_expand_trigger_score`, default
  `0.55`) belongs to a topic cluster,
  [`RagRetriever._expand_topic`](../../../app/core/rag/rag_retriever.py)
  appends up to `agent.rag_expand_max` (default `2`) sibling members of
  that cluster — **beyond** the top-k — whose cosine to the live query
  clears `agent.rag_expand_min_sim` (default `0.45`), so Aiko gets the
  surrounding context, not just the single closest line. Siblings are
  reached by id via two cheap graph readers
  ([`TopicGraph.cluster_id_for`](../../../app/core/conversation/topic_graph.py)
  + `cluster_member_ids`) and scored by a dot product against the query
  embedding (no extra embed, no extra DB search). The new hits carry a
  `RagHit.expansion=True` flag and render in their own
  "Related notes from the same topic" section of `format_block`, so the
  LLM reads them as associative rather than direct recall. This is the
  **graph-aware multi-hop retrieval** explicitly deferred in the K9 spec
  (see [`patterns.md`](../patterns.md) K9). It **does** change prompt content,
  so it's gated + bounded; flip `agent.rag_topic_expansion_enabled=false`
  (or `rag_expand_max=0`) to revert to pure top-k. No-op without a
  persistent topic graph + memory store. Tests: `TopicExpansionTests` +
  `FormatBlockExpansionTests` in
  [`tests/test_rag_retriever_topic_expansion.py`](../../../tests/test_rag_retriever_topic_expansion.py).
- **F10d. Cluster-summary coarse retrieval tier (cluster-scoped recall).
  ✅ SHIPPED.** Coarse → fine retrieval: match a query to a whole topic
  cluster by **centroid cosine**
  ([`TopicGraph.best_clusters_for`](../../../app/core/conversation/topic_graph.py)
  — a handful of dot products against cluster centroids, no member join,
  no embed) then drill into that cluster's members ranked by cosine to the
  query ([`RagRetriever.recall_topic`](../../../app/core/rag/rag_retriever.py)
  returns `(cluster_label, hits)`). Surfaced as the new **`recall_topic`
  tool** ([`builtins.py`](../../../app/llm/tools/builtins.py)): where the base
  `recall` does a global search for the few closest snippets, `recall_topic`
  enumerates one coherent theme — the natural "what do I actually know
  about X?" answer when the user asks Aiko to round up / summarise a
  subject. Gated by `tools.recall_topic` (default on; registered in
  [`tools_registry_mixin.py`](../../../app/core/session/tools_registry_mixin.py)
  + [`base.py`](../../../app/llm/tools/base.py), mapped to the `recall` family
  in [`tool_pass_gate.py`](../../../app/core/session/tool_pass_gate.py)).
  No-op (empty result) without a persistent topic graph. Tests:
  `RecallTopicTests` + `RecallTopicToolTests` in
  [`tests/test_rag_retriever_topic_expansion.py`](../../../tests/test_rag_retriever_topic_expansion.py)
  + `ClusterMemberAndCoarseMatchTests` in
  [`tests/test_topic_graph_persistent.py`](../../../tests/test_topic_graph_persistent.py).
- **F10e. "Interest map" prompt block. ✅ SHIPPED.** A terse **T1
  (semi-stable)** inner-life line listing Aiko's top few topic clusters by
  size — "Topics you and {user} keep coming back to: …" — so she carries a
  sense of "the things we keep coming back to" without any per-turn LLM
  cost. Built by a new cheap
  [`TopicGraph.interest_map`](../../../app/core/conversation/topic_graph.py)
  that reads **only** the live cluster map (label + member count, no join
  back to the memory mirror), so it's safe on the hot path unlike
  `topic_clusters()`. Each topic renders its F10a clean label once the
  [`ClusterLabelWorker`](../../../app/core/conversation/topic_label_worker.py)
  has named it, falling back to the heuristic representative summary the
  batch rebuild stamps on every cluster — and since the label worker names
  the densest clusters first and the interest map *shows* the densest
  clusters, the line converges on clean F10a labels within a couple of
  worker ticks. Rendered by `_render_interest_map_block`
  ([`inner_life_part1.py`](../../../app/core/session/inner_life_part1.py)),
  registered as the `interest_map` provider, and appended in T1 right
  after `goals_block` (the "things Aiko is carrying" cluster: agenda →
  goals → recurring interests). Owned by the assembler's `_StaticSlices`
  cache (paid once per listening window), dropped under `aggressive`
  alongside agenda/goals, no-op in the non-persistent topic-graph mode.
  Settings: `agent.interest_map_{enabled,max_clusters,min_size}`. Tests:
  `InterestMapTests` in
  [`tests/test_topic_graph_persistent.py`](../../../tests/test_topic_graph_persistent.py)
  + `InterestMapProviderTests` in
  [`tests/test_prompt_assembler.py`](../../../tests/test_prompt_assembler.py).
- **F10f. Knowledge-gap targeting — the self-aware beat. ✅ SHIPPED
  (notice half).** The original F10f had three sub-parts; their status:
  (1) **F9 research targeting — already shipped with F9.** The
  [`IdleKnowledgeWorker`](../../../app/core/proactive/idle_knowledge_worker.py)
  picker (`_score_candidates`) already weights *knowledge headroom*
  (0.45) + size (0.35) + freshness, so dense, low-`knowledge`-coverage
  clusters are exactly where F9 digs. No change needed. (2) **The "I
  realised I don't actually know much about X" proactive beat — built
  here.** A new cue producer
  [`KnowledgeGapNoticeWorker`](../../../app/core/proactive/knowledge_gap_notice_worker.py)
  (`name="knowledge_gap_notice"`, no LLM — a cheap kv pass) reads a new
  topic-graph reader
  [`TopicGraph.knowledge_gap_clusters`](../../../app/core/conversation/topic_graph.py)
  (dense clusters whose `kind="knowledge"` fraction is at/below
  `memory.knowledge_gap_notice_max_knowledge_fraction`, ranked by a gap
  score `size·(1−frac)`), and drafts `{at, topic, cluster_key, size,
  knowledge_count}` into the `aiko.knowledge_gap_notices` kv ring with a
  per-topic cooldown (stable label hash, survives cluster renumbering).
  The consumer
  [`_render_knowledge_gap_notice_block`](../../../app/core/session/inner_life_part2.py)
  is a **T6, `user_text`-gated** provider (mirrors the F2 `knowledge_gaps`
  block): it surfaces a drafted notice **only when the live turn is
  lexically on that topic** (so the beat lands in context, not as a
  non-sequitur), once-per-topic via a `knowledge_gap_notice.surfaced_keys`
  set. The cue is a private prompt hint — Aiko phrases the admission
  herself (persona "Topics you keep circling but never dug into" block);
  it is **never** a verbatim nudge. Gated by
  `agent.knowledge_gap_notice_enabled`. F9 quietly *fills* the same gap
  while F10f lets Aiko *own* it out loud — symmetric halves of one
  signal. MCP: `get_knowledge_gap_notice_state` /
  `force_knowledge_gap_notice` (draft, bypass cooldown) /
  `force_knowledge_gap_notice_surface` (bypass relevance + surfaced gates).
  Logs: `knowledge-gap-notice drafted:` (worker) / `knowledge-gap-notice
  fire:` (provider). Tests: `KnowledgeGapClustersTests` in
  [`tests/test_topic_graph_persistent.py`](../../../tests/test_topic_graph_persistent.py),
  [`tests/test_knowledge_gap_notice.py`](../../../tests/test_knowledge_gap_notice.py)
  (worker + helpers + provider), `test_knowledge_gap_notice_settings_round_trip`
  in [`tests/test_settings.py`](../../../tests/test_settings.py). (3) **K35
  consolidation targeting → tracked as F10j** (cluster-scoped memory
  hygiene): F9's research-targeting already covered the "point F9 at gaps"
  intent, so the consolidation re-scoping is the genuinely-separate
  remaining work and lives under F10j below.

**New sub-ideas (added after the F10a-e ship — pick independently).**
The shipped foundation gives every consumer below a cheap, warm set of
primitives on [`topic_graph.py`](../../../app/core/conversation/topic_graph.py):
cluster `centroid`s, `cluster_id_for` (O(1) memory→cluster), `cluster_member_ids`,
`best_clusters_for` (coarse query→cluster), `interest_map` (top-N by size),
and per-cluster `label`s. The ideas below are all just *new readers* of
those primitives — none needs a schema change beyond a `kv_meta` row.

- **F10g. Per-cluster rolling digest memory.** **SHIPPED.** The true
  realisation of the original "cluster-*summary*" idea (F10d shipped as
  on-demand member enumeration, not a stored summary). A
  [`TopicDigestWorker`](../../../app/core/conversation/topic_digest_worker.py)
  idle worker (beside the F10a label worker, same cache-by-representative
  trick) writes one high-salience `kind="topic_digest"` memory per dense
  cluster — a worker-LLM one-paragraph "what I know about X" compression
  of its members — refreshed only on material size drift, updated **in
  place** so the memory id (and the Memory-tab row) is stable. **Open Q
  resolved: the digest lives in the normal pool** (decays, pinnable,
  shows in the Memory tab), but is **excluded from topic-graph
  clustering** (`topic_graph._NON_CLUSTERING_KINDS`, filtered at all three
  mirror chokepoints — `_snapshot_mirror` / `_ensure_cached` /
  `on_memory_added`) so a digest never feeds back into the cluster it
  summarises (no self-summarisation loop, no representative hijack). It's
  also naturally outside the F5/K35 hygiene allow-lists. **Surfacing:**
  the digest shows up through ordinary cosine RAG (it's a high-salience
  embedded memory), and the F10c expansion path *prefers* it — when an
  anchor cluster has a digest, the retriever surfaces the digest as the
  coarse line (its own "What you know about this topic so far:" section,
  longer 600-char truncation) and caps raw sibling enumeration to
  `rag_digest_sibling_cap` (default 1), so a 40-member cluster contributes
  a gist + a specific instead of N lines. The worker rebuilds a
  `{cluster_id: memory_id}` map each tick (persisted to `kv_meta`,
  warm-loaded at construction) that the retriever reads via an injected
  `topic_digest_provider`; stale entries degrade gracefully (the retriever
  verifies the row is still a `topic_digest`). Entirely off the chat path.
  Settings: `agent.topic_digest_enabled` /
  `topic_digest_interval_seconds` (1 h, 60 s floor) /
  `topic_digest_max_per_run` (3) / `topic_digest_max_tokens` (256) /
  `topic_digest_min_cluster_size` (6) / `topic_digest_surface_in_rag` +
  `agent.rag_digest_sibling_cap` (1). MCP: `get_topic_digest_state`
  (switches + the live cluster→digest map with label + content preview).
  Logs `topic_digest run done:`. Tests:
  [`tests/test_topic_digest_worker.py`](../../../tests/test_topic_digest_worker.py),
  a `DigestSurfacingTests` block in
  [`tests/test_rag_retriever_topic_expansion.py`](../../../tests/test_rag_retriever_topic_expansion.py),
  + a settings round-trip in `test_settings.py`.
- **F10h. Topic temperature / per-cluster affect.** **SHIPPED.** A cluster
  isn't just a bag of facts — it has a *vibe*. When the live turn maps (via
  `best_clusters_for`) to a *charged* cluster, Aiko gets a one-line tonal
  Heads-up so she meets a **warm** topic (good moments live there) with a
  little fondness and a **tender** one (vulnerable / patched-up ground)
  gently instead of flat — a topic-scoped sibling of the relationship-axes
  block. **Signal (v1): shared-moment vibes only.** They're the one affect
  source cleanly cluster-attributable — each `shared_moment` is a real
  memory id, so `cluster_member_ids` maps it straight to its cluster and
  its `metadata["vibe"]` is a closed vocabulary
  ([`shared_moment_extractor.VIBE_VOCABULARY`](../../../app/core/relationship/shared_moment_extractor.py)).
  K57 emotion episodes are deferred (global, user-directed, no topic link)
  and K32 reactions deferred (need fragile message→cluster linkage). The
  vibe taxonomy splits into two poles: warm (`warm`/`playful`/`silly`/
  `proud`/`milestone`/`gift`/`victory`/`creative`) lifts `warmth`, tender
  (`tender`/`vulnerable`/`comfort`/`repair`) lifts `tenderness`; both
  saturate so a couple of strong beats is enough and one warm moment in a
  40-member cluster doesn't read as "all warm". **Computed live in the
  provider — no worker, no kv, no schema:** shared moments are few, so the
  per-turn cost is one embed (usually a cache hit — novelty / knowledge-
  grounding embed the same `user_text`) + a few centroid dots + a member
  walk over the *one* matched cluster. Paced by a global turn cooldown.
  Pure scoring in
  [`topic_temperature.py`](../../../app/core/conversation/topic_temperature.py)
  (`score_cluster` / `render_block` / `ClusterTemperature`); provider
  `_render_topic_temperature_block(user_text)` in
  [`inner_life_part2.py`](../../../app/core/session/inner_life_part2.py),
  registered in the **T6** tier right after the F10f gap-notice block (all
  topic-graph-derived cues clustered), dropped under `aggressive=True`.
  Persona: the "Topics that carry weight" block in
  [`aiko_companion.txt`](../../../data/persona/aiko_companion.txt) teaches the
  warm-vs-tender register (it's a tone shift, never a line said out loud).
  Settings: `agent.topic_temperature_enabled` + `memory.topic_temperature_*`
  (`min_sim` 0.45, `threshold` 0.5, `cooldown_turns` 6). MCP:
  `get_topic_temperature_state` (dry-run scan of every charged cluster) +
  `force_topic_temperature_surface` (drops cooldown + thresholds on the
  next turn). Logs `topic-temperature fire:` per surfacing. Tests:
  [`tests/test_topic_temperature.py`](../../../tests/test_topic_temperature.py)
  (pure module + provider) + a settings round-trip in `test_settings.py`.
  Pairs with K8 rupture-repair (don't barrel into a tender cluster).
- **F10i. Per-topic confidence self-model (metacognition).** **SHIPPED.**
  Distinct from F10f, which *researches* gaps — this lets Aiko *express*
  how much she actually knows about a topic. When the live turn maps (via
  `best_clusters_for`) to a cluster, she reads its confidence from
  `(size, learned_count)` — size = conversational familiarity,
  learned_count = `kind in {knowledge, curiosity_finding}` rows = studied
  facts — blended (0.6·size + 0.4·learned, both saturating) into a `[0, 1]`
  score and banded: **thin** (hedge / ask rather than bluff), **familiar**
  (stop over-hedging on what she clearly knows), or silent (the common
  middle). A topic-scoped extension of K20 metacognitive calibration.
  **Separation:** F10f owns *dense-but-unresearched* clusters (high size,
  ~0 knowledge) — those score mid/high here, so they never read as thin;
  the familiar band is an anti-over-hedge *register* cue only, NOT K61's
  "name these specific facts" content push. **Resolved open Q:** kept as
  its own **T6** block, NOT folded into the F10e interest map — the
  interest map is turn-independent (T1) while this is query-aware (depends
  on the live turn's cluster). Cheap reader `cluster_knowledge_stats`
  ([`topic_graph.py`](../../../app/core/conversation/topic_graph.py),
  `O(members)` mirror join, no warm-start); pure scoring in
  [`topic_confidence.py`](../../../app/core/conversation/topic_confidence.py);
  provider `_render_topic_confidence_block(user_text)` in
  [`inner_life_part2.py`](../../../app/core/session/inner_life_part2.py),
  registered in T6 right after the F10h temperature block (all
  topic-graph cues clustered), dropped under `aggressive=True`, paced by a
  global turn cooldown. Persona: the "How much you actually know" block in
  [`aiko_companion.txt`](../../../data/persona/aiko_companion.txt). Settings:
  `agent.topic_confidence_enabled` + `memory.topic_confidence_*`
  (`min_sim` 0.45, `thin_threshold` 0.25, `familiar_threshold` 0.7,
  `cooldown_turns` 6). MCP: `get_topic_confidence_state` (dry-run scan of
  every banded cluster) + `force_topic_confidence_surface` (drops cooldown
  + min_sim, splits bands at 0.5). Logs `topic-confidence fire:`. Tests:
  [`tests/test_topic_confidence.py`](../../../tests/test_topic_confidence.py),
  a `ClusterKnowledgeStatsTests` block in
  [`tests/test_topic_graph_persistent.py`](../../../tests/test_topic_graph_persistent.py),
  + a settings round-trip in `test_settings.py`.
- **F10j. Cluster-scoped memory hygiene.** **SHIPPED.** Both the F5
  conflict detector and the K35 consolidation worker now partition their
  candidate snapshot by topic cluster and scan *within* a cluster instead
  of all-pairs across the whole mirror. Two wins, as designed: the O(n²)
  pairwise cosine drops to `sum(O(k_c²))` over the (much smaller)
  per-cluster sizes (directly unblocks P30's mirror-sweep concern), and
  the surviving pairs are *topically adjacent* — exactly where
  contradictions / near-dupes live, so the rate-limited LLM
  verifier/merger stops burning its budget on cross-topic noise.
  Implementation: one shared helper
  [`partition_by_cluster`](../../../app/core/memory/cluster_scope.py) groups
  candidates by `TopicGraph.cluster_id_for` (O(1) per row), drops
  singleton groups, orders groups newest-first (preserving each worker's
  freshness priority under its shared per-run cap), and buckets
  unclustered rows together. Both workers take a late-bound
  `topic_graph_provider` and a single master switch
  `agent.cluster_scoped_memory_hygiene_enabled` (default on). The conflict
  worker's pairwise loop was extracted into `_scan_group` driven by a
  shared `_ScanState` so the `max_pairs` budget still bounds the whole
  tick; the consolidation worker calls `_build_clusters` per group under a
  shared `max_clusters` budget. **Graceful degradation:** switch off / no
  graph / non-persistent / unwarmed graph → a single group == the full
  candidate list == exact pre-F10j behaviour (the legacy worker tests pass
  untouched because they pass no provider). **Tradeoff** (documented in
  the module + config): a pair split across two clusters is no longer
  compared, but the clustering floor (0.55) is far looser than the
  conflict band (`[0.80, 0.92)`) / dedupe threshold (~0.90), so close
  pairs almost always co-cluster, and it's eventually-consistent across
  re-clusters. The `groups` + `cluster_scoped` fields on each worker's
  result dict (and the per-run INFO line) show whether scoping was active.
  Tests: [`tests/test_cluster_scope.py`](../../../tests/test_cluster_scope.py),
  a `ClusterScopingTests` block in
  [`tests/test_memory_conflict_worker.py`](../../../tests/test_memory_conflict_worker.py),
  + a settings round-trip in `test_settings.py`.
- **F10k. Semantic topic tracking for K6 / K18.** **SHIPPED (additive).**
  The novelty detector (K6) now maps each *measured* turn to its best
  topic-graph cluster via `TopicGraph.best_clusters_for(vec, top_n=1,
  min_sim=topic_tracking_min_sim)` — reusing the vector it already embeds,
  so the only added cost is a handful of centroid dot-products. It keeps
  rolling `_prev_cluster_id` / `_prev_cluster_label` / `_visited_clusters`
  state and exposes per-turn signals (`last_cluster_id` / `_label` /
  `_changed` / `_returning` / `last_prev_cluster_label`, all reset at the
  top of `detect()` like `last_distance`). The **centroid math is
  untouched** — cluster identity is layered *on top* of the existing band
  classification (the "start additive and measure" call), so K6/K18 still
  fire on exactly the same turns; the clusters only enrich the rendered
  cue. K6's `render_inner_life_block` gained a private, don't-quote context
  clause: a *return* to a previously-visited cluster reads "circles back to
  the X thread -- pick it up, not brand-new", a fresh move reads "shift
  from X to Y". K18's render names the looped-on cluster ("(the X thread)")
  by reading K6's just-computed `last_cluster_label` (K18 runs right after
  K6 in the provider order, no re-embed). **Robustness:** a turn below
  `min_sim` is a non-match that *keeps* the prior cluster (a transient miss
  never reads as a topic change); labels are spliced only when clean
  (non-empty, single-line, ≤48 chars) so a heuristic representative
  sentence falls back to label-less copy rather than dumping into the
  prompt; the cue is internal (persona "Surprise and novelty" / "Same topic
  for a while" blocks tell Aiko never to read the topic name aloud). Gated
  by `agent.topic_tracking_enabled` (default on; bound at detector
  construction → restart to toggle); off → the provider is `None` and the
  detectors run byte-identically to pre-F10k. MCP: `get_topic_tracking_state`
  dumps the switch, `min_sim`, the last-turn signals, and the rolling
  prev/visited state. Tests: a `TopicTrackingTests` + `TopicContextRenderTests`
  block in
  [`tests/test_novelty_detector.py`](../../../tests/test_novelty_detector.py),
  topic-label render tests in
  [`tests/test_topic_stagnation.py`](../../../tests/test_topic_stagnation.py),
  + a settings round-trip in `test_settings.py`.
- **F10l. Cluster management UX (user agency over her mental map). SHIPPED.**
  The read-only `TopicGraphPanel` in the Memory tab grew three per-cluster
  actions (persistent-mode only — the panel shows "read-only" otherwise):
  **rename** (overrides the F10a label), **pin all / unpin all** (bulk
  pins/unpins every member), and **forget** (a two-click confirm that
  bulk-archives every *non-pinned* member to `tier=archive`; pinned rows
  are spared — a pin outranks a forget). Wiring:
  [`MemoryFacadeMixin`](../../../app/core/session/memory_facade_mixin.py) gained
  `rename_topic_cluster` / `set_topic_cluster_pinned` / `forget_topic_cluster`
  (each resolves the live cluster via `_resolve_cluster`, then reuses the
  existing per-memory `set_memory_pinned` / `update_memory` so each member
  change still broadcasts `memory_updated` and the Memory list stays live);
  REST `PATCH /api/topic-graph/clusters/{id}` + `POST .../pin` + `POST
  .../forget` in [`memory_world_routes.py`](../../../app/web/rest/memory_world_routes.py);
  `api.renameTopicCluster` / `pinTopicCluster` / `forgetTopicCluster` in
  [`web/src/api.ts`](../../../web/src/api.ts). **Rename durability (the Open Q):**
  cluster ids are reassigned on a full refit, so a rename keyed to the
  cluster id alone would be lost. Instead `rename_topic_cluster` writes the
  label into the **F10a label cache keyed by the cluster's representative
  id** with `user_pinned=true`; the `ClusterLabelWorker` now always
  re-applies a `user_pinned` cache entry and **never regenerates over it**
  (even on size drift), so a user rename survives a refit and is sticky
  until the user renames again. (The one residual limitation: if a refit
  promotes a *new* representative, the rep-keyed cache is orphaned and the
  worker LLM-labels the new rep fresh — reps are the highest-salience
  member so this is rare.) **Merge / split deliberately not built** — they
  fight the auto-clustering (the next refit would undo them) and need real
  persistence design; the high-value, durable verbs (rename / pin / forget)
  are the slice. MCP: `rename_topic_cluster` / `pin_topic_cluster` /
  `forget_topic_cluster` mirror the REST. Tests:
  [`tests/test_topic_cluster_management.py`](../../../tests/test_topic_cluster_management.py)
  (facade rename/pin/forget against a real store+graph) + a
  `user_pinned`-stickiness test in
  [`tests/test_topic_label_worker.py`](../../../tests/test_topic_label_worker.py)
  + F10l wiring assertions in `TopicGraphPanel.test.tsx`.

**Effort.** F10a/F10b/F10e small-medium each; F10c/F10d medium. **The
entire F10 line (F10a–F10l) is shipped** (F10f = the self-aware
knowledge-gap notice; F9 already covered research-targeting; F10h =
per-cluster topic temperature from shared-moment vibes; F10i = per-topic
confidence self-model from size + learned-fact coverage — both
provider-only; F10j = cluster-scoped memory hygiene, which also delivered
F10f's K35 consolidation re-scope alongside the F5 conflict re-scope;
F10k = additive semantic topic tracking layered onto K6/K18 — names the
topic transition and tells a return apart from a brand-new pivot; F10g =
per-cluster rolling digest memory — a `topic_digest` pool memory per dense
cluster, excluded from clustering, surfaced as the coarse RAG line that
caps sibling expansion; F10l = cluster management UX — rename / pin / forget
per cluster in the Memory tab, with renames pinned to the representative so
they survive a refit).
Remaining: **none.** Several follow-on ideas overlap the K64 mind-wandering
family in [`patterns.md`](../patterns.md) (esp. K64b interest-drift) —
cross-check before picking one up so two passes don't build the same
per-cluster aggregator twice. **Provider-walk note:** F10h/F10i both
compute their per-cluster signal live in the provider (member walk over
the *one* matched cluster — cheap), so any future per-cluster aggregator
(K64b drift, F10g digest input) can share `cluster_member_ids` /
`cluster_knowledge_stats` rather than re-deriving it.

**Builds up into concepts (L-series).** The cross-cluster **concept** layer in
[`concepts.md`](../concepts.md) sits directly on top of this topic graph: it links
semantically-distant clusters into higher-order abstractions ("Maker Mode", "he
enjoys understanding systems") that proximity clustering can't produce. The
concept synthesis worker consumes F10's `interest_map` + `topic_digest` +
cluster representatives, and L4 adds the missing per-session **cluster
co-activation** signal alongside `cluster_activity`.

---

## K64a. Associative wandering ("funny, this reminds me of ...")

**Shipped.** First member of the K64 *freedom of thought* family — the
genuinely drifting part of Aiko's interior life, as opposed to the reactive
extract / fact-check / consolidate workers. The
[`AssociativeWanderWorker`](../../../app/core/proactive/associative_wander_worker.py)
is an `IdleWorker` (cue producer, not a verbatim nudge) that, during a quiet
window: reads the K9 topic graph's labelled clusters, forms candidate pairs
from the **most distant `memory.associative_wander_pair_quantile`** (default
`0.10`) of everything the corpus contains, capped by
`memory.associative_wander_max_pair_cosine` (default `0.60`, a *ceiling* so a
single-topic corpus yields nothing) via the pure
`find_distant_pairs`, skips any pair on its per-pair cooldown, pulls a few
member snippets from each cluster as substance, and asks the **worker LLM**
for ONE honest connection (`{"connects": bool, "connection": "..."}` — it
may decline, in which case the pair is still stamped on cooldown so an
unconnectable pair isn't retried every tick). Drafted connections append to
the `aiko.associative_wanders` kv ring as `{at, topic_a, topic_b, pair_key,
connection}`. The consumer
[`InnerLifePart2Mixin._render_associative_wander_block`](../../../app/core/session/inner_life_part2.py)
surfaces one **only when the live turn is lexically on one of the two
topics** (`wander_relevant`, reusing F10f's `topic_relevant`), one-shot per
`pair_key` (recorded in `associative_wander.surfaced_keys`), as a private
T6 hint clustered with the other topic-graph-derived surfaces (after
`topic_confidence_block`; dropped under `aggressive`). The chat model phrases
it in her own words; the connection is **never spoken verbatim**. **Rarity is
the feature**: paced by a long draft interval (`5400s`), a small daily cap
(`2`), a global cooldown (`7200s`), and a **week-long per-pair cooldown**
(`168h`, keyed on a stable hash of the unordered label pair so it survives
cluster renumbering). Persona copy lives in the "When your mind wanders and
connects two things" block of [`aiko_companion.txt`](../../../data/persona/aiko_companion.txt)
(teaches the register — one light real aside, never narrate the mechanism,
drop it silently if it doesn't fit). **MCP-debuggable**:
`get_associative_wander_state` (switch / ring / per-pair cooldowns /
surfaced keys / dry-run of the distant-pair picker), `force_associative_wander`
(run once bypassing all cooldowns — picks the single most-distant pair),
`force_associative_wander_surface` (arm the provider one-shot). Grep
`tail_logs(module_contains="associative_wander")` for `associative-wander
drafted:` / `no-connection:` / `fire:`. **Why the pair selection is a quantile
and not a threshold:** it was `cos <= 0.25`, and the minimum cosine across all
561 eligible pairs in the live graph was `0.2648` — the bar sat below the floor
of the distribution and the worker returned `no_pair` on 107 consecutive runs.
Sentence encoders put unrelated text at 0.3–0.5, so any fixed cosine here is a
guess about the embedding model that breaks on a model swap; a rank cannot be
placed out of reach. See health.md H23. Settings:
`agent.associative_wander_enabled` + the nine `memory.associative_wander_*`
knobs. Tests:
[`tests/test_associative_wander.py`](../../../tests/test_associative_wander.py)
(pure helpers + worker gates + provider plumbing). **Remaining K64 family:**
K64c curiosity gradient, K64d knowledge-map self-reflection (open in
[`patterns.md`](../patterns.md)).

---

## K64b. Interest drift ("I've been weirdly into X lately")

**Shipped.** Second member of the K64 *freedom of thought* family — the slow
under-current sibling of K27 day-colour. Where K64a connects two *distant*
topics, K64b notices Aiko's own attention **shifting over time**. The
[`InterestDriftWorker`](../../../app/core/proactive/interest_drift_worker.py)
is an `IdleWorker` (cue producer, **no LLM** — pure size-delta math) that, on
each tick: reads every labelled cluster's current mass via the cheap
[`TopicGraph.interest_map`](../../../app/core/conversation/topic_graph.py)
(`(label, size)` rows, no member join), appends `(now, size)` to a per-topic
mass time-series in `kv_meta` (`aiko.interest_mass`, keyed by a stable label
hash so it survives cluster renumbering, capped to
`memory.interest_drift_window_samples`=8), and once a topic has
`interest_drift_min_samples`=3 snapshots classifies its drift via the pure
`classify_drift`: fast recent growth (≥ `_RISE_MIN_DELTA`=3 members **and** ≥
`interest_drift_rise_ratio`=0.5 of the starting mass) → `rising`; a sizable
cluster whose window growth is ≤ `interest_drift_fade_max_growth_ratio`=0.05
→ `fading` (attention cooled). The strongest off-cooldown candidate drafts to
the `aiko.interest_drifts` ring as `{at, topic, topic_key, direction,
from_size, to_size, belief}`. The consumer
[`InnerLifePart2Mixin._render_interest_drift_block`](../../../app/core/session/inner_life_part2.py)
surfaces one **only when the live turn is on that topic** (`drift_relevant`,
reusing F10f's `topic_relevant`), one-shot per `topic_key`
(`interest_drift.surfaced_keys`), with distinct rising / fading copy, as a
private **T6** hint after `associative_wander_block` (dropped under
`aggressive`). Phrased by the chat model as a **register shift, never a
verbatim line**. **Rarity is the point** (interests drift slowly): 6h draft
interval, daily cap 3, 72h per-topic cooldown; stale topics are pruned from
the series once their newest sample ages past the window horizon. Persona
copy lives in the "When your interests shift over time" block of
[`aiko_companion.txt`](../../../data/persona/aiko_companion.txt). **MCP-debuggable**:
`get_interest_drift_state` (switch / ring / mass series / cooldowns /
surfaced keys), `force_interest_drift` (run once bypassing caps),
`force_interest_drift_surface` (arm the provider one-shot). Grep
`tail_logs(module_contains="interest_drift")` for `interest-drift drafted:` /
`fire:`. Settings: `agent.interest_drift_enabled` + the ten
`memory.interest_drift_*` knobs. Tests:
[`tests/test_interest_drift.py`](../../../tests/test_interest_drift.py)
(pure classifier + worker warmup/cooldown gates + provider plumbing).
**Remaining K64 family:** K64c curiosity gradient, K64d knowledge-map
self-reflection (open in [`patterns.md`](../patterns.md)).

**L28 follow-up.** A drafted drift now carries `belief` — the most-confident
concept spanning that cluster, read through `ConceptView.for_cluster(rep_id)`
off the `representative_id` `cluster_activity` reports — and the cue appends
"What you hold about it: …", so the beat lands as *why she cares* rather than
a bare direction. The rep-id lookup joins back to the memory mirror, so it is
paid **only for the topic actually being drafted**; the per-tick `interest_map`
sampling pass is untouched. No concept layer, no rep id, or no concept edges
on the cluster all leave the entry and the cue exactly as before. See
[`docs/concept-integration.md`](../../concept-integration.md).

---

## K64c. Curiosity gradient ("I keep brushing past X, I'm curious")

**Shipped.** Third member of the K64 *freedom of thought* family. Where K64a
connects two distant topics and K64b tracks a topic's mass drifting, K64c
notices the **boundary** of what Aiko knows: a *thin* topic cluster sitting
right next to a *dense* one — the under-explored edge of familiar territory,
exactly where genuine curiosity lives. The
[`CuriosityGradientWorker`](../../../app/core/proactive/curiosity_gradient_worker.py)
is an `IdleWorker` (cue producer, **no LLM** — pure cluster geometry) that,
on each tick, reads the labelled clusters and, for each *thin* cluster
(members in `[curiosity_gradient_thin_min_size=2,
curiosity_gradient_thin_max_size=4]`), finds its nearest *dense* cluster
(size ≥ `curiosity_gradient_dense_min_size`=8) by centroid cosine via the
pure `find_gradient_edges`. The pair qualifies as a curiosity edge when that
cosine lands in `[curiosity_gradient_adjacency_min_cosine=0.40,
curiosity_gradient_adjacency_max_cosine=0.90]` (genuinely on the rim — close
enough to be the edge of the familiar topic, not a near-duplicate of it).
The strongest off-cooldown edge drafts to the `aiko.curiosity_gradients` ring
as `{at, dense_topic, thin_topic, edge_key, cosine}`. The consumer
[`InnerLifePart2Mixin._render_curiosity_gradient_block`](../../../app/core/session/inner_life_part2.py)
surfaces one **only when the live turn is on either topic**
(`gradient_relevant`, reusing F10f's `topic_relevant`), one-shot per
`edge_key` (`curiosity_gradient.surfaced_keys`), as a private **T6** hint
after `interest_drift_block` (dropped under `aggressive`). The cue steers the
chat model toward **ONE genuine, specific question** — never spoken verbatim,
never a survey or interrogation. Paced by a 90-min interval, daily cap 3, and
a 96h per-edge cooldown (keyed on a stable hash of the unordered label pair).
Persona copy lives in the "When you're curious about the edge of something
familiar" block of [`aiko_companion.txt`](../../../data/persona/aiko_companion.txt).
**MCP-debuggable**: `get_curiosity_gradient_state` (switch / ring / cooldowns
/ surfaced keys / dry-run of the edge picker), `force_curiosity_gradient`
(run once bypassing caps), `force_curiosity_gradient_surface` (arm the
provider one-shot). Grep `tail_logs(module_contains="curiosity_gradient")`
for `curiosity-gradient drafted:` / `fire:`. Settings:
`agent.curiosity_gradient_enabled` + the nine `memory.curiosity_gradient_*`
knobs. Tests:
[`tests/test_curiosity_gradient.py`](../../../tests/test_curiosity_gradient.py)
(pure edge finder + worker cooldown/cap gates + provider plumbing).

---

## K64d. Knowledge-map self-reflection ("the shape of what I know")

**Shipped.** The introspective capstone of the K64 *freedom of thought*
family. Where K64a/b/c each notice something *local* about the topic graph (a
connection, a drifting topic, an under-explored edge), K64d steps back and
looks at the **whole shape**: which territories of Aiko's mind are rich and
well-trodden, which are thin or blank — a rare "huh, most of what I'm carrying
lately circles X, and I realise I've got almost nothing on Y" meta-thought.
Unlike a/b/c (cue producers surfaced one-shot through a dedicated inner-life
block), K64d is a *reflection* and **reuses the existing DreamWorker /
ReflectionWorker machinery seeded by the graph** instead of raw recent
memories — so there is **no new provider, no prompt-assembler wiring**.

The [`KnowledgeMapReflectionWorker`](../../../app/core/proactive/knowledge_map_reflection_worker.py)
is an `IdleWorker` that, on a ~daily interval during a quiet window, reads the
graph *shape* via `interest_map` (richest = well-trodden territory, top
`knowledge_map_reflection_rich_top_n`=5) + `knowledge_gap_clusters`
(dense-but-unlearned = "blank in the learned sense", top
`knowledge_map_reflection_gap_top_n`=3), runs **one worker-LLM** meta-thought
pass (`_maintenance_client` / worker model — never the chat model, so no chat
quota / no prompt-cache invalidation), and writes ONE `kind="reflection"`
memory prefixed `[mindmap] ` (mirroring DreamWorker's `[dream] `
discriminator) at scratchpad tier with `metadata.source="knowledge_map"`. That
memory then flows through the same paths every reflection does — the RAG
retriever, the **K28** `turning_over` between-session surfacing, the
NarrativeWeaver — so the meta-thought surfaces naturally in Aiko's own words
when relevant. The `turning_over` render strips the `[mindmap] ` prefix and
keeps the waking "thinking about this" framing (the persona's "What I've been
turning over" block gained a bullet teaching her to own it as a self-aware
noticing of her own lopsided attention — *not* "I've been analysing my
memory"). Skips when fewer than `knowledge_map_reflection_min_clusters`=4
labelled clusters exist (`no_context`), or with no graph / LLM / embedder.
Paced hard: a daily interval **plus** a `knowledge_map_reflection_cooldown_hours`=20
wall-clock cooldown (stamped on the kv key `knowledge_map_reflection.last_fired_at`
even on a dedupe so a near-identical reflection isn't re-attempted every tick;
a force-run bypasses it). Every failure path is swallowed and logged at debug.

**L28 follow-up.** Each rich territory now also carries what Aiko *believes*
about it, read through `ConceptView.for_cluster(rep_id)` off the
`representative_id` `cluster_activity` reports — "cooking (18 memories, hot
this week) — you believe: he cooks to wind down, not to eat". Most-confident
first, capped at `knowledge_map_reflection_concepts_per_cluster`=2; 0, a cold
concept layer, or a cluster with no concept edges all restore the exact
pre-L28 size/recency-only payload. See
[`docs/concept-integration.md`](../../concept-integration.md).

**MCP-debuggable**: `get_knowledge_map_reflection_state` (switch / interval /
cooldown stamp / dry-run of the rich + under-explored shape the worker would
reflect on), `force_knowledge_map_reflection` (run once bypassing the
cooldown, returns `wrote` / `memory_id` / `reflection` or a skip reason — the
written row then surfaces on a later turn via RAG / K28, confirm it in the
Memory tab). Grep `tail_logs(module_contains="knowledge_map_reflection")` for
`knowledge-map-reflection wrote memory`. Settings:
`agent.knowledge_map_reflection_enabled` + the eight
`memory.knowledge_map_reflection_*` knobs (interval / cooldown / min_clusters
/ rich_top_n / gap_top_n / concepts_per_cluster / max_tokens / salience). Tests:
[`tests/test_knowledge_map_reflection.py`](../../../tests/test_knowledge_map_reflection.py)
(shape read + LLM pass + `[mindmap]` write + dedupe + cooldown + `force_next`
+ `clean_reflection_output`) plus the `[mindmap]`-prefix-strip case in
[`tests/test_turning_over_picker.py`](../../../tests/test_turning_over_picker.py).
**The K64 freedom-of-thought family is now complete (a + b + c + d).**

---

# Temporal awareness (K-time family)

Continues the **K-time1** lineage (wall-clock prefixes on chat history —
shipped, see [`shipped.md`](../shipped.md)). Relative time is one of the
hardest things for an LLM companion: even with a "now" anchor in the
prompt, the model does date *arithmetic* by reasoning, which it gets
wrong ("yesterday" / "in 3 days" / "last Tuesday" drift constantly).

**What's already solid** (don't rebuild): the chat prompt carries a
date+time anchor (`_ambient_block` → "Right now it's Friday, June 26,
afternoon (1:33 PM)" + the circadian weekday/period line); chat history
is pre-tagged (`[2 min ago]` / `[yesterday 18:45]`, K-time1); retrieved
memories are pre-tagged via `rag_retriever._humanize_past/_future`
("(yesterday)", "(planned for tonight 20:00)", "(ongoing)"); the
`MemoryExtractor` resolves the user's relative phrases to absolute
`event_time` at **write** time (schema v10); and K25 hedges stale
high-confidence rows as "(distant)". The items below fill the gaps those
leave.

---

## K-time2. Date-anchored retrieval for relative-time queries — SHIPPED

Resolves relative-time phrases at **query** time (the extractor already
did it at write time). New [`app/core/infra/time_expr.py`](../../../app/core/infra/time_expr.py)
`parse_time_window(text, now)` turns `yesterday` / `last night` / `this
morning` / `last week` / `this week` / `last month` / `N days|weeks|months
ago` / `last N days` / `on Monday` / `back in March` / `tomorrow` / `next
week` into a concrete `[start, end]` `TimeWindow` against the
`timephrase` now-anchor (so it's DT1-virtual-clock-ready and
deterministic in tests). Past windows carry a `guardable` flag (the
clearly-retrospective ones) so chit-chat like "how are you today" never
arms the guard. [`rag_retriever.py`](../../../app/core/rag/rag_retriever.py)
parses the **raw** query text (not the recent-turns-expanded query) and
adds `_RAG_TIME_WINDOW_BONUS=0.08` to any memory/message hit whose
`created_at` *or* `event_time` falls inside the window — a soft boost,
not a hard filter, so a timezone skew on a day boundary only shifts the
nudge. **Tonal guard:** `block_for` appends an anti-confabulation note
(`time_window_guard_note()`) when a guardable query surfaced zero
in-window hits, phrased as private guidance ("RAG only sees the semantic
top-N, so 'nothing surfaced' != 'nothing exists'") rather than a hard
claim. Tests: `tests/test_time_expr.py` (22),
`tests/test_rag_retriever_time_window.py` (7).

**Follow-up shipped — direct `[start, end]` message recall.** The soft
boost biases the *semantic* top-N but can't surface a line that simply
wasn't in the top-N, so verbatim "what exactly did we say last Tuesday?"
recall was lossy. [`ChatDatabase.messages_in_range(start_iso, end_iso,
*, limit, exclude_session_id)`](../../../app/core/infra/chat_database.py) is
the verbatim fallback: a bounded `created_at` range scan (newest-first,
capped), and [`RagRetriever`](../../../app/core/rag/rag_retriever.py) injects
its rows as synthetic `message` hits — but **only** for *guardable*
(clearly retrospective) windows, so it never fires on chit-chat like "how
are you today". The injected hits score around `_DIRECT_RECALL_BASE=0.55`
(+ the in-window time bonus + per-message recency) so the actual lines
reliably surface for a recall query without overpowering a strong
semantic memory hit; the dedup-by-text pass collapses any overlap with the
semantic message hits, and the SQL bounds are widened ±1 day then
re-filtered through `TimeWindow.contains` so a tz-format difference can't
drop a row. The injected lines also count toward `time_window_hits`, so an
empty *semantic* pass on a day we *do* have messages for no longer trips
the anti-confabulation guard. Gated by `agent.rag_direct_recall_enabled`
(default on) + `agent.rag_direct_recall_max_messages` (default 6, floor 0
= disabled). Tests: `tests/test_rag_retriever_direct_recall.py` (DB method
+ retriever integration), plus a settings round-trip in
`tests/test_settings.py`.

---

## K-time3. Upcoming-horizon block — pre-computed future relative times — SHIPPED

Future date arithmetic is exactly where an LLM companion drifts ("in 3 days"
/ "next Tuesday" computed by reasoning, gotten wrong), and a future plan only
reached Aiko if *semantic* RAG happened to surface it. K-time3 adds the
missing **forward sweep**: the pure
[`app/core/conversation/upcoming_horizon.py`](../../../app/core/conversation/upcoming_horizon.py)
(`select_upcoming` / `build_signature` / `render_block`) filters
`future_plan` memories whose `event_time` lands in `(now, now +
upcoming_horizon_days]` (default 7), sorts soonest-first, caps at
`upcoming_horizon_max_items` (default 3), and renders one terse "Coming up
for {name}: …" cue with the relative phrasing **already resolved** by the
canonical [`timephrase.humanize_future`](../../../app/core/infra/timephrase.py)
("tomorrow morning 09:00", "on Friday 18:00") so the chat model never
recomputes a date. The cue carries an explicit "use these, don't recalculate"
+ "heads-up only, never recite like a calendar" tonal guard.

Consumer is the **live** (no worker / kv)
[`InnerLifePart2Mixin._render_upcoming_horizon_block`](../../../app/core/session/inner_life_part2.py)
— a single mirror scan + a couple of ISO parses — registered as the
`upcoming_horizon` provider and slotted in the **T6** tier right after
`follow_up_block` (both are future-plan / time-anchored surfaces). **Anti-nag
via signature + cooldown:** the cue re-surfaces immediately when the upcoming
set's signature changes (a plan appears or slides out of the window) and
otherwise sits out `upcoming_horizon_cooldown_turns` (default 6) so an
unchanged calendar isn't recited every turn. Gated by
`agent.upcoming_horizon_enabled`. Pairs with the
[`follow_up_worker`](../../../app/core/proactive/follow_up_worker.py) (which
covers the *retrospective* "how did it go?" half once an event passes) and
the `temporal_suffix` RAG tag (which only fires on a semantic hit).
MCP-debuggable: `get_upcoming_horizon_state` (switches + knobs + cooldown +
last signature + a dry-run of the window with resolved phrases) /
`force_upcoming_horizon_surface` (one-shot bypass of the cooldown +
signature gate). Grep `upcoming-horizon fire:`. Tests:
[`tests/test_upcoming_horizon.py`](../../../tests/test_upcoming_horizon.py)
(pure module + provider plumbing), an `affect → upcoming_horizon` slot test
in `tests/test_prompt_assembler.py`, and a settings round-trip in
`tests/test_settings.py`.

---

## K-time4. Session-elapsed & mid-session gap awareness — SHIPPED

There was cross-session gap awareness (J5 reconnection, K14/K28/K36) and
per-message history age (K-time1), but **nothing about the current
conversation's own clock**. K-time4 adds two cheap derived sub-cues off the
recent-message timestamps, folded into one block. The pure
[`app/core/conversation/session_clock.py`](../../../app/core/conversation/session_clock.py)
(`continuous_burst` / `classify` / `render_block`) does the math:

- **elapsed** — `continuous_burst` collapses the newest-first timestamps
  into the duration of the current *uninterrupted sitting* (it walks back
  only while each step's gap stays under `session_clock_break_minutes`, so a
  session that began days ago but has a fresh burst reads as minutes, not
  days), banded `long` (≥ 60 min) / `very_long` (≥ 150 min). Lets Aiko land
  "we've been at this a while" or, paired with the existing circadian block,
  "it's late and we've been talking an hour — get some rest."
- **pause** — a notable *mid-session* pause (delta before the latest
  message) in `[session_clock_gap_min_minutes, session_clock_gap_max_minutes)`
  (default `[10, 30)` min). The upper bound sits **at** the K14
  absence_curiosity floor (30 min) so K-time4 never double-fires with the
  gap-return family that owns everything above it.

Consumer is the **live** (no worker / kv)
[`InnerLifePart4Mixin._render_session_clock_block`](../../../app/core/session/inner_life_part4.py)
— it shares the P22 `_inner_life_recent_messages` read with the other
history-walkers — registered as the `session_clock` provider and slotted in
the **T6** gap cluster right after `reconnection_block` (its within-session
sibling) and before `absence_curiosity_block`. **Anti-nag via two
watermarks:** the elapsed cue fires once **per band per sitting** (a
`(burst_key, fired_band)` pair; a new sitting re-arms it), the pause cue
once per latest-message anchor — an engaged conversation is never reminded
of the clock every turn. Tonal guard lives in the rendered cue: observe,
never police. Gated by `agent.session_clock_enabled`; all five thresholds
are `agent.session_clock_*_minutes` floats. MCP-debuggable:
`get_session_clock_state` (switches + knobs + watermarks + a dry-run measure
of the live signal) / `force_session_clock_surface` (one-shot watermark
bypass). Grep `session-clock fire:`. Tests:
[`tests/test_session_clock.py`](../../../tests/test_session_clock.py) (pure
module + provider plumbing), a `reconnection → session_clock →
absence_curiosity` slot test in `tests/test_prompt_assembler.py`, and a
settings round-trip in `tests/test_settings.py`.

---

## K-time5–9. Temporal toolkit + worker time-awareness — SHIPPED

Shipped together as the [`app/core/infra/timephrase.py`](../../../app/core/infra/timephrase.py)
canonical module plus worker wiring. What landed:

- **K-time5 (now seam + consolidation).** `timephrase.py` holds the single
  injectable "now" (`now()` / `set_now_provider()` — the DT1 virtual-clock
  hook) plus the canonical `humanize_past` / `humanize_future` /
  `temporal_suffix` / `age_prefix`. `rag_retriever.py` and
  `prompt_assembler_helpers_mixin._format_age` now delegate here (re-exported
  as aliases so existing callers/tests stay byte-identical).
- **K-time6 (richer now anchor).** `_ambient_block` appends the year and a
  compact `[YYYY-MM-DD]` ISO stamp to "Right now it's …" so cross-year /
  "how long ago" arithmetic is unambiguous.
- **K-time7 (worker toolkit).** `today_anchor(now)`, `format_memory_line`,
  `format_memory_block(mems, now)`, and `format_transcript(rows, now)`
  exposed for workers, reading the same now seam (so worker tests get
  deterministic time).
- **K-time8 (today anchor in extract workers).** `today_anchor()` prepended
  to the system prompts of `promise_worker` (deadline resolution — the worst
  offender), `belief_worker`, `shared_moment_extractor`, `reflection_worker`,
  and `summary_worker` (plus an explicit "rewrite relative time as a concrete
  date" instruction so stored summaries don't go stale).
- **K-time9 (memory ages to crunchers).** `memory_consolidation_worker`
  renders its merge group via `format_memory_block(group, now)` and is told
  to prefer the fresher note on conflict.

**Evaluated and skipped by design:** `memory_conflict_worker` (its winner
selection already tie-breaks on `created_at` in Python; the LLM only judges
contradiction, so ages add no value there) and `idle_curiosity_worker`
(already picks the oldest `open_question` in Python).

**Follow-up shipped — per-cluster recency in the knowledge-map reflection.**
The `knowledge_map_reflection_worker` feeds cluster *labels + sizes*, not
memory rows, so `format_memory_block` didn't fit — it needed the topic
graph to expose cluster recency. [`TopicGraph.cluster_activity(top_n,
min_size)`](../../../app/core/conversation/topic_graph.py) now does: like
`interest_map` but with one bulk mirror snapshot to find each cluster's
most-recent member touch (`last_used_at` / `created_at`), returning a new
`InterestActivity(label, size, last_active, days_since)`. It's a daily-worker
read, not hot-path. The worker's `_read_shape` prefers `cluster_activity`
(falling back to the recency-free `interest_map` for older / duck-typed
graphs), buckets `days_since` into a short tag via `recency_phrase`
(`hot this week` / `active recently` / `cooled off, weeks since` /
`quiet for a couple months` / `gone quiet, months since`), and threads it
into the LLM seed so Aiko can notice "this territory's recently hot vs.
went quiet months ago" instead of just "big vs. small". Tests:
`tests/test_topic_graph_persistent.py::ClusterActivityTests` and
`RecencyPhraseTests` + `ClusterActivityShapeTests` in
`tests/test_knowledge_map_reflection.py`.

---

## K-time10. Finishing the wiring K-time7 shipped unused — SHIPPED

**The failure mode, stated plainly, because it is the reason the rule now
exists.** `format_transcript()` landed complete in `dd2ab58` with a
docstring naming "promise / belief / moments / summary" as its callers.
Not one of them ever called it. `format_memory_block()` had a single
consumer. K-time8 gave five workers `today_anchor()` — a date in the
system prompt — while still feeding them bare `Speaker: text` and bare
`- {content}`, so each worker knew what day it was and nothing about when
anything it was reading had happened. A complete toolkit with no stated
contract does not get used; it rots quietly and the entry above reads as
if it shipped.

**What that cost.** 851 of 1,089 memories (78%) sat at
`temporal_type='durable'`, which renders with **no time tag at all**, and
the persona read an untagged bullet as present-tense and fair game. 74 of
those rows contained a relative-time phrase in their own text — `Jacob
mowed the lawn today`, written May 27 — and 53 were 30+ days old. Aiko
was reading a two-month-old note as a report about this afternoon, and
she was right to: nothing in the prompt said otherwise.

**Three defensive layers, cheapest first.** Rendering, then the write
boundary, then the prompts:

- **Layer 1 — rendering.** [`rag_retriever.format_block`](../../../app/core/rag/rag_retriever.py)
  falls back to a `(noted 3 days ago)` recorded-at tag when
  `temporal_suffix()` returns empty, suppressed under 48h so fresh rows
  stay clean. The wording is deliberately *not* the existing `(3 days
  ago)`: that one asserts the event happened then, this one only says
  when the note was written. Message snippets gained an `age_prefix`, and
  the `relevant_context` header gained a compact `Today is …` anchor
  (T3 is assembled before the T4 "Right now it's…" line). Persona line 86
  was rewritten to explain the distinction. This layer fixed every row
  already in the database, which is why **no backfill of the 74 rows was
  needed**.
- **Layer 2 — the write boundary.** `has_relative_deictic()` (word-boundary
  regex over today / tonight / tomorrow / yesterday / currently / lately /
  soon / …) plus a guard in [`MemoryStore.add()`](../../../app/core/memory/memory_store.py):
  a `durable` or `preference` row whose text trips the predicate is
  reclassified to `past_event` anchored at `created_at`. **The text is
  never edited** — a mis-tag is recoverable, rewriting what was recorded
  is not. One funnel, so this covers all ~35 producers and anything added
  later.
- **Layer 3 — the prompts, where it should actually be prevented.**
  `format_transcript` / `format_memory_block` wired into the Tier-0
  writers whose output lands in `memories.content` (`memory_extractor`,
  `topic_digest_worker`, `topic_label_worker`, `memory_consolidator`),
  then `today_anchor()` + tagged input across the rest. One canonical
  `STORED_TEXT_TIME_RULE` constant carries the anti-relative wording so
  it cannot drift between workers — paste the constant, never retype it.

**Layer 3b — cue text has a separate failure mode.** A cue is frozen at
draft time and pending rows have surfaced 44 hours later. `resolve_deictics(text,
source_created_at, now)` rewrites "today" to "on May 27" and "currently"
to "at the time", applied at the render sites that quote raw
`mem.content` (`forward_curiosity_worker`, `follow_up_worker`,
`self_callback`, `interest_drift_worker`, `prepared_nudge`,
`associative_wander_worker`) — note the anchor there is the *source
memory's* timestamp, usually much older than the draft. Separately
`CuePoolMixin.take_pool_cue` appends "(you first noticed this yesterday)"
past a 6h draft-to-surface lag, **on a copy**, because post-turn
accounting has to judge the producer's original text.

**Still exempt by design** (unchanged from K-time5–9, restated so nobody
"completes" them): `memory_conflict_worker` judges logical contradiction
between two memories and ages would be noise in that call;
`idle_curiosity_worker` writes about the world, not about the user.

The contract is now written down in three places —
[`AGENTS.md`](../../../AGENTS.md) core rules, a "Temporal awareness
contract (K-time)" entry in [`rules/code-conventions.md`](../../../rules/code-conventions.md)
beside the "now" seam, and [`.cursorrules`](../../../.cursorrules) —
because fixing the code without stating the rule just means the next
worker added reintroduces it. Tests: `tests/test_timephrase.py`
(`HasRelativeDeicticTests`, `ResolveDeicticsTests`),
`tests/test_memory_temporal.py` (Layer 1 rendering +
`DeicticWriteGuardTests`), `tests/test_cue_pool_consumption.py::DraftAgeDisclosureTests`.

---

## G4. Cue outcome accounting — which of the 50-odd workers earn their keep?

**Motivation.** `get_idle_workers_status` could say that a worker **ran** —
overdue seconds, a duration EMA, error counts — but never that it
**mattered**. Whether the cue it produced reached Aiko was unanswerable,
because every way of losing one is silent: a topic gate returning `""`, the
gap-cue priority mutex, the K47 question-balance veto. A worker whose gate
never matches looked exactly like a worker quietly doing its job, so
cooldowns stayed hand-picked constants and an LLM-calling worker could burn
tokens producing cues that were structurally unreachable.

**Armed is the denominator, and it does not mean "a worker ran".** It means
*there was material waiting*. The distinction is the whole feature: a
worker that writes a finding every ten minutes and gets one through per day
is not producing 143 failures, it is producing one delivery and 143
supersessions. Arming is read back out of the state the providers
themselves consult —
[`cue_accounting.py`](../../../app/core/proactive/cue_accounting.py) walks a
registry of 15 cues, checking either the in-memory `_pending_*_seconds`
slot (the four gap cues) or the `kv_meta` journal ring against its
`<feature>.last_surfaced_at` watermark. Using the provider's *own*
definition of "something to say" is deliberate: the ratio cannot drift away
from what the provider actually saw. `away_activities` and
`forward_curiosity` need both a slot and journal content, matching what
their providers require to fire.

**Surfaced needed no instrumentation at all.** The plan was to touch ~60 T6
providers; it turned out
[`PromptTelemetry.block_chars`](../../../app/core/session/prompt_support.py)
has been recording a character count for every registered block on every
assembly since P31a, where `0` means "rendered empty". So "was this cue
surfaced?" was a question the assembler had been answering all along and
nobody was keeping the answer. The cue names in the registry are the
`_PROMPT_BLOCK_TIERS` names, and
`tests/test_cue_accounting.py::RegistryTests` fails if one stops resolving
— without it, renaming a block would report that cue as declined forever,
which is a plausible-looking number rather than an error.

**The gap-cue "one-of lottery" is not a lottery.** It is a deterministic
priority order (`turning_over` → `sleep_return` → `away_activities` →
`forward_curiosity`) enforced by the shared `_gap_cue_surfaced` flag, so
the *same* cue loses every time both are armed. That is a systematic bias
and a far more actionable finding than noise, which is why the decline
reason names the winner: `lost_priority:turning_over`.

**The catch-all was later split (H7).** `provider` — "the provider
declined for its own reasons" — shipped as a placeholder for the
per-provider sweep and became 86% of `concept_hypothesis`'s 342 declines,
which is how that cue type ran at a 1.5% spend rate for months with no
readable cause. Nine of the sixteen cue types were on it. Providers now
report through `note_decline(session, cue, reason)` from their bail
points, into a small closed vocabulary: `topic_miss`, `importance_floor`,
`cadence_block`, `no_stock`, `cross_lane`. Closed rather than free text
because the buckets are compared *across* cue types — "which provider is
losing its cues to a cadence knob" only stays answerable if everyone
spells it the same way.

Most of it lands in one place: `take_pool_cue` knows the cadence gate,
the shelf, and whether the caller's predicate refused everything, so it
classifies its own empty pick and every pooled provider is instrumented
at once. `note_as` names the predicate case (defaulting to `topic_miss`,
which is what that predicate almost always is), and `note_as=None` says
the caller is doing its own accounting — used by the two dual-mode
providers, where an empty first pick is a fallthrough to a second path
rather than the turn's decision. First writer wins, since a provider
returns at the first gate that refuses it. The two structural reasons
still outrank anything a provider says: a cue that lost the gap mutex
never reached the gate it would have reported.

**Two ordering traps, both of which would have produced plausible numbers.**
Arming is snapshotted at the top of `chat_once_streaming`, not during
assembly, because the providers *consume* the state it reads — a later
snapshot would report almost nothing as armed and the reach ratio would
look perfect exactly when the machinery was busiest. And the K47 veto is
snapshotted there too: `_update_question_balance` decrements the countdown
during post-turn and runs *before* the cue recorder, so a suppression
active during assembly would have read as absent and its declines been
misattributed to the providers.

**Declines deliberately do not go in `surfacing_outcomes`.** The backlog
sketch said to reuse that table with `kind="cue"`, but every aggregate over
it means "of the times this reached the prompt" — admitting rows that never
reached it would have inflated the denominator of the ledger's entire
purpose. Declines live in a new `cue_decisions` table (schema v28), and
only cues that actually rendered are also written to the ledger, so they
earn the same engagement settle as a concept or memory. Rows exist only for
*armed* cues: "not armed" is the common case, carries no information, and
would multiply the table by the cue count every turn.

Those ledger rows are **name-keyed** (`item_key`, also v28) with
`item_id = 0`. A cue has no integer identity anywhere in the codebase, and
hashing the name into `item_id` was rejected — collisions would silently
merge two cues' histories and the raw table would stop being readable,
which is most of what a diagnostic ledger is for. The surfaced cues ride
the L37 *carry* rather than taking a second insert, because the ledger
drops its carry pointer on a turn that surfaced nothing: a cue on a
concept-less turn would otherwise have sat unsettled forever, in the one
column the feature exists to fill.

**Found on the way.** Indexing `item_key` from `_CREATE_TABLES` broke
schema init on every *existing* database — `executescript` runs before the
migrations, so `CREATE INDEX` referenced a column the ALTER had not added
yet. `_DEPENDENT_MEMORY_INDICES` exists for exactly this hazard but is
applied partway through the chain (at the v10 step), before the v28 ALTER,
so the ledger index needed its own `_DEPENDENT_LEDGER_INDICES` list run at
the end. The v27→v28 migration test is what caught it.

**Reading it.** `get_cue_outcomes`
([`cue_outcome_tools.py`](../../../app/mcp/server_tools/cue_outcome_tools.py))
reports `reach_rate = surfaced / armed` per cue, the decline reasons, and
`never_armed`. A low rate is *not* automatically a bug — a topic-gated cue
that stays quiet while the conversation is elsewhere is working correctly;
what to act on is a rate near zero over a long window. Five cues
(`interest_drift`, `associative_wander`, `curiosity_gradient`,
`dormant_interest`, `knowledge_gap_notice`) dedupe by a per-topic key set
rather than a watermark, so their arming degrades to "the ring is
non-empty", which over-counts — they are listed in `coarse_arming` so the
rate is read as a floor rather than an estimate, flagged rather than faked.
`never_armed` is the loudest signal: a registered cue with no rows either
never gets written by its worker or is read wrongly by the arming model,
and neither shows up as a bad rate.

**Not done: per-provider decline attribution.** Everything a cue's own
gates refuse is currently bucketed as `provider`. Splitting that into topic
gate / cooldown / no-candidate means editing on the order of a hundred
`return ""` sites across four files that are already near the size budget,
so it is parked as **G6** — worth doing once the reach numbers say which
cues actually need it. `cue_accounting_enabled` (default on) turns the
whole recorder off; nothing consumes the ratios to change behaviour yet
(self-tuning cooldowns remain the follow-up). Tests:
`tests/test_cue_accounting.py`.

<details>
<summary>Original design reasoning (pre-build sketch, kept for the G5/G6 follow-ups)</summary>

**Motivation.** There are somewhere north of fifty workers registered on the
[`IdleWorkerScheduler`](../../../app/core/proactive/idle_worker_scheduler.py),
many of them making LLM calls, and there is no way to answer the only question
that matters about any of them: *did the cue this worker produced ever reach
Aiko, and did the conversation go better when it did?* Today we can see that a
worker **ran** (`get_idle_workers_status` reports overdue seconds, average
duration, error counts) but not that it **mattered**.

The gap is real and structural, not just missing dashboards. A worker writes a
finding to a `kv_meta` journal; a T6 provider decides whether to render it,
often behind a topic gate that silently returns `""` when the live conversation
has moved on; the gap-cue family runs a one-of lottery where only one of
`turning_over`, `sleep_return`, `away_activities` and `forward_curiosity` can
fire per assembly; and `_question_balance_suppressed()` can veto several more.
Every one of those is a legitimate design decision, and every one of them
discards work **without leaving a trace**. A worker whose gate never matches
looks exactly like a worker that is quietly doing its job.

Consequences worth fixing: cooldowns and daily caps are all hand-picked
constants with no evidence behind them, LLM-calling workers can burn the budget
producing cues that are structurally unreachable, and there is no way to retire
a cue type that does not work.

**Key files.**
- [`idle_worker_scheduler.py`](../../../app/core/proactive/idle_worker_scheduler.py)
  — the existing per-worker stats block is the natural home for the aggregate
  view; it already tracks duration EMA and error counts per worker.
- The provider side is spread across
  [`inner_life_part2.py`](../../../app/core/session/inner_life_part2.py) and
  [`inner_life_part3.py`](../../../app/core/session/inner_life_part3.py), where the
  journal-watermark and topic-gate patterns live. These are the sites that
  currently drop cues silently.
- [`prompt_assembler.py`](../../../app/core/session/prompt_assembler.py) — T6
  assembly order and the gap-cue one-of guard.
- Reuses L37's `surfacing_outcomes` table with `kind="cue"`; see
  [`concepts.md`](../concepts.md).

**Sketched approach.** Record three states per cue, not one: **armed** (a worker
wrote a finding), **surfaced** (a provider actually rendered it into the
prompt), and **settled** (the engagement outcome on the following turn, per
L37's off-by-one attribution). The armed-to-surfaced ratio alone is the
diagnostic that does not exist today and would immediately expose the
never-reachable gates.

When a provider declines to render, record *why* — topic gate, watermark
cooldown, lost the one-of lottery, question-balance suppression. This is a small
enumeration and it turns "the cue vanished" into "the cue lost to
`turning_over` eleven times this week", which is actionable in a way the current
debug logs are not.

With that in place, self-tuning cooldowns become a small increment rather than a
guess: a cue type with a healthy engaged rate earns a shorter cooldown, one
that consistently lands flat earns a longer one, both clamped to a sane band
around the configured default so the setting stays meaningful and a bad week
cannot silence a cue permanently. This should be opt-in behind a setting —
observability first, adaptation second, because the aggregate numbers will
probably change how the cooldowns should be shaped in ways we cannot predict
from here.

**Open questions.** (1) Is a topic-gated miss a *failure*? Mostly no — it is the
gate working — but the ratio still needs watching, because a gate that never
matches and a gate that matches appropriately are different situations with the
same signature. (2) Attribution when several cues fire together is shared
credit, same as L37; over a week that is fine, per turn it is noise. (3) Should
a worker whose armed-to-surfaced ratio is near zero have its *interval*
lengthened automatically, or is that too clever and better left as a report a
human acts on? Leaning report-only for the LLM-calling workers, where the cost
is real but so is the risk of switching off something that was about to matter.
(4) Retention, per P34.

**Effort.** Small (accounting + a report) / Small-Medium (self-tuning
cooldowns on top).

**Depends on.** L37 (the ledger and the attribution model). Related to P36
(starvation reporting) — that answers "did it run", this answers "did it
matter", and they belong in the same MCP view.

</details>
