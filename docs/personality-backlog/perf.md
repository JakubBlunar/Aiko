# Performance and observability

Reviewed 24 Oct 2026 against checkout 8c666cf. Only unfinished implementation, explicit validation
and deferred decisions are listed below. Existing tests were inspected, not rerun; source inspection
is not live/model/device verification. Completed slices and superseded proposals are in [the shipped
audit](shipped/audit-2026-10-24.md).

## P7. Typed-mode prefetch parity with voice

**Status: Open implementation.**

**Implemented / available:** Voice RAG prefetch and composing-presence frames exist.

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

**Implemented / available:** post_turn_ms measures the inline cascade separately from reply total time.

**Remaining:** Collect current cost/ordering evidence, then split only safe eventual work with
stale-turn guards; next-turn repair/answer state must remain timely.

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
and ANN maintenance exists.

**Remaining:** Remove decay's full mirror reload if measured worthwhile; stream reload instead of
fetchall and evaluate mirror memory/LRU architecture. SQLite query speed was not the original
bottleneck.

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
scheduling; the old interval-only readiness claim is stale.

**Remaining:** Measure actual scheduler contention and graph growth; define snapshot/event thinning
and any remaining dirty-work refinements without discarding durable history. P34/P49 own retention
decisions.

[Implemented slice](shipped/audit-2026-10-24.md#perf-p32).

**Source check:** [idle_worker_scheduler.py](../../app/core/proactive/idle_worker_scheduler.py),
[concept_lifecycle_worker.py](../../app/core/concepts/concept_lifecycle_worker.py),
[concept_event_store.py](../../app/core/concepts/concept_event_store.py).

## P33. Inner-life providers walk the whole memory mirror every turn

**Status: Partial.**

**Implemented / available:** Kind filtering precedes sorting in list_top/list_recent, with
correctness regressions.

**Remaining:** Index genuinely unbounded iter_by_kind walks and replace whole-knowledge
concatenation with bounded retrieval; keep add/update/delete/reload coherence.

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

## P39. Concept snapshot + quality report N+1 the evidence edges

**Status: Open implementation.**

**Implemented / available:** Snapshot pagination and on-demand quality loading bound N; evidence
resolution remains N+1.

**Remaining:** Add batched evidence lookup and measure the quality report's pairwise embedding cost
before optimizing it.

**Source check:** [concept_snapshot.py](../../app/core/concepts/concept_snapshot.py),
[concept_quality.py](../../app/core/concepts/concept_quality.py),
[concept_store.py](../../app/core/concepts/concept_store.py).

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

**Status: Open implementation.**

**Implemented / available:** Telemetry stores expose pruning APIs and bounded diagnostic receipts now exist.

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
- **P42** — retired: [record](shipped/audit-2026-10-24.md#perf-p42).
- **P45** — shipped: [record](shipped/audit-2026-10-24.md#perf-p45).
- **P51** — retired: [record](shipped/audit-2026-10-24.md#perf-p51).
