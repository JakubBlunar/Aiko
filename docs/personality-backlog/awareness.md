# Awareness + grounding

The goal of this section is to reduce confident hallucination by making
Aiko's uncertainty visible to herself — both as structured state she
can act on and as background work that closes gaps over time. F1
(background fact-checker), F2 (knowledge-gap journal), F3 (confidence
column), and F5 (conflicting-memory detector) shipped together; see
[`shipped.md`](shipped.md) for the implementation summary. The one
remaining follow-up below builds on that foundation.

**Web-search backend (2026).** Web search is now pluggable behind
[`app/llm/search/providers.py`](../../app/llm/search/providers.py):
DuckDuckGo (keyless default) or LangSearch (hybrid search + long-text
summaries, when an API key is configured under the `search` settings
block), with a DuckDuckGo fallback. F6 (query reformulation) shipped
with it; F7 (domain routing) is obsolete as a result. LangSearch's
**Semantic Rerank API** is intentionally **not** wired — Aiko's RAG is
already a local cosine pass and web results come back ranked +
summarized, so a second per-call API hit against the free-tier quota
isn't worth it. Revisit only if a concrete relevance problem appears.

---

## F4. Source-cited memories

When a memory originates from a tool call (`web_search` / `recall` /
document upload), persist the source URL or document id in
`metadata.source_url` (reuses the v7 generic metadata column). Aiko
cites naturally ("according to a thing I read last week..."). The
Memory tab grows a "from web" badge that links out. Key files:
[`app/core/memory/memory_store.py`](../../app/core/memory/memory_store.py),
[`app/llm/tools/web_search.py`](../../app/llm/tools/web_search.py),
[`app/core/proactive/idle_curiosity_worker.py`](../../app/core/proactive/idle_curiosity_worker.py)
(stamps the winning result URL onto each `curiosity_finding`),
[`app/core/memory/idle_fact_checker.py`](../../app/core/memory/idle_fact_checker.py)
(stamps the citation source onto fact-check rewrites),
Memory tab in [`web/src/components/SettingsDrawer.tsx`](../../web/src/components/SettingsDrawer.tsx).
Pairs naturally with F1, which would stamp its own `source_url` on
fact-check rewrites, and with G3's `curiosity_finding` memories which
already know the search query but don't yet record the winning URL.

**Status nudge.** The metadata column is already live (schema v7);
this is pure plumbing through three writers + a UI badge. Cheaper
than the entry implies.

**Correction.** The path is `app/llm/tools/builtins.py`
(`WebSearchTool`), not the non-existent `app/llm/tools/web_search.py`
referenced above. The background search lane lives in
[`app/core/tasks/handlers/web_search.py`](../../app/core/tasks/handlers/web_search.py)
(`WebSearchHandler`), and `web_search` is no longer a brain builtin —
it's a workflow skill plus two private worker instances (F1
fact-checker, G3 curiosity). Keep this in mind for F6-F9 below.

---

## F6. Privacy-preserving query *reformulation* (not reject) — SHIPPED

**Status: shipped.** Implemented as
[`app/core/memory/query_reformulation.py`](../../app/core/memory/query_reformulation.py)
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
[`docs/configuration.md`](../configuration.md) `search` block).

**Motivation.** The single biggest reason Aiko "tries to search but it's
blocked": the privacy gate
([`scrub_claim_for_search`](../../app/core/memory/fact_check_privacy.py))
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
[`app/core/memory/fact_check_privacy.py`](../../app/core/memory/fact_check_privacy.py)
(`scrub_claim_for_search` — add a reformulation step before the
length/word reject), the two callers that today just skip on `None`:
[`app/core/memory/idle_fact_checker.py`](../../app/core/memory/idle_fact_checker.py)
(`_scrub_claim`) and
[`app/core/proactive/idle_curiosity_worker.py`](../../app/core/proactive/idle_curiosity_worker.py)
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

## F7. Domain-aware source routing (MyAnimeList, music, games, film) — OBSOLETE

