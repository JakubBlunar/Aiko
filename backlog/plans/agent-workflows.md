# Agentic workflow follow-ups

Source status checked 23 November 2026. The current behavior is documented in
[the agent harness](../../docs/agent-harness.md); the older
[7 November plan](agent-harness-next-steps-2026-11-07.md) and
[Hermes comparison](../reviews/hermes-harness-review-2026-11-08.md) retain their
historical findings, not a second open implementation checklist.

## Implemented foundations

| Earlier proposal | Current source status |
| --- | --- |
| Checkpointed job contract and bounded continuation | Saved root state, child adoption, epochs, cumulative allowance and restart recovery |
| Durable result reporting | `WorkReports` outbox, stable milestone identity, source-bound drafts and atomic assistant-message/report commitment |
| Main-chat steering | `control_work` supports `guide`; the existing task retains its identity, evidence and counters |
| Original request retention | Full request, matched source-message ID and bounded, revision-bound earlier user passages selected at handoff |
| Local task speech | Workflow-route composition and grounding review, learned speaking guide and persona fallback |
| Recoverable source text | Owned SQLite artifacts, quotas, source/rendered/projection views and result inspection |
| Compound plugin operations | Typed bounded procedures, pinned discovery schemas and optional playbooks |
| Procedural reuse | Compute-only learning from independent supported requests, with correction/source checks |
| Scoped source pilot | Disabled-by-default `self_code` plugin with named host workspaces and read-only inspection |
| Plugin settings lifecycle | Runtime enable/disable/apply, selective ownership, desired/applied state and generation revocation |
| Objective outcomes | Shared versioned outcome reader across API, UI, work checks, reporting, promise resolution and learning; execution completion alone does not certify the objective |
| Completion proof contracts | Owned source envelopes, host-selected outcome index, indexed answer-part/criterion review, exact source quotes and revision-bound certificates |
| Typed effect verification | Generic SDK postcondition data and exact text-note write/read-back adapter; acknowledged submission remains distinct from final effect |
| Workflow reasoning | Bounded reasoning in planning, repair, verification, speech and discussion; existing configured context retained |

These are implementation facts. They do not close the ordinary-use checks below.

## Remaining product and platform work

The [23 November output-correctness review](../reviews/workflow-output-correctness-2026-11-23.md)
records the fixes for search evidence, submission receipts and repeated reads.
The configured 9B route passed six isolated completion checks at 64K, including
an hours-only partial and two accurate quotations that missed the objective.
The next priority is the shared flow from the user's request through evidence,
completion, delivery and follow-up. Exercise it with unrelated companion tasks:
summarize a document, reconcile a calendar change, or answer a question using
two sources. The workshop comparison provides one regression case. For each
task, change one source or remove it and check whether Aiko still gives the
supported part, asks a useful question, or can explain where an answer came from.
Track false completions, unnecessary abstentions and inference calls per delivered answer.

A new capability earns its place when it gives Aiko access to something she
cannot currently observe or do. A reusable procedure can also earn its place
when several different requests need the same reliable sequence. For example,
looking up a calendar entry can serve many goals, while deciding which of two
options fits a user's constraints exercises the general workflow.

The 15 November correctness review
reproduced unsupported approvals through the production review functions with
the configured local 9B model. Three isolated full-harness trials did not falsely
succeed, but exposed missing-evidence-ID retries and missing verification tools.
The resulting contracts above are now implemented in source. Planner finish
examples include evidence IDs, missing/foreign IDs receive a targeted repair,
and malformed/truncated/contradictory reviews cannot issue certificates. Pending
reports bind source revisions and use certified facts; partial results and input
questions avoid unnecessary narration calls. Empty native/fast search results
cannot certify a factual absence. See the current guides for the exact behavior.

Still open: typed adapters for additional noncoding capabilities, semantic
calibration when sources lack structured target identity, and ordinary-use
acceptance. The local reasoning probe still misjudged target identity and empty
search scope before their structured host guards were applied. Matching source
quotations alone do not establish semantic entailment. The final bounded
acceptance probe passed three positive controls and stopped both structured
negative cases, which is diagnostic evidence rather than a broad reliability
measurement. Backend restart and frontend refresh are required to activate the
source changes in an existing app process.

1. **Evidence-aware work discussion.** A source question can select a passage
   from a delivered report and recover the quotations used to approve its wording.
   Test whether ordinary chat reliably selects the right passage from an indirect
   follow-up. General factual questions still rely on bounded saved excerpts.
2. **Referenced conversation context.** The start tool can bind short passages
   from earlier user messages. Test corrections across messages, attachment
   identity and an ambiguous reference resolved after a worker has asked for help.
3. **Platform extensions.** Job-specific resource grants and locks, provider
   idempotency/read-back reconciliation, cross-process leases, media adapters,
   SDK migrations and parallel children remain in the
   [plugin/job roadmap](../personality/plugin-system.md). Implemented text-report
   durability does not establish those broader contracts.

## Ordinary-use acceptance still needed

The [12 November review](../reviews/ordinary-use-review-2026-11-12.md) found only
one recent workflow row. On 22 November a read-only check found four retained
root workflows. The newest started 9 November, and no root or child task had
been created since 15 November. Resume the post-fix review when a new ordinary
workflow supplies a plan, source evidence and a report to inspect.
[Current completion contract](../../docs/agent-harness.md#evidence-and-completion)

Use existing permitted resources and record task IDs and
observations without committing personal dialogue:

- Start a read-only comparison from main chat; inspect whether the plan and
  final answer address the requested criteria with actual owned evidence.
- Guide that same task from chat and the drawer while planning or waiting;
  verify early constraints survive and no duplicate job appears.
- Ask about saved work, including a question whose answer lies beyond the
  discussion excerpts; missing information must remain explicit.
- Pause/restart between reads and around result/report commitment. Confirm
  manual pauses, usage and evidence survive and committed text is not replayed.
- Exercise one normal approval wait and a configuration change before dispatch.
  Use disposable resources for any mutation or uncertain-effect check.
- Observe meaningful progress and final reporting in typed and voice modes,
  including shared-model waits and conversation latency. Check the configured
  workflow route and absence of an automatic main-model reporting rewrite.
- Check desktop docking/overlay, mobile layout, retained drafts and source paging.

Scripted model/tool tests establish state and ownership contracts. Naturalness,
small-model reliability, real browser success and cost/latency improvements need
separate observation; no acceptance pass is implied by this documentation cleanup.
