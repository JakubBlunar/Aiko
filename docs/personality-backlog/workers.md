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

<a id="next-producer-quality-candidates-source-review-6-nov-2026"></a>

### Reflective producer quality (implemented 6 Nov 2026)

**Status: Implemented; ordinary-use validation waiting.**
[Implementation and limits](../reflective-producer-quality.md). Activation requires
another backend restart; do not score older rows as output from these changes.

- Goal reflections now receive bounded relevant source records and deliberate
  notes, distinguish reflection/intention/reported step/outcome, skip unchanged
  context, and retain input revisions. Novelty checks preserve changed conditions
  and negation. Existing generated notes do not become new observations.
- Shared moments now retain narrow clause citations and an explicit exchange,
  recalled-event or virtual scope. Unknown occurrence dates remain unknown;
  recording time alone cannot create an anniversary. Source edits defer writes,
  and deliberate revisions preserve old receipts as history.
- Dreams now receive whole qualified memory rows with input identities/revisions.
  Legacy text stays explicitly identity-unknown. Input edits defer output; derived
  context supplies no additional observation roots or confidence votes.

**Remaining:** Review usefulness and abstention in fresh normal use, including
missed meaningful goal changes, repetitive imagery, genuine outcome retention,
shared-moment source/time accuracy and preserved dream exceptions. The concrete
checklist remains under [L22](concepts.md#l22-follow-up-post-restart-memory-quality-cohort-6-nov-2026).
No worker was forced or live historical memory rewritten during implementation.

Source checks: [goal_reflection_evidence.py](../../app/core/goals/goal_reflection_evidence.py),
[goal_worker.py](../../app/core/goals/goal_worker.py),
[moment_evidence.py](../../app/core/relationship/moment_evidence.py),
[shared_moment_extractor.py](../../app/core/relationship/shared_moment_extractor.py),
[reflective_evidence.py](../../app/core/memory/reflective_evidence.py),
[dream_worker.py](../../app/core/proactive/dream_worker.py).

## Remaining memory boundaries (reviewed 6 Nov 2026)

**Status: Proposed; not implemented.** The read-only
[boundary audit](../memory-boundary-audit-2026-11-06.md) found six remaining areas:

1. Preserve SQLite subject, source mode and virtual/external domain through
   direct and associative recall; kind-only headings currently lose them.
2. Keep hypothesis answer memories bounded to the supported reply and retain
   support receipts through world graduation, with honest answer/event timing.
3. Separate image sharing time from depicted time and retain caption-message
   provenance, subject and uncertainty.
4. Preserve manual gap-answer context and distinguish successful saving from
   deliberate dismissal or a failed write.
5. Retain virtual pursuit episode/research receipts and qualified time scope.
6. Retain catchphrase occurrences and first-observed authorship scope, plus both
   sides and verified timing for a newly shared joke.

The audit includes acceptance scenarios and historical examples. Those examples
are not a fresh post-restart quality measurement; L22's existing review remains
pending. No live memory was modified.

## G-CLEANUP. `consolidator_state.last_cluster_index` is not dead weight — do not drop it

**Status: Open implementation.**

**Implemented / available:** RelationshipPulse uses last_cluster_index as its last-pulse turn count.

**Remaining:** Move pulse state to a dedicated key or migrate both users to a correctly named field;
correct the misleading consolidator comment. Do not delete a column with a real reader.

**Source check:** [memory_consolidator.py](../../app/core/memory/memory_consolidator.py),
[relationship_pulse.py](../../app/core/relationship/relationship_pulse.py).

## Moved out of the active queue

- **G4** — shipped: [record](shipped/audit-2026-10-24.md#workers-g4).
