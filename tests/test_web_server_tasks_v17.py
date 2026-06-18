"""Schema v17 REST + WS tests for ``/api/tasks/{id}/events`` and
``/api/tasks/{id}/inputs``, plus the ``phase`` field round-trip
through ``GET /api/tasks/{id}`` and ``task_progress`` patches.
"""
from __future__ import annotations

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
    EVENT_PROGRESS,
    EVENT_STARTED,
    TaskCompleted,
    TaskEventStore,
    TaskInputNeeded,
    TaskInputStore,
    TaskOrchestrator,
    TaskProgress,
    TaskStore,
)
from app.web.server import create_web_app


class _PhaseHandler:
    name = "phased"

    def start(self, args, emit):
        emit(TaskProgress(progress=0.3, message="scanning", phase="scanning"))
        emit(TaskProgress(progress=0.9, message="matching", phase="matching"))
        emit(TaskCompleted(result={"summary": "ok"}))
        return {}

    def resume(self, state, emit):
        return state

    def on_input(self, state, answer, emit):
        return state

    def cancel(self, state):
        pass


class _AskingHandler:
    name = "asking_v17"

    def start(self, args, emit):
        emit(TaskInputNeeded(prompt="confirm?", options=["yes", "no"]))
        return {}

    def resume(self, state, emit):
        return state

    def on_input(self, state, answer, emit):
        emit(TaskCompleted(result={"answer": answer}))
        return state

    def cancel(self, state):
        pass


class _Fixture:
    def __init__(self, *, user_id: str = "default") -> None:
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"
        self.db: ChatDatabase | None = ChatDatabase(self.db_path)
        self.store = TaskStore(self.db)
        self.event_store = TaskEventStore(self.db)
        self.input_store = TaskInputStore(self.db)
        self.queue = BrainEventQueue()
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="web-task-v17"
        )
        self.orch = TaskOrchestrator(
            self.store,
            queue=self.queue,
            executor=self.executor,
            event_store=self.event_store,
            input_store=self.input_store,
            heartbeat_enabled=False,
        )
        self.user_id = user_id

        session = MagicMock()
        session._user_id = user_id
        session._task_orchestrator = self.orch
        session._task_store = self.store
        session._task_event_store = self.event_store
        session._task_input_store = self.input_store
        session.session_key = "u:s"
        session.effective_chat_model = "test-model"
        session.context_window_size = 8192
        session.context_window_source = "fallback"
        session.avatar_payload.return_value = {}
        session._settings.tts.enabled = True
        self.session = session
        self.app = create_web_app(session)
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

    def spawn_phased(self) -> int:
        self.orch.register_handler(_PhaseHandler())
        tid = self.orch.start_task(
            user_id=self.user_id,
            handler_name="phased",
            args={},
            title="t",
        )
        assert tid is not None
        self.orch.wait_for_task(tid, timeout=2.0)
        return tid

    def spawn_asking(self) -> int:
        self.orch.register_handler(_AskingHandler())
        tid = self.orch.start_task(
            user_id=self.user_id,
            handler_name="asking_v17",
            args={},
            title="t",
        )
        assert tid is not None
        import time
        for _ in range(20):
            if self.input_store.latest_pending(tid) is not None:
                break
            time.sleep(0.05)
        return tid


