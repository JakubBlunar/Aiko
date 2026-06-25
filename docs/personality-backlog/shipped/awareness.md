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
[`SettingsDrawer.tsx`](../../../web/src/components/SettingsDrawer.tsx)
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
[`SettingsDrawer.tsx`](../../../web/src/components/SettingsDrawer.tsx),
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

---

## K64a. Associative wandering ("funny, this reminds me of ...")

**Shipped.** First member of the K64 *freedom of thought* family — the
genuinely drifting part of Aiko's interior life, as opposed to the reactive
extract / fact-check / consolidate workers. The
[`AssociativeWanderWorker`](../../../app/core/proactive/associative_wander_worker.py)
is an `IdleWorker` (cue producer, not a verbatim nudge) that, during a quiet
window: reads the K9 topic graph's labelled clusters, forms candidate pairs
whose **centroid cosine ≤ `memory.associative_wander_max_pair_cosine`**
(default `0.25` — genuinely *distant* topics, not neighbours) via the pure
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
drafted:` / `no-connection:` / `fire:`. Settings: `agent.associative_wander_enabled`
+ the eight `memory.associative_wander_*` knobs. Tests:
[`tests/test_associative_wander.py`](../../../tests/test_associative_wander.py)
(pure helpers + worker gates + provider plumbing). **Remaining K64 family:**
K64b interest drift, K64c curiosity gradient, K64d knowledge-map
self-reflection (all open in [`patterns.md`](../patterns.md)).
