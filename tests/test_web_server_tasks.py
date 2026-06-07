"""End-to-end tests for the ``/api/tasks`` REST surface + WS broadcast bridge (chunk 13).

The REST endpoints exercise the real :class:`TaskOrchestrator` +
:class:`TaskStore` pair (backed by a temporary SQLite DB) so the tests
cover the wire shape, the pagination math, and the orchestrator's
lifecycle in one pass. The WS broadcast bridge is verified through
the FastAPI ``TestClient.websocket_connect`` portal — every orchestrator
lifecycle event must land on every connected client as a JSON frame.

The session stand-in is a MagicMock with two real attributes glued on:

* ``session._task_orchestrator`` — real :class:`TaskOrchestrator`
* ``session._task_store`` — real :class:`TaskStore`

Every other ``session.*`` method goes through MagicMock (the WS hello
frame, listener subscriptions, etc.).
"""
from __future__ import annotations

import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.core.brain import BrainEventQueue
from app.core.infra.chat_database import ChatDatabase
from app.core.tasks import (
    STATUS_DONE,
    TaskCompleted,
    TaskFailed,
    TaskInputNeeded,
    TaskOrchestrator,
    TaskProgress,
    TaskStore,
)
from app.web.server import create_web_app


# ── test handlers ───────────────────────────────────────────────────


class _CompletingHandler:
    """Emits two progress events + TaskCompleted."""

    name = "completing"

    def start(self, args, emit):
        emit(TaskProgress(progress=0.3, message="scanning"))
        emit(TaskProgress(progress=0.8, message="filtering"))
        emit(TaskCompleted(result={"matches": 2, "summary": "found 2"}))
        return {"args": args, "phase": "done"}

    def resume(self, state, emit):
        return state

    def on_input(self, state, answer, emit):
        return state

    def cancel(self, state):
        pass


class _AskingHandler:
    """Emits TaskInputNeeded on start, TaskCompleted on the answer."""

    name = "asking"

    def start(self, args, emit):
        emit(TaskInputNeeded(prompt="which one?", options=["a", "b"]))
        return {"args": args}

    def resume(self, state, emit):
        return state

    def on_input(self, state, answer, emit):
        emit(TaskCompleted(result={"chosen": answer}))
        return {**state, "chosen": answer}

    def cancel(self, state):
        pass


class _FailingHandler:
    name = "failing"

    def start(self, args, emit):
        emit(TaskFailed(error="db is on fire"))
        return {"phase": "errored"}

    def resume(self, state, emit):
        return state

    def on_input(self, state, answer, emit):
        return state

    def cancel(self, state):
        pass


# ── fixture ─────────────────────────────────────────────────────────


