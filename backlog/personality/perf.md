# Performance and observability

Reviewed 24 Oct 2026 against checkout 8c666cf. Only unfinished implementation, explicit validation
and deferred decisions are listed below. Existing tests were inspected, not rerun; source inspection
is not live/model/device verification. Completed slices and superseded proposals are in [the shipped
audit](shipped/audit-2026-10-24.md).

## P7. Typed-mode prefetch parity with voice

**Status: Open implementation.**

**Implemented / available:** Voice RAG prefetch and composing-presence frames exist.
The context builder skips memory retrieval for the turn when its shared query embedding
fails and RAG uses that same embedder. When RAG uses a different embedder, its fallback
still runs. The next turn can try the shared embedder again.

**21 Nov 2026 baseline:** The live prefetcher reported 50 lookup misses and zero submissions.
Across 50 recent completed turns, median context-region time (`rag_lookup_ms`) was 953 ms
and median time to first token was 1,846 ms. The context-region timer includes embedding,
retrieval and context selection. `context_stage_ms` now reports the cost of archive lookup,
prefetch, the shared query embedding, memory candidates, graph candidates, selection, and
rendering in the turn log and last-turn diagnostics. Compare ordinary turns before estimating
how much typed prefetch could save.

**Remaining:** Add bounded cancellable typed-draft prewarm with explicit privacy/opt-out behavior.
Composing status alone is not draft prefetch.

**Source check:** [rag_prefetcher.py](../../app/core/rag/rag_prefetcher.py),
[session_controller.py](../../app/core/session/session_controller.py),
[ChatView.tsx](../../web/src/features/chat/ChatView.tsx).

<a id="p11-reclaim-background-worker-num_predict-from-reasoning-leakage"></a>

## P11. Measure worker reasoning-token overhead on the actual route

**Status: Partial.**

**Implemented / available:** Worker routes and provider reasoning controls/telemetry exist; visible
thinking is filtered.

**Remaining:** Measure reasoning-token leakage on the actual worker model/provider and compare
native controls before prompt suffixes or model changes; the historic qwen route is not assumed
current.

