"""TaskHandler protocol + outcome dataclasses for the brain
orchestration task layer.

A :class:`TaskHandler` is a stateless implementation of one long-
running, possibly interactive workflow (e.g. ``file_search``,
``file_read``). The orchestrator owns the persisted SQLite row; the
handler owns the in-memory :data:`TaskState` blob that flows through
its lifecycle calls.

Contract between handler and orchestrator:

* The orchestrator invokes ``handler.start(args, emit)`` on a worker
  thread with the ``task_id`` contextvar set, then *waits* for the
  call to return.
* During the call the handler may invoke ``emit(outcome)`` any number
  of times — each emit synchronously persists the corresponding row
  patch and (if a brain queue is wired) enqueues the matching
  :class:`BrainEvent`.
* The *final* emit before the call returns dictates the terminal
  status if it was :class:`TaskCompleted` / :class:`TaskFailed` /
  :class:`TaskInputNeeded`. A handler that returns without a terminal
  emit leaves the row in ``status='running'`` — used by handlers
  that spawn their own internal threads / async work and emit later
  from those threads.
* The handler's return value is the *current* :data:`TaskState`. The
  orchestrator persists it via the store's ``update_state`` so a
  subsequent ``resume`` / ``on_input`` can pick up where it left off.

The three lifecycle entry points (``start`` / ``resume`` /
``on_input``) all carry the same ``emit`` shape so handler code can
share helpers between them. ``cancel`` is fire-and-forget — it gives
the handler a chance to release external resources (network
sockets, subprocess handles) but its return value is ignored and the
orchestrator does **not** wait on it.

Naming note: the outcome dataclass for success is :class:`TaskCompleted`
(past tense, matches the existing codebase ``task_completed`` WS
event name). The corresponding terminal *status string* on the row
is the shorter ``"done"`` so SQL filters stay terse —
``WHERE status='done'`` reads better than ``WHERE status='completed'``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Union, runtime_checkable


# ── public status enum (Python-side; validated at the store boundary) ──
#
# Mirrors the audit comment in ``chat_database.py``'s ``tasks`` table
# DDL. New statuses MUST be added to BOTH lists; the
# :class:`TaskStore` validates writes against :data:`VALID_STATUSES`.

STATUS_RUNNING = "running"
STATUS_AWAITING_INPUT = "awaiting_input"
STATUS_PAUSED = "paused"  # reserved for phase 2; not produced by any phase-1 handler
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_INTERRUPTED = "interrupted"

# Tasks in these statuses are still active — the row holds open
# state, the handler may resume. ``list_running`` filters to these.
ACTIVE_STATUSES: frozenset[str] = frozenset(
    (STATUS_RUNNING, STATUS_AWAITING_INPUT, STATUS_PAUSED)
)

# Terminal statuses — once reached, the row never moves again.
# ``recover_interrupted_on_boot`` skips rows already in these states.
TERMINAL_STATUSES: frozenset[str] = frozenset(
    (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED, STATUS_INTERRUPTED)
)

VALID_STATUSES: frozenset[str] = ACTIVE_STATUSES | TERMINAL_STATUSES


# ── initiated_by enum ────────────────────────────────────────────────
#
# Distinguishes Aiko's LLM-tool spawns from internal worker spawns
# and admin/MCP spawns. Filters in REST + MCP listings, and influences
# the default ``notify_aiko`` / ``visible_to_user`` flags.

INITIATED_BY_AIKO = "aiko"
INITIATED_BY_BACKGROUND = "background"
INITIATED_BY_SYSTEM = "system"
VALID_INITIATED_BY: frozenset[str] = frozenset(
    (INITIATED_BY_AIKO, INITIATED_BY_BACKGROUND, INITIATED_BY_SYSTEM)
)


# ── TaskState ────────────────────────────────────────────────────────
#
# Handler-owned, opaque-to-the-orchestrator JSON blob. The orchestrator
# only knows how to (de)serialise it via ``json.dumps`` / ``json.loads``.
# Anything jsonable goes; the handler is responsible for keeping its
# shape stable across versions if it wants resume-on-boot semantics.

TaskState = dict[str, Any]


# ── outcomes ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TaskProgress:
    """Cheap UI-only update.

    Triggers a ``task_progress`` WS broadcast and a row patch
    (``progress`` and/or ``last_message`` and/or ``phase`` columns).
    **Does not** park a cue — see the doc's "Progress events are
    UI-only" section. Used for human-readable "5/100 files searched"
    feedback inside the TaskStrip.

    ``phase`` (schema v17) is an optional per-handler label promoted
    to the ``tasks.phase`` column so every WS broadcast / prompt
    block / cue site can read it without parsing ``state`` JSON.
    Free-text — each handler documents its own phase vocabulary
    (e.g. ``"scanning"`` -> ``"matching"`` -> ``"done"``).
    """

    progress: float | None = None
    message: str | None = None
    phase: str | None = None


@dataclass(frozen=True, slots=True)
class TaskEventEmit:
    """Handler-defined audit entry. Appended to ``task_events``.

    Schema v17 addition. Lets a handler record arbitrary lifecycle
    moments (a future browser handler emitting
    ``TaskEventEmit("visited_url", {"url": ...})``) so the audit
    log captures what the handler actually did — not just the
    UI-facing progress beats.

    Does **not** change the row's hot state and does **not** park a
    cue: it's pure audit. The orchestrator appends one row to
    ``task_events`` with ``type=type`` and ``data=data`` (or the
    ``EVENT_CUSTOM`` constant if ``type`` is empty), bumps the
    heartbeat (handler-still-alive signal), and returns.

    ``type`` is free-text. Recommended: short snake_case labels
    scoped to the handler. ``data`` is jsonable; deeply-nested or
    huge blobs are encoded best-effort (encoding failures collapse
    to NULL on the row but never raise back to the handler).
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TaskInputNeeded:
    """The handler is blocked waiting for the user's answer.

    Persists ``status='awaiting_input'`` + ``input_request`` JSON.
    Parks a :class:`TaskInputNeededEvent` cue on the brain queue so
    Aiko surfaces the question on the next turn (or escalates after
    the silence window).

    ``options`` is a small list of pre-baked answers for the
    TaskStrip "click to answer" fallback path. The chat path doesn't
    use it — Aiko asks the question in prose and the user types
    free-form.
    """

    prompt: str
    options: list[str] | None = None


