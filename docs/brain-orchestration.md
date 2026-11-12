# Brain orchestration and persistent tasks

This guide owns the event loop and task runtime beneath the
[agent harness](agent-harness.md). Workflow planning, report composition and
procedural reuse are described there. This document describes the current
runtime rather than the original schema-v16/v17 rollout sequence.

## Queue and consumer

[`BrainEventQueue`](../app/core/brain/queue.py) is a priority heap with a single
[`BrainLoop`](../app/core/brain/loop.py) consumer. Lower priority numbers win;
ties use monotonic enqueue order. User-message handlers run the conversation
turn on the brain-loop thread. Task handlers and workflow inference execute
outside that thread, so serial event dispatch does not mean all background
computation is serialized with conversation.

### Event taxonomy

The event types and numbers live in [`events.py`](../app/core/brain/events.py).
The loop's `_GATED_KINDS` is the source of truth for free-to-speak deferral.

| Priority | Kind | Current session behavior | Free-to-speak gated |
| --- | --- | --- | --- |
| P0 | `user_message` | Typed/voice/MCP conversational input, reply future and optional streaming callbacks | No |
| P1 | `task_input_needed` | UI input/approval state; this handler does not speak the question | No |
| P2 | `task_result` | Route result to local-report ownership or the legacy cue policy | No |
| P3 | `proactive` | Silence/task escalation or delivery of an already-drafted local work report | Yes |
| P4 | `speaking_window_job` | Defined event; no session handler registered by task orchestration | Yes |
| P5 | `task_progress` | Acknowledgement only; lifecycle listeners provide UI updates | No |
| P6 | `maintenance_due` | Defined event; no session handler registered by task orchestration | Yes |
| P7 | `state_sync` | Defined event; no session handler registered by task orchestration | No |

The session currently registers user, result, input-needed, progress and proactive
handlers in [`TaskOrchestrationMixin`](../app/core/session/task_orchestration_mixin.py).
Idle workers and speaking-window scheduling retain their own scheduling paths.
Do not assume every background producer has migrated onto this queue simply
because an event dataclass exists.

The session's free-to-speak predicate checks conversation, proactive generation
and TTS activity. A gated event is deferred and retried after the gate clears;
predicate failures defer conservatively. A task result can therefore be recorded
while speech is busy, while the eventual proactive speech waits. Priority does
not preempt a handler or GPU call already running. Handler exceptions are logged
without killing the consumer.

## Task state machine

[`TaskOrchestrator`](../app/core/tasks/task_orchestrator.py) owns handler dispatch,
per-user admission, input handling, terminal transitions and lifecycle listeners.
Handlers implement the protocol in
[`task_handler.py`](../app/core/tasks/task_handler.py): `start`, `resume`,
`on_input` and `cancel`, emitting progress, input requests, completion, failure or
custom events. Handler invocations run on worker threads.

| Status | Meaning |
| --- | --- |
| `running` | An invocation or workflow execution slice is active |
| `awaiting_input` | A saved question/approval is waiting for an answer |
| `paused` | Saved work is suspended; `phase` distinguishes manual pause, allowance exhaustion, child/model wait and scheduled continuation |
| `done` | Handler completed; inspect result outcome/evidence before claiming objective success |
| `failed` | Handler failed; result/error can include an uncertain external effect |
| `cancelled` | Future work stopped; an already-dispatched external effect may still finish |
| `interrupted` | Ordinary task was running when its process ended |

A workflow's semantic outcome (such as partial, blocked or uncertain) is separate
from this runtime status. A `done` row alone does not establish successful
completion of the user's criteria. Terminal transitions cannot overwrite one
another. Parent/child relationships form a tree through `parent_task_id`.
Cancellation cascades to descendants when `agent.task_cascade_cancel_children`
is enabled; it cannot undo operations already sent.

## Persistence and inputs

SQLite is authoritative. [`TaskStore`](../app/core/tasks/task_store.py) stores
handler name, arguments, state JSON, result/error, ownership, visibility,
phase, heartbeat and parent identity. The harness adds a versioned checkpoint
inside that existing state; it does not create a separate agent service.

[`TaskEventStore`](../app/core/tasks/task_events.py) keeps an append-only log of
lifecycle events, custom handler events and work messages. The
[`TaskInputStore`](../app/core/tasks/task_inputs.py) stores question/answer history
with `pending`, `answered`, `superseded` and `cancelled` states. A new question
supersedes previous pending questions; `tasks.input_request` is the current
question mirror for inexpensive snapshots.

`notify_aiko` controls whether a task can feed conversational reporting;
`visible_to_user` controls UI/REST visibility. These are independent. Workflow
children normally suppress their own conversational reports, leaving the root
to aggregate evidence. Public task/work routes also enforce current-user and
parent ownership; an arbitrary task ID does not grant access to its results.

Approval waits are input records, answered through the actual task-answer API.
A work-discussion message is not an approval answer. Planner questions can also
be selected by the harness's local reporter; this does not turn the generic
`task_input_needed` event handler into a spoken approval mechanism.

## Recovery and waking

[`recover_interrupted_tasks`](../app/core/tasks/recovery.py) scans nonterminal
rows at boot. With `agent.tasks_resume_on_boot` enabled and an orchestrator
available, checkpointed harness roots reattach through `recover_work`. Their
saved child links, epochs, consumed allowance and manual/budget pauses survive.
The runtime wake loop resumes ready child/model/scheduled waits. Epoch checks
prevent a returning old planner from dispatching or completing newer work.

