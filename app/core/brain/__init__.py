"""Brain orchestration subsystem — central event queue + consumer loop.

This package owns the *input fan-in* layer of Aiko's runtime. Every
producer that wants Aiko's attention (typed message, voice capture,
MCP call, task event, idle-worker wake, presence change) enqueues a
frozen :class:`BrainEvent` here. The single
:class:`BrainLoop` consumer picks the highest-priority event and
dispatches it to the registered handler.

See ``docs/brain-orchestration.md`` for the full design — event
taxonomy, priority ladder, free-to-speak gating, task completion
fold-vs-escalate, and the chunked rollout plan.

Phase 1 ship order (each chunk is a self-contained PR):

1. Skeleton modules + log-context extension (this is chunk 1).
2. Schema v16 + ``TaskOrchestrator`` foundations (chunk 2).
3. ``BrainLoop`` consumer activates; ``chat_once_streaming``, MCP
   ``send_message``, and the WS chat handler redirect through the
   queue (chunk 3).
4. ``LiveSession`` / ``ProactiveDirector`` / ``IdleWorkerScheduler``
   / ``SpeakingWindowScheduler`` / ``SummaryWorker`` migrate (chunk
   4).
5. REST + WS + MCP debug surface (chunk 5).
6. Frontend ``tasksView`` + ``TaskStrip`` (chunk 6).
7. Reference filesystem handler + persona block (chunk 7).

Public surface kept deliberately small — anything outside this list
is implementation detail and may move between chunks.
"""
from __future__ import annotations

from app.core.brain.events import (
    KIND_MAINTENANCE_DUE,
    KIND_PROACTIVE,
    KIND_SPEAKING_WINDOW_JOB,
    KIND_STATE_SYNC,
    KIND_TASK_INPUT_NEEDED,
    KIND_TASK_PROGRESS,
    KIND_TASK_RESULT,
    KIND_USER_MESSAGE,
    BrainEvent,
    MaintenanceDueEvent,
    Priority,
    ProactiveEvent,
    ProducerCallbacks,
    ReplyFuture,
    SpeakingWindowJobEvent,
    StateSyncEvent,
    TaskInputNeededEvent,
    TaskProgressEvent,
    TaskResultEvent,
    UserMessageEvent,
)
from app.core.brain.loop import BrainLoop, EventHandler
from app.core.brain.queue import BrainEventQueue


__all__ = [
    # core types
    "BrainEvent",
    "Priority",
    "ProducerCallbacks",
    "ReplyFuture",
    # concrete events
    "UserMessageEvent",
    "TaskInputNeededEvent",
    "TaskResultEvent",
    "ProactiveEvent",
    "SpeakingWindowJobEvent",
    "TaskProgressEvent",
    "MaintenanceDueEvent",
    "StateSyncEvent",
    # discriminator constants
    "KIND_USER_MESSAGE",
    "KIND_TASK_INPUT_NEEDED",
    "KIND_TASK_RESULT",
    "KIND_PROACTIVE",
    "KIND_SPEAKING_WINDOW_JOB",
    "KIND_TASK_PROGRESS",
    "KIND_MAINTENANCE_DUE",
    "KIND_STATE_SYNC",
    # loop + queue
    "BrainEventQueue",
    "BrainLoop",
    "EventHandler",
]
