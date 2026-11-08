# Harness follow-up: reliability, conversational control and useful capabilities

Source review on 2026-11-07, after `4d342da1` and `d9c553b0`. This is a proposed
follow-up sequence, not an implementation or a live-Aiko test result. The current
[implementation boundary](agent-harness.md) still applies. No app restart, model
call, production mutation or overnight job is needed for this assessment.

Update: the [source code plugin](../plugins/self_code/README.md) now prepares the
read-only pilot through the existing SDK: explicit source scope, bounded editor
tools and syntax adapters. It is disabled by default. This does not complete the
broader P3 grants system, semantic language-server work or live-model validation.

The [2026-11-08 Hermes comparison](hermes-harness-review-2026-11-08.md) adds a
prioritized proposal for worker-authored speech using Aiko's style/concepts,
stable task prompts, recoverable outputs and bounded plugin procedures.

## Recommended order

| Slice | Practical improvement | Can develop without live testing? |
| --- | --- | --- |
| 1. Durable result reporting | Finished work remains reportable after a crash or failed reply | Yes, with injected crashes and disposable SQLite |
| 2. Conversation-to-job steering and context | Aiko can refine the same job from ordinary conversation without losing earlier constraints | Yes, with scripted conversations and owned-job fixtures |
| 3. Evidence-aware work discussion | Questions in the drawer can inspect the relevant saved passage and show its source | Yes, with large and changing result fixtures |
| 4. Scoped source-investigation pilot | Aiko can explain her implementation with file/line/revision evidence | Yes, against a disposable source checkout; naturalness needs normal use |

Develop the first two before expanding the number of agents or operations.
Tomorrow's use can inform wording, profile choices and the drawer interaction;
it is not a prerequisite for the deterministic reliability work.

## 1. Finish the result-to-conversation delivery contract

There are two separate loss windows in the current implementation:

- [`TaskOrchestrator._handle_completed`](../app/core/tasks/task_orchestrator.py)
  commits the terminal result before publishing to the in-memory brain queue.
  Recovery scans non-terminal tasks, so a completed root whose event was lost
  will not be rediscovered by that pass.
- [`TaskCueStore.drain_for_render`](../app/core/tasks/task_cue_store.py) removes
  cues during prompt assembly. A cancelled/failed/unused reply can therefore
  consume the report opportunity even though the result remains in SQLite.

Add a small transactional outbox for reportable completions, beginning with
harness roots. Create the result and its stable report identity in one commit.
Use the existing C6/report/escalation path; queue insertion and prompt assembly
are attempts, not acknowledgements. A failed turn releases its report claim.
Link an accepted report to its committed assistant message atomically so replay
cannot create a second local report for the same completion.

Reuse the existing response outcome and
[`DeliveryLedger`](../app/core/conversation/delivery.py) concepts. Keep these
distinct: result saved, report selected for a turn, report actually expressed,
assistant message committed, text presented and audio played. None establishes
that the user read or understood the answer. Do not mark a cue reported merely
because it appeared in the model's context. How a reply records its expressed
task references needs an explicit contract, including replies that ignore a cue.

Offline acceptance: crash after result commit, before queue insertion, after
cue selection and around assistant-message persistence; failed/truncated reply;
cue ignored in a successful unrelated reply; duplicate events; C6 drop/park;
session switches and cross-user isolation. Test one local committed report per
stable identity, not a promise of exactly-once human perception or TTS playback.

## 2. Make normal conversation a complete work-control surface

[`workflow_tools.py`](../app/llm/tools/workflow_tools.py) currently exposes start,
check, cancel and pause/resume. Only the drawer has a route that posts guidance
into an existing job. Consequently, a main-chat refinement such as "use the older
notes instead" has no dedicated same-job steering operation.

Add an owned-job guidance tool backed by the existing event path. Aiko selects
the relevant job, with an ambiguity question when necessary. Preserve the task
identity, counters and evidence; return whether guidance was queued and whether
an operation was already dispatched. A conversational refinement must not quietly
launch a duplicate job or pretend to undo an operation already sent.

Strengthen the handoff at the same time:

- The current start tool saves a rewritten goal, criteria/constraints, source
  session and up to 2,000 characters of the latest user message. The contract has
  a `source_message_id` slot, but this start path does not populate it.
