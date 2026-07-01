"""Task orchestration subsystem — long-running, possibly interactive
work that Aiko spawns via LLM tool calls (or that internal workers
spawn for themselves).

Companion to :mod:`app.core.brain`. Phase 1 components:

* :class:`TaskHandler` — Protocol for one workflow (file_search,
  file_read, future browser handler, …). Stateless; the orchestrator
  owns the persisted row.
* :class:`TaskOrchestrator` — Registry + lifecycle + brain-queue
  emission. Spawns each handler invocation on a worker pool thread
  with the ``task_id`` correlation id set.
* :class:`TaskStore` — SQLite facade over the schema-v16 ``tasks``
  table.
* :func:`recover_interrupted_tasks` — boot-time scan that demotes
  stranded ``running`` rows to ``interrupted`` and emits a retry
  cue.

See ``docs/brain-orchestration.md`` for the full design — state
machine, schema DDL, awaiting-input resolution, MCP debug surface.
"""
from __future__ import annotations

from app.core.tasks.cue_render import render_cue_block
from app.core.tasks.handler_names import (
    KNOWN_HANDLER_NAMES,
)
from app.core.tasks.recovery import RecoveryReport, recover_interrupted_tasks
from app.core.tasks.task_cue_store import (
    CUE_KIND_INPUT_NEEDED,
    CUE_KIND_RESULT,
    CueDrainResult,
    TaskCue,
    TaskCueStore,
)
from app.core.tasks.task_escalation import (
    EnqueueProactive,
    EscalationConfig,
    FreeToSpeakPredicate,
    LastUserMessageAt,
    TaskEscalationManager,
)
from app.core.tasks.task_cleanup_worker import (
    DEFAULT_MAX_ROWS_PER_TICK,
    TaskCleanupWorker,
)
from app.core.tasks.task_heartbeat import (
    ACTION_FAIL,
    ACTION_WARN,
    VALID_ACTIONS,
    HeartbeatChecker,
)
from app.core.tasks.task_events import (
    EVENT_CANCELLED,
    EVENT_CHILD_SPAWNED,
    EVENT_COMPLETED,
    EVENT_CUSTOM,
    EVENT_FAILED,
    EVENT_HEARTBEAT_STALLED,
    EVENT_INPUT_ANSWER,
    EVENT_INPUT_QUESTION,
    EVENT_INTERRUPTED,
    EVENT_PHASE_CHANGE,
    EVENT_PROGRESS,
    EVENT_STARTED,
    KNOWN_EVENT_TYPES,
    TaskEvent,
    TaskEventStore,
    is_known_event_type,
)
from app.core.tasks.task_handler import (
    ACTIVE_STATUSES,
    INITIATED_BY_AIKO,
    INITIATED_BY_BACKGROUND,
    INITIATED_BY_SYSTEM,
    STATUS_AWAITING_INPUT,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_PAUSED,
    STATUS_RUNNING,
    TERMINAL_STATUSES,
    VALID_INITIATED_BY,
    VALID_STATUSES,
    TaskCompleted,
    TaskEmitFn,
    TaskEventEmit,
    TaskFailed,
    TaskHandler,
    TaskInputNeeded,
    TaskOutcome,
    TaskProgress,
    TaskState,
)
from app.core.tasks.task_inputs import (
    KIND_CHOICE,
    KIND_CONFIRM,
    KIND_FREE_TEXT,
    STATUS_ANSWERED as INPUT_STATUS_ANSWERED,
    STATUS_CANCELLED as INPUT_STATUS_CANCELLED,
    STATUS_PENDING as INPUT_STATUS_PENDING,
    STATUS_SUPERSEDED as INPUT_STATUS_SUPERSEDED,
    TERMINAL_INPUT_STATUSES,
    TaskInput,
    TaskInputStore,
    VALID_INPUT_STATUSES,
)
from app.core.tasks.task_orchestrator import (
    DEFAULT_PER_USER_CAP,
    TASK_LISTENER_COMPLETED,
    TASK_LISTENER_INPUT_NEEDED,
    TASK_LISTENER_PROGRESS,
    TASK_LISTENER_STARTED,
    TaskListenerFn,
    TaskOrchestrator,
    task_snapshot,
)
from app.core.tasks.task_store import TaskRow, TaskStore


__all__ = [
    # handler protocol + outcomes
    "TaskHandler",
    "TaskState",
    "TaskEmitFn",
    "TaskOutcome",
    "TaskProgress",
    "TaskInputNeeded",
    "TaskCompleted",
    "TaskFailed",
    "TaskEventEmit",
    # status enum
    "STATUS_RUNNING",
    "STATUS_AWAITING_INPUT",
    "STATUS_PAUSED",
    "STATUS_DONE",
    "STATUS_FAILED",
    "STATUS_CANCELLED",
    "STATUS_INTERRUPTED",
    "ACTIVE_STATUSES",
    "TERMINAL_STATUSES",
    "VALID_STATUSES",
    # initiated_by enum
    "INITIATED_BY_AIKO",
    "INITIATED_BY_BACKGROUND",
    "INITIATED_BY_SYSTEM",
    "VALID_INITIATED_BY",
    # store + orchestrator + recovery
    "TaskRow",
    "TaskStore",
    "TaskOrchestrator",
    "DEFAULT_PER_USER_CAP",
    "RecoveryReport",
    "recover_interrupted_tasks",
    # cue store + render + escalation (chunk 4)
    "TaskCue",
    "TaskCueStore",
    "CueDrainResult",
    "CUE_KIND_RESULT",
    "CUE_KIND_INPUT_NEEDED",
    "render_cue_block",
    "TaskEscalationManager",
    "EscalationConfig",
    "FreeToSpeakPredicate",
    "LastUserMessageAt",
    "EnqueueProactive",
    # listener fan-out (chunk 13)
    "task_snapshot",
    "TaskListenerFn",
    "TASK_LISTENER_STARTED",
    "TASK_LISTENER_PROGRESS",
    "TASK_LISTENER_INPUT_NEEDED",
    "TASK_LISTENER_COMPLETED",
    # phase 2 (schema v17): event log + input history + handler names
    "TaskEvent",
    "TaskEventStore",
    "EVENT_STARTED",
    "EVENT_PHASE_CHANGE",
    "EVENT_PROGRESS",
    "EVENT_INPUT_QUESTION",
    "EVENT_INPUT_ANSWER",
    "EVENT_HEARTBEAT_STALLED",
    "EVENT_CHILD_SPAWNED",
    "EVENT_COMPLETED",
    "EVENT_FAILED",
    "EVENT_CANCELLED",
    "EVENT_INTERRUPTED",
    "EVENT_CUSTOM",
    "KNOWN_EVENT_TYPES",
    "is_known_event_type",
    "TaskInput",
    "TaskInputStore",
    "INPUT_STATUS_PENDING",
    "INPUT_STATUS_ANSWERED",
    "INPUT_STATUS_SUPERSEDED",
    "INPUT_STATUS_CANCELLED",
    "VALID_INPUT_STATUSES",
    "TERMINAL_INPUT_STATUSES",
    "KIND_FREE_TEXT",
    "KIND_CHOICE",
    "KIND_CONFIRM",
    "KNOWN_HANDLER_NAMES",
    "HeartbeatChecker",
    "ACTION_WARN",
    "ACTION_FAIL",
    "VALID_ACTIONS",
    "TaskCleanupWorker",
    "DEFAULT_MAX_ROWS_PER_TICK",
]
