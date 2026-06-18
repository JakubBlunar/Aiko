"""Event taxonomy for the brain orchestration queue.

Every input that wants the brain's attention enters the
:class:`BrainEventQueue` as a frozen event dataclass. The
:class:`Priority` enum below pins the consumer ordering documented in
`docs/brain-orchestration.md` — lower wins, ties broken by monotonic
enqueue sequence (handled by the queue, not the event itself).

Event kinds and their producers (phase 1):

* :class:`UserMessageEvent` — typed WS chat, voice ``LiveSession``,
  MCP ``send_message``. Priority :attr:`Priority.USER_INPUT`. Always
  wins; bypasses the free-to-speak gate (barge-in is real intent).
* :class:`TaskInputNeededEvent` — emitted by
  :class:`TaskOrchestrator` when a handler returns
  ``TaskInputNeeded``. **UI-only** — surfaces as an ``awaiting_input``
  chip in the TaskStrip; Aiko does not speak the question (verbal
  asking is a deferred, opt-in addition).
* :class:`TaskResultEvent` — emitted by :class:`TaskOrchestrator` on
  ``done`` / ``failed`` / ``cancelled``. The C6 report decision picks
  ``surface_now`` (fire when Aiko is free) / ``park`` (next natural
  turn) / ``drop``; floor (user-requested) tasks always surface.
* :class:`ProactiveEvent` — voice silence timer, typed silence timer,
  and escalated task cues all converge here.
* :class:`SpeakingWindowJobEvent` — post-turn jobs queued via
  :class:`SpeakingWindowScheduler.submit`.
* :class:`TaskProgressEvent` — handler emits ``TaskProgress``.
  **UI-only by design** — never parks a cue, never escalates.
* :class:`MaintenanceDueEvent` — :class:`IdleWorkerScheduler` wake.
  Loop runs ``IdleWorkerScheduler._tick()`` (sequential with turns by
  construction, gated on free-to-speak).
* :class:`StateSyncEvent` — WS presence, REST reaction, world gift,
  anything that mutates state without an LLM call.

Each concrete event class declares its discriminator + priority as
``ClassVar`` so the queue can read them off the instance without
storing them as instance fields (frozen+slotted dataclasses with
inheritance + redeclared fields have well-known slot-collision
gotchas, so we avoid inheritance entirely). The :data:`BrainEvent`
Union below is the public type alias for "anything the queue accepts".

The dataclasses are ``frozen=True`` so events are hashable and safe to
share across threads. ``slots=True`` keeps per-event memory cost
small. Producers do not set the enqueue timestamp — the queue stamps
it on its own ``_QueueEntry`` so re-park keeps the original wall time
without mutating the (frozen) event.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Callable, ClassVar, Literal, Union


class Priority(IntEnum):
    """Brain event priority ladder. Lower wins; ties break by enqueue order.

    The names match `docs/brain-orchestration.md` *Event taxonomy*
    table. Integer values are stable contracts — changing them
    re-orders the consumer behaviour silently, so they're frozen here
    and mirrored in the doc table.
    """

    USER_INPUT = 0
    TASK_INPUT_NEEDED = 1
    TASK_RESULT = 2
    PROACTIVE = 3
    SPEAKING_WINDOW_JOB = 4
    TASK_PROGRESS = 5
    MAINTENANCE = 6
    STATE_SYNC = 7


# Discriminator strings — keep stable; consumed by MCP debug tools, the
# loop's router, and tests. Adding a new event kind means appending a
# new constant + class + (optional) priority entry.
KIND_USER_MESSAGE = "user_message"
KIND_TASK_INPUT_NEEDED = "task_input_needed"
KIND_TASK_RESULT = "task_result"
KIND_PROACTIVE = "proactive"
KIND_SPEAKING_WINDOW_JOB = "speaking_window_job"
KIND_TASK_PROGRESS = "task_progress"
KIND_MAINTENANCE_DUE = "maintenance_due"
KIND_STATE_SYNC = "state_sync"


# A reply-future is attached to every UserMessageEvent so MCP
# ``send_message`` (and any other producer that needs the assistant's
# reply string) can block on the loop's response.
# ``concurrent.futures.Future`` is the right shape — thread-safe,
# cross-thread ``set_result`` / ``set_exception``, ``result(timeout=…)``
# for the caller side. Kept as ``Any`` here to avoid pulling
# ``concurrent.futures`` into every consumer's import graph.
ReplyFuture = Any


@dataclass(frozen=True, slots=True)
class ProducerCallbacks:
    """Streaming callbacks a producer wants the brain loop to thread
    into the underlying :meth:`SessionController.chat_once_streaming`
    call.

    Chunk 7 dispatched user-message events with no callbacks (MCP
    blocks on the reply future, so it doesn't need streaming). Chunk 8
    adds this bundle so the WS chat handler can keep its existing
    contract — per-token chat-bubble updates, generation-status
    progress lines, user-side stop button — even though the turn now
    runs on the brain-loop thread rather than the WS worker thread.

    All three fields are optional callables. The handler calls them
    inline from the brain-loop thread, so producer-side state mutations
    inside the callbacks need to be thread-safe with respect to the
    producer's own consumers (WS hub broadcast is already thread-safe,
    so this is free for the WS case).

    Frozen+slotted, just like the events themselves. Callables are
    hashable by identity so the dataclass stays hashable too.
    """

    on_token: Callable[[str], None] | None = None
    on_generation_status: Callable[[str], None] | None = None
    stop_requested: Callable[[], bool] | None = None


# ── concrete events ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class UserMessageEvent:
    """The user said something. Routed to ``TurnRunner.run()``.

    ``mode`` distinguishes the producer so the consumer can apply the
    right downstream behaviour (typed mode arms the typed-silence
    timer, voice mode skips it, MCP returns the reply via the future).
    ``reply_future`` is filled with the assistant's reply text once
    the turn completes; producers that don't care (typed WS push) pass
    ``None``.

    ``callbacks`` carries streaming callbacks (per-token, status,
    stop-requested) the handler threads into ``chat_once_streaming``.
    Producers that don't need streaming (MCP blocks on the future,
    voice path streams through TTS) pass ``None``. Chunk 8 added the
    field; older producers built before the swap still construct
    valid events because of the default.

    Chunk 11 added three voice-only optional fields so the live-audio
    capture thread can route through the queue without losing the
    information the legacy ``chat_once_streaming`` call carried in
    its kwargs:

    * ``resume_message_id`` — the existing user-message row id that
      this event should update in place (set when phrase B's text
      was folded into phrase A's row by the merge-buffer branch).
      ``None`` means insert a fresh user row.
    * ``capture_ms`` — wall-clock milliseconds spent capturing audio
      in :meth:`Microphone.capture_live_phrase`. Surfaces on the
      ``turn done:`` log line.
    * ``stt_ms`` — wall-clock milliseconds spent inside Whisper /
      RealtimeSTT. Surfaces on the same log line.

    All three default to neutral values so typed / MCP events
    constructed without them remain byte-identical to the pre-chunk-11
    shape.
    """

    kind: ClassVar[str] = KIND_USER_MESSAGE
    priority: ClassVar[Priority] = Priority.USER_INPUT

    session_key: str = ""
    text: str = ""
    mode: Literal["typed", "voice", "mcp"] = "typed"
    reply_future: ReplyFuture | None = None
    skip_tts: bool = False
    callbacks: ProducerCallbacks | None = None
    resume_message_id: int | None = None
    capture_ms: float = 0.0
    stt_ms: float = 0.0
    # D2 Part B — in-chat attachments the user added to this message.
    # Tuple of ``{id, filename, kind, rel_path, bytes}`` dicts (empty
    # for the common no-attachment turn). Threaded into
    # ``chat_once_streaming`` so the row is stamped + the turn hint
    # surfaces the paths. The queue breaks priority ties with a
    # monotonic counter, so this non-hashable field never participates
    # in event ordering.
    attachments: tuple[dict, ...] = ()


@dataclass(frozen=True, slots=True)
class TaskInputNeededEvent:
    """A running task is blocked waiting for an answer.

    UI-only: the orchestrator's input-needed listener surfaces the
    blocked task as a non-terminal ``awaiting_input`` chip in the
    TaskStrip, which stays visible until the user answers or cancels.
    The brain-loop handler parks no chat cue and arms no escalation —
    Aiko does not speak the question (verbal in-conversation asking is
    a deferred, opt-in addition).
    """

    kind: ClassVar[str] = KIND_TASK_INPUT_NEEDED
    priority: ClassVar[Priority] = Priority.TASK_INPUT_NEEDED

    task_id: str = ""
    session_key: str = ""
    prompt: str = ""
    options: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class TaskResultEvent:
    """A task reached a terminal state.

    Same fold-vs-escalate semantics as :class:`TaskInputNeededEvent`.
    ``notify_aiko`` and ``visible_to_user`` mirror the persisted row
    so the loop can decide whether to park a cue and whether to
    broadcast the WS event.
    """

    kind: ClassVar[str] = KIND_TASK_RESULT
    priority: ClassVar[Priority] = Priority.TASK_RESULT

    task_id: str = ""
    session_key: str = ""
    status: Literal["done", "failed", "cancelled"] = "done"
    title: str = ""
    result_summary: str = ""
    error: str | None = None
    notify_aiko: bool = True
    visible_to_user: bool = True


@dataclass(frozen=True, slots=True)
class ProactiveEvent:
    """Aiko speaks unprompted. Routed to ``ProactiveDirector``.

    ``source`` distinguishes the trigger: ``"voice_silence"`` (live
    mode), ``"typed_silence"`` (typed mode), or ``"task_escalation"``
    (a parked task cue exceeded its silence window).
    ``parked_cue_ids`` carries the task ids whose cues triggered the
    escalation, so the director can render them into the proactive
    turn's prompt.
    """

    kind: ClassVar[str] = KIND_PROACTIVE
    priority: ClassVar[Priority] = Priority.PROACTIVE

    session_key: str = ""
    source: Literal["voice_silence", "typed_silence", "task_escalation"] = "typed_silence"
    parked_cue_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SpeakingWindowJobEvent:
    """Post-turn / TTS-drain job submitted via
    :class:`SpeakingWindowScheduler.submit`.

    ``name`` is the job name for logging (``reflection``, ``weaver``,
    ``consolidator``, …). ``callable_`` is the actual work — a
    zero-arg function. ``compare=False`` on the callable so two
    events with the same name are still distinct queue entries.
    """

    kind: ClassVar[str] = KIND_SPEAKING_WINDOW_JOB
    priority: ClassVar[Priority] = Priority.SPEAKING_WINDOW_JOB

    name: str = ""
    callable_: Callable[[], None] | None = None


@dataclass(frozen=True, slots=True)
class TaskProgressEvent:
    """Handler emitted progress. UI-only — never parks a cue.

    The loop broadcasts the WS ``task_progress`` event and is done.
    The only way running-task state reaches the prompt is the
    running-tasks inner-life provider, which reads
    :class:`TaskOrchestrator.list_running` directly.
    """

    kind: ClassVar[str] = KIND_TASK_PROGRESS
    priority: ClassVar[Priority] = Priority.TASK_PROGRESS

    task_id: str = ""
    progress: float | None = None
    message: str | None = None
    status: str | None = None  # mirrored from the row for the WS patch


@dataclass(frozen=True, slots=True)
class MaintenanceDueEvent:
    """The :class:`IdleWorkerScheduler` wake fired.

    Loop runs ``IdleWorkerScheduler._tick()`` on its own thread,
    preserving the existing anti-starvation + EMA budget semantics.
    Gated on the free-to-speak predicate so maintenance never runs
    over Aiko's voice.
    """

    kind: ClassVar[str] = KIND_MAINTENANCE_DUE
    priority: ClassVar[Priority] = Priority.MAINTENANCE


@dataclass(frozen=True, slots=True)
class StateSyncEvent:
    """State mutation that affects the next prompt but doesn't speak.

    Producers: WS ``presence`` frame, REST user reaction click, world
    gift (REST add item), document upload completion. The loop
    forwards the payload to a registered state-sync handler that
    mutates the relevant store and (optionally) arms a downstream
    event.

    ``payload`` is a tuple of ``(key, value)`` pairs so the whole
    event stays hashable; the handler reconstructs a dict on receive.
    """

    kind: ClassVar[str] = KIND_STATE_SYNC
    priority: ClassVar[Priority] = Priority.STATE_SYNC

    subkind: str = ""
    payload: tuple[tuple[str, Any], ...] = ()


# Public union — anything the queue accepts.
BrainEvent = Union[
    UserMessageEvent,
    TaskInputNeededEvent,
    TaskResultEvent,
    ProactiveEvent,
    SpeakingWindowJobEvent,
    TaskProgressEvent,
    MaintenanceDueEvent,
    StateSyncEvent,
]


__all__ = [
    "Priority",
    "BrainEvent",
    "UserMessageEvent",
    "TaskInputNeededEvent",
    "TaskResultEvent",
    "ProactiveEvent",
    "SpeakingWindowJobEvent",
    "TaskProgressEvent",
    "MaintenanceDueEvent",
    "StateSyncEvent",
    "KIND_USER_MESSAGE",
    "KIND_TASK_INPUT_NEEDED",
    "KIND_TASK_RESULT",
    "KIND_PROACTIVE",
    "KIND_SPEAKING_WINDOW_JOB",
    "KIND_TASK_PROGRESS",
    "KIND_MAINTENANCE_DUE",
    "KIND_STATE_SYNC",
]
