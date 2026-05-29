"""Tests for :mod:`app.core.belief_store` and the schema v12 migration."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.core.belief_store import (
    Belief,
    BeliefStore,
    KIND_MOOD,
    KIND_OPINION,
    SOURCE_MANUAL,
    SOURCE_SELF_TAG,
    SOURCE_WORKER,
    STATUS_ACTIVE,
    STATUS_CONFIRMED,
    STATUS_CONTRADICTED,
    STATUS_STALE,
)
from app.core.chat_database import ChatDatabase, _SCHEMA_VERSION


def _build_db() -> tuple[ChatDatabase, BeliefStore, Path]:
    tmp = tempfile.mkdtemp()
    path = Path(tmp) / "test.db"
    db = ChatDatabase(path)
    return db, BeliefStore(db), path


class SchemaMigrationTests(unittest.TestCase):
    def test_fresh_database_lands_on_current_schema(self) -> None:
        _, _, path = _build_db()
        conn = sqlite3.connect(str(path))
        row = conn.execute(
            "SELECT version FROM schema_version LIMIT 1"
        ).fetchone()
        self.assertEqual(row[0], _SCHEMA_VERSION)
        self.assertGreaterEqual(_SCHEMA_VERSION, 12)
        cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info(beliefs)"
            ).fetchall()
        }
        expected = {
            "id", "user_id", "kind", "topic", "topic_embedding",
            "predicted_state", "confidence", "valence", "arousal",
            "source", "source_message_id", "observed_at",
            "last_checked_at", "status", "gap_seen_at", "metadata",
        }
        self.assertTrue(expected.issubset(cols), f"missing cols: {expected - cols}")
        idx = {
            r[1]
            for r in conn.execute(
                "PRAGMA index_list(beliefs)"
            ).fetchall()
        }
        self.assertIn("idx_beliefs_status", idx)
        self.assertIn("idx_beliefs_topic", idx)
        conn.close()

    def test_v11_database_upgrades_to_current_schema(self) -> None:
        """A pre-v12 database (no beliefs table) gets migrated cleanly."""
        tmp = tempfile.mkdtemp()
        path = Path(tmp) / "legacy.db"
        # Build a minimal v11-shaped database by hand.
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_version (version) VALUES (11)")
        conn.commit()
        conn.close()

        # Opening through ChatDatabase triggers migration to the current
        # schema version (v12 added beliefs; later versions are no-ops
        # for the columns this test exercises).
        db = ChatDatabase(path)
        del db

        conn = sqlite3.connect(str(path))
        row = conn.execute(
            "SELECT version FROM schema_version LIMIT 1"
        ).fetchone()
        self.assertEqual(row[0], _SCHEMA_VERSION)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn("beliefs", tables)
        conn.close()


class CRUDTests(unittest.TestCase):
    def test_upsert_inserts_new_row(self) -> None:
        _, store, _ = _build_db()
        belief = store.upsert(
            user_id="u1",
            kind=KIND_MOOD,
            topic="Tokyo Trip",
            predicted_state="excited",
            confidence=0.8,
            valence=0.5,
            arousal=0.7,
            source=SOURCE_SELF_TAG,
        )
        self.assertIsNotNone(belief)
        assert belief is not None
        self.assertEqual(belief.kind, KIND_MOOD)
        # Topic normalised to lowercase/trim.
        self.assertEqual(belief.topic, "tokyo trip")
        self.assertEqual(belief.predicted_state, "excited")
        self.assertEqual(belief.confidence, 0.8)
        self.assertEqual(belief.valence, 0.5)
        self.assertEqual(belief.arousal, 0.7)
        self.assertEqual(belief.source, SOURCE_SELF_TAG)
        self.assertEqual(belief.status, STATUS_ACTIVE)

    def test_upsert_updates_existing_same_topic(self) -> None:
        _, store, _ = _build_db()
        first = store.upsert(
            user_id="u1", kind=KIND_MOOD, topic="tokyo trip",
            predicted_state="excited", confidence=0.6,
        )
        second = store.upsert(
            user_id="u1", kind=KIND_MOOD, topic="Tokyo Trip",
            predicted_state="nervous", confidence=0.9,
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first.id, second.id)
        self.assertEqual(second.predicted_state, "nervous")
        self.assertEqual(second.confidence, 0.9)

    def test_upsert_rejects_empty_topic_or_state(self) -> None:
        _, store, _ = _build_db()
        self.assertIsNone(
            store.upsert(
                user_id="u1", kind=KIND_MOOD, topic="",
                predicted_state="excited",
            )
        )
        self.assertIsNone(
            store.upsert(
                user_id="u1", kind=KIND_MOOD, topic="tokyo",
                predicted_state="",
            )
        )

    def test_upsert_rejects_invalid_kind(self) -> None:
        _, store, _ = _build_db()
        self.assertIsNone(
            store.upsert(
                user_id="u1", kind="bogus", topic="x",
                predicted_state="y",
            )
        )

    def test_topic_embedding_dedupe(self) -> None:
        _, store, _ = _build_db()
        v1 = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
        v1 /= np.linalg.norm(v1)
        first = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="rust language",
            predicted_state="overhyped", topic_embedding=v1,
        )
        # Close vector (~0.99 cosine) -- should fuzzy-merge.
        v2 = np.array([0.99, 0.10, 0.05, 0.0], dtype=np.float32)
        v2 /= np.linalg.norm(v2)
        second = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="the rust lang",
            predicted_state="solid", topic_embedding=v2,
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual(first.id, second.id)
        # And the second upsert's topic + state wins.
        self.assertEqual(second.topic, "the rust lang")
        self.assertEqual(second.predicted_state, "solid")
        # A distant vector (cosine ~0.0) should NOT merge.
        v3 = np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float32)
        third = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="ruby framework",
            predicted_state="ok", topic_embedding=v3,
        )
        self.assertIsNotNone(third)
        assert third is not None
        self.assertNotEqual(third.id, first.id)

    def test_mark_status_helpers(self) -> None:
        _, store, _ = _build_db()
        b = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="x",
            predicted_state="y",
        )
        assert b is not None
        self.assertTrue(store.mark_contradicted(b.id))
        self.assertEqual(store.get(b.id).status, STATUS_CONTRADICTED)
        self.assertIsNotNone(store.get(b.id).gap_seen_at)
        b2 = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="z",
            predicted_state="y",
        )
        assert b2 is not None
        self.assertTrue(store.mark_confirmed(b2.id))
        self.assertEqual(store.get(b2.id).status, STATUS_CONFIRMED)
        self.assertTrue(store.mark_stale(b2.id))
        self.assertEqual(store.get(b2.id).status, STATUS_STALE)

    def test_update_partial(self) -> None:
        _, store, _ = _build_db()
        b = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="x",
            predicted_state="initial", confidence=0.5,
        )
        assert b is not None
        b2 = store.update(b.id, predicted_state="updated", confidence=0.9)
        self.assertIsNotNone(b2)
        assert b2 is not None
        self.assertEqual(b2.predicted_state, "updated")
        self.assertEqual(b2.confidence, 0.9)

    def test_update_rejects_invalid_status(self) -> None:
        _, store, _ = _build_db()
        b = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="x",
            predicted_state="y",
        )
        assert b is not None
        with self.assertRaises(ValueError):
            store.update(b.id, status="bogus")

    def test_delete(self) -> None:
        _, store, _ = _build_db()
        b = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="x",
            predicted_state="y",
        )
        assert b is not None
        self.assertTrue(store.delete(b.id))
        self.assertIsNone(store.get(b.id))


class ListAndCountTests(unittest.TestCase):
    def test_list_active_filters_status(self) -> None:
        _, store, _ = _build_db()
        a = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="a",
            predicted_state="x",
        )
        b = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="b",
            predicted_state="x",
        )
        assert a is not None and b is not None
        store.mark_contradicted(b.id)
        active = store.list_active(user_id="u1")
        self.assertEqual({row.id for row in active}, {a.id})

    def test_list_active_for_gap_check_requires_valence(self) -> None:
        _, store, _ = _build_db()
        # Mood belief without numeric valence: excluded.
        store.upsert(
            user_id="u1", kind=KIND_MOOD, topic="t1",
            predicted_state="happy",
        )
        # Mood belief with valence: included.
        store.upsert(
            user_id="u1", kind=KIND_MOOD, topic="t2",
            predicted_state="happy", valence=0.4,
        )
        rows = store.list_active_for_gap_check(user_id="u1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].topic, "t2")

    def test_count_by_status(self) -> None:
        _, store, _ = _build_db()
        a = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="a",
            predicted_state="x",
        )
        b = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="b",
            predicted_state="x",
        )
        assert a is not None and b is not None
        store.mark_contradicted(b.id)
        counts = store.count_by_status(user_id="u1")
        self.assertEqual(counts[STATUS_ACTIVE], 1)
        self.assertEqual(counts[STATUS_CONTRADICTED], 1)
        self.assertEqual(counts[STATUS_CONFIRMED], 0)
        self.assertEqual(counts[STATUS_STALE], 0)


class MaintenanceTests(unittest.TestCase):
    def test_mark_stale_older_than(self) -> None:
        _, store, _ = _build_db()
        b = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="ancient",
            predicted_state="x",
            observed_at="1990-01-01T00:00:00+00:00",
        )
        assert b is not None
        # cutoff = 2050 -> the ancient row qualifies
        n = store.mark_stale_older_than(
            cutoff_iso="2050-01-01T00:00:00+00:00", user_id="u1",
        )
        self.assertEqual(n, 1)
        self.assertEqual(store.get(b.id).status, STATUS_STALE)

    def test_prune_to_cap_drops_oldest_lowest_confidence(self) -> None:
        _, store, _ = _build_db()
        keep_high = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="keep",
            predicted_state="x", confidence=0.9,
            observed_at="2024-06-01T00:00:00+00:00",
        )
        drop_low = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="drop1",
            predicted_state="x", confidence=0.1,
            observed_at="2024-01-01T00:00:00+00:00",
        )
        drop_mid = store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="drop2",
            predicted_state="x", confidence=0.4,
            observed_at="2024-02-01T00:00:00+00:00",
        )
        assert keep_high is not None and drop_low is not None and drop_mid is not None
        pruned = store.prune_to_cap(user_id="u1", cap=1)
        self.assertEqual(pruned, 2)
        self.assertIsNone(store.get(drop_low.id))
        self.assertIsNone(store.get(drop_mid.id))
        self.assertIsNotNone(store.get(keep_high.id))


if __name__ == "__main__":
    unittest.main()