[Implemented slice](shipped/audit-2026-10-24.md#perf-p11).

**Source check:** [relationship_pulse.py](../../app/core/relationship/relationship_pulse.py),
[curiosity_worker.py](../../app/core/proactive/curiosity_worker.py),
[promise_extractor.py](../../app/core/memory/promise_extractor.py),
[dream_worker.py](../../app/core/proactive/dream_worker.py),
[conversation_arc.py](../../app/core/conversation/conversation_arc.py),
[agenda.py](../../app/core/goals/agenda.py).

## P16. Post-turn inner-life blocks the brain loop

**Status: Partial.**

**Implemented / available:** `post_turn_ms` measures the inline cascade separately from reply
time. It now reports embedding provider calls, provider time and shared-request wait time.
Cue settlement requests a combined turn embedding only when a whole-turn cue has a vector to
compare. Multiple cues share that vector, and the gap resolver can supply it from its cache.

The 25 slow typed turns logged at INFO on 19–20 Nov 2026 had a median post-turn time of
456 ms. This sample includes only cascades at or above the logging threshold. Isolated tests
confirmed zero combined embedding requests for empty, reply-only and lexical-only cue pools,
and one shared request for two embedded whole-turn cues.

**Remaining:** Measure embedding time on ordinary turns and isolate other slow steps before
moving work off-thread. Preserve next-turn repair and answer state.

[Implemented slice](shipped/audit-2026-10-24.md#perf-p16).

**Source check:** [session_controller.py](../../app/core/session/session_controller.py),
[post_turn_mixin.py](../../app/core/session/post_turn_mixin.py),
[loop.py](../../app/core/brain/loop.py).

<a id="p24-voice-latency-batch-reaction-tag-tts-gate-double-stt-pass-first-chunk-threshold"></a>

## P24. Remaining speech latency contracts and benchmarks

**Status: Partial.**

**Implemented / available:** Initial STT partial-read/lifetime and explicit finalization repairs are
implemented; later speech-pipeline changes must be read in current source.

**Remaining:** Complete the benchmark and targeted contracts below: useful-audio onset, safe
endpointing, generation/audio-coverage identity, bounded cancellable finalization, first-chunk
release, incremental synthesis, buffering and interruption recovery. No live mic timing was measured
here.

[Implemented slice](shipped/audit-2026-10-24.md#perf-p24).

**Source check:** [realtime_stt_service.py](../../app/stt/realtime_stt_service.py),
[voice_capture_mixin.py](../../app/core/session/voice_capture_mixin.py),
[test_stt_lazy_load.py](../../tests/test_stt_lazy_load.py),
[turn_runner.py](../../app/core/session/turn_runner.py),
[AudioOutputManager.ts](../../web/src/audio/AudioOutputManager.ts),
[client_mic_source.py](../../app/audio/client_mic_source.py).

## P26. Lip-sync rides the server clock, not the playback clock

**Status: Partial.**

**Implemented / available:** The client playback analyser drives mobile audio-owner lip-sync.

**Remaining:** Extend the same path to desktop/Tauri audio owners, keep WS metering for non-owners
and retune smoothing with measured lag.

[Implemented slice](shipped/audit-2026-10-24.md#perf-p26).

**Source check:** [pocket_tts_service.py](../../app/tts/pocket_tts_service.py),
[AudioOutputManager.ts](../../web/src/audio/AudioOutputManager.ts),
[useAssistantSocket.ts](../../web/src/hooks/useAssistantSocket.ts),
[LipsyncChannel.ts](../../web/src/live2d/channels/LipsyncChannel.ts).

<a id="p30-raise--disable-the-memorymax_memories-cap"></a>

## P30. Memory mirror reload and growth costs

**Status: Partial.**

**Implemented / available:** Tier caps default to zero/no eviction, VectorIndex speeds search/dedupe
and ANN maintenance exists. SQLite rows stream into the mirror. Decay commits its
scores and time anchor together, then refreshes salience and revival scores in place
without rebuilding embeddings, indexes, or memory objects.

On a disposable corpus of 6,200 memories with 1,024-dimensional vectors, median
warm mirror load fell from 242 to 96 ms and traced peak load allocation from
130 to 104 MiB. Median decay time fell from 534 to 316 ms across nine timed
sweeps per build. These timings cover the SQLite mirror and decay, separately
from Lance reconciliation and application startup.

**Remaining:** Evaluate the resident mirror and LRU tradeoff as the corpus grows.

[Implemented slice](shipped/audit-2026-10-24.md#perf-p30).

**Source check:** [vector_index.py](../../app/core/memory/vector_index.py),
[memory_store.py](../../app/core/memory/memory_store.py),
[memory_settings.py](../../app/core/infra/memory_settings.py),
[rag_store.py](../../app/core/rag/rag_store.py).

## P31. Audit + trim the baseline system prompt (~25-30k resting floor)

**Status: Partial.**

**Implemented / available:** Per-block tier-weighted cost telemetry and earlier persona trim exist;
the historical claim that persona dominates cost was corrected.

**Remaining:** Remeasure the actual route/cache, then trim duplicate grammar, gate empty blocks and
audit tier hygiene where evidence warrants. P43/P52 own volatile-tail/context work.

[Implemented slice](shipped/audit-2026-10-24.md#perf-p31).

**Source check:** [prompt_assembler.py](../../app/core/session/prompt_assembler.py),
[inner_life_providers_mixin.py](../../app/core/session/inner_life_providers_mixin.py),
[prompt_support.py](../../app/core/session/prompt_support.py).

<a id="p32-concept-layer--worker-budget--unbounded-graph-growth"></a>

## P32. Concept maintenance and history growth

**Status: Partial.**

**Implemented / available:** Lifecycle work is bounded/batched and synthesis participates in demand
scheduling. Confidence sampling seeks the latest inserted event for each concept in a batch.
Trajectory and drift reads use the concept/time index to stop at their requested limits.

On 240,000 synthetic events, 240 confidence lookups took 3.55 → 0.28 ms and 60 drift
windows took 20.9 → 12.6 ms. Results matched the previous queries. The additional
index occupied 8.5 MiB on that corpus and took 270 ms to build once.

**Remaining:** Measure actual scheduler contention and graph growth; define snapshot/event thinning
and any remaining dirty-work refinements without discarding durable history. P34/P49 own retention
decisions.

[Implemented slice](shipped/audit-2026-10-24.md#perf-p32).

**Source check:** [idle_worker_scheduler.py](../../app/core/proactive/idle_worker_scheduler.py),
[concept_lifecycle_worker.py](../../app/core/concepts/concept_lifecycle_worker.py),
[concept_event_store.py](../../app/core/concepts/concept_event_store.py).

## P33. Inner-life providers walk the whole memory mirror every turn

**Status: Partial.**

**Implemented / available:** Kind-filtered iterators, lists, and counts read a
kind index over the memory mirror. Add, reclassification, deletion, pruning,
and reload keep it in sync. A snapshot of 155 reflections from 6,200 memories
fell from about 91 to 0.7 microseconds in an isolated benchmark.

Knowledge grounding now reuses the query norm and keeps only the strongest distinct
facts up to the prompt limit. It still scores both eligible kinds and preserves input
order for equal scores. With 5,000 synthetic 1,024-dimensional facts and a two-fact
limit, median ranking fell from 15.3 to 11.1 ms with identical output. Peak traced
ranking allocations fell from 510 to 1.8 KiB, excluding the input rows.

**Remaining:** Measure the similarity scan on ordinary questions as the learned corpus grows.

[Implemented slice](shipped/audit-2026-10-24.md#perf-p33).

**Source check:** [memory_store.py](../../app/core/memory/memory_store.py),
[inner_life_part3.py](../../app/core/session/inner_life_part3.py),
[inner_life_part2.py](../../app/core/session/inner_life_part2.py).

## P34. Unbounded tables: `messages`, Lance mirror, `concept_events`

**Status: Open implementation.**

**Implemented / available:** Transcript/session deletion and task/activity cleanup exist;
message/concept history remains durable.

**Remaining:** Decide retention separately for authoritative messages, the Lance mirror and
concept-event noise. Add reconciled index filtering/thinning only with explicit
preservation/deletion semantics.

**Source check:** [chat_database.py](../../app/core/infra/chat_database.py),
[message_indexer.py](../../app/core/rag/message_indexer.py),
[concept_event_store.py](../../app/core/concepts/concept_event_store.py),
[task_cleanup_worker.py](../../app/core/tasks/task_cleanup_worker.py).

## P43. 105 blocks, no arbitration -- replace the aggressive denylist

**Status: Partial.**

**Implemented / available:** Staged pooled admission, essential handling pilot and bounded persisted
content-free assembly/final-claim traces exist, plus offline hypothesis reporting.

**Remaining:** Cover eager providers, protect owed/authoritative blocks, allocate complete
evidence/handling bundles and record actual final inclusion/omissions before learning weights.
RS1/RS4/RS5 and G6 refine the remaining scope.

[Implemented slice](shipped/audit-2026-10-24.md#perf-p43).

**Source check:** [surfacing_trace_store.py](../../app/core/memory/surfacing_trace_store.py),
[hypothesis_surface_report.py](../../scripts/hypothesis_surface_report.py),
[prompt_assembler.py](../../app/core/session/prompt_assembler.py),
[prompt_support.py](../../app/core/session/prompt_support.py),
[context_budget_selector.py](../../app/core/session/context_budget_selector.py).

## P46. Parallel compute-lane drain

**Status: Deferred / evidence or capability gate.**

**Implemented / available:** Compute workers drain before LLM work but serially.

**Remaining:** Make shared WorldStore/ConceptStore/cache/SQLite state safe before parallel compute
drain; do not add concurrency solely to reduce queue counters.

[Implemented slice](shipped/audit-2026-10-24.md#perf-p46).

**Source check:** [idle_worker_scheduler.py](../../app/core/proactive/idle_worker_scheduler.py),
[world_store.py](../../app/core/world/world_store.py),
[concept_store.py](../../app/core/concepts/concept_store.py).

## P49. The telemetry ledgers are the fastest-growing tables

**Status: Partial.**

**Implemented / available:** Telemetry stores expose pruning APIs and bounded diagnostic receipts.
Semantic-floor replay evaluates all six default thresholds in one ledger query. On a disposable
120,000-row ledger, seven-day replay took 265 → 46 ms and lifetime replay took 88 → 28 ms
over nine warm trials per build. Both replay results matched the old query.

Cue reports share one grouped window read across reach and decline reasons. On 180,000
synthetic decisions, the seven-day report's grouped reads took 125 → 66 ms and lifetime
reads took 1,017 → 516 ms. Counts and rates matched the previous reports.

**Remaining:** Schedule appropriate ledger retention and fix remaining non-selective aggregates;
decide transcript/concept-history preservation separately. Bound disk without confusing diagnostics
with authoritative history.

**Source check:** [surfacing_outcome_store.py](../../app/core/memory/surfacing_outcome_store.py),
[cue_decision_store.py](../../app/core/memory/cue_decision_store.py),
[turn_prompt_block_store.py](../../app/core/memory/turn_prompt_block_store.py),
[rag_maintenance_worker.py](../../app/core/rag/rag_maintenance_worker.py).

<a id="p50-the-persona-hoist-has-no-cap-and-it-is-the-second-largest-block"></a>

## P50. Finish joint handling and content admission

**Status: Partial.**

**Implemented / available:** Conditional handling has a size budget and frequent emotion notes moved
into cached T0; essential handling is bundled for the staged pilot.

**Remaining:** Complete semantics-aware content/handling admission for other providers; decide
frequent pooled-note placement and avoid admitting unexplained cues. The cap itself is already
implemented (RS4).

[Implemented slice](shipped/audit-2026-10-24.md#perf-p50).

**Source check:**
[prompt_assembler_helpers_mixin.py](../../app/core/session/prompt_assembler_helpers_mixin.py),
[prompt_support.py](../../app/core/session/prompt_support.py),
[cue_accounting.py](../../app/core/proactive/cue_accounting.py),
[stance.py](../../app/core/conversation/stance.py).

<a id="p52-t3-spends-85k-charsturn-on-30-concept-lines-it-never-repeats"></a>

## P52. Concept volume and topic-aware continuity

**Status: Partial.**

**Implemented / available:** concept_core_overfetch controls carry-over; the turnover fix is implemented.

**Remaining:** Evaluate smaller concept caps and topic-aware carry-over with quality/token/cache
controls. Historical 29.6-line/8.5k-char figures are snapshots, not current measurements; RS3/RS7
own follow-up experiments.

[Implemented slice](shipped/audit-2026-10-24.md#perf-p52).

**Source check:** [inner_life_part1.py](../../app/core/session/inner_life_part1.py),
[concept_surfacing.py](../../app/core/concepts/concept_surfacing.py),
[context_budget_selector.py](../../app/core/session/context_budget_selector.py).

## P53. Reconcile the memory search index without rewriting it on every boot

**Status: Partial.** Startup compares the LanceDB memory projection against
SQLite in batches and writes only missing or changed rows. An unchanged index
keeps its version and files. An empty authoritative store clears orphaned
index entries, while an unreadable mirror skips reconciliation. Ordinary
recall withholds IDs missing from a successful canonical join and uses current
SQLite wording for changed notes. Isolated Lance fixtures cover a reopened
index with unchanged, edited, missing, and orphaned rows at the same row count.
[Storage contract](../../docs/memory-tiers.md#in-memory-the-mirror-and-the-matrix-beside-it).

Exact comparison now reads unchanged vectors as float32 arrays directly from Arrow.
On a reopened disposable index with 6,200 memories and 1,024-dimensional vectors,
warm reconciliation took **1,318 → 59 ms** across seven timed runs per build.
Both versions wrote zero rows and added zero bytes across the unchanged runs.
Both also converged after an edit and an orphan removal.

**Remaining:** Measure complete startup under ordinary cold and warm conditions.
P30 owns the separate SQLite mirror load cost.

## P54. Share an embedding request between simultaneous cache misses

**Status: Partial.** Concurrent misses for one key share a provider request.
Different keys still run independently. The provider sets the request timeout,
and a waiting caller uses that same timeout for its join. A failure releases
waiters and the next call can retry. Cache clearing detaches old work so it
cannot replace a fresh result. Each provider route uses its own embedder.
Ordinary-turn telemetry exposes join count and wait time separately from HTTP
calls. [Telemetry contract](../../docs/prompt-surfacing.md#what-a-surfacing-measurement-means).

**Remaining:** Use ordinary-chat telemetry to measure how often requests
overlap and how much turn time is spent waiting on shared work.

## P55. Delay full topic record formatting until the page is selected

**Status: Partial.** Topic paging batch-fetches members when the store
supports it, then ranks lightweight source state and revisions. Repeated
message reads share a request-local cache. Only selected rows build full
records, using fresh source reads. A selected source change prompts a
restart. Tests cover ordering, historical reads and revision-bound pages,
including 50-, 500-, and 2,000-member fixtures with eight records built per
page. [Read contract](../../docs/fast-brain-tool-improvements.md#topic-page-cost).

An isolated comparison with a shared source message and stub stores measured
these median times for the first and second eight-result pages (five runs
per case). Source reads and full records are totals across both pages.

| Members | Source reads, before → after | Full records, before → after | First / next ms, before → after |
| ---: | ---: | ---: | --- |
| 50 | 232 → 34 | 116 → 16 | 1.5 / 1.5 → 1.2 / 1.2 |
| 500 | 2,032 → 34 | 1,016 → 16 | 15.5 / 13.8 → 7.3 / 8.2 |
| 2,000 | 8,032 → 34 | 4,016 → 16 | 52.6 / 53.4 → 32.3 / 31.4 |

**Remaining:** Measure first-page and continuation latency on ordinary-use
clusters with diverse source messages.

## P24a. Repair the STT partial-read and recorder-lifetime contracts

**Status: partial.**

**Implemented / available:** Callback-fed nonblocking partial_text and per-service shutdown/phrase
reuse exist.

**Remaining:** Add phrase/generation/revision/audio-coverage identity, bound/cancel finalization and
exercise a real recorder on isolated synthetic audio plus microphone checks.

**Source check:** [realtime_stt_service.py](../../app/stt/realtime_stt_service.py),
[voice_capture_mixin.py](../../app/core/session/voice_capture_mixin.py),
[turn_runner.py](../../app/core/session/turn_runner.py),
[session_text_utils.py](../../app/core/session/session_text_utils.py),
[pocket_tts_service.py](../../app/tts/pocket_tts_service.py),
[AudioOutputManager.ts](../../web/src/audio/AudioOutputManager.ts).

## P24b. Measure end of user speech to useful audio, not just TTFT

**Status: open.**

**Implemented / available:** Server stage timings and client clipStart/firstPcm diagnostics exist.

**Remaining:** Join input end, endpoint, final transcript, brain/tool/prompt, first speakable chunk,
synthesis/PCM and scheduled onset by IDs. Respect clock mapping, count failures/underruns and
compare p50/p95 useful-answer onset rather than filler.

**Source check:** [realtime_stt_service.py](../../app/stt/realtime_stt_service.py),
[voice_capture_mixin.py](../../app/core/session/voice_capture_mixin.py),
[turn_runner.py](../../app/core/session/turn_runner.py),
[session_text_utils.py](../../app/core/session/session_text_utils.py),
[pocket_tts_service.py](../../app/tts/pocket_tts_service.py),
[AudioOutputManager.ts](../../web/src/audio/AudioOutputManager.ts).

## P24c. Make endpointing responsive without cutting off thinking pauses

**Status: partial.**

**Implemented / available:** Tiered endpointing exists; capture still uses a 1.5-second
minimum-start window and 100 ms blocks.

**Remaining:** Separate voiced evidence from elapsed capture time, derive thresholds from frame
duration and replay short answers/negations/hesitations/noise against premature cuts, not just
faster endpoint time.

**Source check:** [realtime_stt_service.py](../../app/stt/realtime_stt_service.py),
[voice_capture_mixin.py](../../app/core/session/voice_capture_mixin.py),
[turn_runner.py](../../app/core/session/turn_runner.py),
[session_text_utils.py](../../app/core/session/session_text_utils.py),
[pocket_tts_service.py](../../app/tts/pocket_tts_service.py),
[AudioOutputManager.ts](../../web/src/audio/AudioOutputManager.ts).

## P24d. Finalize the already-fed audio once; keep speculation reversible

**Status: partial.**

**Implemented / available:** WAV finalization calls perform_final_transcription on supplied samples
without a second VAD feed; partials drive existing speculative retrieval.

**Remaining:** Validate real-recorder two-phrase/PTT/concurrent-interim cancellation and cache
freshness. In-memory finalization and speculative drafting remain experiments; provisional audio
must not execute tools, consume cues, write memory or speak.

**Source check:** [realtime_stt_service.py](../../app/stt/realtime_stt_service.py),
[voice_capture_mixin.py](../../app/core/session/voice_capture_mixin.py),
[turn_runner.py](../../app/core/session/turn_runner.py),
[session_text_utils.py](../../app/core/session/session_text_utils.py),
[pocket_tts_service.py](../../app/tts/pocket_tts_service.py),
[AudioOutputManager.ts](../../web/src/audio/AudioOutputManager.ts).

## P24e. Release a useful first speech chunk before the whole answer

**Status: partial.**

**Implemented / available:** Sentence streaming, late/no-reaction completion fallback and an
explicit pause-beat short-sentence boundary exist.

**Remaining:** The normal streaming branch still requires a parsed mood; add safe provisional
readiness and a bounded complete-first-chunk policy without leaking tags or truncating
numbers/filenames/negation. Preserve shared offsets and cancellation.

**Source check:** [realtime_stt_service.py](../../app/stt/realtime_stt_service.py),
[voice_capture_mixin.py](../../app/core/session/voice_capture_mixin.py),
[turn_runner.py](../../app/core/session/turn_runner.py),
[session_text_utils.py](../../app/core/session/session_text_utils.py),
[pocket_tts_service.py](../../app/tts/pocket_tts_service.py),
[AudioOutputManager.ts](../../web/src/audio/AudioOutputManager.ts).

## P24f. Stream synthesis, not merely a completed waveform

**Status: open.**

**Implemented / available:** ClipCache/SynthesisGate prefetch and paced PCM playback exist; both
engines still synthesize completed clips before playback.

**Remaining:** Implement incremental synthesis with first-PCM/error/cancel semantics while
preserving cache/gate, gain/brightness/tempo and pacing parity; emitted chunks of a finished
waveform are not incremental generation.

**Source check:** [realtime_stt_service.py](../../app/stt/realtime_stt_service.py),
[voice_capture_mixin.py](../../app/core/session/voice_capture_mixin.py),
[turn_runner.py](../../app/core/session/turn_runner.py),
[session_text_utils.py](../../app/core/session/session_text_utils.py),
[pocket_tts_service.py](../../app/tts/pocket_tts_service.py),
[AudioOutputManager.ts](../../web/src/audio/AudioOutputManager.ts).

## P24g. Tune client buffering only with underrun evidence

**Status: validation.**

**Implemented / available:** AudioOutputManager schedules clips with a 0.15-second first-idle margin
and analyser/ownership support.

**Remaining:** Measure onset/underruns/device jitter before reducing or adapting buffering; test
subsequent chained clips separately from the idle first clip.

**Source check:** [realtime_stt_service.py](../../app/stt/realtime_stt_service.py),
[voice_capture_mixin.py](../../app/core/session/voice_capture_mixin.py),
[turn_runner.py](../../app/core/session/turn_runner.py),
[session_text_utils.py](../../app/core/session/session_text_utils.py),
[pocket_tts_service.py](../../app/tts/pocket_tts_service.py),
[AudioOutputManager.ts](../../web/src/audio/AudioOutputManager.ts).

## P24h. Reduce recovery time after interruption, not just reply onset

**Status: partial.**

**Implemented / available:** Energy barge-in, overlap capture, playback draining and client audio
flush exist.

**Remaining:** Measure cancellation through mic/STT/model/TTS/sidecar/client and new-turn recovery;
exercise false interrupts and overlap. Full duplex/AEC remains immersion H7c.

**Source check:** [realtime_stt_service.py](../../app/stt/realtime_stt_service.py),
[voice_capture_mixin.py](../../app/core/session/voice_capture_mixin.py),
[turn_runner.py](../../app/core/session/turn_runner.py),
[session_text_utils.py](../../app/core/session/session_text_utils.py),
[pocket_tts_service.py](../../app/tts/pocket_tts_service.py),
[AudioOutputManager.ts](../../web/src/audio/AudioOutputManager.ts).

## P24i. Benchmark model/prompt cost after exposing the actual waits

**Status: validation.**

**Implemented / available:** Tool-pass gating, route metrics and per-block/cache telemetry exist.

**Remaining:** Benchmark warm/cold, tool/no-tool, short/long reply and cache conditions after stage
tracing. Keep answer quality and interruption outcomes alongside latency; no automatic faster-model
claim.

**Source check:** [realtime_stt_service.py](../../app/stt/realtime_stt_service.py),
[voice_capture_mixin.py](../../app/core/session/voice_capture_mixin.py),
[turn_runner.py](../../app/core/session/turn_runner.py),
[session_text_utils.py](../../app/core/session/session_text_utils.py),
[pocket_tts_service.py](../../app/tts/pocket_tts_service.py),
[AudioOutputManager.ts](../../web/src/audio/AudioOutputManager.ts).

## Moved out of the active queue

- **P35** — shipped: [record](shipped/audit-2026-10-24.md#perf-p35).
- **P36** — shipped: [record](shipped/audit-2026-10-24.md#perf-p36).
- **P37** — shipped: [record](shipped/audit-2026-10-24.md#perf-p37).
- **P38** — shipped: [record](shipped/audit-2026-10-24.md#perf-p38).
- <a id="p39-concept-snapshot--quality-report-n1-the-evidence-edges"></a>**P39** — shipped:
  [record](shipped/perf.md#p39-batched-concept-evidence).
- **P42** — retired: [record](shipped/audit-2026-10-24.md#perf-p42).
- **P45** — shipped: [record](shipped/audit-2026-10-24.md#perf-p45).
- **P51** — retired: [record](shipped/audit-2026-10-24.md#perf-p51).
