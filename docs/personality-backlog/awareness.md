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

## K-time2. Date-anchored retrieval for relative-time queries — SHIPPED

Resolves relative-time phrases at **query** time (the extractor already
did it at write time). New [`app/core/infra/time_expr.py`](../../app/core/infra/time_expr.py)
`parse_time_window(text, now)` turns `yesterday` / `last night` / `this
morning` / `last week` / `this week` / `last month` / `N days|weeks|months
ago` / `last N days` / `on Monday` / `back in March` / `tomorrow` / `next
week` into a concrete `[start, end]` `TimeWindow` against the
`timephrase` now-anchor (so it's DT1-virtual-clock-ready and
deterministic in tests). Past windows carry a `guardable` flag (the
clearly-retrospective ones) so chit-chat like "how are you today" never
arms the guard. [`rag_retriever.py`](../../app/core/rag/rag_retriever.py)
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
*, limit, exclude_session_id)`](../../app/core/infra/chat_database.py) is
the verbatim fallback: a bounded `created_at` range scan (newest-first,
capped), and [`RagRetriever`](../../app/core/rag/rag_retriever.py) injects
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
[`app/core/conversation/upcoming_horizon.py`](../../app/core/conversation/upcoming_horizon.py)
(`select_upcoming` / `build_signature` / `render_block`) filters
`future_plan` memories whose `event_time` lands in `(now, now +
upcoming_horizon_days]` (default 7), sorts soonest-first, caps at
`upcoming_horizon_max_items` (default 3), and renders one terse "Coming up
for {name}: …" cue with the relative phrasing **already resolved** by the
canonical [`timephrase.humanize_future`](../../app/core/infra/timephrase.py)
("tomorrow morning 09:00", "on Friday 18:00") so the chat model never
recomputes a date. The cue carries an explicit "use these, don't recalculate"
+ "heads-up only, never recite like a calendar" tonal guard.

Consumer is the **live** (no worker / kv)
[`InnerLifePart2Mixin._render_upcoming_horizon_block`](../../app/core/session/inner_life_part2.py)
— a single mirror scan + a couple of ISO parses — registered as the
`upcoming_horizon` provider and slotted in the **T6** tier right after
`follow_up_block` (both are future-plan / time-anchored surfaces). **Anti-nag
via signature + cooldown:** the cue re-surfaces immediately when the upcoming
set's signature changes (a plan appears or slides out of the window) and
otherwise sits out `upcoming_horizon_cooldown_turns` (default 6) so an
unchanged calendar isn't recited every turn. Gated by
`agent.upcoming_horizon_enabled`. Pairs with the
[`follow_up_worker`](../../app/core/proactive/follow_up_worker.py) (which
covers the *retrospective* "how did it go?" half once an event passes) and
the `temporal_suffix` RAG tag (which only fires on a semantic hit).
MCP-debuggable: `get_upcoming_horizon_state` (switches + knobs + cooldown +
last signature + a dry-run of the window with resolved phrases) /
`force_upcoming_horizon_surface` (one-shot bypass of the cooldown +
signature gate). Grep `upcoming-horizon fire:`. Tests:
[`tests/test_upcoming_horizon.py`](../../tests/test_upcoming_horizon.py)
(pure module + provider plumbing), an `affect → upcoming_horizon` slot test
in `tests/test_prompt_assembler.py`, and a settings round-trip in
`tests/test_settings.py`.

---

## K-time4. Session-elapsed & mid-session gap awareness — SHIPPED

There was cross-session gap awareness (J5 reconnection, K14/K28/K36) and
per-message history age (K-time1), but **nothing about the current
conversation's own clock**. K-time4 adds two cheap derived sub-cues off the
recent-message timestamps, folded into one block. The pure
[`app/core/conversation/session_clock.py`](../../app/core/conversation/session_clock.py)
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
[`InnerLifePart4Mixin._render_session_clock_block`](../../app/core/session/inner_life_part4.py)
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
[`tests/test_session_clock.py`](../../tests/test_session_clock.py) (pure
module + provider plumbing), a `reconnection → session_clock →
absence_curiosity` slot test in `tests/test_prompt_assembler.py`, and a
settings round-trip in `tests/test_settings.py`.

---

## K-time5–9. Temporal toolkit + worker time-awareness — SHIPPED

Shipped together as the [`app/core/infra/timephrase.py`](../../app/core/infra/timephrase.py)
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
(already picks the oldest `open_question` in Python). **Follow-up (not
built):** the `knowledge_map_reflection_worker` feeds cluster *labels +
sizes*, not memory rows, so `format_memory_block` doesn't fit — giving it
per-cluster recency ("this territory is recently hot vs. went quiet months
ago") would need the topic graph to expose cluster recency stats. Tracked
as a future enrichment.

---
