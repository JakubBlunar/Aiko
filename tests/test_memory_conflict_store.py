"""Tests for :mod:`app.core.memory_conflict_store` and the schema v11 migration."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.core.chat_database import ChatDatabase
from app.core.memory_conflict_store import (
    ACTION_DEMOTE,
    ACTION_DELETE,
    FLAGGED_BY_AIKO,
    FLAGGED_BY_AUTO,
    HEURISTIC_BORDERLINE,
    HEURISTIC_DEFINITE,
    MemoryConflictStore,
    STATUS_AUTO_RESOLVED,
    STATUS_DISMISSED,
    STATUS_OPEN,
    STATUS_USER_RESOLVED,
)


def _build_db() -> tuple[ChatDatabase, MemoryConflictStore, Path]:
    tmp = tempfile.mkdtemp()
    path = Path(tmp) / "test.db"
    db = ChatDatabase(path)
    return db, MemoryConflictStore(db), path


class SchemaMigrationTests(unittest.TestCase):
    def test_fresh_database_lands_on_current_version(self) -> None:
        db, _, path = _build_db()
        conn = sqlite3.connect(str(path))
        row = conn.execute(
            "SELECT version FROM schema_version LIMIT 1",
        ).fetchone()
        from app.core.chat_database import _SCHEMA_VERSION
        self.assertEqual(row[0], _SCHEMA_VERSION)
        # Confirm the memory_conflicts table exists with the right cols.
        cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info(memory_conflicts)"
            ).fetchall()
        }
        expected = {
            "id", "memory_a_id", "memory_b_id", "similarity",
            "confidence_delta", "heuristic_label", "heuristic_signals",
            "llm_verdict", "llm_reason", "status", "winner_id",
            "loser_id", "resolution_action", "flagged_by",
            "detected_at", "resolved_at",
        }
        self.assertTrue(expected.issubset(cols), f"missing cols: {expected - cols}")
        # And the status index is in place.
        idx = {
            r[1]
            for r in conn.execute(
                "PRAGMA index_list(memory_conflicts)"
            ).fetchall()
        }
        self.assertIn("idx_memory_conflicts_status", idx)
        conn.close()

    def test_v10_database_upgrades_through_v11(self) -> None:
        """A pre-v11 database (no memory_conflicts) gets migrated cleanly through to the current version."""
        tmp = tempfile.mkdtemp()
        path = Path(tmp) / "legacy.db"
        # Build a minimal v10-shaped database by hand: just the
        # ``schema_version`` row and the ``memories`` table (so the v11
        # migration has something to find on disk).
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version (version) VALUES (10)")
        conn.execute(
            "CREATE TABLE memories ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "content TEXT NOT NULL,"
            "kind TEXT NOT NULL,"
            "salience REAL NOT NULL DEFAULT 0.5,"
            "embedding BLOB NOT NULL,"
            "source_session TEXT,"
            "source_message_id INTEGER,"
            "created_at TEXT NOT NULL,"
            "last_used_at TEXT,"
            "use_count INTEGER NOT NULL DEFAULT 0,"
            "pinned INTEGER NOT NULL DEFAULT 0,"
            "metadata TEXT,"
            "tier TEXT NOT NULL DEFAULT 'long_term',"
            "revival_score REAL NOT NULL DEFAULT 0.0,"
            "confidence REAL NOT NULL DEFAULT 0.7,"
            "event_time TEXT,"
            "temporal_type TEXT NOT NULL DEFAULT 'durable',"
            "relevance_until TEXT"
            ")"
        )
        conn.commit()
        conn.close()

        # Opening through ChatDatabase triggers the migration to v11.
        db = ChatDatabase(path)
        del db  # close

        conn = sqlite3.connect(str(path))
        row = conn.execute(
            "SELECT version FROM schema_version LIMIT 1",
        ).fetchone()
        from app.core.chat_database import _SCHEMA_VERSION
        self.assertEqual(row[0], _SCHEMA_VERSION)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn("memory_conflicts", tables)
        conn.close()


class CRUDTests(unittest.TestCase):
    def test_record_sorts_pair_ids(self) -> None:
        _, store, _ = _build_db()
        pair_id = store.record(
            memory_a_id=10,
            memory_b_id=5,
            similarity=0.85,
            confidence_delta=0.4,
            heuristic_label=HEURISTIC_DEFINITE,
            heuristic_signals=["antonym:loves/hates"],
        )
        self.assertIsNotNone(pair_id)
        pair = store.get(int(pair_id))
        self.assertIsNotNone(pair)
        self.assertEqual(pair.memory_a_id, 5)  # smaller id first
        self.assertEqual(pair.memory_b_id, 10)
        self.assertEqual(pair.status, STATUS_OPEN)
        self.assertEqual(pair.flagged_by, FLAGGED_BY_AUTO)
        self.assertEqual(pair.heuristic_signals, ["antonym:loves/hates"])

    def test_record_is_idempotent_on_pair(self) -> None:
        _, store, _ = _build_db()
        first = store.record(
            memory_a_id=1,
            memory_b_id=2,
            similarity=0.9,
            confidence_delta=0.1,
            heuristic_label=HEURISTIC_BORDERLINE,
            heuristic_signals=["number_mismatch:35.0!=60.0"],
        )
        second = store.record(
            memory_a_id=2,  # swap order; still same pair
            memory_b_id=1,
            similarity=0.9,
            confidence_delta=0.1,
            heuristic_label=HEURISTIC_BORDERLINE,
        )
        self.assertEqual(first, second)
        self.assertEqual(store.count_open(), 1)

    def test_record_rejects_self_pair(self) -> None:
        _, store, _ = _build_db()
        result = store.record(
            memory_a_id=5,
            memory_b_id=5,
            similarity=1.0,
            confidence_delta=0.0,
            heuristic_label=HEURISTIC_DEFINITE,
        )
        self.assertIsNone(result)

    def test_record_invalid_status_raises(self) -> None:
        _, store, _ = _build_db()
        with self.assertRaises(ValueError):
            store.record(
                memory_a_id=1,
                memory_b_id=2,
                similarity=0.9,
                confidence_delta=0.1,
                heuristic_label=HEURISTIC_DEFINITE,
                status="bogus",
            )

    def test_has_pair(self) -> None:
        _, store, _ = _build_db()
        self.assertFalse(store.has_pair(1, 2))
        store.record(
            memory_a_id=1,
            memory_b_id=2,
            similarity=0.9,
            confidence_delta=0.1,
            heuristic_label=HEURISTIC_DEFINITE,
        )
        self.assertTrue(store.has_pair(1, 2))
        self.assertTrue(store.has_pair(2, 1))  # order-insensitive

    def test_mark_user_resolved(self) -> None:
        _, store, _ = _build_db()
        pid = store.record(
            memory_a_id=1,
            memory_b_id=2,
            similarity=0.9,
            confidence_delta=0.2,
            heuristic_label=HEURISTIC_DEFINITE,
        )
        ok = store.mark_user_resolved(
            int(pid), winner_id=1, loser_id=2, action=ACTION_DEMOTE,
        )
        self.assertTrue(ok)
        pair = store.get(int(pid))
        self.assertEqual(pair.status, STATUS_USER_RESOLVED)
        self.assertEqual(pair.winner_id, 1)
        self.assertEqual(pair.loser_id, 2)
        self.assertEqual(pair.resolution_action, ACTION_DEMOTE)
        self.assertIsNotNone(pair.resolved_at)

    def test_mark_user_resolved_invalid_action_raises(self) -> None:
        _, store, _ = _build_db()
        pid = store.record(
            memory_a_id=1,
            memory_b_id=2,
            similarity=0.9,
            confidence_delta=0.2,
            heuristic_label=HEURISTIC_DEFINITE,
        )
        with self.assertRaises(ValueError):
            store.mark_user_resolved(
                int(pid), winner_id=1, loser_id=2, action="dismiss",
            )

    def test_mark_auto_resolved(self) -> None:
        _, store, _ = _build_db()
        pid = store.record(
            memory_a_id=1,
            memory_b_id=2,
            similarity=0.9,
            confidence_delta=0.5,
            heuristic_label=HEURISTIC_DEFINITE,
        )
        ok = store.mark_auto_resolved(int(pid), winner_id=2, loser_id=1)
        self.assertTrue(ok)
        pair = store.get(int(pid))
        self.assertEqual(pair.status, STATUS_AUTO_RESOLVED)
        self.assertEqual(pair.winner_id, 2)
        self.assertEqual(pair.loser_id, 1)
        self.assertEqual(pair.resolution_action, ACTION_DEMOTE)

    def test_dismiss(self) -> None:
        _, store, _ = _build_db()
        pid = store.record(
            memory_a_id=1,
            memory_b_id=2,
            similarity=0.9,
            confidence_delta=0.2,
            heuristic_label=HEURISTIC_DEFINITE,
        )
        ok = store.dismiss(int(pid))
        self.assertTrue(ok)
        pair = store.get(int(pid))
        self.assertEqual(pair.status, STATUS_DISMISSED)
        self.assertIsNotNone(pair.resolved_at)

    def test_aiko_flagged(self) -> None:
        _, store, _ = _build_db()
        pid = store.record(
            memory_a_id=1,
            memory_b_id=2,
            similarity=0.9,
            confidence_delta=0.2,
            heuristic_label=HEURISTIC_DEFINITE,
            flagged_by=FLAGGED_BY_AIKO,
        )
        pair = store.get(int(pid))
        self.assertEqual(pair.flagged_by, FLAGGED_BY_AIKO)


class ListAndCountTests(unittest.TestCase):
    def test_list_open_excludes_resolved(self) -> None:
        _, store, _ = _build_db()
        a = store.record(
            memory_a_id=1, memory_b_id=2,
            similarity=0.9, confidence_delta=0.2,
            heuristic_label=HEURISTIC_DEFINITE,
        )
        b = store.record(
            memory_a_id=3, memory_b_id=4,
            similarity=0.85, confidence_delta=0.5,
            heuristic_label=HEURISTIC_DEFINITE,
            status=STATUS_AUTO_RESOLVED,
            winner_id=3, loser_id=4, resolution_action=ACTION_DEMOTE,
        )
        self.assertEqual(store.count_open(), 1)
        open_ids = {p.id for p in store.list_open()}
        self.assertEqual(open_ids, {int(a)})
        recent_ids = {p.id for p in store.list_recent()}
        self.assertEqual(recent_ids, {int(a), int(b)})

    def test_list_recently_auto_resolved(self) -> None:
        _, store, _ = _build_db()
        store.record(
            memory_a_id=1, memory_b_id=2,
            similarity=0.9, confidence_delta=0.5,
            heuristic_label=HEURISTIC_DEFINITE,
            status=STATUS_AUTO_RESOLVED,
            winner_id=1, loser_id=2, resolution_action=ACTION_DEMOTE,
        )
        store.record(
            memory_a_id=3, memory_b_id=4,
            similarity=0.85, confidence_delta=0.2,
            heuristic_label=HEURISTIC_DEFINITE,
        )
        recent = store.list_recently_auto_resolved()
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0].status, STATUS_AUTO_RESOLVED)

    def test_count_by_status(self) -> None:
        _, store, _ = _build_db()
        store.record(
            memory_a_id=1, memory_b_id=2,
            similarity=0.9, confidence_delta=0.2,
            heuristic_label=HEURISTIC_DEFINITE,
        )
        store.record(
            memory_a_id=3, memory_b_id=4,
            similarity=0.85, confidence_delta=0.5,
            heuristic_label=HEURISTIC_DEFINITE,
            status=STATUS_AUTO_RESOLVED,
            winner_id=3, loser_id=4, resolution_action=ACTION_DEMOTE,
        )
        counts = store.count_by_status()
        self.assertEqual(counts[STATUS_OPEN], 1)
        self.assertEqual(counts[STATUS_AUTO_RESOLVED], 1)
        self.assertEqual(counts[STATUS_USER_RESOLVED], 0)
        self.assertEqual(counts[STATUS_DISMISSED], 0)

    def test_list_recent_invalid_status_raises(self) -> None:
        _, store, _ = _build_db()
        with self.assertRaises(ValueError):
            store.list_recent(status="bogus")


class CascadeCleanupTests(unittest.TestCase):
    def test_delete_for_memory_drops_referencing_pairs(self) -> None:
        _, store, _ = _build_db()
        store.record(
            memory_a_id=1, memory_b_id=2,
            similarity=0.9, confidence_delta=0.2,
            heuristic_label=HEURISTIC_DEFINITE,
        )
        store.record(
            memory_a_id=1, memory_b_id=3,
            similarity=0.85, confidence_delta=0.3,
            heuristic_label=HEURISTIC_DEFINITE,
        )
        store.record(
            memory_a_id=2, memory_b_id=3,
            similarity=0.82, confidence_delta=0.1,
            heuristic_label=HEURISTIC_DEFINITE,
        )
        # Deleting memory 1 should remove the two pairs that reference it.
        dropped = store.delete_for_memory(1)
        self.assertEqual(dropped, 2)
        remaining = store.list_open()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].memory_a_id, 2)
        self.assertEqual(remaining[0].memory_b_id, 3)

    def test_delete_for_memory_with_no_references_is_noop(self) -> None:
        _, store, _ = _build_db()
        store.record(
            memory_a_id=1, memory_b_id=2,
            similarity=0.9, confidence_delta=0.2,
            heuristic_label=HEURISTIC_DEFINITE,
        )
        dropped = store.delete_for_memory(999)
        self.assertEqual(dropped, 0)
        self.assertEqual(store.count_open(), 1)


if __name__ == "__main__":
    unittest.main()
