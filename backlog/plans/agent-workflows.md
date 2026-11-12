# Agentic workflow follow-ups

Source status checked 12 November 2026. The current behavior is documented in
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
| Original request retention | Full active request plus matched source-message ID; all saved guidance is available to context selection |
| Local task speech | Workflow-route composition and grounding review, learned speaking guide and persona fallback |
| Recoverable source text | Owned SQLite artifacts, quotas, source/rendered/projection views and result inspection |
| Compound plugin operations | Typed bounded procedures, pinned discovery schemas and optional playbooks |
| Procedural reuse | Compute-only learning from independent supported requests, with correction/source checks |
| Scoped source pilot | Disabled-by-default `self_code` plugin with named host workspaces and read-only inspection |
| Plugin settings lifecycle | Runtime enable/disable/apply, selective ownership, desired/applied state and generation revocation |

These are implementation facts. They do not close the ordinary-use checks below.

## Remaining product and platform work

1. **Evidence-aware work discussion.** Discussion is read-only and sees bounded
   saved excerpts (root result prefix and recent children); it cannot retrieve
   an omitted passage itself. Add bounded, owned result selection without
   authorizing external operations from the discussion lane.
2. **Referenced conversation context.** The start tool retains the active request
   and its matched message ID. It does not assemble every earlier reference or
   attachment into a revision-bound handoff packet. Preserve actor, source time
   and ownership if extending this contract; avoid duplicating the whole chat.
3. **Platform extensions.** Job-specific resource grants and locks, provider
   idempotency/read-back reconciliation, cross-process leases, media adapters,
   SDK migrations and parallel children remain in the
   [plugin/job roadmap](../personality/plugin-system.md). Implemented text-report
   durability does not establish those broader contracts.

## Ordinary-use acceptance still needed

The [12 November review](../reviews/ordinary-use-review-2026-11-12.md) found only
one recent workflow row. That cohort cannot validate representative work or
delivery quality. Use existing permitted resources and record task IDs and
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
