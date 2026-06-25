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

**Shipped** — see [`shipped/awareness.md`](shipped/awareness.md#f6-privacy-preserving-query-reformulation-not-reject--shipped).

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

**Shipped** — see [`shipped/awareness.md`](shipped/awareness.md#f10-topic-graph-utilisation-rag--prompt--knowledge-integration).

---