class _Fixture:
    """Real orchestrator + store wired into a MagicMock session.

    The MagicMock stubs out every other ``session.*`` method so
    :func:`create_web_app` boots cleanly (the existing listener
    subscriptions just record MagicMock calls). The orchestrator
    is single-threaded for deterministic test ordering.
    """

    def __init__(self, *, user_id: str = "default") -> None:
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"
        self.db: ChatDatabase | None = ChatDatabase(self.db_path)
        self.store = TaskStore(self.db)
        self.queue = BrainEventQueue()
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="web-task"
        )
        self.orch = TaskOrchestrator(
            self.store,
            queue=self.queue,
            executor=self.executor,
        )
        self.user_id = user_id

        session = MagicMock()
        session._user_id = user_id
        session._task_orchestrator = self.orch
        session._task_store = self.store
        session.session_key = "u:s"
        session.effective_chat_model = "test-model"
        session.context_window_size = 8192
        session.context_window_source = "fallback"
        session.avatar_payload.return_value = {}
        session._settings.tts.enabled = True
        self.session = session
        self.app = create_web_app(session)
        # ``TestClient`` must be entered as a context manager for the
        # FastAPI ``@app.on_event("startup")`` hook to fire — that's
        # the hook that calls ``hub.attach_loop(...)``. Without it
        # ``hub._loop`` stays ``None`` and every cross-thread
        # broadcast (i.e. every orchestrator listener fire) is
        # silently dropped before it reaches a connected socket. We
        # do the ``__enter__`` here so ``f.client`` is usable for
        # plain REST calls AND ``ws.websocket_connect`` works for
        # broadcast tests.
        self.client = TestClient(self.app)
        self.client.__enter__()

    def close(self) -> None:
        try:
            self.client.__exit__(None, None, None)
        except Exception:
            pass
        try:
            self.orch.shutdown(wait=True)
        except Exception:
            pass
        try:
            self.executor.shutdown(wait=True)
        except Exception:
            pass
        try:
            self.queue.close()
        except Exception:
            pass
        if self.db is not None:
            conn = getattr(self.db._local, "conn", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                self.db._local.conn = None
            self.db = None
        try:
            self.tmp.cleanup()
        except PermissionError:
            pass

    def spawn(
        self,
        handler_name: str,
        *,
        title: str = "t",
        visible_to_user: bool = True,
        notify_aiko: bool = True,
        args: dict[str, Any] | None = None,
    ) -> int:
        """Create a task and wait for the worker to settle."""
        tid = self.orch.start_task(
            user_id=self.user_id,
            handler_name=handler_name,
            args=args or {},
            title=title,
            notify_aiko=notify_aiko,
            visible_to_user=visible_to_user,
        )
        assert tid is not None
        self.orch.wait_for_task(tid, timeout=2.0)
        return tid


# ── REST: GET /api/tasks ────────────────────────────────────────────


class ListTasksTests(unittest.TestCase):
    def test_returns_empty_list_when_no_tasks(self) -> None:
        f = _Fixture()
        try:
            resp = f.client.get("/api/tasks")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["tasks"], [])
            self.assertEqual(data["count"], 0)
            self.assertEqual(data["total"], 0)
            self.assertTrue(data["enabled"])
        finally:
            f.close()

    def test_returns_completed_task_snapshot(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_CompletingHandler())
            tid = f.spawn("completing", title="search memory")
            resp = f.client.get("/api/tasks")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["count"], 1)
            self.assertEqual(data["total"], 1)
            task = data["tasks"][0]
            self.assertEqual(task["id"], tid)
            self.assertEqual(task["handler_name"], "completing")
            self.assertEqual(task["status"], "done")
            self.assertEqual(task["title"], "search memory")
            self.assertIsNotNone(task["result"])
            self.assertEqual(task["result"]["matches"], 2)
        finally:
            f.close()

    def test_hides_invisible_tasks(self) -> None:
        """``visible_to_user=False`` tasks must not appear in list."""
        f = _Fixture()
        try:
            f.orch.register_handler(_CompletingHandler())
            visible_tid = f.spawn("completing", title="visible")
            f.spawn("completing", title="hidden", visible_to_user=False)
            resp = f.client.get("/api/tasks")
            data = resp.json()
            self.assertEqual(data["count"], 1)
            self.assertEqual(data["total"], 1)
            self.assertEqual(data["tasks"][0]["id"], visible_tid)
        finally:
            f.close()

    def test_pagination_clamps_limit(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_CompletingHandler())
            for i in range(3):
                f.spawn("completing", title=f"t{i}")
            resp = f.client.get("/api/tasks?limit=2&offset=0")
            data = resp.json()
            self.assertEqual(data["count"], 2)
            self.assertEqual(data["total"], 3)
            # Newest-first ordering — last spawn comes back first.
            self.assertEqual(data["tasks"][0]["title"], "t2")
            self.assertEqual(data["tasks"][1]["title"], "t1")
        finally:
            f.close()

    def test_pagination_offset(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_CompletingHandler())
            for i in range(3):
                f.spawn("completing", title=f"t{i}")
            resp = f.client.get("/api/tasks?limit=10&offset=2")
            data = resp.json()
            self.assertEqual(data["count"], 1)
            self.assertEqual(data["total"], 3)
            self.assertEqual(data["tasks"][0]["title"], "t0")
        finally:
            f.close()

    def test_status_filter(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_CompletingHandler())
            f.orch.register_handler(_FailingHandler())
            f.spawn("completing", title="good")
            f.spawn("failing", title="bad")
            resp = f.client.get("/api/tasks?status=done")
            data = resp.json()
            self.assertEqual(data["count"], 1)
            self.assertEqual(data["tasks"][0]["title"], "good")
            resp = f.client.get("/api/tasks?status=failed")
            data = resp.json()
            self.assertEqual(data["count"], 1)
            self.assertEqual(data["tasks"][0]["title"], "bad")
        finally:
            f.close()

    def test_status_filter_rejects_invalid(self) -> None:
        f = _Fixture()
        try:
            resp = f.client.get("/api/tasks?status=garbage")
            self.assertEqual(resp.status_code, 400)
        finally:
            f.close()

    def test_subsystem_disabled_returns_empty(self) -> None:
        """When ``session._task_store`` is None the endpoint stays open."""
        session = MagicMock()
        session._user_id = "default"
        session._task_orchestrator = None
        session._task_store = None
        session.session_key = "u:s"
        session.effective_chat_model = "m"
        session.context_window_size = 8192
        session.avatar_payload.return_value = {}
        session._settings.tts.enabled = True
        app = create_web_app(session)
        with TestClient(app) as client:
            resp = client.get("/api/tasks")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["tasks"], [])
        self.assertFalse(data["enabled"])


