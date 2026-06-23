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
- **F10c. Topic expansion / multi-hop.** When a query hits a cluster
  strongly, optionally pull 1-2 sibling members from the same cluster to
  round out context. This is the **graph-aware multi-hop retrieval**
  explicitly deferred in the K9 spec (see [`patterns.md`](patterns.md)
  K9). Riskier — changes prompt content — so gate it and measure.
- **F10d. Cluster-summary coarse retrieval tier.** Embed the F10a labels
  and retrieve at the *cluster* level first, then drill into members —
  cheaper recall on a large corpus and a natural "what do I actually
  know about X" answer.
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
- **F10f. Knowledge-gap + consolidation targeting.** Clusters that are
  conversationally dense but low on `kind="knowledge"` coverage are
  exactly where F9 should dig and where a "I realised I don't actually
  know much about X" proactive beat could come from; dense clusters are
  also natural merge targets to point the K35 consolidation worker at.

**Effort.** F10a/F10b/F10e small-medium each; F10c/F10d medium and
riskier (touch retrieval + prompt). **F10a, F10b and F10e are shipped** —
remaining are the riskier F10c/F10d coarse / multi-hop retrieval tiers (a
cluster-scoped `recall` variant fits here — the base `recall` tool
already lets Aiko search memory on demand) and F10f (knowledge-gap +
consolidation targeting).