class EventsEndpointTests(unittest.TestCase):
    def test_list_events_returns_chronological_log(self) -> None:
        f = _Fixture()
        try:
            tid = f.spawn_phased()
            resp = f.client.get(f"/api/tasks/{tid}/events")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["task_id"], tid)
            types = [e["type"] for e in data["events"]]
            self.assertEqual(types[0], EVENT_STARTED)
            self.assertIn(EVENT_PROGRESS, types)
            self.assertGreater(data["total"], 0)
        finally:
            f.close()

    def test_list_events_404_for_unknown_task(self) -> None:
        f = _Fixture()
        try:
            resp = f.client.get("/api/tasks/9999/events")
            self.assertEqual(resp.status_code, 404)
        finally:
            f.close()

    def test_list_events_limit_and_offset(self) -> None:
        f = _Fixture()
        try:
            tid = f.spawn_phased()
            resp = f.client.get(
                f"/api/tasks/{tid}/events?limit=1&offset=0"
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(len(data["events"]), 1)
            self.assertGreater(data["total"], 1)
        finally:
            f.close()


class InputsEndpointTests(unittest.TestCase):
    def test_list_inputs_returns_pending_row(self) -> None:
        f = _Fixture()
        try:
            tid = f.spawn_asking()
            resp = f.client.get(f"/api/tasks/{tid}/inputs")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["task_id"], tid)
            self.assertEqual(len(data["inputs"]), 1)
            row = data["inputs"][0]
            self.assertEqual(row["prompt"], "confirm?")
            self.assertEqual(row["options"], ["yes", "no"])
            self.assertEqual(row["status"], "pending")
        finally:
            f.close()

    def test_list_inputs_404_for_unknown_task(self) -> None:
        f = _Fixture()
        try:
            resp = f.client.get("/api/tasks/9999/inputs")
            self.assertEqual(resp.status_code, 404)
        finally:
            f.close()

    def test_list_inputs_after_answer_marks_answered(self) -> None:
        f = _Fixture()
        try:
            tid = f.spawn_asking()
            resp = f.client.post(
                f"/api/tasks/{tid}/answer", json={"input": "yes"},
            )
            self.assertEqual(resp.status_code, 200)
            f.orch.wait_for_task(tid, timeout=2.0)
            resp = f.client.get(f"/api/tasks/{tid}/inputs")
            data = resp.json()
            self.assertEqual(data["inputs"][0]["status"], "answered")
            self.assertEqual(data["inputs"][0]["response"], "yes")
        finally:
            f.close()


class PhaseFieldTests(unittest.TestCase):
    def test_get_task_includes_phase(self) -> None:
        f = _Fixture()
        try:
            tid = f.spawn_phased()
            resp = f.client.get(f"/api/tasks/{tid}")
            self.assertEqual(resp.status_code, 200)
            task = resp.json()["task"]
            self.assertEqual(task.get("phase"), "matching")
            self.assertIn("heartbeat_at", task)
            self.assertIn("parent_task_id", task)
        finally:
            f.close()


class ChildrenEndpointTests(unittest.TestCase):
    def test_list_children_returns_spawn_order(self) -> None:
        f = _Fixture()
        try:
            parent = f.store.create(
                user_id=f.user_id,
                handler_name="goal_workflow",
                title="parent",
                args={},
                state={},
            )
            c1 = f.store.create(
                user_id=f.user_id,
                handler_name="file_write",
                title="step 1",
                args={},
                state={},
                parent_task_id=parent,
            )
            c2 = f.store.create(
                user_id=f.user_id,
                handler_name="file_read",
                title="step 2",
                args={},
                state={},
                parent_task_id=parent,
            )
            resp = f.client.get(f"/api/tasks/{parent}/children")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["task_id"], parent)
            self.assertEqual([c["id"] for c in data["children"]], [c1, c2])
            self.assertEqual(data["count"], 2)
        finally:
            f.close()

    def test_list_children_empty_for_leaf_task(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id=f.user_id,
                handler_name="file_search",
                title="leaf",
                args={},
                state={},
            )
            resp = f.client.get(f"/api/tasks/{tid}/children")
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["children"], [])
            self.assertEqual(data["count"], 0)
        finally:
            f.close()

    def test_list_children_404_for_unknown_task(self) -> None:
        f = _Fixture()
        try:
            resp = f.client.get("/api/tasks/9999/children")
            self.assertEqual(resp.status_code, 404)
        finally:
            f.close()


class RootsOnlyListTests(unittest.TestCase):
    def test_roots_only_excludes_children_and_total(self) -> None:
        f = _Fixture()
        try:
            parent = f.store.create(
                user_id=f.user_id,
                handler_name="goal_workflow",
                title="parent",
                args={},
                state={},
            )
            child = f.store.create(
                user_id=f.user_id,
                handler_name="file_write",
                title="step",
                args={},
                state={},
                parent_task_id=parent,
            )
            # Unfiltered list carries both rows.
            all_data = f.client.get("/api/tasks").json()
            all_ids = {t["id"] for t in all_data["tasks"]}
            self.assertIn(child, all_ids)
            self.assertEqual(all_data["total"], 2)
            # roots_only collapses to the parent + matching total.
            roots = f.client.get("/api/tasks?roots_only=true").json()
            roots_ids = {t["id"] for t in roots["tasks"]}
            self.assertIn(parent, roots_ids)
            self.assertNotIn(child, roots_ids)
            self.assertEqual(roots["total"], 1)
        finally:
            f.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