# ── REST: GET /api/tasks/{id} ───────────────────────────────────────


class GetTaskTests(unittest.TestCase):
    def test_returns_single_task(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_CompletingHandler())
            tid = f.spawn("completing", title="single")
            resp = f.client.get(f"/api/tasks/{tid}")
            self.assertEqual(resp.status_code, 200)
            task = resp.json()["task"]
            self.assertEqual(task["id"], tid)
            self.assertEqual(task["title"], "single")
        finally:
            f.close()

    def test_unknown_task_404(self) -> None:
        f = _Fixture()
        try:
            resp = f.client.get("/api/tasks/99999")
            self.assertEqual(resp.status_code, 404)
        finally:
            f.close()

    def test_invisible_task_404(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_CompletingHandler())
            tid = f.spawn(
                "completing", title="hidden", visible_to_user=False
            )
            resp = f.client.get(f"/api/tasks/{tid}")
            self.assertEqual(resp.status_code, 404)
        finally:
            f.close()


# ── REST: POST /api/tasks/{id}/cancel ───────────────────────────────


class CancelTaskTests(unittest.TestCase):
    def test_cancel_running_task(self) -> None:
        """An awaiting_input task is the easiest stable target for cancel."""
        f = _Fixture()
        try:
            f.orch.register_handler(_AskingHandler())
            tid = f.orch.start_task(
                user_id=f.user_id, handler_name="asking",
                args={}, title="ask",
            )
            time.sleep(0.1)  # let handler emit input_needed
            resp = f.client.post(f"/api/tasks/{tid}/cancel")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["task_id"], tid)
            self.assertTrue(data["cancelled"])
        finally:
            f.close()

    def test_cancel_already_terminal_idempotent(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_CompletingHandler())
            tid = f.spawn("completing", title="t")
            # First cancel is a no-op (status is already 'done').
            resp = f.client.post(f"/api/tasks/{tid}/cancel")
            self.assertEqual(resp.status_code, 200)
            self.assertFalse(resp.json()["cancelled"])
        finally:
            f.close()

    def test_cancel_unknown_task_404(self) -> None:
        f = _Fixture()
        try:
            resp = f.client.post("/api/tasks/99999/cancel")
            self.assertEqual(resp.status_code, 404)
        finally:
            f.close()

    def test_cancel_invisible_task_404(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_CompletingHandler())
            tid = f.spawn(
                "completing", title="hidden", visible_to_user=False
            )
            resp = f.client.post(f"/api/tasks/{tid}/cancel")
            self.assertEqual(resp.status_code, 404)
        finally:
            f.close()


