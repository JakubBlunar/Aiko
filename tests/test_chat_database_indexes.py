"""P41 -- ``messages`` is the fastest-growing table; two hot reads full-scanned it.

``messages`` had indexes on ``(session_id, created_at)`` and
``(role, created_at)``. Both are left-prefixed by a column the day/week
digest reader doesn't filter on, so ``messages_in_range``'s bare
``created_at BETWEEN`` scanned every row ever written. ``list_sessions``
had a related problem: its per-session "first user message" subquery
orders by ``id``, which the ``(session_id, created_at)`` index can't
supply.

These tests assert against ``EXPLAIN QUERY PLAN`` rather than timings.
An index that exists but that the planner declines to use is worth
nothing, and a wall-clock assertion on a test-sized table proves neither.
"""
from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.infra.chat_database import ChatDatabase


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        # WAL sidecars can linger a moment on Windows after the last
        # connection closes; a failed rmdir shouldn't fail the test.
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "chat.db"

    def _db(self) -> ChatDatabase:
        db = ChatDatabase(self.path)
        # Connections are thread-local and there is no public close(), so
        # release the one this thread opened; otherwise the DROP INDEX
        # test can't reopen the file cleanly on Windows.
        self.addCleanup(lambda: db._get_conn().close())
        return db

    @staticmethod
    def _index_names(db: ChatDatabase) -> set[str]:
        conn = db._get_conn()
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='messages'",
        ).fetchall()
        return {r[0] for r in rows}

    @staticmethod
    def _plan(db: ChatDatabase, sql: str, params: tuple = ()) -> str:
        conn = db._get_conn()
        rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
        return " | ".join(str(r[-1]) for r in rows)


class IndexPresenceTests(_Base):
    def test_both_new_indexes_exist_on_a_fresh_database(self) -> None:
        names = self._index_names(self._db())
        self.assertIn("idx_messages_created", names)
        self.assertIn("idx_messages_session_role_id", names)

    def test_the_pre_existing_indexes_survive(self) -> None:
        names = self._index_names(self._db())
        self.assertIn("idx_messages_session", names)
        self.assertIn("idx_messages_role_created", names)

    def test_they_land_on_an_existing_database_too(self) -> None:
        # No schema-version bump: the CREATE INDEX statements live in the
        # idempotent script that re-runs on every open, same as P10's.
        # This is what makes an existing install pick them up.
        db = self._db()
        db.add_message("s1", "user", "hello")
        db._get_conn().close()
        conn = sqlite3.connect(self.path)
        conn.execute("DROP INDEX idx_messages_created")
        conn.execute("DROP INDEX idx_messages_session_role_id")
        conn.commit()
        conn.close()

        names = self._index_names(self._db())
        self.assertIn("idx_messages_created", names)
        self.assertIn("idx_messages_session_role_id", names)

    def test_reopening_is_idempotent(self) -> None:
        first = self._index_names(self._db())
        self.assertEqual(self._index_names(self._db()), first)


class QueryPlanTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.db = self._db()
        # ANALYZE-free planning is fine here: these are the only candidate
        # indexes, so the choice doesn't depend on row-count statistics.
        for i in range(40):
            self.db.add_message(f"s{i % 4}", "user" if i % 2 else "assistant",
                                 f"line {i}")

    def test_created_at_range_uses_the_new_index(self) -> None:
        plan = self._plan(
            self.db,
            "SELECT id, session_id, role, content, created_at FROM messages "
            "WHERE created_at >= ? AND created_at <= ? "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            ("2000-01-01", "2100-01-01", 8),
        )
        self.assertIn("idx_messages_created", plan)
        self.assertNotIn("SCAN messages", plan)

    def test_the_range_scan_needs_no_temp_sort(self) -> None:
        # The whole point of trailing ``id``: ``ORDER BY created_at DESC,
        # id DESC`` is satisfied by walking the index backwards. A
        # "USE TEMP B-TREE FOR ORDER BY" line here would mean SQLite is
        # still materialising and sorting the matched rows.
        plan = self._plan(
            self.db,
            "SELECT id FROM messages WHERE created_at >= ? AND created_at <= ? "
            "ORDER BY created_at DESC, id DESC LIMIT 8",
            ("2000-01-01", "2100-01-01"),
        )
        self.assertNotIn("TEMP B-TREE", plan.upper())

    def test_the_first_user_message_subquery_uses_the_new_index(self) -> None:
        plan = self._plan(
            self.db,
            "SELECT content FROM messages WHERE session_id = ? "
            "AND role = 'user' ORDER BY id LIMIT 1",
            ("s1",),
        )
        self.assertIn("idx_messages_session_role_id", plan)
        self.assertNotIn("TEMP B-TREE", plan.upper())

    def test_the_role_scoped_range_query_still_uses_its_own_index(self) -> None:
        # P10's index must not be shadowed by the new ones.
        plan = self._plan(
            self.db,
            "SELECT id FROM messages WHERE role = 'user' AND created_at >= ?",
            ("2000-01-01",),
        )
        self.assertIn("idx_messages_role_created", plan)


class ReadPathTests(_Base):
    """The queries still return the right rows, index changes aside."""

    def test_messages_in_range_still_filters_and_orders(self) -> None:
        db = self._db()
        for i in range(5):
            db.add_message("s1", "user", f"line {i}")
        rows = db.messages_in_range("2000-01-01", "2100-01-01", limit=3)
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["content"] for r in rows],
                         ["line 4", "line 3", "line 2"])

    def test_messages_in_range_excludes_the_live_session(self) -> None:
        db = self._db()
        db.add_message("live", "user", "current")
        db.add_message("old", "user", "previous")
        rows = db.messages_in_range(
            "2000-01-01", "2100-01-01", limit=10, exclude_session_id="live",
        )
        self.assertEqual([r["content"] for r in rows], ["previous"])

    def test_list_sessions_titles_from_the_first_user_message(self) -> None:
        db = self._db()
        db.add_message("s1", "assistant", "greeting first")
        db.add_message("s1", "user", "the real title")
        db.add_message("s1", "user", "a later line")
        sessions = {s["session_id"]: s for s in db.list_sessions()}
        self.assertEqual(sessions["s1"]["title"], "the real title")
        self.assertEqual(sessions["s1"]["message_count"], 3)


if __name__ == "__main__":
    unittest.main()
