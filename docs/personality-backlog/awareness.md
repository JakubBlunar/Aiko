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

## F8. `knowledge` memory kind + web→RAG retrieval boost (+ F4 source-citing)

**Shipped** — see [`shipped/awareness.md`](shipped/awareness.md#f8-knowledge-memory-kind--webrag-retrieval-boost--f4-source-citing).

---

## F9. Interest-driven knowledge enrichment worker

**Shipped** — see [`shipped/awareness.md`](shipped/awareness.md#f9-interest-driven-knowledge-enrichment-worker).

---

## F10. Topic-graph utilisation (RAG / prompt / knowledge integration)

**Shipped** — see [`shipped/awareness.md`](shipped/awareness.md#f10-topic-graph-utilisation-rag--prompt--knowledge-integration).

---

# Temporal awareness (K-time family)

Continues the **K-time1** lineage (wall-clock prefixes on chat history —
shipped, see [`shipped.md`](shipped.md)). Relative time is one of the
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

## K-time2. Date-anchored retrieval for relative-time queries

**Motivation.** The single highest-value temporal fix. The extractor
resolves relative phrases at **write** time, but **nothing resolves them
at query time** — RAG is a pure semantic cosine pass, so "what did I tell
you *yesterday* about the dashboard?" or "remember that thing from *last
week*?" retrieves the semantic nearest neighbours regardless of when they
were said. Aiko then answers confidently off the wrong day. Parse the time
expression in the user's message deterministically and use it to *filter
or boost* retrieval toward that window. Key files: a new
[`app/core/infra/time_expr.py`](../../app/core/infra/) (regex set —
`yesterday`, `this morning`, `last week`, `on Monday`, `N days/weeks ago`,
`last month`, `back in March` — resolved against the now-anchor to a
`(start, end)` range), [`rag_retriever.py`](../../app/core/rag/rag_retriever.py)
(a date-window score bonus / filter over the existing `created_at` +
`event_time`), and [`chat_database.py`](../../app/core/infra/chat_database.py)
(a direct "messages in `[start, end]`" lookup for "what did we say
then"). **Tonal guard:** when the window is empty, Aiko should say she
doesn't have anything from then, not confabulate. **Effort.** Medium.

---

## K-time3. Upcoming-horizon block — pre-computed future relative times

**Motivation.** Future date arithmetic is exactly where the LLM fails, and
future plans only reach Aiko today if *semantic* RAG happens to surface
them. Add a proactive **forward scan** over `event_time` rows
(`future_plan` / agenda / D1 reminders) within a horizon window (e.g. the
next 7 days) and render a single terse "coming up" inner-life cue with the
relative phrasing **already resolved** ("tomorrow morning", "in 3 days",
"this weekend") so Aiko never computes a future date herself.
`rag_retriever._humanize_future` already exists — this is the missing
*forward sweep* that doesn't wait for a semantic hit. Surfaces only when
something falls in the window; one-shot / anti-repeat watermarked so it
doesn't nag. Pairs with D1 (reminders), the agenda block, and the
[`follow_up_worker`](../../app/core/proactive/follow_up_worker.py). Key
files: a forward scan in the memory/agenda layer, a new inner-life
provider + its tier in [`prompt_assembler.py`](../../app/core/session/prompt_assembler.py).
**Tonal guard:** a heads-up, not a calendar readout. **Effort.** Medium.

---

## K-time4. Session-elapsed & mid-session gap awareness

**Motivation.** There's cross-session gap awareness (J5 reconnection, K28)
and per-message history age (K-time1), but **nothing about the current
conversation's own clock**: how long *this* session has run ("we've been
at this a while now") or a notable *mid-session* pause ("you stepped away
for 20 min and came back" — too short for a full reconnection beat, too
long to ignore). A tiny derived signal off the session's first-message
timestamp plus the delta between the last two messages, rendered as an
optional one-line cue, lets Aiko land natural beats like "it's gotten
late and we've been talking an hour — you should sleep." Key files:
[`session_controller.py`](../../app/core/session/session_controller.py)
(session start time + last-message delta, both already nearly available —
`_last_assistant_age_hours` is a cousin), a small grounding cue in the
ambient cluster. **Tonal guard:** observe, don't police ("you've been on
here too long"). **Effort.** Small.

---

## K-time5. Unified time-phrasing module + single "now" seam

**Motivation.** Relative phrasing is computed in **~6 independent
humanizers** with slightly different bandings —
[`reconnection.humanize_gap`](../../app/core/relationship/reconnection.py),
[`promise_lifecycle.humanize_age`](../../app/core/memory/promise_lifecycle.py),
[`rag_retriever._humanize_past/_future`](../../app/core/rag/rag_retriever.py),
the [`prompt_assembler_helpers`](../../app/core/session/prompt_assembler_helpers_mixin.py)
age-prefix, [`follow_up._humanize_clock`](../../app/core/proactive/follow_up_worker.py),
and [`wants_ledger`](../../app/core/conversation/wants_ledger.py) — so the
*same instant* can read "yesterday" on one surface and "1 day ago" on
another in the same turn. Consolidate into one
[`app/core/infra/timephrase.py`](../../app/core/infra/) (past / future /
duration / clock formatters) used everywhere, reading a **single
injectable "now"** — which is also exactly where the DT1 virtual clock
plugs in. Removes drift, keeps every relative phrase consistent, and is
the prerequisite that makes DT1 (and DT4's deterministic scenarios) clean.
**Effort.** Medium (mechanical), high consistency payoff. Do before DT1.

---

## K-time6. Enrich the "now" anchor with year + ISO

**Motivation.** `_ambient_block` renders "Right now it's Friday, June 26,
afternoon (1:33 PM)" — **no year**, friendly-form only. For the residual
cases where the model still does its own arithmetic (cross-year spans,
"how long ago was X"), append the year and a compact ISO stamp
(`2026-06-26`) so the anchor is unambiguous. Trivial one-liner in
[`prompt_assembler_helpers_mixin.py`](../../app/core/session/prompt_assembler_helpers_mixin.py)
`_ambient_block`. **Effort.** Trivial. (Fold into K-time5 if that lands
first.)

---

## K-time7. Worker time toolkit — shared anchor + memory-age renderer

**Motivation.** The worker-facing half of K-time5, and the answer to "give
the workers a tool to handle time better." A codebase audit found that
**only `MemoryExtractor` gives its LLM a "today" anchor**, and **no worker
ever shows the LLM a memory's timestamp** — every worker hand-rolls
`- {content}` with no age, even though the chat RAG path already has
`rag_retriever._temporal_suffix` / `_humanize_past` / `_humanize_future`
producing "(yesterday)" / "(planned for tonight 20:00)". Workers crunch
memories that *have* `created_at` / `event_time` / `last_used_at`
([`memory_store.py`](../../app/core/memory/memory_store.py) `Memory`) but
throw the time away before the model sees it. Build one small toolkit
(the natural extension of K-time5's `timephrase.py`) exposing three things
workers can drop in: (1) `today_anchor(now)` — the
"Today is {weekday, date, time} ({iso})" line `MemoryExtractor._build_system_prompt`
already hand-writes, extracted for reuse; (2) `format_memory_block(mems, now)`
— bullets with the temporal suffix (generalise the existing
`_temporal_suffix`); (3) `format_transcript(rows, now)` — transcript lines
with K-time1-style relative-age prefixes for message-crunching workers.
Reads the single injectable "now" (DT1 clock seam), so worker tests get
deterministic time for free. Key files: new/extended
[`app/core/infra/timephrase.py`](../../app/core/infra/), export from
[`rag_retriever.py`](../../app/core/rag/rag_retriever.py). **Effort.**
Medium. **Prereq for K-time8 + K-time9.**

---

## K-time8. Give the "today" anchor to every relative-time-resolving worker

**Motivation.** Several workers ask their LLM to *extract a time* from a
transcript but give it **no anchor to resolve relatives against** — so
"tonight at 8", "next Monday", "in two weeks" can't be resolved correctly.
The worst is [`promise_worker.py`](../../app/core/memory/promise_worker.py)
`PromiseExtractionWorker`, whose prompt asks for a `deadline` field with no
"today is…" line at all. Same gap in
[`belief_worker.py`](../../app/core/relationship/belief_worker.py),
[`reflection_worker.py`](../../app/core/proactive/reflection_worker.py),
the [`shared_moment_extractor.py`](../../app/core/relationship/shared_moment_extractor.py)
`MomentDetector`, the summary / thread-resummary workers, and the
[`follow_up_worker.py`](../../app/core/proactive/follow_up_worker.py)
draft. Drop the K-time7 `today_anchor(now)` line into each of their system
prompts (one line each). Cheap, and directly lifts extraction quality —
relative deadlines/dates stop being mis-resolved. **Effort.** Small
(per-worker one-liner once K-time7 exists).

---

## K-time9. Feed memory ages to the memory-crunching workers

**Motivation.** Workers that *reason over a set of memories* make worse
decisions because recency is invisible to them. Concretely:
[`memory_conflict_worker.py`](../../app/core/memory/memory_conflict_worker.py)
**already tie-breaks contradictions on `created_at` in Python but never
tells the LLM which memory is newer** — feeding the age would let the
contradiction gate itself prefer the fresher claim;
[`memory_consolidation_worker.py`](../../app/core/memory/memory_consolidation_worker.py)
merges a group of `- {content}` notes blind to whether it's folding a
fresh note into a stale one; the new
[`knowledge_map_reflection_worker.py`](../../app/core/proactive/knowledge_map_reflection_worker.py)
"shape of what I know" reflection is far richer if it can see "this
territory is *recently* hot vs. went quiet months ago"; and the curiosity
seed / idle-curiosity workers pick staler questions without it. Swap their
hand-rolled `- {content}` joins for the K-time7 `format_memory_block(mems,
now)` so each row carries its age. **Effort.** Small–Medium (mostly
swapping the formatter call once K-time7 exists). **Tonal/quality guard:**
this is for the worker's *reasoning*, not user-facing text — the chat
model still gets recency via RAG's own suffix.

---