# ── REST: POST /api/tasks/{id}/answer ───────────────────────────────


class AnswerTaskTests(unittest.TestCase):
    def test_answer_awaiting_input_task(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_AskingHandler())
            tid = f.orch.start_task(
                user_id=f.user_id, handler_name="asking",
                args={}, title="ask",
            )
            time.sleep(0.1)
            resp = f.client.post(
                f"/api/tasks/{tid}/answer", json={"input": "b"}
            )
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["accepted"])
            f.orch.wait_for_task(tid, timeout=2.0)
            row = f.store.get(tid)
            self.assertEqual(row.status, STATUS_DONE)
            self.assertEqual(row.result["chosen"], "b")
        finally:
            f.close()

    def test_answer_accepts_legacy_field_name(self) -> None:
        """Forgiving alias: ``answer`` works in place of ``input``."""
        f = _Fixture()
        try:
            f.orch.register_handler(_AskingHandler())
            tid = f.orch.start_task(
                user_id=f.user_id, handler_name="asking",
                args={}, title="ask",
            )
            time.sleep(0.1)
            resp = f.client.post(
                f"/api/tasks/{tid}/answer", json={"answer": "a"}
            )
            self.assertEqual(resp.status_code, 200)
        finally:
            f.close()

    def test_answer_empty_input_400(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_AskingHandler())
            tid = f.orch.start_task(
                user_id=f.user_id, handler_name="asking",
                args={}, title="ask",
            )
            time.sleep(0.1)
            resp = f.client.post(
                f"/api/tasks/{tid}/answer", json={"input": "   "}
            )
            self.assertEqual(resp.status_code, 400)
        finally:
            f.close()

    def test_answer_missing_field_400(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_AskingHandler())
            tid = f.orch.start_task(
                user_id=f.user_id, handler_name="asking",
                args={}, title="ask",
            )
            time.sleep(0.1)
            resp = f.client.post(f"/api/tasks/{tid}/answer", json={})
            self.assertEqual(resp.status_code, 400)
        finally:
            f.close()

    def test_answer_wrong_status_409(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_CompletingHandler())
            tid = f.spawn("completing", title="t")
            resp = f.client.post(
                f"/api/tasks/{tid}/answer", json={"input": "x"}
            )
            self.assertEqual(resp.status_code, 409)
        finally:
            f.close()

    def test_answer_unknown_task_404(self) -> None:
        f = _Fixture()
        try:
            resp = f.client.post(
                "/api/tasks/99999/answer", json={"input": "x"}
            )
            self.assertEqual(resp.status_code, 404)
        finally:
            f.close()

    def test_answer_invisible_task_404(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_AskingHandler())
            tid = f.orch.start_task(
                user_id=f.user_id, handler_name="asking",
                args={}, title="ask",
                visible_to_user=False,
            )
            time.sleep(0.1)
            resp = f.client.post(
                f"/api/tasks/{tid}/answer", json={"input": "a"}
            )
            self.assertEqual(resp.status_code, 404)
        finally:
            f.close()


# ── WS broadcast bridge ─────────────────────────────────────────────


def _drain_until(
    ws: Any, predicate: Any, *, max_messages: int = 50
) -> dict[str, Any]:
    """Read JSON frames until ``predicate`` matches or budget exhausted.

    Mirrors the helper in :mod:`tests.test_web_server_voice_owner` —
    ``receive_json`` blocks until a frame arrives, so the test must
    always trigger a broadcast that satisfies the predicate before
    calling this. Used by the positive-broadcast tests below.
    """
    for _ in range(max_messages):
        msg = ws.receive_json()
        if predicate(msg):
            return msg
    raise AssertionError("expected message never arrived")


def _collect_all_until(
    ws: Any, predicate: Any, *, max_messages: int = 50
) -> list[dict[str, Any]]:
    """Read frames and return everything seen *until* ``predicate`` matches.

    Includes the matching final frame. Used by the visibility-filter
    test below: we always end on a known-broadcast (the visible-task
    completion) so we can scan the prefix for the *absence* of any
    hidden-task frames.
    """
    out: list[dict[str, Any]] = []
    for _ in range(max_messages):
        msg = ws.receive_json()
        out.append(msg)
        if predicate(msg):
            return out
    raise AssertionError("predicate never matched")


