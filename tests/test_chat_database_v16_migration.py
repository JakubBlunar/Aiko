"""Schema v16 migration: the brain-orchestration ``tasks`` table.

Adds one new TEXT-heavy table with two indexes. Migration is purely
additive (CREATE TABLE IF NOT EXISTS) so existing data is never
touched. Tests cover:

* Fresh-install path creates ``tasks`` + both indexes + bumps
  ``schema_version`` to 16+.
* Upgrade-from-v15 path: force-downgrade an existing DB, drop the
  ``tasks`` table, reopen via :class:`ChatDatabase`, verify the
  migration ladder restores the table + both indexes and bumps
  ``schema_version``. The legacy ``messages`` row survives.
* Re-opening an already-current DB is a no-op (no version drift,
  no duplicate index errors).
* The 19-column DDL contract is pinned in order so callers
  unpacking ``SELECT *`` rows (TaskStore, MCP debug) catch any
  DDL drift at the unit-test layer.
* The default columns (``status='running'``, ``notify_aiko=1``,
  ``visible_to_user=1``, ``initiated_by='aiko'``) match the doc +
  the orchestrator's expectations.

All in-memory + tempfile DBs with the standard close-before-cleanup
pattern so Windows file-locks don't break ``TemporaryDirectory.cleanup``.
"""
from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.infra.chat_database import _SCHEMA_VERSION, ChatDatabase


class _Fixture:
    """Mirror of ``tests/test_chat_database_v15_migration.py::_Fixture``.

    Holds a :class:`ChatDatabase` over a tempfile-backed SQLite
    database. ``close()`` releases the per-thread connection
    explicitly so Windows can delete the temp file.
    """

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


class FreshInstallTests(unittest.TestCase):
    def test_schema_version_at_v16_or_above(self) -> None:
        """Defensive — chunk 2 sets ``_SCHEMA_VERSION`` to exactly 16.
        Future bumps push higher, but the tasks-table contract below
        stays valid as long as 16 sits somewhere on the ladder."""
        self.assertGreaterEqual(_SCHEMA_VERSION, 16)

    def test_tasks_table_exists(self) -> None:
        f = _Fixture()
        try:
            row = f.db._get_conn().execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='tasks'"
            ).fetchone()
            self.assertIsNotNone(row, "tasks table missing on fresh install")
        finally:
            f.close()

    def test_both_indexes_created(self) -> None:
        f = _Fixture()
        try:
            names = sorted(
                r[0]
                for r in f.db._get_conn().execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='tasks'"
                ).fetchall()
            )
            self.assertIn("idx_tasks_status", names)
            self.assertIn("idx_tasks_user_status", names)
        finally:
            f.close()

    def test_column_contract_pinned(self) -> None:
        """The tasks-table DDL is the contract TaskStore depends on.

        Failing this test means the table shape changed — every
        ``_SELECT_COLS`` and ``_row_to_task`` site needs the same
        change in lockstep. Schema v17 added the final three columns
        (``phase`` / ``parent_task_id`` / ``heartbeat_at``) for the
        nested-task / goal-workflow work.
        """
        f = _Fixture()
        try:
            cols = f.db._get_conn().execute(
                "PRAGMA table_info(tasks)"
            ).fetchall()
            names = [c[1] for c in cols]
        finally:
            f.close()
        self.assertEqual(
            names,
            [
                "id",
                "user_id",
                "handler_name",
                "args",
                "state",
                "status",
                "title",
                "progress",
                "last_message",
                "input_request",
                "result",
                "error",
                "notify_aiko",
                "visible_to_user",
                "initiated_by",
                "created_at",
                "updated_at",
                "completed_at",
                "metadata",
                "phase",
                "parent_task_id",
                "heartbeat_at",
            ],
        )

    def test_status_defaults_to_running(self) -> None:
        f = _Fixture()
        try:
            conn = f.db._get_conn()
            conn.execute(
                "INSERT INTO tasks ("
                "  user_id, handler_name, args, state, title, "
                "  created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "u1",
                    "h1",
                    "{}",
                    "{}",
                    "test",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT status, notify_aiko, visible_to_user, initiated_by "
                "FROM tasks LIMIT 1"
            ).fetchone()
        finally:
            f.close()
        self.assertEqual(row[0], "running")
        self.assertEqual(row[1], 1)
        self.assertEqual(row[2], 1)
        self.assertEqual(row[3], "aiko")

    def test_nullable_columns_accept_null(self) -> None:
        """``progress`` / ``last_message`` / ``input_request`` /
        ``result`` / ``error`` / ``completed_at`` / ``metadata`` are
        all nullable — a fresh row uses NULL for every one of them."""
        f = _Fixture()
        try:
            conn = f.db._get_conn()
            conn.execute(
                "INSERT INTO tasks ("
                "  user_id, handler_name, args, state, title, "
                "  created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "u",
                    "h",
                    "{}",
                    "{}",
                    "t",
                    "2026-01-01T00:00:00Z",
                    "2026-01-01T00:00:00Z",
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT progress, last_message, input_request, result, "
                "error, completed_at, metadata FROM tasks LIMIT 1"
            ).fetchone()
        finally:
            f.close()
        self.assertEqual(row, (None, None, None, None, None, None, None))


