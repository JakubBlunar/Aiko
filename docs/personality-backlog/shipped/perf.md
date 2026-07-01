# Shipped — Performance & observability (P-series)

Part of the [shipped log index](../shipped.md). One paragraph per entry; full detail lives in the linked implementation files.

---

## P1. Per-turn embed budget + timing

Single shared
[`Embedder`](../../../app/llm/embedder.py)
serves three live consumers per turn —
[`RagRetriever`](../../../app/core/rag/rag_retriever.py) embeds
`"ctx || query"`, K6
[`NoveltyDetector`](../../../app/core/conversation/novelty_detector.py) embeds the
raw user message, K18
[`TopicStagnationDetector`](../../../app/core/conversation/topic_stagnation.py)
piggybacks on K6's distance — plus the async
[`MessageIndexer`](../../../app/core/rag/message_indexer.py) on the
background thread. Two HTTP `/api/embeddings` round-trips per turn
is the common case once novelty + RAG are both on. Before P1 there
was no per-turn count or wall time, so "my turn felt slow" couldn't
be attributed to embeds without a custom log dive.

The embedder now exposes a tiny per-thread budget API:
`begin_turn()` resets a thread-local counter pair on the calling
thread, every cache-miss `embed()` call adds its measured wall time
+ one increment, and `end_turn()` returns the
`(calls, ms)` tuple and clears state. LRU cache hits don't count as
calls (they're free). The counters are *thread-local* on purpose —
`MessageIndexer` shares the same `Embedder` instance from a
background worker, and we don't want its async writes polluting the
turn thread's accounting; threads that never call `begin_turn` see
`active=False` and skip all accounting.
[`TurnRunner.run`](../../../app/core/session/turn_runner.py) brackets each
turn with begin/end, stamps the result onto
`PromptTelemetry.embed_calls` / `embed_ms` right before the
`turn done:` INFO log, and the public `run()`'s `finally` calls
`end_turn` again as a defensive cleanup so an exception mid-flow
can't leak counter state into the next turn.

The headline INFO line gained four new fields
(`embed_calls=N embed_ms=N assemble_ms=N rag_lookup_ms=N`) and the
[`SessionController`](../../../app/core/session/session_controller.py) metrics
dict carries them through to
[`get_last_response_detail`](../../../app/mcp/server.py) so MCP can
grep regressions over time. Tests:
`tests/test_embedder.py` (begin/end/peek/double-begin/cache-hit
isolation/thread-isolation), plus
`tests/test_turn_runner_telemetry.py::EmbedTurnBoundaryTests`
(stamping, cleanup-on-raise, no-embedder fallback, early-return
edge case).

Out of scope (deferred): substring-match de-duplication across the
RAG `"ctx || query"` and K6 `query`-only strings — different
strings produce different vectors, so the obvious win is "make K6
and RAG use the same embedding when the second string is a
substring of the first", which needs more design than this
observability slice could carry. Tracked as a follow-up.

---

## P2. Prompt-build phase telemetry

`turn done:` already logged `rag_prefetch=` / `prebuild=` slice-cache
events but not the wall time of RAG retrieval, individual
inner-life providers, or the total assemble. The DEBUG
`prompt built:` line counted only a hardcoded ten inner blocks —
the eleven that have shipped since
(belief-gaps, novelty, stagnation, activity, anniversary, axes,
knowledge-gaps, …) were invisible. A regression in any of them
couldn't be attributed without instrumenting the suspect by hand.

[`PromptAssembler`](../../../app/core/session/prompt_assembler.py) now wraps
every provider call through a `_safe_provider(timing_sink=…)` /
`_timed_phase` pair into a flat
`provider_ms: dict[str, float]` keyed by the provider name. The
RAG lookup phase (prefetch lookup + live retrieval + legacy
fallback) is timed into `rag_lookup_ms`; the entire
`assemble_with_budget` body is timed into `assemble_ms`. All three
join `PromptTelemetry` (and `as_dict()` so JSON consumers see the
same shape), get re-emitted on the
`turn done:` INFO line, and propagate via the SessionController
metrics dict to `get_last_response_detail`. The DEBUG
`prompt built:` line dropped the legacy 10-block counter and now
emits `providers=N provider_ms_total=N slowest_provider=name:ms`
derived from the live timing dict, so adding a future provider
(e.g. K17 clarification-repair) automatically lands in the
headline without a code change.

A timed provider that *raises* still records its bucket — the
operator wants to see "novelty took 3ms and exploded", not "novelty
silently disappeared from the telemetry". Tests:
`tests/test_prompt_assembler.py::PhaseTelemetryTests` (empty when
nothing wired, populated for each live provider, round-trip via
`as_dict`, `assemble_ms` covers the full build, P1 fields stay
zero on direct assemble) and
`tests/test_prompt_assembler.py::FailingProviderTimingTests` (raise
still records).

---

## P3. Cheap slice-cache validation (skip two SQLite reads on a hit)

`assemble_with_budget` short-circuited on a slice-cache hit, but the
validation path still ran `get_messages` (the full recent-window
fetch) + `get_latest_summary` on **every** typed turn just to recompute
the cache key, even when nothing had changed. P3 adds a cheap head
signature: [`ChatDatabase.get_history_head`](../../../app/core/infra/chat_database.py)
returns `(max_message_id, message_count, summary_signature)` via two
scalar aggregate queries, and the assembler stores it alongside the
slice-cache entry (`_slice_head_sig`). On a turn the new
[`_fast_slice_signature`](../../../app/core/session/prompt_assembler_helpers_mixin.py)
(head + persona/self-image mtime + last reaction + window + aggressive)
is compared first; when it matches, the cache is trusted **without
touching `get_messages` / `get_latest_summary` at all**. The signature
is a conservative superset of the full cache key — any new/deleted
message moves `max_id`/`count`, any summary rewrite moves the summary
signature — so a match can never serve stale slices. On a miss the
existing full validation runs unchanged and re-stamps the signature.
Tests: `tests/test_listening_window.py::StaticSliceCacheTests`
(`test_hit_skips_get_messages_read`, `test_new_summary_invalidates_cache`)
and `tests/test_chat_database.py::TestHistoryHead`.

---

## P4. RAG memory-hit batch lookup (`get_many`)

`RagRetriever.retrieve` applied pin / tier / confidence / temporal /
goal-alignment scoring by calling `memory_store.get(id)` **once per
Lance hit** (up to `per_source_top_k` times), each acquiring the mirror
lock. P4 adds [`MemoryStore.get_many`](../../../app/core/memory/memory_store.py)
(one lock acquisition, `{id: Memory}`); the retriever batch-fetches all
hit ids once before the scoring loop and reads from the dict. Falls
back to the per-hit `get` for duck-typed stores that don't expose
`get_many`. Tests: `tests/test_memory_store_metadata.py::TestGetMany`.

---

## P18. Streaming accumulator no longer O(n²)

The stream loop did `accumulator.append(delta)` then
`full = "".join(accumulator)` **per token** — O(n²) work + allocation
churn on long replies. P18 grows a single running `full += delta`
string instead (CPython amortises this to linear), with `full_raw =
full` after the loop. Byte-identical output; the reaction-tag parse and
streaming-safe-prefix logic (which the recent streaming-bug fix
depends on) are untouched. The further reaction-parse micro-opt from
the sketch was deliberately *not* taken — the `^\s*\[\[reaction:…\]\]\s*\n*`
regex greedily consumes trailing whitespace, so freezing a tag offset
would risk re-introducing the off-by-newline streaming bug.
Covered by `tests/test_turn_runner_mood_fallback.py`.

---

## P15. (Invalid) One user-text embed per turn — already handled by the LRU

Marked **invalid** after validation, not implemented. The premise was
that K6 novelty, F2 `pick_relevant`, and K29 opinion-injection each
fire a separate HTTP `/api/embeddings` round-trip for `user_text`
(50–200 ms × 3). In reality all three embed the *identically-normalised*
stripped `user_text`, and [`Embedder`](../../../app/llm/embedder.py) has a
256-entry LRU keyed by `sha1(model + text)` — so all but the first
collapse to sub-microsecond cache hits. The hot path is therefore
already at the 2-embed steady state P15 targeted (RAG's contextual
`ctx || query` is the only other distinct vector). The only remaining
win — substring de-dup of the RAG `ctx || query` string against the
raw `user_text` — is the harder follow-up already flagged as
out-of-scope under P1, not the "thread one vector everywhere" refactor
P15 described.

---

## P21. K29 borderline gate moved off the hot path

When the opinion-injection heuristic returned `borderline`, the LLM
YES/NO verdict ran **synchronously inside `assemble_with_budget`** —
0.5–8 s of added TTFT for a one-line cue, before any token streamed.
P21 defers it: `opinion_injection_detector.detect(defer_borderline=True)`
returns the borderline candidate as a `PENDING` result **without
calling the LLM**; the provider stashes it and stays silent that turn.
The post-turn hook
[`_resolve_opinion_injection_pending`](../../../app/core/session/inner_life_part3.py)
runs the rate-limited verdict after streaming completes, and a
confirmed contradiction arms a one-shot cue that renders on the **next**
turn (the stance hasn't changed in those seconds, so the lag is
invisible). `definite` hits still fire inline — they never needed the
LLM. Cooldown / per-session cap / K59 tease-bank arm only on the
confirmed fire. Pending state clears on session switch / clear. The hot
path now only ever pays the pure-Python heuristic. Tests:
`tests/test_opinion_injection_detector.py` (`defer_borderline` returns
PENDING / definite-still-fires / require_definite-overrides) and
`tests/test_opinion_injection_provider.py::DeferredBorderlineTests`
(arm-not-cooldown, no-ollama-stays-definite, resolver YES/NO/rate-limited,
next-turn one-shot render).

---

## P5 + P23. Lance scan push-down for `list_recent_user_vectors`

[`RagStore.list_recent_user_vectors`](../../../app/core/rag/rag_store.py)
(the K6 novelty warm-up + the K28 "turning over" provider) materialised
the **entire** Lance `messages` table via `to_arrow()` — every column
(including the big `content` payload) and every row (all roles, all
users) — then filtered `role='user'` + the session-id prefix in Python.
A full-table read on the worst turn there is (the cold "welcome back"
turn that already pays cold caches everywhere), growing linearly with
total chat history. P5/P23 pushes the filter into the Lance scan:
`self._messages.search().where("role = 'user' AND session_id LIKE
'<prefix>:%'", prefilter=True).select(["created_at", "vector"])` — so
only this user's user-role rows leave disk, and only the two columns the
caller actually reads. The Python-side sort + cap is unchanged (Lance
has no server-side ORDER BY in 0.30), but it now runs over a fraction of
the rows. A `_recent_user_vectors_fallback` keeps the old full-`to_arrow`
+ PyArrow-compute mask path for older Lance builds where the predicate
query raises. The `:`-delimited prefix protects against cross-user
collisions (`alice:%` won't match `alice2:…`), matching the old
`startswith(f"{prefix}:")` semantics. Tests:
`tests/test_rag_store.py::*list_recent_user_vectors*` (role/prefix
filter, most-recent-first ordering, cap, empty case).

---

## P17. K22 callback detector: filtered mirror walk, not a full copy

`callback_detector.detect` ran **every turn** post-turn and pulled the
whole memory mirror via `memory_store.list_recent(limit=10_000)` — a
full `list(self._mirror.values())` copy plus two O(n log n) sorts
(created_at, then pinned) — only to immediately discard every row whose
kind wasn't in the eight-entry `CALLBACK_KINDS` allow-list. On an aged
corpus dominated by `observation` / `knowledge_gap` / `scratchpad` rows
that's a lot of copying + sorting + per-row Python overhead for a
handful of eligible candidates. P17 adds
[`MemoryStore.iter_by_kinds`](../../../app/core/memory/memory_store.py)
(plural sibling of `iter_by_kind`): one locked mirror walk filtered to
the kind set, **no sort** (the detector sorts by cosine itself). The
detector calls it with the allow-list, so the cosine walk only ever
touches callback-eligible rows. Falls back to `list_recent(10_000)` for
duck-typed stores without `iter_by_kinds`. Tests:
`tests/test_callback_detector.py` (`test_p17_prefers_iter_by_kinds_over_full_mirror`
+ the existing suite re-run through the new path via the fake store's
`iter_by_kinds`).

---

## P10. `(role, created_at)` index for the schedule learner

The G2/K3 [`ScheduleLearner`](../../../app/core/infra/schedule_learner.py)
query is `WHERE role='user' AND created_at >= ? ORDER BY created_at ASC`
with **no** `session_id`, so the existing `idx_messages_session`
(`session_id`-leading) couldn't serve it — SQLite full-scanned the
`messages` table, fine today but linear with multi-year history. P10
adds `idx_messages_role_created ON messages(role, created_at)`. Both are
base columns, so the index lives directly in the `_CREATE_TABLES` schema
script (`CREATE INDEX IF NOT EXISTS` runs on every open — existing DBs
pick it up with no version bump). The planner now serves the query as a
**covering index** scan (it only needs `created_at`, which the index
carries), so there's zero table access. Tests:
`tests/test_chat_database.py::TestScheduleLearnerIndex` (index exists +
`EXPLAIN QUERY PLAN` shows `idx_messages_role_created`, not `SCAN`).

---

## P19. RAG reader-writer lock + parallel per-source searches

`RagStore` serialised **every** operation behind one
`threading.Lock`, and `RagRetriever.retrieve` ran `search_memories` →
`search_messages` → `search_documents` sequentially under it, so the
three independent ANN queries summed instead of max-ing and a turn-thread
read queued behind a `MessageIndexer` write on an unrelated table. P19
splits this two ways. **(a)** A small reader-writer lock
([`_RWLock`](../../../app/core/rag/rag_store.py) — concurrent readers,
exclusive writers, reader-preferring so turn-latency reads never starve
behind a backfill write storm) replaces the coarse `Lock`; all searches /
`knn` / `list_*` / `counts` / `has_message` take the shared read side,
only the single-row `add` / `delete` / `ensure_vector_index` writes take
the exclusive side. Lance datasets are MVCC-safe for concurrent reads, so
this is sound. **(b)** `RagRetriever` gained a 3-worker
`ThreadPoolExecutor`; `_search_all_sources` dispatches the enabled source
searches concurrently (Lance frees the GIL during the ANN query, so the
threads make real wall-clock progress) and joins, each guarded so one
source raising never aborts the others. Disabled sources skip their
search; a single active source or a closed pool (`retriever.close()`,
wired into `SessionController.shutdown`) runs inline. The per-source
scoring after the join is unchanged — same merge, dedupe, diversity
re-rank, and `mark_used`. Tests: `tests/test_rag_store.py::RWLockTests`
(concurrent readers overlap; a writer excludes a reader until it exits)
and `::ParallelSearchTests` (all three sources searched + merged; close
falls back to inline + is idempotent).

---

## P20. Synchronous LLM compaction no longer stalls the turn

When a turn's assembled prompt projected over budget,
`TurnRunner._run_inner` ran `SummaryWorker.compact_now` **inline on the
turn thread** — a full summarisation LLM call (up to the worker's
`timeout_seconds`) between user-send and first token, the single
worst-case latency spike in the system, landing on exactly the heaviest
turns. P20 removes it from the hot path. The synchronous `compact_now`
was never needed to *fit*: the `aggressive=True` reassembly that already
followed it is guaranteed to fit (it pops oldest raw history until
`system+user+history` is within budget), and a fresh summary only *adds*
to the system prompt, so deferring it never hurts the fit. The overflow
branch now (1) calls `SummaryWorker.notify_compaction_soon(session_key)`
to push the background-summariser deadline to *now*, and (2) reassembles
aggressively to fit this turn by dropping the oldest raw history. The
*next* turn picks up the proper rolling summary instead of dropped
context — a one-turn quality dip in exchange for never blocking first
token on a worker LLM call. `compactions_run` stays `0` (no synchronous
compaction happens), and the post-turn `notify_compaction_soon`
threshold check (`max_prompt_tokens_pct`) is unchanged, so most overflows
are still pre-empted a turn early. `compact_now` stays on `SummaryWorker`
(still unit-tested, still callable) but no longer has a hot-path caller.
Tests: `tests/test_turn_runner_telemetry.py::DeferredCompactionTests`
(overflow schedules async + skips `compact_now` + reassembles aggressive;
no-overflow leaves the summary worker untouched; `compactions_run` stays
0).

---

## P6. MessageIndexer queue/stats visibility

The async embed-and-store pipeline
([`MessageIndexer`](../../../app/core/rag/message_indexer.py)) had no
introspection surface: the unbounded work queue only logged on an
(effectively impossible) `put_nowait` failure, and there was no MCP
counterpart to `get_rag_prefetcher_stats`, so a slow embedder silently
stacking thousands of pending writes — or a row that fell out of RAG on
the give-up path — was invisible until you went log-diving. P6 adds
lifecycle counters threaded through every mutation point
(`enqueued` / `indexed` / `skipped_short` / `already_present` /
`embed_failures` / `write_failures` / `retries_scheduled` / `gave_up` /
`dropped_queue_full` / `backfill_walked`), all bumped under a dedicated
`_stats_lock` since the indexer is written from four threads (DB-listener
enqueue, worker, backfill, retry timers). A new `stats()` method snapshots
the counters plus live state computed on demand: `queue_depth`
(`Queue.qsize`), `pending_retries` (outstanding back-off timers),
`worker_alive`, `backfill_running`, `last_index_age_seconds` (monotonic
delta since the last successful write), and `last_give_up`
(`{message_id, stage, attempts}` for the most recent permanently-dropped
row — the silent-RAG-rot canary). The unbounded-queue drop log was also
lifted DEBUG → WARNING so a real backlog drop is visible in `tail_logs`.
Surfaced over MCP as `get_message_indexer_stats` (mirrors the
prefetcher-stats tool: `{"enabled": false}` when no indexer, else
`{"enabled": true, **stats()}`). Tests:
`tests/test_message_indexer_retry.py::StatsTests` (clean success bumps
`indexed` + stamps `last_index_age_seconds`; embed failure counts
`embed_failures` + `retries_scheduled` + `pending_retries`; give-up
records `last_give_up`; short body counts `skipped_short`).

---

## P22. Inner-life shared recent-history memo

~35 inner-life providers run inside `assemble_with_budget` every turn,
and three of them — K23 `_render_misattunement_block` (last 6 rows), K30
`_render_self_noticing_block` and K54 `_render_topic_appetite_block` (last
`max(window*4, 20)` ≈ 24 rows each) — issued **separate overlapping**
`chat_db.get_messages` queries for the same recent window within a single
assembly. P22 collapses them into one read via a per-assembly memo. The
assembler now bumps a monotonic
[`_assembly_seq`](../../../app/core/session/prompt_assembler.py) at the top
of every `assemble_with_budget` call; a new host helper
[`_inner_life_recent_messages(limit)`](../../../app/core/session/inner_life_part1.py)
caches `(token, window, rows)` keyed on `(session_key, _assembly_seq)` and
serves a wider cached window to a narrower later caller by handing back
the same rows (callers already walk `reversed(rows)`, so extra older rows
are harmless). The fetch floor (`_INNER_LIFE_RECENT_MIN = 24`) means the
first caller — whatever its window — fetches enough for all three, so in
the default config the three reads become **one**; a larger configured
window just refetches and updates the memo. Correctness is structural: a
new turn (or a session switch, which changes `session_key`) always
mismatches the token and refetches, and when no assembler is wired (seq
absent — e.g. a provider called outside an assembly, or a test stub) the
helper never trusts the memo and reads through every time. This shipped
sub-item (a) of the original P22 sketch; sub-items (b) short-circuit
disabled providers before their timed block and (c) TTL-cache
minute-scale trend blocks were assessed as lower-value/higher-risk and
left for a later pass. Tests:
`tests/test_inner_life_recent_memo.py` (overlapping reads collapse to one
query; new seq invalidates; larger window refetches; no-assembler skips
the cache; floor applies to a small request) plus the unchanged
`test_misattunement_provider.py` / `test_self_noticing_provider.py` /
`test_topic_appetite.py` suites still pass against the indirection.

---

## P8. Idle-worker queue visibility + multi-worker drain

`IdleWorkerScheduler` was capped at one worker per 60 s tick.
With ~10+ workers registered (decay, promotion, schedule,
fact-check, conflict, belief, follow-up, idle-curiosity, …) the
loser of a tie waited a full minute, and a single misconfigured
cadence could quietly starve the backlog with no MCP-visible signal
beyond rummaging through `last_run_at` rows. The natural quiet
window between turns — Aiko's 10-30 s of TTS plus the user's typing
time before the next submit — was being thrown away.

The scheduler now drains as many due workers as fit into a per-tick
wall-time budget (`tick_budget_ms`, default 3000). Workers are
sorted oldest `last_run_at` first; each worker's
[`avg_duration_ms`](../../../app/core/proactive/idle_worker.py) (EMA, alpha=0.3
on `IdleWorkerRecord`) is the cost estimate. Anti-starvation always
admits the most-overdue ready worker even if its estimate exceeds
the remaining budget, so a tight budget on a slow machine still
makes progress instead of looping forever. A hard
`max_per_tick` cap (0 = unlimited) is available for operators who
want to clamp tick log volume on heavy backlogs;
`max_per_tick=1` reproduces the legacy single-worker behaviour.

Per-run wall time is folded into the EMA on success; failures bump
`error_count` (cumulative, separate from `last_error` which gets
cleared on the next clean run). The scheduler emits one structured
INFO line per non-empty tick:

```
idle_workers tick: ran=3 due=5 skipped_budget=2 queue_after=2
                   tick_ms=472 budget_ms=3000 names=memory_decay,memory_promotion,fact_checker
```

A new MCP tool `get_idle_workers_status` returns the enriched
view: scheduler config (`wake_seconds`, `tick_budget_ms`,
`max_per_tick`, `quiet`) plus a `workers` list sorted most-overdue
first. Each row carries `last_run_at`, `next_due_at`,
`overdue_seconds` (positive = waiting), `avg_duration_ms`,
`last_duration_ms`, `total_duration_ms`, `run_count`,
`error_count`, `last_error`. The legacy `inspect_idle_workers`
tool stays for quick checks; reach for `get_idle_workers_status`
when you want to answer "which workers are starving and why?".

Settings: [`MemorySettings.idle_worker_tick_budget_ms`](../../../app/core/infra/settings.py)
+ `idle_worker_max_per_tick`, mirrored in
[`config/default.json`](../../../config/default.json). Tests:
`tests/test_idle_worker_p8.py` (EMA shape, multi-worker drain,
anti-starvation under tight/zero budgets, oldest-first ordering,
error counter, `get_status` shape with never-run vs. run workers,
summary log content), plus the legacy
`tests/test_idle_worker_scheduler.py` updated for the new
multi-worker default and a `max_per_tick=1` regression.

## P12. Bulk memory-mirror on startup

`MemoryStore.migrate_to_rag` re-pushed every SQLite memory into
LanceDB on every boot via a per-row
[`RagStore.add_memory`](../../../app/core/rag/rag_store.py) loop. Each
call did its own `delete` + `add` under the write lock, so 135
memories meant 270 LanceDB write ops with manifest churn between
each. On Windows that landed at ~525 ms per op, ~71 s total — a
visible startup hang between `RagStore ready` and `RAG: mirrored
N existing memories into LanceDB` in the log, and one that
scaled linearly with memory count.

The mirror now goes through a new
[`RagStore.add_memories_bulk`](../../../app/core/rag/rag_store.py)
batch path: one `delete` with an `id IN (...)` predicate plus one
`add(rows)` per chunk. With `chunk_size=500` (the default) a
typical install lands all rows in a single chunk — two write ops
total instead of 2*N. `migrate_to_rag` builds the records list
once, drops embedding-less / blank-content rows up front (same
implicit filter the per-row path had), and now wraps the whole
bulk call in `try`/`except` so a misbehaving LanceDB doesn't
abort startup. The "RAG: mirrored N existing memories into
LanceDB" log line is preserved.

Empirically: ~71 s -> ~1-2 s on the same 135-memory install,
and the cost stays roughly flat as the memory count grows
because LanceDB writes a single fragment per `add(batch)` call
regardless of batch size. The bulk path also escapes apostrophes
in record ids defensively before splicing into the SQL predicate.

Tests:
[`tests/test_rag_store.py::BulkAddMemoriesTests`](../../../tests/test_rag_store.py)
covers new-rows, upsert-existing, mixed batches, the
`chunk_size` boundary, empty-content / missing-embedding skipping,
and id-with-apostrophe escaping.
[`tests/test_memory_migrate_bulk.py`](../../../tests/test_memory_migrate_bulk.py)
pins the migration shape: one `add_memories_bulk` call per
boot, `add_memory` never touched, no-embedding rows filtered out
before the bulk batch is built, `None` rag store is a no-op, and
a raised bulk exception returns 0 instead of crashing.

## P14. Heuristic tool-pass gate — skip the forced decision pass on banter turns

With any tools registered, every turn used to pay a full non-streaming `chat_with_tools` round-trip (`tool_choice="required"` + the `respond_directly` escape tool) before `chat_stream` even connected — 200 ms to several seconds of time-to-first-token, and the most common outcome on banter turns was "the model picked `respond_directly`". P14 puts a pure, embedding-free gate in front of the pass: [`tool_pass_gate.py`](../../../app/core/session/tool_pass_gate.py) (`should_run_tool_pass(user_text, registered_tool_names, context=GateContext) -> GateDecision`) consults per-tool-family regex signal tables (`time` / `web` / `recall` / `files` / `world` / `goals` / `tasks` + a generic action-request fallback), only for families that actually have registered tools this turn — disabling `tools.world` in config deactivates the room patterns. **Conservative by construction**: continuity signals always run the pass regardless of text — finished-task block in the prompt (preserves the `tool_choice="auto"` relaxation path), any task `running` / `awaiting_input` / `paused` (via the `tasks_active_provider` hook on [`task_orchestration_mixin._any_tasks_active`](../../../app/core/session/task_orchestration_mixin.py)), the previous turn dispatched a real tool (`_last_turn_dispatched_tool`, owned by `_maybe_run_tool_pass` so escape-only picks clear it), the one-shot MCP force flag, and **any registered tool whose name has no pattern family** (a future tool added without updating `_TOOL_FAMILY` degrades to the status quo instead of silently never being callable — add the family mapping + patterns when adding a tool). When the pass does run, its semantics are byte-identical to before (forced choice + escape tool untouched). Kill-switch: `agent.tool_pass_gate_enabled=false` restores always-run. Telemetry: `tool_gate=run:<reason>|skip:<reason>` + `tool_pass_ms=` on the `turn done:` INFO line, mirrored on MCP `get_last_response_detail`; per-decision `tool-gate:` INFO lines via `tail_logs(module_contains="tool_pass_gate")`. MCP: `get_tool_gate_state()` (switch, last decision, skip/run counters, rolling `avg_pass_ms`, `est_ms_saved`) and `force_tool_pass()` (one-shot bypass).

**Escalation ladder — if real-world tool recall regresses** (symptoms: `tool dispatch:` frequency drops after upgrade, "she said she'd check but never called the tool", `get_tool_gate_state.last_decision.reason == "no_signal"` on turns that should have run): first extend the family patterns in `tool_pass_gate.py` (cheapest), then flip the kill-switch while diagnosing. If the heuristic fundamentally can't hold, the next levers — in order — are: **option D, small-model router**: route the decision pass to `routes.worker_default` with a trimmed prompt on ambiguous turns, keeping the forced-choice semantics but paying a fast local call instead of the chat provider; **option B, speculative parallel stream**: run the tool pass and `chat_stream` concurrently and cancel the stream when a real tool fires — zero latency *and* zero recall regression at ~2× token cost on tool turns, but real surgery on the streaming path. Do NOT loosen the gate into a near-always-run state instead — that silently re-pays the full tax while keeping the complexity.

Tests: [`tests/test_tool_pass_gate.py`](../../../tests/test_tool_pass_gate.py) (continuity priority order, per-family signals, family gating by registered tools, unknown-tool degradation, skip paths, `as_event` shapes), `ToolPassGateTests` in [`tests/test_turn_runner_tool_pass.py`](../../../tests/test_turn_runner_tool_pass.py) (banter skip never calls `chat_with_tools`, tool-shaped run, kill-switch, tasks-active, one-shot force, multi-turn `last_turn_tool` continuity incl. escape-pick clearing, gate-state counters, `turn done:` log fields).

---

## P23. Context-compaction hardening + adaptive token estimator

Three classes of fix to make the context-squash path correct, robust, and observable.

**Bug fixes.** (1) The background compaction counter was stuck at `0`: `SummaryWorker._compactions_total` was only incremented inside the deprecated synchronous `compact_now`, which P20 removed from the hot path — so the now-default background `_maybe_summarize` loop never counted its runs. The increment moved into `_maybe_summarize` (on a successful `save_summary`), and `compact_now` no longer double-counts (it delegates). (2) The `prompt built:` DEBUG line mislabelled its history fields — it printed `history_msgs_in=<kept> history_msgs_out=<dropped>`. Now `history_msgs_in = len(history_msgs)` (messages supplied) and `history_msgs_out = kept_count` (messages that survived into the prompt), matching the field semantics in the debugging guide. (3) The projected-overflow branch in [`TurnRunner`](../../../app/core/session/turn_runner.py) gated the *aggressive reassembly* on `self._summary is not None`, so a build with no summary worker (worker disabled) sent the overflowing prompt to the model un-trimmed. The fit (aggressive reassembly) now always runs on overflow; only the background-compaction scheduling stays summary-worker-gated.

**Adaptive token estimator.** [`token_utils.py`](../../../app/llm/token_utils.py) `estimate_tokens` was a fixed `chars / 3.5` heuristic. It's now calibrated at runtime: after each streaming pass, `TurnRunner` calls `observe_actual_usage(prompt_chars, actual_prompt_tokens)` (subtracting per-message framing) which folds the observed chars/token into a slow EMA (`alpha=0.05`), clamped to `[2.5, 5.0]` and gated to plausible samples (≥400 chars, ≥50 tokens, in-band ratio). Models with very different tokenizers (code-heavy Qwen vs English-prose Llama) converge to their true ratio without a tokenizer dependency, so budget maths stay honest. `calibration_state()` / `reset_calibration()` back the MCP surface and tests.

**Deep hardening.** (a) A pathological single user message (a pasted log / file dump larger than the whole budget) is clipped via `clip_text_to_tokens` ([`prompt_support.py`](../../../app/core/session/prompt_support.py) — head-75%/tail-25% around a truncation marker) to `max(2048, budget − system − 512)` tokens, so it can never leave zero room for the system prompt + history. (b) The RAG memory block is token-budgeted to 30% of usable context (`_RAG_BLOCK_MAX_FRACTION`); since the retriever orders by score, a clip drops the weakest tail. (c) After the tool pass appends tool-result messages, `TurnRunner._retrim_messages_to_budget` drops the oldest raw history in place — preserving `messages[0]`, the current user message, and the entire tool exchange (so no dangling `tool_call` 400s the stream) — when the appended results push the prompt back over budget.

**Observability.** MCP `get_compaction_state()` dumps the current summary (present / size / messages-summarized), the compaction counter + last-run age, the unsummarized backlog, and the live `token_calibration` snapshot; `force_compaction()` runs an immediate synchronous pass (bar lowered to 2 unsummarized msgs). Tests: [`tests/test_token_utils.py`](../../../tests/test_token_utils.py) (EMA convergence, band clamp, sample gating, reset), `HardeningClipTests` in [`tests/test_prompt_assembler.py`](../../../tests/test_prompt_assembler.py) (user-message + RAG clip, normal message untouched), `ToolPassRetrimTests` + `test_overflow_refits_even_without_summary_worker` in [`tests/test_turn_runner_telemetry.py`](../../../tests/test_turn_runner_telemetry.py), and the existing `test_compact_now_*` in [`tests/test_summary_worker.py`](../../../tests/test_summary_worker.py) (counter still 1 per successful pass).

---
