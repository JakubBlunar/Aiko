# Performance + observability

Companion polish often loses to a hot path that's too slow or too
opaque to debug. The P-series collects performance and observability
gaps that aren't features in their own right but pay back across the
whole personality stack: every K-series entry rides on the same
turn-build, RAG retrieval, and idle-worker plumbing, so making those
faster or more measurable compounds.

These items are intentionally narrow — most are a single afternoon
once you sit down with them. Pair any K-series entry with the
relevant P-item if it's near the same code; otherwise pick whichever
unblocks the testing flow you're stuck on.

---

## P1. Per-turn embed budget + timing

**Motivation.** A typed turn can hit Ollama `/api/embeddings` more
than once with different strings: `RagRetriever._build_query`
embeds `"ctx || query"`, K6 `NoveltyDetector.detect` embeds raw
`user_text`, and `MessageIndexer` may re-embed the user message
asynchronously. The shared `Embedder` LRU only matches on exact
key, so two HTTP embeds per turn is the common case once novelty +
RAG are both on. Today there's no per-turn count or wall-time, so
"my turn felt slow" can't be attributed to embeds without a custom
log dive.

**Key files.** [`app/llm/embedder.py`](../../app/llm/embedder.py),
[`app/core/rag_retriever.py`](../../app/core/rag_retriever.py),
[`app/core/novelty_detector.py`](../../app/core/novelty_detector.py),
[`app/core/turn_runner.py`](../../app/core/turn_runner.py).

**Sketched approach.** Add a small per-turn embed coordinator in
the embedder (cache by normalised text within a turn boundary so
RAG and novelty share a vector when the strings substring-match).
Extend `PromptTelemetry` and the `turn done:` INFO line with
`embed_ms=` / `embed_calls=` fields, and surface them on
`get_last_response_detail` so MCP can grep regressions over time.

**Effort.** Medium.

---

## P2. Prompt-build phase telemetry

**Motivation.** `turn done:` already logs `rag_prefetch=` and
`prebuild=` slice-cache events but not the wall time of RAG
retrieval, individual inner-life providers, or total prompt
assembly. The DEBUG `prompt built:` line counts only the legacy 10
inner blocks — knowledge-gaps, belief-gaps, novelty, activity,
relationship axes, anniversary, routines, and circadian are
invisible — so a regression in a single provider can't be
attributed without instrumenting the suspect by hand.

**Key files.**
[`app/core/prompt_assembler.py`](../../app/core/prompt_assembler.py)
(`PromptTelemetry`, the `prompt built:` DEBUG line),
[`app/core/turn_runner.py`](../../app/core/turn_runner.py)
(`turn done:`),
[`app/mcp/server.py`](../../app/mcp/server.py)
(`get_last_response_detail`).

**Sketched approach.** Time each inner-life provider with a
context manager (`with self._timed("novelty"):`) into a flat
`provider_ms: dict[str, float]`; emit at DEBUG and roll up into
`PromptTelemetry`. Update the DEBUG `prompt built:` field list to
match the live providers — drop the legacy hard-coded 10. New
fields are additive, no consumer breaks.

**Effort.** Small.

---

## P3. Slice-cache validation still pays two SQLite reads

**Motivation.** `assemble_with_budget` ostensibly short-circuits
on a slice-cache hit, but the validation path still calls
`get_messages` + `get_latest_summary` + recomputes the cache key
before trusting cached slices. Voice prebuild helps the inner-life
blocks, but every typed turn pays those two reads even when
nothing changed since the last turn. A cheaper invalidator would
let hits actually be cheap.

**Key files.**
[`app/core/prompt_assembler.py`](../../app/core/prompt_assembler.py),
[`app/core/chat_database.py`](../../app/core/chat_database.py)
(lightweight `MAX(id)` / `summaries.updated_at` head query).

**Sketched approach.** Cache the `(max_message_id,
summary_updated_at)` tuple alongside the slice-cache entry. On
hit, do a single 2-column SELECT and compare; if equal, skip the
`get_messages` round-trip entirely. On miss, fall back to the
current full validation.

**Effort.** Small.

---

## P4. RAG memory-hit batch lookups

**Motivation.** After Lance ANN search, `RagRetriever.retrieve`
calls `memory_store.get(id)` per hit (up to `per_source_top_k`
times) to apply pin / tier / confidence / temporal scoring. The
mirror dict makes each call O(1) but the per-iteration try/except
+ scoring is repeated work; a single batch fetch (or pre-enriching
Lance rows once) would simplify the hot loop and make the next
optimisation wave (e.g. async scoring, cross-source de-dupe) much
easier to reason about.

**Key files.**
[`app/core/rag_retriever.py`](../../app/core/rag_retriever.py),
[`app/core/memory_store.py`](../../app/core/memory_store.py)
(`get_many` returning a dict; the in-memory mirror already has
all the data).

**Effort.** Small.

---

## P5. Novelty warm-up: full Lance scan

**Motivation.** `RagStore.list_recent_user_vectors` materialises
the entire Lance `messages` table via `to_arrow()` then filters in
Python. Cheap on day one, linear with chat history, runs once per
session on the first K6 detect. Multi-month installs will pay
real wall-time on the first novel turn after a fresh start.

