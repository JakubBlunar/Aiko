# Background workers

Reviewed 24 Oct 2026 against checkout 8c666cf. Only unfinished implementation, explicit validation
and deferred decisions are listed below. Existing tests were inspected, not rerun; source inspection
is not live/model/device verification. Completed slices and superseded proposals are in [the shipped
audit](shipped/audit-2026-10-24.md).

## G5. Self-tuning cue cooldowns

**Status: Open implementation.**

**Implemented / available:** Reach/decline reports, demand scheduling, inventory and cooldowns exist.

**Remaining:** Validate an attributable usefulness signal and armed denominators before bounded
opt-in cooldown adaptation. Do not tune away topic mismatch or use copied turn engagement as item
reward.

**Source check:** [cue_accounting.py](../../app/core/proactive/cue_accounting.py),
[cue_decision_store.py](../../app/core/memory/cue_decision_store.py).

## G6. Per-provider decline attribution

**Status: Partial.**

**Implemented / available:** Structural and several provider-specific declines are named; bounded
assembly receipts distinguish provider/error states and final claims.

**Remaining:** Instrument remaining generic provider bailouts by failing family; preserve
supply-versus-gate and error-versus-silence distinctions. Accurate final inclusion precedes learned
selection.

[Implemented slice](shipped/audit-2026-10-24.md#workers-g6).

**Source check:** [surfacing_trace_store.py](../../app/core/memory/surfacing_trace_store.py),
[hypothesis_surface_report.py](../../scripts/hypothesis_surface_report.py).

<a id="g7-worker-prompts-have-no-input-token-accounting"></a>

## G7. Worker prompt-budget adoption beyond the first three writers

**Status: Partial.**

**Implemented / available:** Shared whole-request budgeting is adopted by summary, memory extraction
and belief inference; cursors advance only through included messages and omissions are reported.

**Remaining:** Roll out to other worker builders with their actual route/output limits and
worker-owned reduction rules. The helper and summary cursor repair are already implemented.

[Implemented slice](shipped/audit-2026-10-24.md#workers-g7).

**Source check:** [worker_prompt_budget.py](../../app/core/proactive/worker_prompt_budget.py),
[summary_worker.py](../../app/core/proactive/summary_worker.py),
[belief_worker.py](../../app/core/relationship/belief_worker.py),
[test_summary_worker.py](../../tests/test_summary_worker.py).

## Memory/time quality: ordinary-use follow-up

**Status: Waiting for post-restart data (6 Nov 2026).** The concrete review
checklist lives under [L22's post-restart memory cohort](concepts.md#l22-follow-up-post-restart-memory-quality-cohort-6-nov-2026).
Check fresh worker output against its actual sources after normal use, including
missed important memories, and keep historical issues separate. Do not count a
healthy startup or a sparse worker cohort as semantic quality validation.

### Next producer-quality candidates (source review, 6 Nov 2026)

These are proposed improvements, not implemented fixes or new post-restart
quality scores. The earlier producer inventory is historical; inspect current
owners before carrying one of its findings forward.

1. **Goal reflections: ground novelty in something beyond earlier reflections.**
   `GoalWorker._run_reflection` supplies a goal and its earlier generated notes,
   without fresh conversation/action/result evidence. `GoalStore.add_progress`
   stores worker notes long-term at confidence 0.8 with insert dedupe bypassed.
   Recent stored examples repeat similar quiet/hum/continuity imagery across
   different goals. Preserve Aiko's subjective voice and deliberate notes;
   distinguish a changed intention, a reflection and a reported step/outcome.
   Supply small relevant source packets when available, retain derivation IDs,
   and permit no new entry when there is no meaningful change. Rewording the
   same sentiment must not count as observed progress.
2. **Shared moments: separate supporting exchange from event time.** The current
   detector retains a `context_window` receipt for the offered messages, then
   assigns `when` from the last offered timestamp. That does not establish which
   lines support each clause or when a remembered earlier event occurred. Add
   narrow supporting citations and explicit reported-event versus recording
   time, with unknown occurrence time allowed. Preserve shared virtual and
   interpersonal meaning; do not turn a broad window into proof of every detail.
   See [shared moments](moments.md).
3. **Dream derivations: retain input identity and whole qualifications.** The
   current input seam already age-tags memories, but renders content prefixes
   into strings; the stored dream retains a sleep episode ID without the actual
   input-memory revisions. Keep bounded source identities/revisions, preserve
   conditions through packet packing, and label the output as associative
   reflection. Reprocessing old material must not become a fresh observation.
   This is traceability for creative inner life, not a requirement for literal
   reporting or a new confidence formula.

Source checks: [goal_worker.py](../../app/core/goals/goal_worker.py),
[goal_store.py](../../app/core/goals/goal_store.py),
[shared_moment_extractor.py](../../app/core/relationship/shared_moment_extractor.py),
[chat_turn_mixin.py](../../app/core/session/chat_turn_mixin.py),
[dream_worker.py](../../app/core/proactive/dream_worker.py).

## G-CLEANUP. `consolidator_state.last_cluster_index` is not dead weight — do not drop it

**Status: Open implementation.**

**Implemented / available:** RelationshipPulse uses last_cluster_index as its last-pulse turn count.

**Remaining:** Move pulse state to a dedicated key or migrate both users to a correctly named field;
correct the misleading consolidator comment. Do not delete a column with a real reader.

**Source check:** [memory_consolidator.py](../../app/core/memory/memory_consolidator.py),
[relationship_pulse.py](../../app/core/relationship/relationship_pulse.py).

## Moved out of the active queue

- **G4** — shipped: [record](shipped/audit-2026-10-24.md#workers-g4).
