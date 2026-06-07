"""Schema v17 migration tests.

Pins the three new ``tasks`` columns (``phase`` / ``parent_task_id``
/ ``heartbeat_at``), the new sibling tables (``task_events``,
``task_inputs``), and the migration's idempotency.
"""
from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.infra.chat_database import _SCHEMA_VERSION, ChatDatabase


class _Fixture:
    def __init__(self) -> None:
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"
        self.db: ChatDatabase | None = ChatDatabase(self.db_path)

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


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


class V17ColumnTests(unittest.TestCase):
    def test_schema_version_is_v17(self) -> None:
        f = _Fixture()
        try:
            self.assertEqual(_SCHEMA_VERSION, 17)
            assert f.db is not None
            conn = f.db._get_conn()
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            self.assertEqual(int(row[0]), 17)
        finally:
            f.close()

    def test_tasks_table_has_new_columns(self) -> None:
        f = _Fixture()
        try:
            assert f.db is not None
            cols = _columns(f.db._get_conn(), "tasks")
            self.assertIn("phase", cols)
            self.assertIn("parent_task_id", cols)
            self.assertIn("heartbeat_at", cols)
        finally:
            f.close()

    def test_task_events_table_exists(self) -> None:
        f = _Fixture()
        try:
            assert f.db is not None
            cols = _columns(f.db._get_conn(), "task_events")
            self.assertSetEqual(
                cols, {"id", "task_id", "type", "data", "created_at"}
            )
        finally:
            f.close()

    def test_task_inputs_table_exists(self) -> None:
        f = _Fixture()
        try:
            assert f.db is not None
            cols = _columns(f.db._get_conn(), "task_inputs")
            self.assertSetEqual(
                cols,
                {
                    "id",
                    "task_id",
                    "prompt",
                    "kind",
                    "options",
                    "status",
                    "response",
                    "created_at",
                    "answered_at",
                },
            )
        finally:
            f.close()

    def test_indices_exist(self) -> None:
        f = _Fixture()
        try:
            assert f.db is not None
            conn = f.db._get_conn()
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            self.assertIn("idx_task_events_task", names)
            self.assertIn("idx_task_inputs_task_status", names)
            self.assertIn("idx_tasks_parent", names)
            self.assertIn("idx_tasks_heartbeat", names)
        finally:
            f.close()


class V17IdempotencyTests(unittest.TestCase):
    def test_repeat_open_is_safe(self) -> None:
        # Open + close + reopen the same db file; schema must stay
        # at v17 without raising.
        tmp = TemporaryDirectory()
        db_path = Path(tmp.name) / "chat.db"
        try:
            db1 = ChatDatabase(db_path)
            conn1 = db1._get_conn()
            conn1.close()
            db1._local.conn = None

            db2 = ChatDatabase(db_path)
            row = db2._get_conn().execute(
                "SELECT version FROM schema_version"
            ).fetchone()
            self.assertEqual(int(row[0]), 17)
            db2._get_conn().close()
            db2._local.conn = None
        finally:
            try:
                tmp.cleanup()
            except PermissionError:
                pass


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