**Key files.**
[`app/core/rag_store.py`](../../app/core/rag_store.py)
(`list_recent_user_vectors`),
[`app/core/novelty_detector.py`](../../app/core/novelty_detector.py)
(warmer call site).

**Sketched approach.** Push the role/session-prefix filter into
the Lance query (Lance supports SQL-like predicates), cap the row
limit at `2 * novelty_window`, and order by `created_at DESC`
server-side so we drop the Python sort entirely. Alternative: a
small `kv_meta` cache of the latest N user vector ids per user, refilled by the message indexer on write.

**Effort.** Medium.

---

## P6. MessageIndexer queue visibility

**Motivation.** Startup backfill walks every session/message with
embed + Lance write, competing with live-turn RAG/novelty embeds.
The queue is unbounded (only logs on impossible `put_nowait`
failure), so a slow Ollama response can stack thousands of
pending writes invisibly. There's no MCP surface for queue depth
/ lag — `get_rag_prefetcher_stats` exists for the prefetcher but
not the indexer.

**Key files.**
[`app/core/message_indexer.py`](../../app/core/message_indexer.py),
[`app/mcp/server.py`](../../app/mcp/server.py)
(new `get_message_indexer_stats` tool),
AGENTS.md log field table.

**Effort.** Small.

---

## P7. Typed-mode prefetch parity with voice

**Motivation.** RAG prefetch + static-slice prebuild fire from
`feed_stt_partial` / live capture only. Typed users compose for
seconds with zero prewarm — every typed turn pays full embed +
3× Lance search + ~15 inner-life providers cold. The voice win
documented in `rag_prefetcher.py` is achievable for typed turns
just by hooking the composer.

**Key files.**
[`app/core/rag_prefetcher.py`](../../app/core/rag_prefetcher.py),
[`app/core/session_controller.py`](../../app/core/session_controller.py)
(new `feed_typed_draft` entry point),
[`web/src/components/ChatView.tsx`](../../web/src/components/ChatView.tsx)
(debounced WS frame on draft length crossing a threshold).

**Sketched approach.** New WS command `composer_draft` with
`{text, length}`. Frontend debounces (~250 ms) and only sends
once `length > prefetch_min_chars`. Backend reuses the existing
`RagPrefetcher` with a `source="typed_draft"` tag so the cache
key doesn't collide with voice. Cancellable on send / clear /
component unmount.

**Open questions.** Privacy posture — typed drafts are even more
sensitive than partial STT (the user is mid-thought). Default
should be ON-but-bounded, with a settings opt-out and
draft-length cap (~120 chars) so we never prefetch a long-form
diary entry.

**Effort.** Medium.

---

## P8. Idle-worker queue visibility + starvation

**Motivation.** `IdleWorkerScheduler` runs **one** worker per
60 s tick, but ~10+ workers register (decay, promotion, schedule,
curiosity, fact-check, conflict, belief, follow-up, …). When two
workers come due simultaneously, the loser waits a full tick.
Overdue workers are invisible except by log-grep; there's no
`jobs_overdue=` / `next_due=` summary in MCP or the INFO drain
line. A single misconfigured cadence can quietly starve the
backlog for hours.

**Key files.**
[`app/core/idle_worker_scheduler.py`](../../app/core/idle_worker_scheduler.py)
(emit a per-tick summary log + new MCP-surfacable stats),
[`app/mcp/server.py`](../../app/mcp/server.py)
(new `get_idle_workers_status` tool returning name / last_run /
next_due / overdue_seconds rows),
AGENTS.md log field table.

**Effort.** Small.

---

## P9. Frontend streaming append: O(n) per token

**Motivation.** The Virtuoso virtualisation fixed the *render*
cost on a long history, but `appendAssistantToken` still clones
the entire `messages` array on every chunk. Long conversations +
fast token streaming = unnecessary JS work that can re-introduce
the freeze symptom under load even with virtualisation.

**Key files.**
[`web/src/store.ts`](../../web/src/store.ts)
(`appendAssistantToken`),
[`web/src/components/ChatView.tsx`](../../web/src/components/ChatView.tsx)
(maybe move the streaming bubble to isolated state so updates
don't go through the global messages array).

**Sketched approach.** Either (a) keep the streaming text in a
per-bubble ref and only commit to the messages array on stream
end, or (b) use immer/structural sharing so the cloned messages
array reuses the prefix. Option (a) is the cleaner companion-AI
fit because it lets us render a "speaking" bubble distinctly
from finalised history.

**Effort.** Medium.

---

## P10. Schedule-learner missing index

**Motivation.** G2 / K3 query
`WHERE role='user' AND created_at >= ?` against `messages`. Code
comments assume row count stays small ("per-day, not per-message
so a full scan is cheap enough"), which is true today. Multi-year
installs will full-scan; a tiny `(role, created_at)` index closes
that out before it bites.

**Key files.**
[`app/core/chat_database.py`](../../app/core/chat_database.py)
(schema bump + migration),
[`app/core/schedule_learner.py`](../../app/core/schedule_learner.py)
(comment update).

**Alternative.** Persist daily/hourly aggregated buckets in
`kv_meta` keyed by `(user_id, iso_week, weekday, bucket)` so the
worker reads compact rows instead of raw messages. Heavier but
also unlocks multi-week recurrence trends past the rolling
window.

**Effort.** Small (index) or medium (aggregate cache).
