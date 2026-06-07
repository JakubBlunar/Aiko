"""Tests for :mod:`app.core.tasks.task_cleanup_worker`.

Pins the prune-by-completed_at semantics + cascade-delete of events
and inputs. Uses the SQLite ``UPDATE`` escape hatch to age rows
without waiting.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.infra.chat_database import ChatDatabase
from app.core.tasks.task_cleanup_worker import TaskCleanupWorker
from app.core.tasks.task_events import EVENT_PROGRESS, TaskEventStore
from app.core.tasks.task_handler import STATUS_RUNNING
from app.core.tasks.task_inputs import TaskInputStore
from app.core.tasks.task_store import TaskStore


class _Fixture:
    def __init__(self) -> None:
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"
        self.db: ChatDatabase | None = ChatDatabase(self.db_path)
        assert self.db is not None
        self.store = TaskStore(self.db)
        self.event_store = TaskEventStore(self.db)
        self.input_store = TaskInputStore(self.db)

    def close(self) -> None:
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

    def _make_done_task(self, *, days_ago: int) -> int:
        tid = self.store.create(
            user_id="u", handler_name="h", title="t", state={}
        )
        self.store.mark_done(tid, result={"ok": True})
        assert self.db is not None
        # Manually age completed_at.
        anchor = (
            datetime.now(timezone.utc) - timedelta(days=days_ago)
        ).isoformat()
        conn = self.db._get_conn()
        conn.execute(
            "UPDATE tasks SET completed_at = ? WHERE id = ?",
            (anchor, int(tid)),
        )
        conn.commit()
        return tid


class CleanupTests(unittest.TestCase):
    def test_old_terminal_rows_are_deleted(self) -> None:
        f = _Fixture()
        try:
            tid_old = f._make_done_task(days_ago=60)
            tid_fresh = f._make_done_task(days_ago=1)
            worker = TaskCleanupWorker(
                f.store,
                event_store=f.event_store,
                input_store=f.input_store,
                retention_days=30,
                interval_seconds=3600,
            )
            stats = worker.run()
            self.assertEqual(stats.get("deleted_tasks"), 1)
            self.assertIsNone(f.store.get(tid_old))
            self.assertIsNotNone(f.store.get(tid_fresh))
        finally:
            f.close()

    def test_running_rows_are_never_deleted(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t", state={}
            )
            # Even with a completed_at hack, running rows aren't
            # eligible. But the store filter doesn't even surface them.
            worker = TaskCleanupWorker(
                f.store,
                event_store=f.event_store,
                input_store=f.input_store,
                retention_days=1,
                interval_seconds=3600,
            )
            stats = worker.run()
            self.assertEqual(stats.get("deleted_tasks"), 0)
            row = f.store.get(tid)
            assert row is not None
            self.assertEqual(row.status, STATUS_RUNNING)
        finally:
            f.close()

    def test_cascade_deletes_events_and_inputs(self) -> None:
        f = _Fixture()
        try:
            tid = f._make_done_task(days_ago=60)
            for _ in range(3):
                f.event_store.append(tid, type=EVENT_PROGRESS, data={"x": 1})
            f.input_store.create(tid, prompt="?")
            worker = TaskCleanupWorker(
                f.store,
                event_store=f.event_store,
                input_store=f.input_store,
                retention_days=30,
                interval_seconds=3600,
            )
            stats = worker.run()
            self.assertEqual(stats.get("deleted_tasks"), 1)
            self.assertGreaterEqual(int(stats.get("deleted_events") or 0), 3)
            self.assertGreaterEqual(int(stats.get("deleted_inputs") or 0), 1)
        finally:
            f.close()

    def test_disabled_worker_is_noop(self) -> None:
        f = _Fixture()
        try:
            tid = f._make_done_task(days_ago=60)
            worker = TaskCleanupWorker(
                f.store,
                event_store=f.event_store,
                input_store=f.input_store,
                retention_days=30,
                interval_seconds=3600,
                enabled=False,
            )
            stats = worker.run()
            self.assertTrue(stats.get("skipped"))
            self.assertIsNotNone(f.store.get(tid))
        finally:
            f.close()

    def test_max_rows_per_tick(self) -> None:
        f = _Fixture()
        try:
            for _ in range(5):
                f._make_done_task(days_ago=60)
            worker = TaskCleanupWorker(
                f.store,
                event_store=f.event_store,
                input_store=f.input_store,
                retention_days=30,
                interval_seconds=3600,
                max_rows_per_tick=2,
            )
            stats = worker.run()
            self.assertEqual(stats.get("deleted_tasks"), 2)
        finally:
            f.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