class WsBroadcastTests(unittest.TestCase):
    """The WS bridge fans every orchestrator event to connected clients."""

    def test_task_started_broadcast(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_CompletingHandler())
            with f.client.websocket_connect("/ws") as ws:
                ws.receive_json()  # drain hello
                tid = f.spawn("completing", title="started")
                frame = _drain_until(
                    ws, lambda m: m.get("type") == "task_started"
                )
                self.assertEqual(frame["task"]["id"], tid)
                self.assertEqual(frame["task"]["status"], "running")
                self.assertEqual(frame["task"]["title"], "started")
        finally:
            f.close()

    def test_task_progress_broadcast(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_CompletingHandler())
            with f.client.websocket_connect("/ws") as ws:
                ws.receive_json()
                f.spawn("completing", title="p")
                frame = _drain_until(
                    ws, lambda m: m.get("type") == "task_progress"
                )
                self.assertIn("task_id", frame)
                self.assertIn("patch", frame)
                self.assertEqual(frame["patch"]["status"], "running")
                self.assertIn("progress", frame["patch"])
                self.assertIn("last_message", frame["patch"])
        finally:
            f.close()

    def test_task_completed_broadcast(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_CompletingHandler())
            with f.client.websocket_connect("/ws") as ws:
                ws.receive_json()
                tid = f.spawn("completing", title="done-cast")
                done = _drain_until(
                    ws, lambda m: m.get("type") == "task_completed"
                )
                self.assertEqual(done["task"]["id"], tid)
                self.assertEqual(done["task"]["status"], "done")
                self.assertEqual(done["task"]["result"]["matches"], 2)
        finally:
            f.close()

    def test_task_input_needed_broadcast(self) -> None:
        f = _Fixture()
        try:
            f.orch.register_handler(_AskingHandler())
            with f.client.websocket_connect("/ws") as ws:
                ws.receive_json()
                tid = f.orch.start_task(
                    user_id=f.user_id, handler_name="asking",
                    args={}, title="ask-cast",
                )
                cue = _drain_until(
                    ws, lambda m: m.get("type") == "task_input_needed"
                )
                self.assertEqual(cue["task"]["id"], tid)
                self.assertEqual(cue["task"]["status"], "awaiting_input")
                self.assertEqual(
                    cue["task"]["input_request"]["prompt"], "which one?"
                )
        finally:
            f.close()

    def test_invisible_task_filtered_from_broadcast(self) -> None:
        """Spawn an invisible task, then a visible one; assert the
        visible-task `task_completed` arrives but no frames carry the
        hidden task's id along the way."""
        f = _Fixture()
        try:
            f.orch.register_handler(_CompletingHandler())
            with f.client.websocket_connect("/ws") as ws:
                ws.receive_json()
                hidden_tid = f.spawn(
                    "completing", title="hidden", visible_to_user=False
                )
                visible_tid = f.spawn(
                    "completing", title="visible", visible_to_user=True
                )
                seen = _collect_all_until(
                    ws,
                    lambda m: (
                        m.get("type") == "task_completed"
                        and m.get("task", {}).get("id") == visible_tid
                    ),
                )
                # Hidden task ID must never appear in any broadcast.
                for frame in seen:
                    task = frame.get("task")
                    if isinstance(task, dict):
                        self.assertNotEqual(task.get("id"), hidden_tid)
                    self.assertNotEqual(
                        frame.get("task_id"), hidden_tid
                    )
        finally:
            f.close()

    def test_listener_subscribed_on_app_boot(self) -> None:
        """``create_web_app`` registers exactly one listener."""
        f = _Fixture()
        try:
            with f.orch._listeners_lock:
                self.assertEqual(len(f.orch._task_listeners), 1)
        finally:
            f.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