@dataclass(frozen=True, slots=True)
class TaskCompleted:
    """Handler succeeded. ``result`` is a jsonable summary.

    ``notify_aiko`` overrides the row default (the ``tasks.notify_aiko``
    column set at start time) — used by handlers that want to silence a
    completion that turned out to be trivial (e.g. an empty search
    that doesn't deserve a "found 0 results" cue), or amplify a
    completion that was originally silent.
    """

    result: dict[str, Any] = field(default_factory=dict)
    notify_aiko: bool | None = None


@dataclass(frozen=True, slots=True)
class TaskFailed:
    """Handler hit an unrecoverable error.

    ``error`` is a short human string for the row's ``error`` column
    and the cue. Long tracebacks go to the log; the cue gets the
    concise version. ``notify_aiko`` has the same override semantics
    as :class:`TaskCompleted`.
    """

    error: str = ""
    notify_aiko: bool | None = None


# Union of every outcome an emit can pass. Used by the orchestrator's
# router; concrete handlers always emit one of the five concrete
# classes directly (the four phase-1 outcomes plus the phase-2
# :class:`TaskEventEmit` audit outcome).
TaskOutcome = Union[
    TaskProgress,
    TaskInputNeeded,
    TaskCompleted,
    TaskFailed,
    TaskEventEmit,
]


# Callback the orchestrator hands to every lifecycle entry point.
# Handlers may call it any number of times per invocation; each call
# synchronously persists the corresponding state to the store and
# enqueues the matching brain event.
TaskEmitFn = Callable[[TaskOutcome], None]


# ── handler Protocol ─────────────────────────────────────────────────


@runtime_checkable
class TaskHandler(Protocol):
    """Stateless implementation of one task workflow.

    Concrete handlers should subclass :class:`object` (not the
    Protocol itself) — :func:`isinstance` against this Protocol is
    structural, so any class with the four lifecycle methods + a
    ``name`` attribute qualifies.

    All four methods take an ``emit`` callback and return the
    *current* :data:`TaskState` (a jsonable dict). The orchestrator
    persists the returned state immediately. If the handler emits a
    terminal outcome (Completed / Failed) during the call, the
    returned state is still recorded — handlers should clear sensitive
    intermediate fields before returning a terminal state.

    Threading: every entry point runs on a worker thread spawned by
    the orchestrator with the ``task_id`` contextvar already set, so
    log lines auto-correlate. Handlers that spawn additional threads
    (``concurrent.futures``, raw :class:`threading.Thread`) must
    propagate the contextvar themselves via
    :func:`contextvars.copy_context` — plain ``threading.Thread`` does
    NOT inherit it.
    """

    name: str

    def start(
        self, args: dict[str, Any], emit: TaskEmitFn
    ) -> TaskState:
        """Begin a fresh task. ``args`` is the jsonable input bag.

        Return value is the initial :data:`TaskState`. Common shape:
        ``{"args": args, "phase": "scanning", ...}`` so a future
        ``resume`` knows what it was doing. Even a handler that
        completes synchronously should return a non-empty state
        (e.g. ``{"args": args}``) so post-mortem inspection works.
        """
        ...

    def resume(
        self, state: TaskState, emit: TaskEmitFn
    ) -> TaskState:
        """Continue from a persisted ``state`` blob.

        Used by manual resume actions and (in phase 2) by
        ``tasks_resume_on_boot`` for tasks that survived a restart in
        ``paused`` status. Phase 1 handlers are not required to
        implement a real resume — returning ``state`` unchanged with
        an explanatory :class:`TaskFailed` emit is acceptable.
        """
        ...

    def on_input(
        self, state: TaskState, answer: str, emit: TaskEmitFn
    ) -> TaskState:
        """Resolve an ``awaiting_input`` block with the user's answer.

        ``answer`` is the raw free-text string the user typed (or the
        click-resolved option). The handler validates and either emits
        a terminal outcome OR another :class:`TaskInputNeeded` with a
        narrower follow-up.
        """
        ...

    def cancel(self, state: TaskState) -> None:
        """Best-effort resource cleanup.

        Called by the orchestrator after the row is already marked
        ``cancelled``. Handlers should close subprocesses, cancel
        outstanding HTTP requests, etc. Return value is ignored;
        exceptions are caught + logged by the orchestrator so a buggy
        cancel can't crash the loop.
        """
        ...


__all__ = [
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
    # types
    "TaskState",
    "TaskOutcome",
    "TaskEmitFn",
    # outcomes
    "TaskProgress",
    "TaskInputNeeded",
    "TaskCompleted",
    "TaskFailed",
    "TaskEventEmit",
    # protocol
    "TaskHandler",
]
