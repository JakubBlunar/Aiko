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

## G-CLEANUP. `consolidator_state.last_cluster_index` is not dead weight — do not drop it

**Status: Open implementation.**

**Implemented / available:** RelationshipPulse uses last_cluster_index as its last-pulse turn count.

**Remaining:** Move pulse state to a dedicated key or migrate both users to a correctly named field;
correct the misleading consolidator comment. Do not delete a column with a real reader.

**Source check:** [memory_consolidator.py](../../app/core/memory/memory_consolidator.py),
[relationship_pulse.py](../../app/core/relationship/relationship_pulse.py).

## Moved out of the active queue

- **G4** — shipped: [record](shipped/audit-2026-10-24.md#workers-g4).
