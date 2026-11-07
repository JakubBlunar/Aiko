"""Durable workflow checkpoints and waking; the existing task remains the job."""

from __future__ import annotations

from copy import deepcopy
import logging
import threading
from typing import Any

from app.core.tasks.task_execution_mixin import _ActiveTask, task_snapshot

log = logging.getLogger("app.tasks.runtime")


class TaskRuntimeMixin:
    def emit_work_outcome(self, task_id: int, epoch: int, outcome) -> None:
        with self._lock:
            row = self._store.get(task_id)
            if row and row.status == "running" and row.state.get("epoch") == epoch:
                self._dispatch_outcome(task_id, outcome)

    def claim_operation_dispatch(self) -> bool:
        from app.core.infra.log_context import get_task_id

        try:
            task_id = int(str(get_task_id()), 16)
        except (TypeError, ValueError):
            return False
        with self._lock:
            row = self._store.get(task_id)
            if row is None or row.status != "running":
                return False
            if row.parent_task_id is not None:
                parent = self._store.get(row.parent_task_id)
                if parent is None or parent.status in {
                    "cancelled",
                    "failed",
                    "done",
                    "interrupted",
                }:
                    return False
            self._append_event(
                task_id, type="operation_dispatch", data={"handler": row.handler_name}
            )
            return True

    def checkpoint(self, task_id: int, state: dict[str, Any]) -> bool:
        with self._lock:
            saved = self._store.write_checkpoint(task_id, state)
            active = self._active.get(task_id)
            if saved and active is not None:
                active.state = deepcopy(state)
            return saved

    def suspend_work(self, task_id: int, *, phase: str = "waiting_child") -> bool:
        with self._lock:
            ok = self._store.runtime_transition(task_id, status="paused", phase=phase)
        if ok:
            self._runtime_snapshot(task_id)
            self.ensure_work_waker()
        return ok

    def pause_work(self, task_id: int) -> bool:
        row = self._store.get(task_id)
        if row is None or row.state.get("harness_version") != 1:
            return False
        if row.status == "paused":
            # A manual pause owns the wait, even if a child finishes later.
            ok = self._store.runtime_transition(
                task_id, status="paused", phase="user_pause", expected="paused"
            )
            self._runtime_snapshot(task_id)
            return ok
        return self.suspend_work(task_id, phase="user_pause")

    def resume_work(self, task_id: int, *, automatic: bool = False) -> bool:
        with self._lock:
            row = self._store.get(task_id)
            if row is None or row.status != "paused" or row.state.get("harness_version") != 1:
                return False
            if automatic and row.phase == "user_pause":
                return False
            if automatic and row.phase == "budget_exhausted":
                return False
            handler = self.handler_for(row.handler_name)
            if handler is None or (automatic and not handler.ready_to_resume(row.state)):
                return False
            state = deepcopy(row.state)
            state["active_seconds"] += state.pop("active_reserved", 0)
            if row.phase == "budget_exhausted":
                usage = {
                    "iterations": state["iteration"],
                    "children": state["children"],
                    "active_seconds": state["active_seconds"],
                    "tokens": state["tokens"],
                }
                for name, extra, maximum in (
                    ("iterations", 16, 128),
                    ("children", 16, 128),
                    ("active_seconds", 300, 7200),
                    ("tokens", 10000, 200000),
                ):
                    if usage[name] >= maximum:
                        return False
                    state["limits"][name] = min(maximum, state["limits"][name] + extra)
            state["epoch"] = int(state.get("epoch", 0)) + 1
            if not self._store.runtime_transition(
                task_id, status="running", phase="planning", state=state, expected="paused"
            ):
                return False
            active = _ActiveTask(
                task_id=task_id,
                user_id=row.user_id,
                handler_name=row.handler_name,
                notify_aiko=row.notify_aiko,
                visible_to_user=row.visible_to_user,
                state=state,
            )
            self._active[task_id] = active
            active.future = self._submit_invocation(
                task_id,
                lambda: handler.resume(state, self._make_emit_for(task_id)),
                "resume",
            )
        self._runtime_snapshot(task_id)
        return True

    def recover_work(self, row: Any) -> bool:
        if row.state.get("harness_version") != 1 or row.parent_task_id is not None:
            return False
        if row.status == "running":
            if not self._store.runtime_transition(row.id, status="paused", phase="recovering"):
                return False
        self.ensure_work_waker()
        return True

    def ensure_work_waker(self) -> None:
        with self._lock:
            if getattr(self, "_runtime_shutting_down", False):
                return
            thread = getattr(self, "_work_waker", None)
            if thread is not None and thread.is_alive():
                return
            stop = threading.Event()
            self._work_wake_stop = stop

            def wake() -> None:
                while not stop.wait(0.5):
                    try:
                        for row in self._store.list_non_terminal():
                            if row.status == "paused" and row.state.get("harness_version") == 1:
                                self.resume_work(row.id, automatic=True)
                    except Exception:
                        log.debug("work wake sweep failed", exc_info=True)

            self._work_waker = threading.Thread(target=wake, name="work-wakes", daemon=True)
            self._work_waker.start()

    def append_work_message(
        self,
        task_id: int,
        *,
        role: str,
        content: str,
        kind: str = "update",
        profile: str = "general",
    ) -> int:
        if self._event_store is None:
            return 0
        event_id = self._event_store.append(
            task_id,
            type="work_message",
            data={
                "role": role,
                "content": content[:12000],
                "kind": kind,
                "profile": profile,
            },
        )
        self._runtime_snapshot(task_id)
        return event_id

    def work_messages(self, task_id: int, *, after: int | None = None) -> list[Any]:
        if self._event_store is None:
            return []
        if after is None:
            return self._event_store.latest_work_messages(task_id)
        return self._event_store.list_after(task_id, after=after, type="work_message", limit=100)

    def list_children(self, task_id: int) -> list[Any]:
        return self._store.list_children(task_id)

    def post_work_message(
        self, task_id: int, content: str, *, mode: str, client_id: str
    ) -> tuple[int, bool]:
        with self._lock:
            row = self._store.get(task_id)
            if row is None or self._event_store is None:
                raise ValueError("Work is unavailable.")
            existing = self._event_store.find_work_message(task_id, client_id)
            if existing is not None:
                data = existing.data or {}
                if data.get("content") != content or data.get("kind") != mode:
                    raise ValueError("This client message ID already belongs to another message.")
                return existing.id, False
            if mode == "guidance" and row.status not in {"running", "paused", "awaiting_input"}:
                raise ValueError(
                    "This work has finished. Ask a question or start a follow-up in chat."
                )
            handler = self.handler_for(row.handler_name)
            if handler is None or not hasattr(handler, "discuss_work"):
                raise ValueError("This work has no discussion handler.")
            message_id = self._event_store.append(
                task_id,
                type="work_message",
                data={
                    "role": "user",
                    "kind": mode,
                    "content": content,
                    "client_id": client_id,
                },
            )
        handler.discuss_work(task_id, message_id)
        self._runtime_snapshot(task_id)
        return message_id, True

    def _runtime_snapshot(self, task_id: int) -> None:
        row = self._store.get(task_id)
        if row is not None:
            self._dispatch_to_listeners("task_updated", {"task": task_snapshot(row)})
