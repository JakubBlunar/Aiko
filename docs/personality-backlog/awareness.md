# Awareness + grounding

The goal of this section is to reduce confident hallucination by making
Aiko's uncertainty visible to herself — both as structured state she
can act on and as background work that closes gaps over time. F1
(background fact-checker), F2 (knowledge-gap journal), F3 (confidence
column), and F5 (conflicting-memory detector) shipped together; see
[`shipped.md`](shipped.md) for the implementation summary. The
remaining follow-ups below build on that foundation — everything that
landed, including the whole K-time family, lives in
[`shipped/awareness.md`](shipped/awareness.md).

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

## F11. Relevance-driven memory resurrection (latent recall from the archive)

**Motivation.** Today "revival" is **citation-driven and passive**:
`revival_score` (schema v8) is bumped post-turn only for memories Aiko actually
cited (`_mark_revived_memories` in
[`post_turn_helpers_mixin.py`](../../app/core/session/post_turn_helpers_mixin.py)),
and `MemoryStore.decay()` gives a small rebate proportional to it so cited rows
resist fading. Nothing *proactively* reaches into the `archive` tier when a
dormant topic comes back to life. The payoff callback — "this might be
completely out of date, but I vaguely remember you once mentioning macro
photography two years ago; are you getting back into it?" — is exactly what
makes someone feel genuinely remembered, and it's driven by **latent
relevance**, not age. The thing to avoid is resurrecting a memory just because
it's old.

**Key files.**
[`rag_retriever.py`](../../app/core/rag/rag_retriever.py) (archive-tier scoring
+ the `(distant)` / `(faded)` K25 hedges already live here),
[`memory_decay_worker.py`](../../app/core/memory/memory_decay_worker.py) /
[`memory_promotion_worker.py`](../../app/core/memory/memory_promotion_worker.py)
(tier transitions into/out of `archive`),
[`topic_graph.py`](../../app/core/conversation/topic_graph.py) (the
re-engagement trigger — K6 "return-to-known" / cluster reactivation),
[`long_arc_callback.py`](../../app/core/conversation/long_arc_callback.py) +
[`callback_detector.py`](../../app/core/conversation/callback_detector.py)
(existing aged-callback lanes to build on, not duplicate).

**Sketched approach.** Trigger on **topic re-engagement**: when the live user
text re-activates a cluster that has been quiet for a long stretch (reuse the
K6 return-to-known signal + F10 cluster recency), run a scoped semantic search
of the `archive` tier for that cluster/topic. Surface a hit only when latent
relevance clears a bar (strong cosine to the current topic AND meaningfully
stale) and hand it up as a **hedged** callback cue — phrased with the K25
"(distant)" register and an explicit "may be out of date, treat as a soft
question" guard — rather than a confident statement. Standard anti-nag
signature + cooldown so a re-opened topic doesn't dredge the archive every turn.

**Relation to existing work.** This *reweights* revival toward relevance; it
doesn't replace the citation-driven `revival_score` rebate (that stays as the
"kept alive because we keep using it" signal). Pairs with the concept layer:
an `active` concept or cluster coming back into focus (L4 / L5) is a natural
resurrection trigger too.

**Open questions.** Latent-relevance threshold (cosine floor + minimum
staleness)? Proactive nudge (via `prepared_nudge`) vs. a passive RAG boost that
only fires when the topic is already live? Cap on resurrections per session?

**Effort.** Medium.