**Status: obsolete / superseded.** The web-search backend is now
pluggable and defaults to LangSearch when configured (a hybrid
keyword + vector search that returns clean long-text summaries from
billions of documents — see the `search` block in
[`docs/configuration.md`](../configuration.md) and
[`app/llm/search/providers.py`](../../app/llm/search/providers.py)).
That directly attacks the "generic web slop" problem this entry was
meant to solve, and LangSearch has no `site:` parameter to inject
anyway, so the per-domain routing mechanism does not port. If a future
need for structured per-source data (e.g. Jikan/MAL fields) reappears it
should be a dedicated fetch handler, not query routing. No code planned.

**Motivation.** Search is DuckDuckGo-only with no source steering, so
domain questions get generic web slop instead of the canonical source.
For anime specifically the user wants MyAnimeList; the same shape covers
music, games, and film. Better sources → more specific, more accurate
findings → directly attacks the "general response" problem.

**Key files.**
[`app/core/tasks/handlers/web_search.py`](../../app/core/tasks/handlers/web_search.py)
(`WebSearchHandler` — add a pre-search domain classifier + `site:`
injection), optionally a new `app/core/tasks/handlers/jikan.py` (the free
unauthenticated **Jikan** MyAnimeList API),
[`app/core/tasks/workflow/skill_registry.py`](../../app/core/tasks/workflow/skill_registry.py)
(register any new fetch skill), the two worker `WebSearchTool` callers.

**Sketched approach.** Start cheap: a small keyword/embedding classifier
maps a query to a domain and prepends a `site:` filter — anime →
`site:myanimelist.net`, music → MusicBrainz / `site:rateyourmusic.com`,
games → `site:igdb.com`, film/TV → Letterboxd / TMDB. Phase 2 (optional):
a dedicated `Jikan` fetch handler for structured MAL data (titles,
studios, scores, genres — no auth, generous rate limit) so anime
enrichment returns clean fields instead of scraped HTML. Config-gated per
source so a user can disable any of them.

**Effort.** Medium.

---

## F8. `knowledge` memory kind + web→RAG ingestion + retrieval boost

**Motivation.** Almost nothing fetched from the web survives the turn:
only G3's `curiosity_finding` and F1's gap-resolution `fact` persist, and
neither is a first-class, queryable knowledge store. Without an
accumulating, retrievable knowledge pool, Aiko can never get *less*
generic over time — every informational turn starts from the model's
parametric knowledge. Add a real home for learned facts.

**Key files.**
[`app/core/memory/memory_store.py`](../../app/core/memory/memory_store.py)
(`VALID_KINDS` — add `knowledge`; it mirrors into LanceDB automatically),
[`app/core/rag/rag_retriever.py`](../../app/core/rag/rag_retriever.py)
(retrieval boost + a `(learned)` surfacing tag, mirroring the existing
`(curiosity)` tag),
[`app/core/rag/rag_store.py`](../../app/core/rag/rag_store.py),
plus the F1/G3/F7 writers that produce the findings. **Do F8 with F4**
(source-cited memories) — every `knowledge` row should carry
`metadata.source_url`.

**Sketched approach.** New `kind="knowledge"` for distilled, impersonal,
non-time-sensitive facts (band names in a genre, a studio's filmography,
how a thing works), distinct from personal `fact`/`event` memory. Give
`RagRetriever` a small score bonus for `knowledge` hits **when the live
turn is informational** (read the K4 dialogue-act tag — don't boost
knowledge during emotional/support turns). Dedup via the existing
cosine-collapse path so repeat research merges instead of piling up.

**Effort.** Medium.

---

## F9. Interest-driven knowledge enrichment worker