Ordinary running tasks become `interrupted`; pending questions for those rows
are cancelled. `awaiting_input` and `paused` rows are preserved. Turning off
boot resume disables automatic harness recovery and recovery-result cues; it
does not leave an ordinary `running` row alive after its process disappeared.
The harness's retry/uncertain-effect policy is documented in
[waiting and recovery](agent-harness.md#waiting-recovery-and-allowance).

## Result delivery

There are two paths; the root's ownership determines which handles a result.

- **Checkpointed roots with local reporting:**
  [`WorkReports`](../app/core/tasks/work_reports.py) discovers saved milestones,
  composes and checks a workflow-route draft, and persists it in `work_reports`.
  A `proactive` event with source `work_report` requests delivery after the brain
  gate clears. The assistant-message insertion and committed report identity
  share a transaction. The legacy task-result handler skips owned roots.
- **Other reportable tasks:** the C6 worker verdict chooses `surface_now`,
  `park_for_natural_opening` or `drop`, subject to the configured user-requested
  floor. [`TaskCueStore`](../app/core/tasks/task_cue_store.py) parks a T6 cue for
  a conversation turn; [`TaskEscalationManager`](../app/core/tasks/task_escalation.py)
  can arm fire-when-free reporting. These cues and queue entries are in memory.

`TaskProgress` remains status-only. Meaningful workflow speech is an explicit
milestone, question or terminal report, not narration of every progress event.
Neither committed text nor a notification is proof of audio playback or human
perception. See [direct reporting](agent-harness.md#direct-reporting-and-delivery)
for deduplication, draft invalidation and attempt accounting.

## Heartbeats and retention

[`HeartbeatChecker`](../app/core/tasks/task_heartbeat.py) independently scans
`running` rows for stale heartbeats. Progress/emits bump the heartbeat. Paused
or awaiting-input work is not considered stalled. Defaults are a 30-second check,
a 300-second stale threshold and `warn` action; `fail` is an optional policy.

[`TaskCleanupWorker`](../app/core/tasks/task_cleanup_worker.py) is an idle worker
that removes old terminal tasks in bounded batches, including associated event
and input rows. Default retention is 30 days. Result artifacts and report records
are task-owned through cascading foreign keys. These stores are retained work
history, not a permanent archive independent of task cleanup.

Use the [configuration reference](configuration.md) and
[`AgentSettings`](../app/core/infra/agent_settings.py) for settings and limits.
Workflow allowance expiry is distinct from terminal-history retention.

## REST and WebSocket surfaces

Routes live in [`tasks_files_routes.py`](../app/web/rest/tasks_files_routes.py)
and [`work_routes.py`](../app/web/rest/work_routes.py).

| Route | Purpose |
| --- | --- |
| `GET /api/tasks` | Paginated visible history; status and root-only filters |
| `GET /api/tasks/{id}` | Owned visible task snapshot |
| `GET /api/tasks/{id}/children` | Child history |
| `GET /api/tasks/{id}/events` | Paged event log |
| `GET /api/tasks/{id}/inputs` | Question/answer history |
| `POST /api/tasks/{id}/answer` | Answer the pending input (`input` string) |
| `POST /api/tasks/{id}/cancel` | Stop future work |
| `GET /api/work` | Drawer root history including active work |
| `GET /api/tasks/{id}/work` | Work detail and message history |
| `GET /api/tasks/{id}/result` | Owned result paging/search; source/rendered/projection views |
| `POST /api/tasks/{id}/work/messages` | Save discussion or next-step guidance |
| `POST /api/tasks/{id}/work/control` | Pause/resume/cancel work |

Lifecycle listeners broadcast visible task snapshots and progress patches through
WebSocket. `task_started`, `task_progress`, `task_input_needed` and `task_completed`
update the client store; work-message/control changes also refresh work state.
The listener filters hidden tasks rather than exposing internal child traffic.

## Diagnostics

Use the [embedded MCP debug server](../rules/mcp-server.md) and
[logging guide](../rules/debugging.md) before forcing workers or changing state.
Read-only starting points include task snapshots, `list_task_events(task_id)`,
`list_task_inputs(task_id)`, `get_heartbeat_state()` and
`get_task_report_decision_state()`. Mutation/debug tools are not required merely
to inspect progress.

| Symptom | Inspect |
| --- | --- |
| Work waiting without model usage | Root `phase`, model gate and `work_context_budget` events |
| Saved result but no spoken report | Local `work_reports` draft/attempt/commit state first; use C6 cues only for non-owned tasks |
| Approval no longer usable | Pending input plus live plugin configuration generation and operation signature |
| Old decision returning after guidance/pause | Checkpoint epoch and work-message event order |
| Consumer stopped or event unrouted | `brain-loop dispatched:`, `brain-loop deferred:`, `brain-loop no handler:` |
| Running task appears stalled | `heartbeat_stalled` event and configured warn/fail policy |

Changes to this runtime need focused task/brain tests and isolated databases.
Representative coverage lives in `test_brain_loop_consume`, `test_brain_loop_gate`, `test_task_orchestrator`,
`test_workflow_harness`, `test_work_speech` and `test_work_report_grounding`,
plus related operation/recovery tests.
