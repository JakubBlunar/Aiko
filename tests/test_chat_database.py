"""Tests for app.core.chat_database.ChatDatabase."""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

import numpy as np

from app.core.chat_database import (
    ChatDatabase,
    MessageRow,
    SummaryRow,
    PersonalityNoteRow,
    RecentTopicRow,
    _encode_embedding,
    _decode_embedding,
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
    def test_fresh_database_creates_tables(self):
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
                "message_embeddings",
                "session_summaries",
                "personality_notes",
                "recent_topics",
                "schema_version",
            ):
                self.assertIn(expected, tables)

    def test_schema_version_is_set(self):
        with _TempDB() as db:
            conn = db._get_conn()
            row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            self.assertIsNotNone(row)
            self.assertGreaterEqual(row[0], 2)


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

    def test_clear_messages_default(self):
        with _TempDB() as db:
            db.add_message("s1", "user", "hello")
            db.save_summary("s1", "summary", 10, 1)
            db.upsert_personality_note("s1", "pref", "likes cats", 0.9)
            db.add_recent_topic("s1", "cats")

            deleted = db.clear_messages("s1")
            self.assertEqual(deleted, 1)
            self.assertEqual(db.get_message_count("s1"), 0)
            self.assertIsNone(db.get_latest_summary("s1"))
            # personality and topics should survive default clear
            self.assertGreater(len(db.get_personality_notes("s1")), 0)
            self.assertGreater(len(db.get_recent_topics("s1")), 0)

    def test_clear_messages_full_reset(self):
        with _TempDB() as db:
            db.add_message("s1", "user", "hello")
            db.upsert_personality_note("s1", "pref", "likes cats", 0.9)
            db.add_recent_topic("s1", "cats")

            db.clear_messages("s1", full_reset=True)
            self.assertEqual(db.get_message_count("s1"), 0)
            self.assertEqual(len(db.get_personality_notes("s1")), 0)
            self.assertEqual(len(db.get_recent_topics("s1")), 0)


class TestEmbeddings(unittest.TestCase):
    def test_encode_decode_roundtrip(self):
        vec = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        blob = _encode_embedding(vec)
        restored = _decode_embedding(blob)
        np.testing.assert_array_almost_equal(vec, restored)

    def test_add_and_get_embeddings(self):
        with _TempDB() as db:
            mid = db.add_message("s1", "user", "test embedding")
            vec = np.random.rand(128).astype(np.float32)
            db.add_embedding(mid, "s1", vec)

            rows = db.get_all_embeddings("s1")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].message_id, mid)
            self.assertEqual(rows[0].content, "test embedding")
            np.testing.assert_array_almost_equal(rows[0].embedding, vec)

    def test_message_ids_with_embeddings(self):
        with _TempDB() as db:
            m1 = db.add_message("s1", "user", "a")
            m2 = db.add_message("s1", "user", "b")
            db.add_embedding(m1, "s1", np.zeros(4, dtype=np.float32))
            ids = db.get_message_ids_with_embeddings("s1")
            self.assertIn(m1, ids)
            self.assertNotIn(m2, ids)

    def test_embeddings_cleared_with_messages(self):
        with _TempDB() as db:
            mid = db.add_message("s1", "user", "x")
            db.add_embedding(mid, "s1", np.ones(4, dtype=np.float32))
            db.clear_messages("s1")
            self.assertEqual(len(db.get_all_embeddings("s1")), 0)


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


class TestPersonalityNotes(unittest.TestCase):
    def test_upsert_creates_and_updates(self):
        with _TempDB() as db:
            nid = db.upsert_personality_note("s1", "preference", "likes cats", 0.8)
            self.assertIsInstance(nid, int)
            notes = db.get_personality_notes("s1")
            self.assertEqual(len(notes), 1)
            self.assertEqual(notes[0].note, "likes cats")
            self.assertAlmostEqual(notes[0].confidence, 0.8)

            nid2 = db.upsert_personality_note("s1", "preference", "likes cats", 0.95)
            self.assertEqual(nid, nid2)
            notes = db.get_personality_notes("s1")
            self.assertEqual(len(notes), 1)
            self.assertAlmostEqual(notes[0].confidence, 0.95)

    def test_min_confidence_filter(self):
        with _TempDB() as db:
            db.upsert_personality_note("s1", "pref", "high conf", 0.9)
            db.upsert_personality_note("s1", "pref", "low conf", 0.2)
            high = db.get_personality_notes("s1", min_confidence=0.5)
            self.assertEqual(len(high), 1)
            self.assertEqual(high[0].note, "high conf")

    def test_replace_personality_notes(self):
        with _TempDB() as db:
            db.upsert_personality_note("s1", "old", "old note", 0.8)
            db.replace_personality_notes("s1", [
                ("new", "note one", 0.9),
                ("new", "note two", 0.7),
            ])
            notes = db.get_personality_notes("s1")
            self.assertEqual(len(notes), 2)
            texts = {n.note for n in notes}
            self.assertEqual(texts, {"note one", "note two"})

    def test_decay_and_prune(self):
        with _TempDB() as db:
            db.upsert_personality_note("s1", "a", "survives", 0.9)
            db.upsert_personality_note("s1", "b", "pruned", 0.3)
            pruned = db.decay_personality_notes("s1", decay_rate=0.15, prune_threshold=0.2)
            self.assertEqual(pruned, 1)
            notes = db.get_personality_notes("s1")
            self.assertEqual(len(notes), 1)
            self.assertEqual(notes[0].note, "survives")

    def test_cap_personality_notes(self):
        with _TempDB() as db:
            for i in range(5):
                db.upsert_personality_note("s1", "cat", f"note {i}", 0.5 + i * 0.1)
            removed = db.cap_personality_notes("s1", max_notes=2)
            self.assertEqual(removed, 3)
            notes = db.get_personality_notes("s1")
            self.assertEqual(len(notes), 2)


class TestRecentTopics(unittest.TestCase):
    def test_add_and_get_topics(self):
        with _TempDB() as db:
            db.add_recent_topic("s1", "weather")
            db.add_recent_topic("s1", "cooking")
            topics = db.get_recent_topics("s1")
            self.assertEqual(len(topics), 2)
            self.assertIsInstance(topics[0], RecentTopicRow)
            self.assertEqual(topics[0].topic, "cooking")

    def test_auto_trim_to_20(self):
        with _TempDB() as db:
            for i in range(25):
                db.add_recent_topic("s1", f"topic {i}")
            topics = db.get_recent_topics("s1", limit=100)
            self.assertLessEqual(len(topics), 20)

    def test_topics_isolated_by_session(self):
        with _TempDB() as db:
            db.add_recent_topic("s1", "a")
            db.add_recent_topic("s2", "b")
            self.assertEqual(len(db.get_recent_topics("s1")), 1)
            self.assertEqual(len(db.get_recent_topics("s2")), 1)


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
