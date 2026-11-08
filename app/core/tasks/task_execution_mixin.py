"""Task admission, cancellation and runtime ownership behind TaskOrchestrator."""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field
import logging
from typing import Any

from app.core.brain.events import TaskResultEvent
from app.core.tasks.task_events import (
    EVENT_CANCELLED,
    EVENT_CHILD_SPAWNED,
    EVENT_STARTED,
)
from app.core.tasks.task_handler import (
    ACTIVE_STATUSES,
    INITIATED_BY_AIKO,
    STATUS_CANCELLED,
    TERMINAL_STATUSES,
    TaskState,
)
from app.core.tasks.task_store import TaskRow

log = logging.getLogger("app.task_orchestrator")

TASK_LISTENER_STARTED: str = "task_started"
TASK_LISTENER_PROGRESS: str = "task_progress"
TASK_LISTENER_INPUT_NEEDED: str = "task_input_needed"
TASK_LISTENER_COMPLETED: str = "task_completed"


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
        "work": (
            {
                key: row.state.get(key)
                for key in (
                    "contract",
                    "profile",
                    "plan",
                    "notes",
                    "iteration",
                    "children",
                    "active_seconds",
                    "queue_seconds",
                    "context_budget",
                    "tokens",
                    "limits",
                    "pending",
                    "verification",
                )
            }
            if row.state.get("harness_version") == 1
            else None
        ),
        "user_id": str(row.user_id),
        "handler_name": str(row.handler_name),
        "title": str(row.title or ""),
        "status": str(row.status),
        "progress": (float(row.progress) if row.progress is not None else None),
        "last_message": row.last_message,
        "phase": row.phase,
        "initiated_by": str(row.initiated_by),
        "args": dict(row.args or {}),
        "input_request": (dict(row.input_request) if row.input_request is not None else None),
        "result": (dict(row.result) if row.result is not None else None),
        "error": row.error,
        "notify_aiko": bool(row.notify_aiko),
        "visible_to_user": bool(row.visible_to_user),
        "created_at": str(row.created_at or ""),
        "updated_at": str(row.updated_at or ""),
        "completed_at": row.completed_at,
        "heartbeat_at": row.heartbeat_at,
        "parent_task_id": row.parent_task_id,
        "metadata": (dict(row.metadata) if row.metadata is not None else None),
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


class TaskExecutionMixin:
    """Execution lifecycle shared by legacy handlers and durable workflows."""

    def start_task(self, **kwargs: Any) -> int | None:
        # Admission and parent cancellation share a lock. A child is either
        # registered before the cascade or rejected after cancellation.
        with self._lock:
            if getattr(self, "_runtime_shutting_down", False):
                return None
            return self._start_task_locked(**kwargs)

    def _start_task_locked(
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
        if parent_task_id is not None:
            parent = self._store.get(int(parent_task_id))
            if parent is None or parent.user_id != user_id or parent.status != "running":
                log.info("task spawn rejected: parent unavailable parent=%s", parent_task_id)
                return None
            from app.core.tasks.workflow.step_context import current_step

            step = current_step.get()
            if step is not None and step[0] == parent_task_id:
                existing = self._store.child_for_step(parent_task_id, step[1])
                if existing is not None:
                    return existing.id
                metadata = {**(metadata or {}), "harness_step_id": step[1], "epoch": step[2]}
        running_count = self._store.count_active_for_user(user_id)
        if running_count >= self._per_user_cap:
            log.warning(
                "task spawn rejected: reason=per_user_cap user=%s running_count=%d cap=%d",
                user_id,
                running_count,
                self._per_user_cap,
            )
            return None
        parent_norm = (
            int(parent_task_id) if parent_task_id is not None and int(parent_task_id) > 0 else None
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
            self._dispatch_to_listeners(TASK_LISTENER_STARTED, {"task": task_snapshot(started_row)})
        emit = self._make_emit_for(task_id)
        active.future = self._submit_invocation(
            task_id, lambda: handler.start(dict(args or {}), emit), "start"
        )
        return task_id

    def cancel(self, task_id: int) -> bool:
        with self._lock:
            return self._cancel_task_locked(task_id)

    def _cancel_task_locked(self, task_id: int) -> bool:
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
            cascaded = self._cancel_children_recursive(int(task_id), _depth=0)
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
                "notify_aiko": (bool(active.notify_aiko) if active else bool(row.notify_aiko)),
            },
        )
        self._bump_heartbeat(int(task_id))
        self._emit_brain_event(
            TaskResultEvent(
                task_id=_format_task_id(task_id),
                session_key=self._session_key_for(row.user_id),
                status=STATUS_CANCELLED,  # type: ignore[arg-type]
                title=row.title,
                result_summary="cancelled by user",
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
            self._dispatch_to_listeners(TASK_LISTENER_COMPLETED, {"task": task_snapshot(fresh)})
        with self._lock:
            if active is not None:
                active.finalized = True
        return True

    def _cancel_children_recursive(self, parent_id: int, *, _depth: int) -> list[int]:
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
        children = self._store.list_children(parent_id, statuses=ACTIVE_STATUSES)
        cancelled: list[int] = []
        for child in children:
            # Recurse first so the deepest children cancel before
            # their parents — matches the cleanup order a user would
            # expect (sub-task aborts before the outer task).
            cancelled.extend(self._cancel_children_recursive(int(child.id), _depth=_depth + 1))
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
                    self._executor.submit(child_handler.cancel, state_snapshot)
                except Exception:  # pragma: no cover - executor race
                    log.exception(
                        "task cascade-cancel handler.cancel submit failed task=%d",
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
