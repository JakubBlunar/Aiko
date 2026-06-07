"""TaskOrchestrator — registry, lifecycle, and brain-event emission.

Sits between :class:`SessionController` (which spawns tasks via LLM
tool calls and answers them via user messages) and concrete
:class:`TaskHandler` implementations. Owns the persisted SQLite row,
runs each handler invocation on its own pool worker thread with the
``task_id`` contextvar set, dispatches the handler's ``emit`` events
to the :class:`BrainEventQueue`, and enforces the per-user concurrency
cap.

Threading model:

* The orchestrator's public methods (``start_task`` / ``answer`` /
  ``cancel``) run on the caller's thread and return *immediately*.
  Work is submitted to an internal :class:`ThreadPoolExecutor` (one
  pool, sized by config).
* Each pool worker invocation sets the ``task_id`` contextvar at the
  top of the runner so every log line + every spawned sub-context
  carries the correlation id.
* Handler-side emits land back on the orchestrator via a closure
  (``_make_emit_for``); the closure runs on the worker thread, holds
  the orchestrator's per-task lock briefly to read terminal state,
  then persists + enqueues.
* Per-task lifecycle is single-stream by construction — we never
  pre-empt a handler. If a user answer arrives while ``start`` is
  still running, the orchestrator rejects the answer until the
  in-flight call has returned. In practice handlers either emit
  ``TaskInputNeeded`` and return quickly OR they emit a terminal
  outcome — so the answer always lands on a fully-quiesced row.

Brain-queue events emitted:

* ``TaskInputNeededEvent`` — handler emitted :class:`TaskInputNeeded`.
* ``TaskResultEvent(status="done" | "failed" | "cancelled")`` —
  terminal transition.
* ``TaskProgressEvent`` — handler emitted :class:`TaskProgress`.

If the orchestrator was constructed without a queue (test harness),
emits skip the enqueue half but still persist + log. The
``last_emitted_event`` shim on the instance carries the most recent
event so tests that don't wire a full queue can still assert on
emit shape.

Logging contract — see ``docs/brain-orchestration.md`` *Logging*.
Every lifecycle moment lands as one structured INFO line; the field
shape is pinned by :mod:`tests.test_brain_log_fields`.
"""
from __future__ import annotations

