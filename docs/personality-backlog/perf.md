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

(P1 per-turn embed budget + timing, P2 prompt-build phase
telemetry, P8 idle-worker queue visibility + multi-worker drain,
and P12 bulk memory-mirror on startup have shipped — see
[`shipped.md`](shipped.md).)

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
[`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py),
[`app/core/infra/chat_database.py`](../../app/core/infra/chat_database.py)
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
[`app/core/rag/rag_retriever.py`](../../app/core/rag/rag_retriever.py),
[`app/core/memory/memory_store.py`](../../app/core/memory/memory_store.py)
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
[`app/core/rag/rag_store.py`](../../app/core/rag/rag_store.py)
(`list_recent_user_vectors`),
[`app/core/conversation/novelty_detector.py`](../../app/core/conversation/novelty_detector.py)
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
[`app/core/rag/message_indexer.py`](../../app/core/rag/message_indexer.py),
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
[`app/core/rag/rag_prefetcher.py`](../../app/core/rag/rag_prefetcher.py),
[`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
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
[`app/core/infra/chat_database.py`](../../app/core/infra/chat_database.py)
(schema bump + migration),
[`app/core/infra/schedule_learner.py`](../../app/core/infra/schedule_learner.py)
(comment update).

**Alternative.** Persist daily/hourly aggregated buckets in
`kv_meta` keyed by `(user_id, iso_week, weekday, bucket)` so the
worker reads compact rows instead of raw messages. Heavier but
also unlocks multi-week recurrence trends past the rolling
window.

**Effort.** Small (index) or medium (aggregate cache).

---

## P11. Reclaim background-worker `num_predict` from reasoning leakage

**Motivation.** Reasoning-tuned models (qwen3.x family especially,
including the `jaahas/qwen3.5-uncensored:9b` build we run today)
ignore `think=False` and still emit `<think>...</think>` tokens
that count fully against `num_predict`. We strip those blocks
post-hoc in `OllamaClient`, and the truncation warning is now
downgraded to DEBUG when the visible answer reaches a natural
stop, so the noise is gone — but the *budget* is still being
spent on a trace that the operator never sees. A self-image pulse
with `max_tokens=320` may only have ~200 tokens of actual prose;
the rest is reasoning we throw away. That eats wall-time on every
worker run and forces us to over-provision the cap to avoid real
truncation.

**Key files.**
[`app/core/persona/self_image_worker.py`](../../app/core/persona/self_image_worker.py)
(`_PROMPT`),
[`app/core/relationship/relationship_pulse.py`](../../app/core/relationship/relationship_pulse.py)
(`_build_pulse_prompt`),
[`app/core/proactive/curiosity_worker.py`](../../app/core/proactive/curiosity_worker.py),
[`app/core/memory/promise_extractor.py`](../../app/core/memory/promise_extractor.py),
[`app/core/proactive/dream_worker.py`](../../app/core/proactive/dream_worker.py),
[`app/core/conversation/conversation_arc.py`](../../app/core/conversation/conversation_arc.py),
[`app/core/goals/agenda.py`](../../app/core/goals/agenda.py),
[`app/core/memory/memory_consolidator.py`](../../app/core/memory/memory_consolidator.py),
[`app/core/proactive/reflection_worker.py`](../../app/core/proactive/reflection_worker.py),
[`app/core/infra/user_profile.py`](../../app/core/infra/user_profile.py),
[`app/core/relationship/shared_moment_extractor.py`](../../app/core/relationship/shared_moment_extractor.py),
[`app/core/proactive/prepared_nudge.py`](../../app/core/proactive/prepared_nudge.py),
[`app/llm/ollama_client.py`](../../app/llm/ollama_client.py)
(maybe a centralized `no_think_hint` helper applied to the user
message of any background-worker call).

**Sketched approach.** Append `/no_think` to the user-content
side of every background-worker prompt (qwen3 honours it as a
soft directive in some fine-tunes; it's a no-op on
non-reasoning models). Compare before/after: the
`completion_tokens` field on the MCP `get_last_response_detail`
should drop noticeably for surfaces tagged
`self_image_worker`, `relationship_pulse`, etc. If qwen3.5
uncensored ignores it, fall back to (a) wrapping prompts with
`<no_think>...</no_think>` tags some templates support, or (b)
running background workers on a non-reasoning Ollama model
(e.g. a small 3B instruct) via a `chat_llm.background_model`
setting; the main turn still uses the reasoning model.

**Open questions.** Does `/no_think` actually save tokens on
`jaahas/qwen3.5-uncensored:9b` specifically, or does this fine-
tune ignore both? If it ignores, the cleaner path is the dual-
model split (background_model setting), which is more code but
also unlocks faster background-worker turnaround independently.
Note: P13 below makes the dual-model split actually work; until
that lands, the only way to change the worker model is editing
`ollama.chat_model` in `user.json` directly.

**Effort.** Small (just the prompt suffix + before/after token
measurements). Medium if we need the dual-model split.

---

## P13. Route-driven worker model + context (catalogue isn't the source of truth yet)

**Motivation.** The LLM provider catalogue + role mapping (see
[`docs/llm-providers.md`](../llm-providers.md)) was shipped to let
[`llm.routes`](../../app/core/infra/settings.py) be the source of
truth for which model serves which role. The migration code
([`_sync_llm_routes_from_legacy`](../../app/core/session/session_controller.py))
mirrors the legacy `chat_llm` / `ollama` blocks **into** the
routes one-way at boot, but no reverse path exists: editing
`llm.routes.worker_default.model` in `user.json` (or via the
forthcoming Settings UI) has zero effect at runtime. Workers
resolve their model from `settings.ollama.chat_model` directly
inside `_effective_worker_model`, both in the constructor and in
`reconfigure_chat_llm`. The route table is cosmetic for the
worker role today.

A second, compounding gap: [`set_chat_model`](../../app/core/session/session_controller.py)
fans `update_runtime(model=…)` out to **three** workers
(`_summary_worker`, `_memory_extractor`, `_dialogue_act_tagger`)
but not the other ~12 (`_relationship_pulse`, `_reflection_worker`,
`_dream_worker`, `_belief_worker`, `_memory_conflict_worker`,
`_curiosity_worker`, `_promise_extractor`,
`_shared_moment_extractor`, `_self_image_worker`, `_goal_worker`,
`_schedule_learner`, etc.). Even if route reads are wired
correctly, only the cascaded three would pick up a hot-reload —
the rest stay on the old model until process restart.

Combined symptom (seen during phase-2 testing): user sets
`routes.worker_default.model = "qwen3-coder:30b"` via the UI,
log still emits `model=jaahas/qwen3.5-uncensored:9b` because (a)
the route isn't read and (b) even if it were, only 3/12 workers
would refresh.

**Key files.**
[`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
— two resolution sites for `_effective_worker_model` (constructor
around L466-474 and `reconfigure_chat_llm` around L3493-3503),
plus the cascade in `set_chat_model` around L3349-3362.
[`app/core/infra/settings.py`](../../app/core/infra/settings.py)
— `LlmRoute` dataclass (already carries `model` /
`context_window` / `max_tokens` / `temperature`).
[`docs/llm-providers.md`](../llm-providers.md) — design doc for
the route system.

**Sketched approach.** Two parts, both surgical:

1. **Resolution precedence.** Wherever `_effective_worker_model`
   is computed, prefer
   `settings.llm.routes[LLM_ROLE_WORKER_DEFAULT].model` when set,
   fall back to `ollama.chat_model`. Same precedence for
   `context_window` (route → legacy) so workers that honour it
   (most do via the OllamaClient `options.num_ctx`) pick up the
   route value. Also keep the mirror in
   `_sync_llm_routes_from_legacy` (legacy → route) so a legacy
   PATCH still produces a consistent route view; the read path
   just trusts the route first.
2. **Cascade completeness.** Replace the hand-coded three-worker
   block in `set_chat_model` with a loop over a registry of
   workers that have `update_runtime`. A simple
   `self._worker_runtime_updaters: list[Callable[[str], None]]`
   built in `__init__` keeps the cascade declarative — each
   worker init appends its `update_runtime` closure to the list,
   and `set_chat_model` iterates. Same pattern unlocks future
   context-window cascades without another N-place edit.

**Tests.** Pin both behaviours:

- `tests/test_session_controller_provider_switch.py` — extend
  with: set `routes.worker_default.model="X"` AND
  `ollama.chat_model="Y"`, instantiate `SessionController`,
  assert `_effective_worker_model == "X"`.
- New test asserting every worker registered with
  `_worker_runtime_updaters` receives the new model on
  `set_chat_model`.

**Open questions.** Should `routes.worker_default.max_tokens` be
write-through to per-worker `max_tokens` overrides? Today every
worker that calls the LLM passes its own `max_tokens` (sized to
the prompt's expected output — relationship_pulse=256,
self_image=320, reflection larger). A blanket route override
would muddle that. Probably keep route `max_tokens` as a
*ceiling* (workers may pass smaller, never larger) rather than a
mandate. Same logic for `temperature`.

**Effort.** Small. ~30 LoC for the resolution change, ~30 LoC
for the cascade registry, ~50 LoC of tests. No schema, no
migration, no UI change.

**Pairs naturally with.** P11 — once P13 lands, the
`background_model` / dual-model split P11 alludes to is just
a different route value, no additional plumbing.
