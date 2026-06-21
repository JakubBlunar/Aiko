"""Tests for app.core.infra.chat_database.ChatDatabase."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from app.core.infra.chat_database import (
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

    def test_schema_version_is_current(self):
        from app.core.infra.chat_database import _SCHEMA_VERSION
        with _TempDB() as db:
            conn = db._get_conn()
            row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], _SCHEMA_VERSION)

    def test_inner_life_tables_created(self):
        """Phase 2/3/4 tables exist on a fresh schema."""
        with _TempDB() as db:
            conn = db._get_conn()
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for expected in (
                "affect_state",
                "user_profile",
                "user_state_now",
                "user_relationship",
                "agenda",
                "conversation_arc",
                "prepared_nudge",
                "consolidator_state",
            ):
                self.assertIn(expected, tables)


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


class TestThreadNotes(unittest.TestCase):
    """K21 fresh-eyes thread notes + sidebar title resolution."""

    def test_save_and_get_thread_note(self):
        with _TempDB() as db:
            self.assertIsNone(db.get_thread_note("s1"))
            db.save_thread_note("s1", "Parser bug", "Deep in a bug, close.", 42)
            row = db.get_thread_note("s1")
            self.assertIsNotNone(row)
            self.assertEqual(row.title, "Parser bug")
            self.assertEqual(row.note, "Deep in a bug, close.")
            self.assertEqual(row.messages_at, 42)

    def test_save_thread_note_upserts(self):
        with _TempDB() as db:
            db.save_thread_note("s1", "old title", "old note", 10)
            db.save_thread_note("s1", "new title", "new note", 30)
            row = db.get_thread_note("s1")
            self.assertEqual(row.title, "new title")
            self.assertEqual(row.note, "new note")
            self.assertEqual(row.messages_at, 30)

    def test_list_sessions_prefers_thread_title(self):
        with _TempDB() as db:
            db.add_message("s1", "user", "how do I center a div")
            db.add_message("s1", "assistant", "flexbox")
            db.save_thread_note("s1", "CSS layout help", "Working on layout.", 2)
            rows = {r["session_id"]: r for r in db.list_sessions()}
            self.assertEqual(rows["s1"]["title"], "CSS layout help")

    def test_list_sessions_falls_back_to_first_user_message(self):
        with _TempDB() as db:
            db.add_message("s2", "assistant", "hi")  # not a user message
            db.add_message("s2", "user", "tell me about black holes please")
            rows = {r["session_id"]: r for r in db.list_sessions()}
            self.assertEqual(
                rows["s2"]["title"], "tell me about black holes please",
            )

    def test_list_sessions_truncates_long_first_message(self):
        with _TempDB() as db:
            long_msg = "word " * 60
            db.add_message("s3", "user", long_msg)
            rows = {r["session_id"]: r for r in db.list_sessions()}
            title = rows["s3"]["title"]
            self.assertLessEqual(len(title), 80)
            self.assertTrue(title.endswith("\u2026"))

    def test_clear_messages_drops_thread_note(self):
        with _TempDB() as db:
            db.add_message("s1", "user", "hello")
            db.save_thread_note("s1", "t", "n", 1)
            db.clear_messages("s1", full_reset=True)
            self.assertIsNone(db.get_thread_note("s1"))


class TestSchemaV7Migration(unittest.TestCase):
    """Schema v6 → v7 upgrade adds the ``memories.metadata`` column and
    the ``relationship_axes`` table without losing existing rows."""

    def test_v6_database_upgrades_to_v7(self):
        import sqlite3 as _sqlite3
        from app.core.infra.chat_database import _SCHEMA_VERSION

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            # Hand-write a v6-shaped database -- old memories schema (no
            # ``metadata`` column), no ``relationship_axes`` table.
            conn = _sqlite3.connect(str(path))
            conn.executescript(
                """
                CREATE TABLE schema_version (version INTEGER NOT NULL);
                INSERT INTO schema_version (version) VALUES (6);
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    token_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    salience REAL NOT NULL DEFAULT 0.5,
                    embedding BLOB NOT NULL,
                    source_session TEXT,
                    source_message_id INTEGER,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    pinned INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            conn.execute(
                "INSERT INTO memories (content, kind, embedding, created_at) "
                "VALUES (?, ?, ?, ?)",
                ("legacy preserved", "user_fact", b"\x00" * 8, "2025-01-01T00:00:00Z"),
            )
            conn.commit()
            conn.close()

            # Opening the DB triggers ``_init_schema`` which runs the v6→v7
            # ALTER + table create.
            db = ChatDatabase(path)
            try:
                conn = db._get_conn()
                version = conn.execute(
                    "SELECT version FROM schema_version LIMIT 1"
                ).fetchone()[0]
                self.assertEqual(version, _SCHEMA_VERSION)

                cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
                self.assertIn("metadata", cols)

                tables = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                self.assertIn("relationship_axes", tables)

                # Existing rows survive the migration intact.
                row = conn.execute(
                    "SELECT content, metadata FROM memories WHERE content = 'legacy preserved'"
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row[0], "legacy preserved")
                self.assertIsNone(row[1])
            finally:
                conn = getattr(db._local, "conn", None)
                if conn is not None:
                    conn.close()
                    db._local.conn = None


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
