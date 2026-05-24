"""Tests for app.core.chat_database.ChatDatabase."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from app.core.chat_database import (
    ChatDatabase,
    MessageRow,
    SummaryRow,
)


class _TempDB:
    """Context manager that provides a ChatDatabase backed by a temp file."""

    def __enter__(self) -> ChatDatabase:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "test.db"
        self.db = ChatDatabase(self.path)
        return self.db

    def __exit__(self, *exc):
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            conn.close()
            self.db._local.conn = None
        try:
            self._dir.cleanup()
        except PermissionError:
            pass


class TestSchemaCreation(unittest.TestCase):
    def test_fresh_database_creates_expected_tables(self):
        with _TempDB() as db:
            conn = db._get_conn()
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for expected in (
                "messages",
                "session_summaries",
                "memories",
                "schema_version",
            ):
                self.assertIn(expected, tables)
            # Obsolete tables must not exist on a fresh schema.
            for obsolete in (
                "personality_notes",
                "recent_topics",
                "message_embeddings",
            ):
                self.assertNotIn(obsolete, tables)

    def test_schema_version_is_v3(self):
        with _TempDB() as db:
            conn = db._get_conn()
            row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], 3)


class TestMessages(unittest.TestCase):
    def test_add_and_get_messages(self):
        with _TempDB() as db:
            mid = db.add_message("s1", "user", "hello", token_count=5)
            self.assertIsInstance(mid, int)
            db.add_message("s1", "assistant", "hi there", token_count=8)

            msgs = db.get_messages("s1")
            self.assertEqual(len(msgs), 2)
            self.assertIsInstance(msgs[0], MessageRow)
            self.assertEqual(msgs[0].role, "user")
            self.assertEqual(msgs[0].content, "hello")
            self.assertEqual(msgs[0].token_count, 5)
            self.assertEqual(msgs[1].role, "assistant")

    def test_get_messages_with_limit(self):
        with _TempDB() as db:
            for i in range(10):
                db.add_message("s1", "user", f"msg {i}")
            msgs = db.get_messages("s1", limit=3)
            self.assertEqual(len(msgs), 3)
            self.assertEqual(msgs[-1].content, "msg 9")

    def test_get_messages_isolates_sessions(self):
        with _TempDB() as db:
            db.add_message("s1", "user", "a")
            db.add_message("s2", "user", "b")
            self.assertEqual(len(db.get_messages("s1")), 1)
            self.assertEqual(len(db.get_messages("s2")), 1)

    def test_message_count(self):
        with _TempDB() as db:
            self.assertEqual(db.get_message_count("s1"), 0)
            db.add_message("s1", "user", "x")
            db.add_message("s1", "user", "y")
            self.assertEqual(db.get_message_count("s1"), 2)

    def test_clear_messages_drops_messages_and_summary(self):
        with _TempDB() as db:
            db.add_message("s1", "user", "hello")
            db.save_summary("s1", "summary", 10, 1)

            deleted = db.clear_messages("s1")
            self.assertEqual(deleted, 1)
            self.assertEqual(db.get_message_count("s1"), 0)
            self.assertIsNone(db.get_latest_summary("s1"))


class TestBackfillLegacyMetaTags(unittest.TestCase):
    def test_dirty_assistant_rows_are_re_stripped(self):
        with _TempDB() as db:
            dirty = (
                "[[reaction:gentle]]\n[[spoken]]Hi there.[[/spoken]]\n"
                "[[detail]]private rambling[[/detail]]"
            )
            db.add_message("s1", "assistant", dirty)
            db._backfill_legacy_meta_tags()  # idempotent re-run
            rows = db.get_messages("s1")
            self.assertEqual(len(rows), 1)
            content = rows[0].content
            self.assertNotIn("[[", content)
            self.assertNotIn("private", content)
            self.assertIn("Hi there", content)


class TestSummaries(unittest.TestCase):
    def test_save_and_get_summary(self):
        with _TempDB() as db:
            self.assertIsNone(db.get_latest_summary("s1"))
            db.save_summary("s1", "conversation about AI", 50, 10)
            row = db.get_latest_summary("s1")
            self.assertIsNotNone(row)
            self.assertIsInstance(row, SummaryRow)
            self.assertEqual(row.summary, "conversation about AI")
            self.assertEqual(row.summary_tokens, 50)
            self.assertEqual(row.messages_summarized, 10)

    def test_latest_summary_returns_newest(self):
        with _TempDB() as db:
            db.save_summary("s1", "old", 10, 5)
            db.save_summary("s1", "new", 20, 10)
            row = db.get_latest_summary("s1")
            self.assertEqual(row.summary, "new")


class TestThreadSafety(unittest.TestCase):
    def test_concurrent_inserts(self):
        with _TempDB() as db:
            errors: list[Exception] = []

            def inserter(thread_id: int):
                try:
                    for i in range(20):
                        db.add_message("s1", "user", f"thread {thread_id} msg {i}")
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=inserter, args=(t,)) for t in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(len(errors), 0, f"Errors in threads: {errors}")
            self.assertEqual(db.get_message_count("s1"), 100)


if __name__ == "__main__":
    unittest.main()
