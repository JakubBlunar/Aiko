"""Tests for :mod:`app.core.tasks.task_heartbeat`.

Pin the sweep loop's behaviour in both ``warn`` and ``fail`` modes
without relying on real wall-clock waits. The sweeper exposes
:meth:`HeartbeatChecker.run_once` for exactly this — tests adjust the
``heartbeat_at`` column directly and invoke the sweep one step at a
time.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.infra.chat_database import ChatDatabase
from app.core.tasks.task_events import EVENT_HEARTBEAT_STALLED, TaskEventStore
from app.core.tasks.task_handler import STATUS_FAILED, STATUS_RUNNING
from app.core.tasks.task_heartbeat import (
    ACTION_FAIL,
    ACTION_WARN,
    HeartbeatChecker,
)
from app.core.tasks.task_store import TaskStore


class _Fixture:
    def __init__(self) -> None:
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"
        self.db: ChatDatabase | None = ChatDatabase(self.db_path)
        assert self.db is not None
        self.store = TaskStore(self.db)
        self.event_store = TaskEventStore(self.db)

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

    def _set_heartbeat(self, task_id: int, *, seconds_ago: int) -> None:
        anchor = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
        assert self.db is not None
        conn = self.db._get_conn()
        conn.execute(
            "UPDATE tasks SET heartbeat_at = ? WHERE id = ?",
            (anchor.isoformat(), int(task_id)),
        )
        conn.commit()


class WarnModeTests(unittest.TestCase):
    def test_warn_appends_event_but_leaves_row_running(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t", state={}
            )
            f._set_heartbeat(tid, seconds_ago=600)
            checker = HeartbeatChecker(
                f.store,
                event_store=f.event_store,
                check_interval_seconds=5,
                stalled_seconds=300,
                action=ACTION_WARN,
                enabled=True,
            )
            self.assertEqual(checker.run_once(), 1)
            row = f.store.get(tid)
            assert row is not None
            self.assertEqual(row.status, STATUS_RUNNING)
            latest = f.event_store.latest_for_task(
                tid, type=EVENT_HEARTBEAT_STALLED
            )
            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(latest.type, EVENT_HEARTBEAT_STALLED)
        finally:
            f.close()

    def test_warn_skips_fresh_rows(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t", state={}
            )
            # Created moments ago; heartbeat_at is seeded to created_at.
            checker = HeartbeatChecker(
                f.store, event_store=f.event_store,
                stalled_seconds=300, action=ACTION_WARN,
            )
            self.assertEqual(checker.run_once(), 0)
            # No event row created either.
            self.assertEqual(
                f.event_store.count_for_task(tid),
                0,
            )
        finally:
            f.close()


class FailModeTests(unittest.TestCase):
    def test_fail_marks_row_failed(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t", state={}
            )
            f._set_heartbeat(tid, seconds_ago=900)
            checker = HeartbeatChecker(
                f.store,
                event_store=f.event_store,
                stalled_seconds=300,
                action=ACTION_FAIL,
            )
            self.assertEqual(checker.run_once(), 1)
            row = f.store.get(tid)
            assert row is not None
            self.assertEqual(row.status, STATUS_FAILED)
            self.assertIn("stalled", str(row.error or ""))
            self.assertEqual(checker.failed_total, 1)
        finally:
            f.close()


class DisabledTests(unittest.TestCase):
    def test_disabled_run_once_is_noop(self) -> None:
        f = _Fixture()
        try:
            tid = f.store.create(
                user_id="u", handler_name="h", title="t", state={}
            )
            f._set_heartbeat(tid, seconds_ago=600)
            checker = HeartbeatChecker(
                f.store, event_store=f.event_store,
                stalled_seconds=300, enabled=False,
            )
            self.assertEqual(checker.run_once(), 0)
            row = f.store.get(tid)
            assert row is not None
            self.assertEqual(row.status, STATUS_RUNNING)
        finally:
            f.close()


class ConfigClampTests(unittest.TestCase):
    def test_invalid_action_clamps_to_warn(self) -> None:
        checker = HeartbeatChecker(
            store=object(),  # type: ignore[arg-type]
            event_store=None,
            stalled_seconds=300,
            action="nonsense",
        )
        self.assertEqual(checker._action, ACTION_WARN)

    def test_floor_clamps(self) -> None:
        checker = HeartbeatChecker(
            store=object(),  # type: ignore[arg-type]
            event_store=None,
            check_interval_seconds=1,  # below the 5s floor
            stalled_seconds=1,  # below the 60s floor
            action=ACTION_WARN,
        )
        self.assertGreaterEqual(checker._interval, 5)
        self.assertGreaterEqual(checker._stalled_seconds, 60)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