- Attachments and earlier referenced messages are not automatically included.
  Build a bounded packet of explicit message/attachment/source references,
  recording subject, source time and revision. Resolve relative dates against
  the request's time, rather than the later worker turn.
- `_guidance` retains the latest ten messages in the planner frame. The complete
  event history persists, but older requirements need an explicit retained
  contract/amendment view rather than relying on the model's short notes.
- Keep original requirements, subsequent user changes, worker findings and
  hypotheses distinguishable. Use existing memory/concept access boundaries for
  relevant knowledge; do not copy all companion memories into every job.

A goal refinement is not a resource-permission grant. Broadening access still
goes through host policy. A completed job can later support an explicitly linked
follow-up, with selected evidence and a new allowance, rather than resurrection
of an immutable terminal task.

Offline acceptance: same-job steering during planning and a child wait; duplicate
message retries; source-message ownership; references to two different people;
relative dates across midnight/restart; more than ten amendments with an early
restriction still effective; unrelated chat not treated as guidance.

## 3. Let work discussion consult evidence instead of fixed prefixes

[`WorkDiscussion`](../app/core/tasks/workflow/work_discussion.py) is deliberately
read-only. Its current frame contains the first 4,000 root-result characters and
1,500 characters from each of the last eight children. The execution planner can
use [`inspect_result`](../app/core/tasks/workflow/harness_results.py), but the
discussion cannot request an omitted passage. This limits useful follow-up
questions even when the answer is already saved.

Give discussion a narrow, bounded read interface to its own saved evidence:
search/page by child id and revision, then answer with a source reference. Do not
give it the execution tool registry. Show clickable source references in the
drawer and retain distinctions between excerpt, complete result, provider
acknowledgement and read-back evidence. Add typed file/document/media artifacts
as a separate step; a non-text block count is not a usable image or artifact.

Offline acceptance: a requested fact beyond the prefix, an older child, changed
source revision, missing/deleted source, foreign job ids, hostile source text,
bounded inspection calls and zero new external operations.

## 4. Add a useful source-investigation capability through the same harness

Use the existing [read-only source pilot](personality-backlog/plugin-system.md#12-pilot-aiko-can-inspect-her-code).
An example request is "Explain how your completed work reaches our conversation."
The result should identify implementation owners and neighboring tests with
repository paths, line ranges, content hashes and checkout revision.

Start with an explicit source root, bounded text search and reads. Enforce
resource scope on every resolved path, including junctions/symlinks; exclude
credentials, private data, logs, dependency trees and build output. Record dirty
checkout state and distinguish inspected source from the running build. This
needs the smallest usable resource-scope contract from P3, not just a new profile
name or a broadly exposed filesystem server.

No edit, shell/test execution, package installation, app restart, commit or push
is implied by source inspection. Keep findings in the job/evidence workspace;
source details should not become autobiographical memories by default.

Only after ownership, resource arbitration and cumulative reservations are proven
should independent reads run in parallel. Browser sessions, files and accounts
need shared resource coordination before multiple workers mutate them.

## Short live checklist for the next testing session

Use an already-permitted folder and ordinary, non-sensitive text files.

1. Ask Aiko in main chat to compare two files and give a specific conclusion.
   Check that a job starts, its plan fits the request and her conclusion cites
   actual findings rather than only saying the task finished.
2. While it is active, use the drawer's **Guide next step** to change its focus.
   Check the same task continues and distinguishes saved guidance from an
   operation that had already started. Main-chat guidance is still a proposal.
3. Ask a question using **Ask about work**. It should describe saved progress
   honestly and should not launch a new operation.
4. When convenient, pause and restart during read-only work. Confirm the task
   retains its plan and usage. Manual pause should stay paused until resumed.
5. Close/reopen the drawer with a draft, inspect a child result, and resize the
   desktop layout. Check that the avatar width stays fixed. Mobile can wait for
   a later session; there is no need to test every surface at once.

Record the task id, what was requested and what seemed wrong. Existing task
events/results give us a starting point for diagnosis. Voice/chat responsiveness,
the usefulness of her profiles and natural completion wording need ordinary use;
scripted checks alone cannot establish those.