import contextvars
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from app.core.brain.events import (
    TaskInputNeededEvent,
    TaskProgressEvent,
    TaskResultEvent,
)
from app.core.infra.log_context import reset_task_id, set_task_id
from app.core.tasks.task_events import (
    EVENT_CANCELLED,
    EVENT_CHILD_SPAWNED,
    EVENT_COMPLETED,
    EVENT_CUSTOM,
    EVENT_FAILED,
    EVENT_INPUT_ANSWER,
    EVENT_INPUT_QUESTION,
    EVENT_INTERRUPTED,
    EVENT_PHASE_CHANGE,
    EVENT_PROGRESS,
    EVENT_STARTED,
    TaskEventStore,
)
from app.core.tasks.task_heartbeat import ACTION_WARN, HeartbeatChecker
from app.core.tasks.task_handler import (
    ACTIVE_STATUSES,
    INITIATED_BY_AIKO,
    STATUS_AWAITING_INPUT,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
    STATUS_RUNNING,
    TERMINAL_STATUSES,
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
from app.core.tasks.task_inputs import TaskInputStore
from app.core.tasks.task_store import TaskRow, TaskStore

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.brain.queue import BrainEventQueue


log = logging.getLogger("app.task_orchestrator")


# Default per-user cap matches ``agent.tasks_per_user_cap`` in
# ``config/default.json``. Kept here as a fallback so the orchestrator
# can be unit-tested in isolation from the settings layer.
DEFAULT_PER_USER_CAP = 8


# ── Chunk 13: listener event kinds ──────────────────────────────────────
# Stable string kinds for :meth:`TaskOrchestrator.add_task_listener`.
# The WS bridge in ``app/web/server.py`` maps these 1:1 to outbound
# WS event names so changing a kind here is a wire-protocol break.
TASK_LISTENER_STARTED: str = "task_started"
TASK_LISTENER_PROGRESS: str = "task_progress"
TASK_LISTENER_INPUT_NEEDED: str = "task_input_needed"
TASK_LISTENER_COMPLETED: str = "task_completed"


# Listener signature: ``(kind, payload)``. ``payload`` shape depends
# on ``kind``:
#
# * ``task_started`` / ``task_input_needed`` / ``task_completed`` —
#   ``{"task": <snapshot dict>}``
# * ``task_progress`` —
#   ``{"task_id": int, "patch": {"progress"?, "last_message"?, "status"?}}``
#
# Listeners run **synchronously** on the orchestrator's worker thread
# (or the caller's thread for ``start_task`` / ``cancel``). Implementations
# must keep them cheap and non-blocking — push to a queue / hub /
# whatever rather than doing work inline. Exceptions are caught +
# logged so a buggy listener can't poison the next one.
TaskListenerFn = Callable[[str, dict[str, Any]], None]


def task_snapshot(row: TaskRow) -> dict[str, Any]:
    """Render a :class:`TaskRow` as a JSON-safe dict for REST + WS.

    Field set mirrors the design doc's ``TaskSnapshot`` shape:

    * Identity: ``id``, ``handler_name``, ``title``, ``user_id``,
      ``initiated_by``
    * Lifecycle: ``status``, ``progress``, ``last_message``,
      ``phase``, ``created_at``, ``updated_at``, ``completed_at``,
      ``heartbeat_at``
    * Outcome: ``result``, ``error``
    * Awaiting-input cue: ``input_request`` (None when not blocked)
    * Capability flags: ``notify_aiko``, ``visible_to_user``
    * Extensibility: ``metadata`` (None when handler didn't set any)
    * Task tree: ``parent_task_id`` (None when top-level)
    * Original args (omitted from broadcasts? — included so the
      frontend doesn't need a second fetch to show "you asked for
      X"; sensitive args are the handler's responsibility to keep
      out of the bag)

    Pure function — no I/O. Callers gate ``visible_to_user`` at
    broadcast time (the orchestrator emits regardless, so future
    listeners can opt-in to system-internal tasks).

    Schema v17 adds ``phase`` / ``parent_task_id`` / ``heartbeat_at``
    so the frontend can render the human-readable phase, draw
    parent/child trees, and surface stalled rows.
    """
    return {
        "id": int(row.id),
        "user_id": str(row.user_id),
        "handler_name": str(row.handler_name),
        "title": str(row.title or ""),
        "status": str(row.status),
        "progress": (
            float(row.progress) if row.progress is not None else None
        ),
        "last_message": row.last_message,
        "phase": row.phase,
        "initiated_by": str(row.initiated_by),
        "args": dict(row.args or {}),
        "input_request": (
            dict(row.input_request) if row.input_request is not None else None
        ),
        "result": (
            dict(row.result) if row.result is not None else None
        ),
        "error": row.error,
        "notify_aiko": bool(row.notify_aiko),
        "visible_to_user": bool(row.visible_to_user),
        "created_at": str(row.created_at or ""),
        "updated_at": str(row.updated_at or ""),
        "completed_at": row.completed_at,
        "heartbeat_at": row.heartbeat_at,
        "parent_task_id": row.parent_task_id,
        "metadata": (
            dict(row.metadata) if row.metadata is not None else None
        ),
    }


def _format_task_id(task_id: int) -> str:
    """Render the ``task_id`` contextvar value.

    Eight-char zero-padded so the log column width stays stable and a
    grep for ``task=00000042`` finds the row regardless of how large
    the autoincrement got. Mirrors the existing 8-char ``turn`` id
    convention.
    """
    return f"{int(task_id):08x}"


@dataclass(slots=True)
class _ActiveTask:
    """Orchestrator-internal record for a task currently in flight.

    Distinct from :class:`TaskRow` (the DB shape). Holds the
    in-memory :data:`TaskState` blob the handler is reading + the
    cancellation flag the emit callback consults so a late emit
    after :meth:`TaskOrchestrator.cancel` is dropped silently.
    """

    task_id: int
    user_id: str
    handler_name: str
    notify_aiko: bool
    visible_to_user: bool
    state: TaskState = field(default_factory=dict)
    future: Future | None = None
    cancelled: bool = False
    finalized: bool = False


class TaskOrchestrator:
    """Registry + lifecycle + brain-queue emission.

    One instance lives on :class:`SessionController` (wired in chunk
    3). Owns the :class:`TaskStore` and an internal
    :class:`ThreadPoolExecutor`. Tests construct one directly with a
    standalone store and a (possibly None) queue.
    """

    def __init__(
        self,
        store: TaskStore,
        *,
        queue: "BrainEventQueue | None" = None,
        executor: ThreadPoolExecutor | None = None,
        per_user_cap: int = DEFAULT_PER_USER_CAP,
        session_key_resolver: Callable[[str], str] | None = None,
        event_store: TaskEventStore | None = None,
        input_store: TaskInputStore | None = None,
        cascade_cancel_children: bool = True,
        heartbeat_enabled: bool = True,
        heartbeat_check_interval_seconds: int = 30,
        heartbeat_stalled_seconds: int = 300,
        heartbeat_action: str = ACTION_WARN,
    ) -> None:
        """Construct the orchestrator.

        ``queue`` is the shared :class:`BrainEventQueue` used by
        :class:`BrainLoop`. When ``None`` (tests + chunk-1 isolation)
        the orchestrator still persists every transition but skips
        the enqueue half; ``last_emitted_event`` carries the most
        recent event for assertion convenience.

        ``executor`` lets tests inject a single-worker pool for
        deterministic ordering. Production callers should pass
        ``None`` and let the orchestrator create its own.

        ``session_key_resolver`` maps ``user_id -> session_key`` for
        :class:`BrainEvent` enqueues. Defaults to identity, which
        works for the existing single-session-per-user model; chunk 3
        wires the real :meth:`SessionController.session_key_for_user`.

        ``event_store`` (schema v17) is the append-only
        :class:`TaskEventStore`. The orchestrator appends one row
        per emit; missing store collapses every append to a no-op
        so phase-1 tests that don't wire it still pass.

        ``input_store`` (schema v17) is the per-task input/answer
        history. The orchestrator writes a fresh ``pending`` row on
        every ``TaskInputNeeded`` and marks it ``answered`` on
        ``answer()``; missing store collapses to no-op + legacy
        column write only.

        ``cascade_cancel_children`` (schema v17): when True, a
        :meth:`cancel` call recursively cancels every active child
        in the task tree. Reads at orchestrator boot; flip on
        :class:`SessionController` reconfigure if needed.
        """
        self._store = store
        self._event_store = event_store
        self._input_store = input_store
        self._cascade_cancel = bool(cascade_cancel_children)
        self._queue = queue
        self._handlers: dict[str, TaskHandler] = {}
        self._active: dict[int, _ActiveTask] = {}
        self._lock = threading.RLock()
        self._executor = (
            executor
            if executor is not None
            else ThreadPoolExecutor(max_workers=8, thread_name_prefix="task-orch")
        )
        self._owns_executor = executor is None
        self._per_user_cap = max(1, int(per_user_cap))
        self._session_key_for = (
            session_key_resolver if session_key_resolver is not None else (lambda u: u)
        )
        # Test-only side channel: last emit's resulting brain event.
        # Read by tests that don't wire a full BrainEventQueue.
        self.last_emitted_event: Any = None
        # Chunk 13: external listener fan-out. Used by the WS bridge
        # in ``app/web/server.py`` to broadcast ``task_started`` /
        # ``task_progress`` / ``task_input_needed`` / ``task_completed``
        # to connected frontends, and by tests that want a deterministic
        # observable. Listeners are NOT held under ``_lock`` while
        # firing — the orchestrator's lock guards row state, not
        # listener IO. A broken listener can't block another from
        # firing (each fire is wrapped in its own try/except).
        self._task_listeners: list[TaskListenerFn] = []
        self._listeners_lock = threading.RLock()
        log.info(
            "task-orchestrator init: handlers=0 per_user_cap=%d queue=%s "
            "event_store=%s input_store=%s cascade_cancel=%d",
            self._per_user_cap,
            "wired" if queue is not None else "none",
            "wired" if event_store is not None else "none",
            "wired" if input_store is not None else "none",
            1 if self._cascade_cancel else 0,
        )
        # Schema v17: heartbeat sweeper. Owned by the orchestrator so
        # the lifecycle (start at construction, stop at shutdown) is
        # one place. ``heartbeat_enabled=False`` keeps the
        # ``HeartbeatChecker`` instance for MCP introspection but the
        # daemon thread is never started.
        self._heartbeat = HeartbeatChecker(
            store,
            event_store=event_store,
            check_interval_seconds=int(heartbeat_check_interval_seconds),
            stalled_seconds=int(heartbeat_stalled_seconds),
            action=str(heartbeat_action),
            enabled=bool(heartbeat_enabled),
        )
        self._heartbeat.start()

    # ── schema v17 helpers: best-effort store writes ─────────────────

    def _append_event(
        self, task_id: int, *, type: str, data: Any | None = None
    ) -> None:
        """Append one ``task_events`` row, best-effort.

        Missing event store collapses to a no-op so phase-1 tests
        that don't wire it still pass. Encoding/IO failures are
        logged and never raised back to the caller — append is
        contract-best-effort. Bumps ``heartbeat_at`` on the parent
        row as a side effect (every emit is a liveness signal).
        """
        if self._event_store is None:
            return
        try:
            self._event_store.append(
                int(task_id), type=str(type), data=data
            )
        except Exception:
            log.exception(
                "task event append failed: task=%d type=%s",
                int(task_id),
                type,
            )

    def _bump_heartbeat(self, task_id: int) -> None:
        """Bump ``tasks.heartbeat_at`` to now. Best-effort, never raises."""
        try:
            self._store.update_heartbeat(int(task_id))
        except Exception:
            log.exception(
                "task heartbeat bump failed: task=%d", int(task_id)
            )

    @property
    def heartbeat(self) -> HeartbeatChecker:
        """Expose the heartbeat sweeper for MCP debug / tests."""
        return self._heartbeat

    def set_cascade_cancel_children(self, enabled: bool) -> None:
        """Hot-toggle the cascade-cancel behaviour (schema v17).

        Read by :class:`SessionController` when the
        ``agent.task_cascade_cancel_children`` setting changes at
        runtime. The orchestrator's per-cancel decision picks up the
        new flag on the next :meth:`cancel` call.
        """
        self._cascade_cancel = bool(enabled)

    # ── handler registry ─────────────────────────────────────────────

    def register_handler(self, handler: TaskHandler) -> None:
        """Register a handler under its ``name`` attribute.

        Re-registering with the same name overwrites — the
        orchestrator has one canonical owner per ``handler_name``,
        same convention as :class:`BrainLoop.register_handler`. Useful
        for hot-reload during development.
        """
        name = str(getattr(handler, "name", "") or "").strip()
        if not name:
            raise ValueError("handler must have a non-empty 'name' attribute")
        if not callable(getattr(handler, "start", None)):
            raise TypeError(f"handler {name!r} is missing start()")
        with self._lock:
            self._handlers[name] = handler
        log.info(
            "task-orchestrator register: handler=%s total=%d",
            name,
            len(self._handlers),
        )

    def handler_for(self, name: str) -> TaskHandler | None:
        """Lookup helper. Returns ``None`` for unknown names."""
        with self._lock:
            return self._handlers.get(str(name))

    def list_handlers(self) -> list[str]:
        """Snapshot of registered handler names. Used by MCP debug."""
        with self._lock:
            return sorted(self._handlers.keys())

    # ── chunk 13: listener fan-out ───────────────────────────────────

    def add_task_listener(self, callback: TaskListenerFn) -> None:
        """Subscribe ``callback`` to every task lifecycle event.

        The callback is invoked on the same thread that performed the
        triggering write (worker pool thread for emits, caller's thread
        for ``start_task`` / ``cancel``). It must be cheap + non-blocking;
        listeners that need to do real work should push to a queue
        (the WS bridge in :mod:`app.web.server` follows that pattern).

        Idempotent — the same callback object is only stored once.
        Exceptions raised by the callback are caught + logged by the
        dispatcher so a broken listener can't poison its siblings or
        the originating orchestrator method.
        """
        if callback is None or not callable(callback):
            return
        with self._listeners_lock:
            if callback in self._task_listeners:
                return
            self._task_listeners.append(callback)

    def remove_task_listener(self, callback: TaskListenerFn) -> bool:
        """Unsubscribe ``callback``. Returns True if it was registered."""
        with self._listeners_lock:
            try:
                self._task_listeners.remove(callback)
                return True
            except ValueError:
                return False

    def _dispatch_to_listeners(
        self, kind: str, payload: dict[str, Any]
    ) -> None:
        """Fan out ``(kind, payload)`` to every registered listener.

        Snapshots the listener list under the lock then releases before
        firing — that way a listener that subscribes / unsubscribes
        during dispatch doesn't deadlock or self-reentrance. Per-fire
        exceptions are isolated.
        """
        with self._listeners_lock:
            listeners = list(self._task_listeners)
        for fn in listeners:
            try:
                fn(kind, payload)
            except Exception:
                log.exception(
                    "task listener raised: kind=%s payload_keys=%s",
                    kind,
                    sorted(payload.keys()) if isinstance(payload, dict) else "?",
                )

    # ── public lifecycle surface ─────────────────────────────────────

    def start_task(
        self,
        *,
        user_id: str,
        handler_name: str,
        args: dict[str, Any],
        title: str,
        initiated_by: str = INITIATED_BY_AIKO,
        notify_aiko: bool = True,
        visible_to_user: bool = True,
        metadata: dict[str, Any] | None = None,
        parent_task_id: int | None = None,
    ) -> int | None:
        """Spawn a new task and return its ``task_id``.

        Returns ``None`` and emits a WARNING when:

        * The handler name isn't registered.
        * The per-user cap is hit (use :meth:`count_active_for_user`
          to check first if the caller wants graceful UX).

        ``initiated_by`` follows the constants in
        :mod:`task_handler` — Aiko's LLM tool calls pass
        ``"aiko"``; internal workers spawning their own tasks pass
        ``"background"``; MCP / admin paths pass ``"system"``.

        After persisting the row, the handler's ``start(args, emit)``
        is submitted to the worker pool. The call returns *before*
        the handler runs, so the caller (e.g. an LLM tool call) sees
        only the ``task_id`` — exactly the latency Aiko needs for the
        "I'll start that for you" reply.
        """
        with self._lock:
            handler = self._handlers.get(str(handler_name))
        if handler is None:
            log.warning(
                "task spawn rejected: reason=unknown_handler user=%s handler=%s",
                user_id,
                handler_name,
            )
            return None
        running_count = self._store.count_active_for_user(user_id)
        if running_count >= self._per_user_cap:
            log.warning(
                "task spawn rejected: reason=per_user_cap user=%s "
                "running_count=%d cap=%d",
                user_id,
                running_count,
                self._per_user_cap,
            )
            return None
        parent_norm = (
            int(parent_task_id)
            if parent_task_id is not None and int(parent_task_id) > 0
            else None
        )
        try:
            task_id = self._store.create(
                user_id=user_id,
                handler_name=handler_name,
                title=title,
                args=args,
                state={},
                notify_aiko=notify_aiko,
                visible_to_user=visible_to_user,
                initiated_by=initiated_by,
                metadata=metadata,
                parent_task_id=parent_norm,
            )
        except Exception as exc:
            log.exception(
                "task spawn store error: user=%s handler=%s exc=%r",
                user_id,
                handler_name,
                exc,
            )
            return None
        active = _ActiveTask(
            task_id=task_id,
            user_id=str(user_id),
            handler_name=str(handler_name),
            notify_aiko=bool(notify_aiko),
            visible_to_user=bool(visible_to_user),
            state={},
        )
        with self._lock:
            self._active[task_id] = active
        log.info(
            "task spawned: task=%d handler=%s initiated_by=%s notify_aiko=%d "
            "visible_to_user=%d running_count=%d parent=%s",
            task_id,
            handler_name,
            initiated_by,
            1 if notify_aiko else 0,
            1 if visible_to_user else 0,
            running_count + 1,
            parent_norm if parent_norm is not None else "-",
        )
        # Schema v17: append the EVENT_STARTED row first so the event
        # log starts at the same instant the task is announced. The
        # data payload carries enough to re-render the start moment
        # without a join (handler name + title + initiated_by + args).
        self._append_event(
            task_id,
            type=EVENT_STARTED,
            data={
                "handler": handler_name,
                "title": title,
                "initiated_by": initiated_by,
                "args": dict(args or {}),
                "parent_task_id": parent_norm,
            },
        )
        # If a parent was supplied, record the spawn moment on the
        # parent's event log too so a parent-replay sees the full
        # tree without traversing children. Best-effort like every
        # event append.
        if parent_norm is not None:
            self._append_event(
                parent_norm,
                type=EVENT_CHILD_SPAWNED,
                data={
                    "child_task_id": task_id,
                    "handler": handler_name,
                    "title": title,
                },
            )
        # Chunk 13: ``task_started`` listener fan-out fires BEFORE the
        # handler runs so the frontend sees the row land as ``running``
        # immediately. A fresh store read keeps the snapshot honest
        # (created_at / updated_at populated by the create call).
        started_row = self._store.get(task_id)
        if started_row is not None:
            self._dispatch_to_listeners(
                TASK_LISTENER_STARTED, {"task": task_snapshot(started_row)}
            )
        emit = self._make_emit_for(task_id)
        active.future = self._submit_invocation(
            task_id, lambda: handler.start(dict(args or {}), emit), "start"
        )
        return task_id

    def answer(self, task_id: int, answer: str) -> bool:
        """Resolve an ``awaiting_input`` task with the user's answer.

        Returns False if:

        * The task doesn't exist.
        * The task isn't currently ``awaiting_input``.
        * The handler is no longer registered.

        Persists ``status='running'`` first (clearing the
        ``input_request``), then submits ``handler.on_input(state,
        answer, emit)`` to the worker pool.
        """
        row = self._store.get(int(task_id))
        if row is None:
            log.warning("task answer rejected: reason=unknown task=%d", task_id)
            return False
        if row.status != STATUS_AWAITING_INPUT:
            log.warning(
                "task answer rejected: reason=wrong_status task=%d status=%s",
                task_id,
                row.status,
            )
            return False
        handler = self.handler_for(row.handler_name)
        if handler is None:
            log.warning(
                "task answer rejected: reason=unknown_handler task=%d handler=%s",
                task_id,
                row.handler_name,
            )
            return False
        # Schema v17: mark the latest pending input row as answered
        # BEFORE clearing the legacy column so the audit trail records
        # the resolution even if the column write races. Missing input
        # store collapses to the legacy-only path.
        answered_input_id: int | None = None
        if self._input_store is not None:
            try:
                pending = self._input_store.latest_pending(int(task_id))
            except Exception:
                pending = None
            if pending is not None:
                try:
                    if self._input_store.answer(
                        pending.id, response=str(answer or "")
                    ):
                        answered_input_id = pending.id
                except Exception:
                    log.exception(
                        "task input answer failed: task=%d input=%d",
                        task_id,
                        pending.id,
                    )
        self._store.clear_awaiting_input(int(task_id))
        with self._lock:
            active = self._active.get(int(task_id))
            if active is None:
                # Active record may have been dropped on app restart but
                # the row survived; rebuild a minimal active entry so
                # emits still route.
                active = _ActiveTask(
                    task_id=int(task_id),
                    user_id=row.user_id,
                    handler_name=row.handler_name,
                    notify_aiko=row.notify_aiko,
                    visible_to_user=row.visible_to_user,
                    state=dict(row.state),
                )
                self._active[int(task_id)] = active
            state_snapshot: TaskState = dict(active.state or row.state)
        log.info(
            "task transition: task=%d from=%s to=%s input_id=%s",
            task_id,
            STATUS_AWAITING_INPUT,
            STATUS_RUNNING,
            answered_input_id if answered_input_id is not None else "-",
        )
        # Schema v17: audit the answer on the event log. Stored
        # response is capped at 1000 chars so a giant paste doesn't
        # bloat the log; the full text lives on the ``task_inputs``
        # row.
        self._append_event(
            int(task_id),
            type=EVENT_INPUT_ANSWER,
            data={
                "input_id": answered_input_id,
                "response_preview": str(answer or "")[:1000],
                "response_len": len(str(answer or "")),
            },
        )
        self._bump_heartbeat(int(task_id))
        emit = self._make_emit_for(int(task_id))
        active.future = self._submit_invocation(
            int(task_id),
            lambda: handler.on_input(state_snapshot, str(answer), emit),
            "on_input",
        )
        return True

    def cancel(self, task_id: int) -> bool:
        """User-initiated cancel.

        Marks the row ``cancelled``, sets the in-memory ``cancelled``
        flag so any late emit from the still-running handler call is
        suppressed, then best-effort invokes ``handler.cancel(state)``
        on the worker pool. Emits a :class:`TaskResultEvent` with
        ``status='cancelled'``.

        Returns False if the task is unknown or already terminal.

        Schema v17: when ``cascade_cancel_children`` is on (the
        default), recursively cancels every active child in the task
        tree before finalising the parent. Children are walked depth-
        first so a tree like ``A -> B -> C`` cancels ``C`` first,
        then ``B``, then ``A`` — each child gets a clean
        ``handler.cancel(state)`` callback. The cascade depth is
        bounded by the per-user cap (eight by default) so accidental
        recursion can't blow the stack.
        """
        row = self._store.get(int(task_id))
        if row is None:
            return False
        if row.status in TERMINAL_STATUSES:
            return False
        # Schema v17: cascade before touching the parent so a child
        # that emits a late progress beat lands while the parent is
        # still ``running`` (the emit guard reads the in-memory
        # ``cancelled`` flag, not the row status).
        cascaded: list[int] = []
        if self._cascade_cancel:
            cascaded = self._cancel_children_recursive(
                int(task_id), _depth=0
            )
        ok = self._store.mark_cancelled(int(task_id))
        if not ok:
            return False
        with self._lock:
            active = self._active.get(int(task_id))
            if active is not None:
                active.cancelled = True
                state_snapshot: TaskState = dict(active.state or row.state)
            else:
                state_snapshot = dict(row.state)
        handler = self.handler_for(row.handler_name)
        if handler is not None:
            try:
                self._executor.submit(handler.cancel, state_snapshot)
            except Exception as exc:  # pragma: no cover - executor shutdown race
                log.exception(
                    "task cancel: handler.cancel submit failed task=%d exc=%r",
                    task_id,
                    exc,
                )
        # Schema v17: cancel any orphan pending input rows. The
        # legacy ``input_request`` column is already NULLed by the
        # ``mark_cancelled`` SQL on cancel-style termination paths;
        # this hook keeps the dedicated table in sync.
        if self._input_store is not None:
            try:
                self._input_store.cancel_pending_for_task(int(task_id))
            except Exception:
                log.exception(
                    "task input cancel-pending on cancel failed: task=%d",
                    task_id,
                )
        log.info(
            "task completed: task=%d status=%s notify_aiko=%d cascaded=%d",
            task_id,
            STATUS_CANCELLED,
            1 if (active and active.notify_aiko) else 0,
            len(cascaded),
        )
        # Audit cancel on event log. ``cascaded`` carries the list of
        # child task ids that were also moved to ``cancelled`` as a
        # side effect.
        self._append_event(
            int(task_id),
            type=EVENT_CANCELLED,
            data={
                "reason": "user_request",
                "cascaded_children": cascaded,
                "notify_aiko": (
                    bool(active.notify_aiko) if active else bool(row.notify_aiko)
                ),
            },
        )
        self._bump_heartbeat(int(task_id))
        self._emit_brain_event(
            TaskResultEvent(
                task_id=_format_task_id(task_id),
                session_key=self._session_key_for(row.user_id),
                status=STATUS_CANCELLED,  # type: ignore[arg-type]
                title=row.title,
                result_summary=f"cancelled by user",
                notify_aiko=active.notify_aiko if active else row.notify_aiko,
                visible_to_user=active.visible_to_user if active else row.visible_to_user,
            )
        )
        # Chunk 13: same ``task_completed`` listener event as the
        # natural-terminate paths — frontend doesn't need to track
        # "was this a cancel or a done?" separately, ``status`` on
        # the snapshot says it all.
        fresh = self._store.get(int(task_id))
        if fresh is not None:
            self._dispatch_to_listeners(
                TASK_LISTENER_COMPLETED, {"task": task_snapshot(fresh)}
            )
        with self._lock:
            if active is not None:
                active.finalized = True
        return True

    def _cancel_children_recursive(
        self, parent_id: int, *, _depth: int
    ) -> list[int]:
        """Depth-first cancel of every active descendant.

        Returns the list of task ids that were actually moved to
        ``cancelled`` by this cascade (already-terminal children are
        skipped). Defensive depth cap at 16 — the per-user cap is 8
        and a tree taller than that almost certainly means a handler
        is recursively spawning itself.
        """
        if _depth >= 16:
            log.warning(
                "task cascade-cancel depth cap hit: parent=%d depth=%d",
                parent_id,
                _depth,
            )
            return []
        children = self._store.list_children(
            parent_id, statuses=ACTIVE_STATUSES
        )
        cancelled: list[int] = []
        for child in children:
            # Recurse first so the deepest children cancel before
            # their parents — matches the cleanup order a user would
            # expect (sub-task aborts before the outer task).
            cancelled.extend(
                self._cancel_children_recursive(
                    int(child.id), _depth=_depth + 1
                )
            )
            ok = self._store.mark_cancelled(int(child.id))
            if not ok:
                continue
            cancelled.append(int(child.id))
            log.info(
                "task cascade-cancelled: task=%d parent=%d depth=%d",
                child.id,
                parent_id,
                _depth + 1,
            )
            self._append_event(
                int(child.id),
                type=EVENT_CANCELLED,
                data={
                    "reason": "cascade_from_parent",
                    "parent_task_id": parent_id,
                },
            )
            self._bump_heartbeat(int(child.id))
            if self._input_store is not None:
                try:
                    self._input_store.cancel_pending_for_task(int(child.id))
                except Exception:
                    log.exception(
                        "task input cancel-pending in cascade failed: task=%d",
                        child.id,
                    )
            # Mark in-memory active record so any late emit drops.
            with self._lock:
                active_child = self._active.get(int(child.id))
                if active_child is not None:
                    active_child.cancelled = True
                    active_child.finalized = True
                    state_snapshot = dict(active_child.state or child.state)
                else:
                    state_snapshot = dict(child.state)
            child_handler = self.handler_for(child.handler_name)
            if child_handler is not None:
                try:
                    self._executor.submit(
                        child_handler.cancel, state_snapshot
                    )
                except Exception:  # pragma: no cover - executor race
                    log.exception(
                        "task cascade-cancel handler.cancel submit failed "
                        "task=%d",
                        child.id,
                    )
            # Fire the same ``task_completed`` listener event so the
            # frontend sees the cascade in real time, not as a delta
            # on the next REST refresh.
            fresh_child = self._store.get(int(child.id))
            if fresh_child is not None:
                self._dispatch_to_listeners(
                    TASK_LISTENER_COMPLETED,
                    {"task": task_snapshot(fresh_child)},
                )
            # Also emit a brain-queue event so any cue surface sees
            # the cancellation; ``notify_aiko=False`` because Aiko's
            # one parent-level "cancelled" cue is enough — children
            # falling silent is implied.
            self._emit_brain_event(
                TaskResultEvent(
                    task_id=_format_task_id(int(child.id)),
                    session_key=self._session_key_for(child.user_id),
                    status=STATUS_CANCELLED,  # type: ignore[arg-type]
                    title=child.title,
                    result_summary="cancelled as part of parent",
                    notify_aiko=False,
                    visible_to_user=bool(child.visible_to_user),
                )
            )
        return cancelled

    # ── reads ─────────────────────────────────────────────────────────

    def get(self, task_id: int) -> TaskRow | None:
        """Pass-through to the store. Returns the freshest persisted row."""
        return self._store.get(int(task_id))

    def list_running(self, user_id: str | None = None) -> list[TaskRow]:
        """Active rows for the running-tasks inner-life provider.

        Reads the store directly so callers always see the freshest
        ``status`` / ``last_message`` / ``input_request``. The
        in-memory ``_active`` table is a write-side cache for emit
        routing only — never the source of truth.
        """
        return self._store.list_running(user_id=user_id)

    def list_for_user(
        self,
        user_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[TaskRow]:
        """REST + UI history pagination. Pass-through to the store."""
        return self._store.list_for_user(
            user_id, status=status, limit=limit, offset=offset
        )

    def count_active_for_user(self, user_id: str) -> int:
        """For per-user-cap pre-checks. Pass-through to the store."""
        return self._store.count_active_for_user(user_id)

    # ── test hook ────────────────────────────────────────────────────

    def wait_for_task(self, task_id: int, *, timeout: float = 5.0) -> str:
        """Block until the task reaches a terminal status or timeout.

        **Test helper** — production code should listen for
        :class:`TaskResultEvent` on the brain queue instead. Returns
        the final status string, or ``"timeout"`` if the deadline
        elapsed. Polls via the active record's future + the row
        status; both are needed because handlers that emit a
        terminal outcome from a self-spawned thread (not from the
        primary ``start`` call) leave the future resolved early.
        """
        import time

        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._lock:
                active = self._active.get(int(task_id))
            future = active.future if active else None
            if future is not None:
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    future.result(timeout=remaining)
                except Exception:
                    pass
            row = self._store.get(int(task_id))
            if row is not None and row.status in TERMINAL_STATUSES:
                return row.status
            if time.monotonic() >= deadline:
                return "timeout"
            time.sleep(0.005)

    # ── shutdown ─────────────────────────────────────────────────────

    def shutdown(self, *, wait: bool = True, timeout: float | None = 1.0) -> None:
        """Drain the executor + log a summary.

        Mirrors :class:`BrainLoop.stop` semantics: idempotent, safe to
        call from any thread, never raises. ``wait=False`` returns
        immediately and lets pool workers finish on daemon-thread
        decay. ``timeout`` only applies when ``wait=True`` and is best
        effort — Python's ``ThreadPoolExecutor.shutdown`` doesn't
        return early on timeout, but we'll have logged the surrender
        decision either way.

        Schema v17: also stops the heartbeat daemon thread. Stop is
        idempotent so callers can invoke shutdown multiple times.
        """
        # Stop the heartbeat sweep first so a final sweep can't race
        # the executor drain.
        try:
            self._heartbeat.stop(timeout=float(timeout) if timeout else 2.0)
        except Exception:  # pragma: no cover - defensive
            log.exception("task-orchestrator heartbeat stop error")
        if not self._owns_executor:
            log.info(
                "task-orchestrator shutdown: executor=external active=%d",
                len(self._active),
            )
            return
        try:
            self._executor.shutdown(wait=wait, cancel_futures=not wait)
        except Exception as exc:  # pragma: no cover
            log.exception("task-orchestrator shutdown error: exc=%r", exc)
        log.info(
            "task-orchestrator shutdown: active_at_stop=%d", len(self._active)
        )

    # ── internal: invocation runner ──────────────────────────────────

    def _submit_invocation(
        self,
        task_id: int,
        thunk: Callable[[], TaskState],
        kind: str,
    ) -> Future:
        """Submit a handler invocation to the executor with the
        ``task_id`` contextvar set + exception isolation.

        ``thunk`` is a zero-arg closure that wraps the actual
        ``handler.<method>(args..., emit)`` call. Returning a
        :data:`TaskState` is the protocol contract; we persist
        whatever the handler returns via the store and update the
        in-memory cache.
        """
        ctx = contextvars.copy_context()

        def runner() -> TaskState | None:
            token = set_task_id(_format_task_id(task_id))
            try:
                try:
                    new_state = thunk()
                except Exception as exc:
                    log.exception(
                        "task handler error: task=%d kind=%s exc=%r",
                        task_id,
                        kind,
                        exc,
                    )
                    # Treat an unhandled exception as a TaskFailed
                    # outcome so the row reaches a terminal state.
                    self._dispatch_outcome(
                        task_id,
                        TaskFailed(
                            error=f"{type(exc).__name__}: {exc}"[:200],
                        ),
                    )
                    return None
                if isinstance(new_state, dict):
                    with self._lock:
                        active = self._active.get(int(task_id))
                        if active is not None:
                            active.state = dict(new_state)
                    self._store.update_state(int(task_id), dict(new_state))
                return new_state
            finally:
                reset_task_id(token)

        return self._executor.submit(ctx.run, runner)  # type: ignore[arg-type]

    # ── internal: emit / dispatch ────────────────────────────────────

    def _make_emit_for(self, task_id: int) -> TaskEmitFn:
        """Build a closure the handler can invoke as ``emit(outcome)``.

        The closure captures ``task_id`` so the orchestrator knows
        which row to patch without relying on the contextvar (which
        could be wrong if a handler invokes emit from a sub-thread it
        spawned without :func:`contextvars.copy_context`).
        """
        captured_id = int(task_id)

        def emit(outcome: TaskOutcome) -> None:
            self._dispatch_outcome(captured_id, outcome)

        return emit

    def _dispatch_outcome(self, task_id: int, outcome: TaskOutcome) -> None:
        """Route a handler emit to the store + brain queue.

        Centralised so :class:`TaskProgress` /
        :class:`TaskInputNeeded` / :class:`TaskCompleted` /
        :class:`TaskFailed` paths share validation + cancellation
        checks. Best-effort: persist failures are logged but never
        raise back to the handler.
        """
        with self._lock:
            active = self._active.get(int(task_id))
            cancelled = bool(active.cancelled) if active is not None else False
            finalized = bool(active.finalized) if active is not None else False
            notify_aiko_default = (
                active.notify_aiko if active is not None else True
            )
            visible_to_user = (
                active.visible_to_user if active is not None else True
            )
            user_id = active.user_id if active is not None else ""
            handler_name = (
                active.handler_name if active is not None else ""
            )
        if cancelled or finalized:
            log.debug(
                "task emit suppressed: task=%d outcome=%s reason=%s",
                task_id,
                type(outcome).__name__,
                "cancelled" if cancelled else "finalized",
            )
            return

        row = self._store.get(int(task_id))
        if row is None:
            log.warning(
                "task emit on missing row: task=%d outcome=%s",
                task_id,
                type(outcome).__name__,
            )
            return
        # Re-fetch user_id / title from the row when the active
        # record is missing (e.g. after a recovery that wiped the
        # in-memory cache).
        if not user_id:
            user_id = row.user_id
        if not handler_name:
            handler_name = row.handler_name

        if isinstance(outcome, TaskProgress):
            self._handle_progress(row, outcome)
        elif isinstance(outcome, TaskInputNeeded):
            self._handle_input_needed(row, outcome)
        elif isinstance(outcome, TaskCompleted):
            self._handle_completed(
                row, outcome, default_notify=notify_aiko_default
            )
        elif isinstance(outcome, TaskFailed):
            self._handle_failed(
                row, outcome, default_notify=notify_aiko_default
            )
        elif isinstance(outcome, TaskEventEmit):
            self._handle_event_emit(row, outcome)
        else:  # pragma: no cover - defensive guard
            log.warning(
                "task emit unknown outcome: task=%d type=%s",
                task_id,
                type(outcome).__name__,
            )

    def _handle_progress(self, row: TaskRow, outcome: TaskProgress) -> None:
        self._store.update_progress(
            row.id, progress=outcome.progress, message=outcome.message
        )
        # Schema v17: phase promotion to first-class column. Only
        # write when the handler actually supplied one — None leaves
        # the previous phase intact (a handler can choose to mutate
        # progress without re-declaring phase). Empty string clears.
        phase_changed = False
        if outcome.phase is not None:
            prev_phase = row.phase
            new_phase = str(outcome.phase).strip()
            self._store.update_phase(row.id, new_phase)
            if (new_phase or None) != prev_phase:
                phase_changed = True
                # Audit phase transitions on the event log so a
                # replay sees the handler's narrative arc, not just
                # a string of progress beats.
                self._append_event(
                    row.id,
                    type=EVENT_PHASE_CHANGE,
                    data={"from": prev_phase, "to": new_phase or None},
                )
        log.debug(
            "task progress: task=%d progress=%s message=%s phase=%s",
            row.id,
            outcome.progress,
            outcome.message,
            outcome.phase,
        )
        # Append a progress event so the replay timeline carries the
        # human-readable message + the numeric beat. Heartbeat bump
        # piggybacks here because every emit is a liveness signal.
        self._append_event(
            row.id,
            type=EVENT_PROGRESS,
            data={
                "progress": outcome.progress,
                "message": outcome.message,
                "phase": outcome.phase,
            },
        )
        self._bump_heartbeat(row.id)
        event = TaskProgressEvent(
            task_id=_format_task_id(row.id),
            progress=outcome.progress,
            message=outcome.message,
            status=STATUS_RUNNING,
        )
        self._emit_brain_event(event)
        # Chunk 13: minimal patch payload — frontend applies it on
        # top of the current row instead of rebroadcasting the whole
        # snapshot every progress tick (cheaper for high-frequency
        # tasks like file_search's per-25-dir progress emits).
        # Schema v17: ``phase`` is now part of the patch so the
        # frontend can render the new column without re-fetching.
        patch: dict[str, Any] = {"status": STATUS_RUNNING}
        if outcome.progress is not None:
            patch["progress"] = float(outcome.progress)
        if outcome.message is not None:
            patch["last_message"] = str(outcome.message)
        if outcome.phase is not None:
            patch["phase"] = (
                str(outcome.phase).strip() or None
            )
        # If the phase changed, the listener still gets the patch via
        # the existing TASK_LISTENER_PROGRESS path; no separate
        # listener kind needed because the patch carries everything.
        del phase_changed  # used for the audit log + future hooks
        self._dispatch_to_listeners(
            TASK_LISTENER_PROGRESS,
            {"task_id": int(row.id), "patch": patch},
        )

    def _handle_input_needed(
        self, row: TaskRow, outcome: TaskInputNeeded
    ) -> None:
        # Schema v17: supersede any older pending input + create a
        # fresh row in the dedicated table. The legacy
        # ``tasks.input_request`` column stays as a denormalised view
        # of the latest pending row for backward compat — the
        # frontend reads either, but the new input store is the
        # source of truth.
        new_input_id: int | None = None
        superseded: int = 0
        if self._input_store is not None:
            try:
                superseded = self._input_store.supersede_pending_for_task(
                    int(row.id)
                )
            except Exception:
                log.exception(
                    "task input supersede failed: task=%d", row.id
                )
            try:
                new_input_id = self._input_store.create(
                    int(row.id),
                    prompt=outcome.prompt,
                    kind=None,
                    options=list(outcome.options) if outcome.options else None,
                )
            except Exception:
                log.exception(
                    "task input create failed: task=%d", row.id
                )
        self._store.mark_awaiting_input(
            row.id, prompt=outcome.prompt, options=outcome.options
        )
        log.info(
            "task transition: task=%d from=%s to=%s input_id=%s "
            "superseded=%d",
            row.id,
            row.status,
            STATUS_AWAITING_INPUT,
            new_input_id if new_input_id is not None else "-",
            superseded,
        )
        self._append_event(
            row.id,
            type=EVENT_INPUT_QUESTION,
            data={
                "input_id": new_input_id,
                "prompt": outcome.prompt,
                "options": (
                    list(outcome.options) if outcome.options else None
                ),
                "superseded": superseded,
            },
        )
        self._bump_heartbeat(row.id)
        event = TaskInputNeededEvent(
            task_id=_format_task_id(row.id),
            session_key=self._session_key_for(row.user_id),
            prompt=outcome.prompt,
            options=tuple(outcome.options) if outcome.options else None,
        )
        self._emit_brain_event(event)
        # Chunk 13: fresh fetch so the snapshot reflects the new
        # ``status='awaiting_input'`` + ``input_request`` JSON.
        fresh = self._store.get(int(row.id))
        if fresh is not None:
            self._dispatch_to_listeners(
                TASK_LISTENER_INPUT_NEEDED, {"task": task_snapshot(fresh)}
            )

    def _handle_completed(
        self,
        row: TaskRow,
        outcome: TaskCompleted,
        *,
        default_notify: bool,
    ) -> None:
        result = dict(outcome.result or {})
        self._store.mark_done(row.id, result=result)
        notify_flag = (
            bool(outcome.notify_aiko) if outcome.notify_aiko is not None
            else bool(default_notify)
        )
        # ``result_size`` is the length of the JSON-encoded result blob
        # (matches the doc's grep target). Big handler outputs surface
        # here so a "blew the cue budget" pattern is easy to spot.
        import json as _json
        try:
            result_size = len(_json.dumps(result, ensure_ascii=False, default=str))
        except Exception:
            result_size = 0
        # Schema v17: audit on event log + cancel any still-pending
        # input rows (defensive — a well-behaved handler answers
        # them all before completing, but a buggy one shouldn't
        # leave orphans).
        if self._input_store is not None:
            try:
                self._input_store.cancel_pending_for_task(int(row.id))
            except Exception:
                log.exception(
                    "task input cancel-pending on complete failed: task=%d",
                    row.id,
                )
        self._append_event(
            row.id,
            type=EVENT_COMPLETED,
            data={
                "notify_aiko": notify_flag,
                "result_keys": list(result.keys())[:32],
                "result_size": result_size,
                "summary": result.get("summary")
                if isinstance(result.get("summary"), str)
                else None,
            },
        )
        self._bump_heartbeat(row.id)
        log.info(
            "task completed: task=%d status=%s notify_aiko=%d result_size=%d",
            row.id,
            STATUS_DONE,
            1 if notify_flag else 0,
            result_size,
        )
        event = TaskResultEvent(
            task_id=_format_task_id(row.id),
            session_key=self._session_key_for(row.user_id),
            status=STATUS_DONE,  # type: ignore[arg-type]
            title=row.title,
            result_summary=_summary_text(result),
            notify_aiko=notify_flag,
            visible_to_user=row.visible_to_user,
        )
        self._emit_brain_event(event)
        # Chunk 13: ``task_completed`` covers done / failed / cancelled
        # — the frontend reads ``status`` off the snapshot to decide
        # how to render. Fresh fetch keeps result + completed_at fresh.
        fresh = self._store.get(int(row.id))
        if fresh is not None:
            self._dispatch_to_listeners(
                TASK_LISTENER_COMPLETED, {"task": task_snapshot(fresh)}
            )
        self._finalize(row.id)

    def _handle_failed(
        self,
        row: TaskRow,
        outcome: TaskFailed,
        *,
        default_notify: bool,
    ) -> None:
        error_text = str(outcome.error or "").strip() or "unspecified error"
        self._store.mark_failed(row.id, error=error_text)
        notify_flag = (
            bool(outcome.notify_aiko) if outcome.notify_aiko is not None
            else bool(default_notify)
        )
        # Schema v17: audit + cancel orphan pending inputs.
        if self._input_store is not None:
            try:
                self._input_store.cancel_pending_for_task(int(row.id))
            except Exception:
                log.exception(
                    "task input cancel-pending on fail failed: task=%d",
                    row.id,
                )
        self._append_event(
            row.id,
            type=EVENT_FAILED,
            data={"notify_aiko": notify_flag, "error": error_text},
        )
        self._bump_heartbeat(row.id)
        log.info(
            "task completed: task=%d status=%s notify_aiko=%d error=%s",
            row.id,
            STATUS_FAILED,
            1 if notify_flag else 0,
            error_text,
        )
        event = TaskResultEvent(
            task_id=_format_task_id(row.id),
            session_key=self._session_key_for(row.user_id),
            status=STATUS_FAILED,  # type: ignore[arg-type]
            title=row.title,
            result_summary=error_text,
            error=error_text,
            notify_aiko=notify_flag,
            visible_to_user=row.visible_to_user,
        )
        self._emit_brain_event(event)
        # Chunk 13: see _handle_completed for rationale.
        fresh = self._store.get(int(row.id))
        if fresh is not None:
            self._dispatch_to_listeners(
                TASK_LISTENER_COMPLETED, {"task": task_snapshot(fresh)}
            )
        self._finalize(row.id)

    def _handle_event_emit(self, row: TaskRow, outcome: TaskEventEmit) -> None:
        """Persist a handler-defined audit entry on the event log.

        Schema v17 addition. Does not touch row state; the handler is
        deliberately responsible for separately emitting a
        :class:`TaskProgress` (or a terminal outcome) if it wants the
        UI / cue surface to react. Unknown / empty ``type`` strings
        collapse to :data:`EVENT_CUSTOM` so the schema-v17 invariant
        ("every row has a non-empty type") holds even for sloppy
        handler input.

        Heartbeat is bumped (every emit is a liveness signal) but
        ``progress`` / ``last_message`` / ``phase`` are left
        untouched.
        """
        type_norm = str(outcome.type or "").strip() or EVENT_CUSTOM
        self._append_event(
            row.id, type=type_norm, data=dict(outcome.data or {})
        )
        self._bump_heartbeat(row.id)
        log.debug(
            "task custom event: task=%d type=%s data_keys=%s",
            row.id,
            type_norm,
            sorted(outcome.data.keys()) if isinstance(outcome.data, dict)
            else "?",
        )

    def _finalize(self, task_id: int) -> None:
        """Mark the active record as finalized so any late emit drops."""
        with self._lock:
            active = self._active.get(int(task_id))
            if active is not None:
                active.finalized = True

    def _emit_brain_event(self, event: Any) -> None:
        """Push an event to the brain queue, if wired.

        ``last_emitted_event`` is always updated so tests that don't
        provide a queue can still verify what would have shipped.
        """
        self.last_emitted_event = event
        if self._queue is None:
            return
        try:
            self._queue.put(event)
        except Exception as exc:  # pragma: no cover - defensive
            log.exception(
                "task event enqueue failed: kind=%s exc=%r",
                getattr(event, "kind", "?"),
                exc,
            )

    # ── recovery hook ────────────────────────────────────────────────

    def register_recovered(
        self,
        *,
        task_id: int,
        user_id: str,
        title: str,
        notify_aiko: bool,
        visible_to_user: bool,
    ) -> None:
        """Hook used by :mod:`app.core.tasks.recovery` on boot.

        After the recovery pass marks a stranded row as
        ``interrupted``, it calls this method so the orchestrator
        emits the matching :class:`TaskResultEvent` (status
        ``interrupted`` → maps to ``cancelled``-style cue) for Aiko
        to surface a "the X task stopped, want me to retry?" line on
        her next turn.

        The interrupted row does not get a worker submitted —
        explicit user intent is required for retry, per the doc's
        "Auto-resume is a sharper footgun than asking once" stance.
        """
        log.info(
            "task recovered on boot: task=%d was_status=running "
            "now_status=%s",
            int(task_id),
            STATUS_INTERRUPTED,
        )
        # Schema v17: cancel any orphan pending inputs (a stranded
        # row that was awaiting_input at crash time is recorded as
        # ``preserved`` upstream and never reaches this hook; this
        # path only catches the rarer running-row-with-pending-input
        # case).
        if self._input_store is not None:
            try:
                self._input_store.cancel_pending_for_task(int(task_id))
            except Exception:
                log.exception(
                    "task input cancel-pending on recover failed: task=%d",
                    int(task_id),
                )
        # Audit the boot-recovery transition on the event log so the
        # replay history shows the discontinuity.
        self._append_event(
            int(task_id),
            type=EVENT_INTERRUPTED,
            data={"reason": "boot_recovery", "title": title},
        )
        # We use TaskResultEvent with status="cancelled" for the
        # brain-queue shape (it's the same UI/cue path) but tag the
        # result_summary so the loop's downstream copy can read
        # "stopped" specifically. The status string carried on the
        # event is "cancelled" because that's the closest documented
        # union member; the row itself carries the real "interrupted"
        # status for REST + MCP introspection.
        event = TaskResultEvent(
            task_id=_format_task_id(task_id),
            session_key=self._session_key_for(user_id),
            status=STATUS_CANCELLED,  # type: ignore[arg-type]
            title=title,
            result_summary="task stopped when we last talked",
            notify_aiko=notify_aiko,
            visible_to_user=visible_to_user,
        )
        self._emit_brain_event(event)


def _summary_text(result: dict[str, Any]) -> str:
    """Cheap one-line summary used for the cue text + log line.

    Handlers may include a ``"summary"`` key in their result for a
    custom one-liner; otherwise we fall back to a compact rendering
    of the dict for logging only. The orchestrator never spends LLM
    tokens here — the persona block teaches Aiko how to render task
    outcomes naturally from the result blob.
    """
    if not isinstance(result, dict):
        return ""
    summary = result.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()[:200]
    keys = list(result.keys())
    return ("result keys=" + ",".join(map(str, keys[:8])))[:200]


__all__ = ["TaskOrchestrator", "DEFAULT_PER_USER_CAP"]