**Motivation.** F6-F8 give Aiko the ability to search, good sources, and
a place to keep findings — F9 is the engine that *fills* it without
waiting for a fact-check trigger. It reads the **topic graph (K9)** to
find the user's recurring interests and proactively researches *specifics*
in those domains during idle windows ("Jacob keeps bringing up shoegaze →
learn three defining bands + albums"). This is what turns "I like a genre"
into "I can name things in it," over weeks.

**Key files.** New
`app/core/proactive/idle_knowledge_worker.py` (register with
[`IdleWorkerScheduler`](../../app/core/proactive/idle_worker_scheduler.py),
mirror the F1/G3 audit-logging pattern),
[`app/core/conversation/topic_graph.py`](../../app/core/conversation/topic_graph.py)
(`build_topic_graph_snapshot` — interest source), F6 (reformulation),
F7 (source routing), F8 (`knowledge` writes).

**Sketched approach.** On an idle tick, pick the top under-researched
interest cluster from the topic graph, generate 1-2 reformulated queries
(F6) routed to the right source (F7), distil the results with the local
worker model, and write `knowledge` memories (F8) with `source_url`.
Per-cluster cooldown so it doesn't grind the same interest; daily cap on
searches. **Strictly silent** — never fires a proactive message; the new
knowledge just quietly makes her next on-topic reply sharper. MCP debug:
`force_run("knowledge_worker")`, `get_knowledge_worker_state`.

**Effort.** Medium-Large (but mostly composition of F6-F8).

---

## F10. Topic-graph utilisation (RAG / prompt / knowledge integration)

**Foundation shipped.** The topic graph used to be **one giant
single-link cluster** — useless for anything downstream. It now uses an
**adaptive mutual-k-NN clusterer** ([`topic_graph.py`](../../app/core/conversation/topic_graph.py)
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
  [`ClusterLabelWorker`](../../app/core/conversation/topic_label_worker.py)
  idle worker now names each cluster ("weekend hiking plans") via a tiny
  worker-LLM pass, applied through
  [`TopicGraph.set_cluster_label`](../../app/core/conversation/topic_graph.py)
  (updates the live `_LiveCluster.label` + persists to `topic_clusters`).
  Labels are cached in `kv_meta` keyed by the cluster representative
  (`aiko.topic_label.<rep>`) with the size-at-label-time, so a batch
  refit doesn't force a re-label: the next tick re-applies the cached
  label for free (no LLM) and only regenerates when the representative is
  new or the size drifted >50%. Per-tick LLM spend bounded by
  `agent.topic_label_max_per_run` (largest-first). Surfaces as the
  cluster `summary` in the snapshot / `GET /api/topic-graph` / Memory
  drawer. Settings: `agent.topic_label_{enabled,interval_seconds,max_per_run,max_tokens}`.
  Tests: [`tests/test_topic_label_worker.py`](../../tests/test_topic_label_worker.py).
- **F10b. Cluster-aware RAG diversity. ✅ SHIPPED.** In
  [`rag_retriever.py`](../../app/core/rag/rag_retriever.py), the final
  top-k selection now caps how many hits may come from a single topic
  cluster so a dense knot (e.g. the big "get to know the user" cluster)
  can't monopolise every slot and crowd out other relevant context.
  Implemented as a deterministic MMR-lite: walk the deduped,
  score-descending candidates and defer a memory hit once its cluster
  already holds `rag_max_per_cluster` (default 3) admitted hits, then
  **backfill** from the deferred overflow in score order so the re-rank
  only ever *reorders* the top-k — it never shrinks it. Cluster id comes
  from [`TopicGraph.cluster_id_for`](../../app/core/conversation/topic_graph.py)
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
  [`tests/test_rag_retriever_cluster_diversity.py`](../../tests/test_rag_retriever_cluster_diversity.py).
- **F10c. Topic expansion / multi-hop. ✅ SHIPPED.** When a turn's
  strongest memory hit (score ≥ `agent.rag_expand_trigger_score`, default
  `0.55`) belongs to a topic cluster,
  [`RagRetriever._expand_topic`](../../app/core/rag/rag_retriever.py)
  appends up to `agent.rag_expand_max` (default `2`) sibling members of
  that cluster — **beyond** the top-k — whose cosine to the live query
  clears `agent.rag_expand_min_sim` (default `0.45`), so Aiko gets the
  surrounding context, not just the single closest line. Siblings are
  reached by id via two cheap graph readers
  ([`TopicGraph.cluster_id_for`](../../app/core/conversation/topic_graph.py)
  + `cluster_member_ids`) and scored by a dot product against the query
  embedding (no extra embed, no extra DB search). The new hits carry a
  `RagHit.expansion=True` flag and render in their own
  "Related notes from the same topic" section of `format_block`, so the
  LLM reads them as associative rather than direct recall. This is the
  **graph-aware multi-hop retrieval** explicitly deferred in the K9 spec
  (see [`patterns.md`](patterns.md) K9). It **does** change prompt content,
  so it's gated + bounded; flip `agent.rag_topic_expansion_enabled=false`
  (or `rag_expand_max=0`) to revert to pure top-k. No-op without a
  persistent topic graph + memory store. Tests: `TopicExpansionTests` +
  `FormatBlockExpansionTests` in
  [`tests/test_rag_retriever_topic_expansion.py`](../../tests/test_rag_retriever_topic_expansion.py).
- **F10d. Cluster-summary coarse retrieval tier (cluster-scoped recall).
  ✅ SHIPPED.** Coarse → fine retrieval: match a query to a whole topic
  cluster by **centroid cosine**
  ([`TopicGraph.best_clusters_for`](../../app/core/conversation/topic_graph.py)
  — a handful of dot products against cluster centroids, no member join,
  no embed) then drill into that cluster's members ranked by cosine to the
  query ([`RagRetriever.recall_topic`](../../app/core/rag/rag_retriever.py)
  returns `(cluster_label, hits)`). Surfaced as the new **`recall_topic`
  tool** ([`builtins.py`](../../app/llm/tools/builtins.py)): where the base
  `recall` does a global search for the few closest snippets, `recall_topic`
  enumerates one coherent theme — the natural "what do I actually know
  about X?" answer when the user asks Aiko to round up / summarise a
  subject. Gated by `tools.recall_topic` (default on; registered in
  [`tools_registry_mixin.py`](../../app/core/session/tools_registry_mixin.py)
  + [`base.py`](../../app/llm/tools/base.py), mapped to the `recall` family
  in [`tool_pass_gate.py`](../../app/core/session/tool_pass_gate.py)).
  No-op (empty result) without a persistent topic graph. Tests:
  `RecallTopicTests` + `RecallTopicToolTests` in
  [`tests/test_rag_retriever_topic_expansion.py`](../../tests/test_rag_retriever_topic_expansion.py)
  + `ClusterMemberAndCoarseMatchTests` in
  [`tests/test_topic_graph_persistent.py`](../../tests/test_topic_graph_persistent.py).
- **F10e. "Interest map" prompt block. ✅ SHIPPED.** A terse **T1
  (semi-stable)** inner-life line listing Aiko's top few topic clusters by
  size — "Topics you and {user} keep coming back to: …" — so she carries a
  sense of "the things we keep coming back to" without any per-turn LLM
  cost. Built by a new cheap
  [`TopicGraph.interest_map`](../../app/core/conversation/topic_graph.py)
  that reads **only** the live cluster map (label + member count, no join
  back to the memory mirror), so it's safe on the hot path unlike
  `topic_clusters()`. Each topic renders its F10a clean label once the
  [`ClusterLabelWorker`](../../app/core/conversation/topic_label_worker.py)
  has named it, falling back to the heuristic representative summary the
  batch rebuild stamps on every cluster — and since the label worker names
  the densest clusters first and the interest map *shows* the densest
  clusters, the line converges on clean F10a labels within a couple of
  worker ticks. Rendered by `_render_interest_map_block`
  ([`inner_life_part1.py`](../../app/core/session/inner_life_part1.py)),
  registered as the `interest_map` provider, and appended in T1 right
  after `goals_block` (the "things Aiko is carrying" cluster: agenda →
  goals → recurring interests). Owned by the assembler's `_StaticSlices`
  cache (paid once per listening window), dropped under `aggressive`
  alongside agenda/goals, no-op in the non-persistent topic-graph mode.
  Settings: `agent.interest_map_{enabled,max_clusters,min_size}`. Tests:
  `InterestMapTests` in
  [`tests/test_topic_graph_persistent.py`](../../tests/test_topic_graph_persistent.py)
  + `InterestMapProviderTests` in
  [`tests/test_prompt_assembler.py`](../../tests/test_prompt_assembler.py).
- **F10f. Knowledge-gap targeting — the self-aware beat. ✅ SHIPPED
  (notice half).** The original F10f had three sub-parts; their status:
  (1) **F9 research targeting — already shipped with F9.** The
  [`IdleKnowledgeWorker`](../../app/core/proactive/idle_knowledge_worker.py)
  picker (`_score_candidates`) already weights *knowledge headroom*
  (0.45) + size (0.35) + freshness, so dense, low-`knowledge`-coverage
  clusters are exactly where F9 digs. No change needed. (2) **The "I
  realised I don't actually know much about X" proactive beat — built
  here.** A new cue producer
  [`KnowledgeGapNoticeWorker`](../../app/core/proactive/knowledge_gap_notice_worker.py)
  (`name="knowledge_gap_notice"`, no LLM — a cheap kv pass) reads a new
  topic-graph reader
  [`TopicGraph.knowledge_gap_clusters`](../../app/core/conversation/topic_graph.py)
  (dense clusters whose `kind="knowledge"` fraction is at/below
  `memory.knowledge_gap_notice_max_knowledge_fraction`, ranked by a gap
  score `size·(1−frac)`), and drafts `{at, topic, cluster_key, size,
  knowledge_count}` into the `aiko.knowledge_gap_notices` kv ring with a
  per-topic cooldown (stable label hash, survives cluster renumbering).
  The consumer
  [`_render_knowledge_gap_notice_block`](../../app/core/session/inner_life_part2.py)
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
  [`tests/test_topic_graph_persistent.py`](../../tests/test_topic_graph_persistent.py),
  [`tests/test_knowledge_gap_notice.py`](../../tests/test_knowledge_gap_notice.py)
  (worker + helpers + provider), `test_knowledge_gap_notice_settings_round_trip`
  in [`tests/test_settings.py`](../../tests/test_settings.py). (3) **K35
  consolidation targeting → tracked as F10j** (cluster-scoped memory
  hygiene): F9's research-targeting already covered the "point F9 at gaps"
  intent, so the consolidation re-scoping is the genuinely-separate
  remaining work and lives under F10j below.

**New sub-ideas (added after the F10a-e ship — pick independently).**
The shipped foundation gives every consumer below a cheap, warm set of
primitives on [`topic_graph.py`](../../app/core/conversation/topic_graph.py):
cluster `centroid`s, `cluster_id_for` (O(1) memory→cluster), `cluster_member_ids`,
`best_clusters_for` (coarse query→cluster), `interest_map` (top-N by size),
and per-cluster `label`s. The ideas below are all just *new readers* of
those primitives — none needs a schema change beyond a `kv_meta` row.

- **F10g. Per-cluster rolling digest memory.** The true realisation of
  the original "cluster-*summary*" idea (F10d shipped as on-demand member
  enumeration, not a stored summary). An idle worker writes one
  high-salience `kind="topic_digest"` memory per dense cluster — a
  worker-LLM one-paragraph compression of its members — refreshed only on
  size drift (same cache-by-representative trick as F10a labels). RAG then
  surfaces *the digest* as the coarse "what I know about X" answer and
  only drills into raw members when the turn needs specifics, which also
  caps prompt size on a 40-member cluster (today F10c expansion just
  appends more lines). Key files: a new worker beside
  [`topic_label_worker.py`](../../app/core/conversation/topic_label_worker.py),
  `MemoryStore` for the digest write, `rag_retriever.py` for the
  surface-digest-first path. **Open Q:** does the digest live in the
  normal memory pool (so it decays / shows in the Memory tab) or in a
  side table keyed by cluster? Pool is simpler and lets pinning work.
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
  ([`shared_moment_extractor.VIBE_VOCABULARY`](../../app/core/relationship/shared_moment_extractor.py)).
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
  [`topic_temperature.py`](../../app/core/conversation/topic_temperature.py)
  (`score_cluster` / `render_block` / `ClusterTemperature`); provider
  `_render_topic_temperature_block(user_text)` in
  [`inner_life_part2.py`](../../app/core/session/inner_life_part2.py),
  registered in the **T6** tier right after the F10f gap-notice block (all
  topic-graph-derived cues clustered), dropped under `aggressive=True`.
  Persona: the "Topics that carry weight" block in
  [`aiko_companion.txt`](../../data/persona/aiko_companion.txt) teaches the
  warm-vs-tender register (it's a tone shift, never a line said out loud).
  Settings: `agent.topic_temperature_enabled` + `memory.topic_temperature_*`
  (`min_sim` 0.45, `threshold` 0.5, `cooldown_turns` 6). MCP:
  `get_topic_temperature_state` (dry-run scan of every charged cluster) +
  `force_topic_temperature_surface` (drops cooldown + thresholds on the
  next turn). Logs `topic-temperature fire:` per surfacing. Tests:
  [`tests/test_topic_temperature.py`](../../tests/test_topic_temperature.py)
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
  ([`topic_graph.py`](../../app/core/conversation/topic_graph.py),
  `O(members)` mirror join, no warm-start); pure scoring in
  [`topic_confidence.py`](../../app/core/conversation/topic_confidence.py);
  provider `_render_topic_confidence_block(user_text)` in
  [`inner_life_part2.py`](../../app/core/session/inner_life_part2.py),
  registered in T6 right after the F10h temperature block (all
  topic-graph cues clustered), dropped under `aggressive=True`, paced by a
  global turn cooldown. Persona: the "How much you actually know" block in
  [`aiko_companion.txt`](../../data/persona/aiko_companion.txt). Settings:
  `agent.topic_confidence_enabled` + `memory.topic_confidence_*`
  (`min_sim` 0.45, `thin_threshold` 0.25, `familiar_threshold` 0.7,
  `cooldown_turns` 6). MCP: `get_topic_confidence_state` (dry-run scan of
  every banded cluster) + `force_topic_confidence_surface` (drops cooldown
  + min_sim, splits bands at 0.5). Logs `topic-confidence fire:`. Tests:
  [`tests/test_topic_confidence.py`](../../tests/test_topic_confidence.py),
  a `ClusterKnowledgeStatsTests` block in
  [`tests/test_topic_graph_persistent.py`](../../tests/test_topic_graph_persistent.py),
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
  [`partition_by_cluster`](../../app/core/memory/cluster_scope.py) groups
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
  Tests: [`tests/test_cluster_scope.py`](../../tests/test_cluster_scope.py),
  a `ClusterScopingTests` block in
  [`tests/test_memory_conflict_worker.py`](../../tests/test_memory_conflict_worker.py),
  + a settings round-trip in `test_settings.py`.
- **F10k. Semantic topic tracking for K6 / K18.** Today novelty (K6) and
  stagnation (K18) reason about a rolling *centroid* of recent user
  vectors — a fuzzy, identity-less notion of "the current topic". Map
  recent turns to actual clusters via `best_clusters_for` and the shift
  becomes *nameable*: "we moved from the `weekend hiking` cluster to the
  `work stress` cluster" is a cleaner, more robust topic-change signal
  than a cosine drift, and it lets a pivot back to a *previously-visited*
  cluster read differently from a brand-new one. Key files:
  [`novelty_detector.py`](../../app/core/conversation/novelty_detector.py),
  [`topic_stagnation.py`](../../app/core/conversation/topic_stagnation.py).
  **Open Q:** additive signal alongside the centroid, or a replacement?
  Start additive and measure.
- **F10l. Cluster management UX (user agency over her mental map).** The
  Memory tab can already edit/pin individual memories; a cluster browser
  would let the user rename a cluster (overriding the F10a label),
  merge / split, pin a *whole* cluster, or "forget this topic" (bulk
  archive). Makes the topic graph a thing the user *steers*, not just an
  internal index. Key files: `GET /api/topic-graph` already serves the
  snapshot; add mutation endpoints + a panel under
  [`web/src/components/settings/`](../../web/src/components/settings/).
  **Open Q:** how do user renames survive a batch refit that reassigns
  the cluster representative? (Pin the label to the cluster id, not the
  representative.)

**Effort.** F10a/F10b/F10e small-medium each; F10c/F10d medium. **F10a-f +
F10h + F10i + F10j are shipped** (F10f = the self-aware knowledge-gap
notice; F9 already covered research-targeting; F10h = per-cluster topic
temperature from shared-moment vibes; F10i = per-topic confidence
self-model from size + learned-fact coverage — both provider-only; F10j =
cluster-scoped memory hygiene, which also delivered F10f's K35
consolidation re-scope alongside the F5 conflict re-scope). Remaining:
F10g/F10k/F10l — F10k is a low-risk re-scoping, F10g is medium (digest
worker + prompt block), F10l is a self-contained UX slice. Several overlap the K64 mind-wandering
family in [`patterns.md`](patterns.md) (esp. K64b interest-drift) —
cross-check before picking one up so two passes don't build the same
per-cluster aggregator twice. **Provider-walk note:** F10h/F10i both
compute their per-cluster signal live in the provider (member walk over
the *one* matched cluster — cheap), so any future per-cluster aggregator
(K64b drift, F10g digest input) can share `cluster_member_ids` /
`cluster_knowledge_stats` rather than re-deriving it.