class UpgradeFromV15Tests(unittest.TestCase):
    """Force a downgrade of the schema, drop the ``tasks`` table,
    then reopen via :class:`ChatDatabase` to confirm the migration
    ladder restores it and bumps ``schema_version``."""

    def test_v15_upgrade_to_v16_creates_tasks_table(self) -> None:
        f = _Fixture()
        try:
            f.db.add_message("s1", "user", "legacy row")
            conn = f.db._get_conn()
            conn.execute("UPDATE schema_version SET version = 15")
            try:
                conn.execute("DROP INDEX IF EXISTS idx_tasks_user_status")
                conn.execute("DROP INDEX IF EXISTS idx_tasks_status")
                conn.execute("DROP TABLE IF EXISTS tasks")
            except sqlite3.OperationalError:
                pass
            conn.commit()
            conn.close()
            f.db._local.conn = None

            # Reopen — migration should re-create tasks + bump version.
            f.db = ChatDatabase(f.db_path)
            conn = f.db._get_conn()
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='tasks'"
            ).fetchone()
            self.assertIsNotNone(row)
            version = conn.execute(
                "SELECT version FROM schema_version"
            ).fetchone()[0]
            self.assertEqual(int(version), _SCHEMA_VERSION)
            self.assertGreaterEqual(_SCHEMA_VERSION, 16)
            # Legacy row preserved.
            rows = f.db.get_messages("s1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].content, "legacy row")
        finally:
            f.close()

    def test_v15_upgrade_recreates_indexes(self) -> None:
        f = _Fixture()
        try:
            conn = f.db._get_conn()
            conn.execute("UPDATE schema_version SET version = 15")
            conn.execute("DROP INDEX IF EXISTS idx_tasks_user_status")
            conn.execute("DROP INDEX IF EXISTS idx_tasks_status")
            conn.execute("DROP TABLE IF EXISTS tasks")
            conn.commit()
            conn.close()
            f.db._local.conn = None

            f.db = ChatDatabase(f.db_path)
            names = sorted(
                r[0]
                for r in f.db._get_conn().execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='tasks'"
                ).fetchall()
            )
            self.assertIn("idx_tasks_status", names)
            self.assertIn("idx_tasks_user_status", names)
        finally:
            f.close()


class NoOpReopenTests(unittest.TestCase):
    def test_reopening_v16_db_is_idempotent(self) -> None:
        """Opening a fully-current DB twice must not change anything.

        Catches regressions in the migration ladder where a defensive
        ALTER / CREATE silently re-fires every boot.
        """
        f = _Fixture()
        try:
            v1 = f.db._get_conn().execute(
                "SELECT version FROM schema_version"
            ).fetchone()[0]
            f.db._local.conn.close()  # type: ignore[union-attr]
            f.db._local.conn = None
            f.db = ChatDatabase(f.db_path)
            v2 = f.db._get_conn().execute(
                "SELECT version FROM schema_version"
            ).fetchone()[0]
            # Exactly one row, version unchanged.
            count = f.db._get_conn().execute(
                "SELECT COUNT(*) FROM schema_version"
            ).fetchone()[0]
            self.assertEqual(v1, v2)
            self.assertEqual(count, 1)
            # tasks table still there.
            row = f.db._get_conn().execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='tasks'"
            ).fetchone()
            self.assertIsNotNone(row)
        finally:
            f.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
