"""Tests for :mod:`app.core.conversation.topic_cluster_store` (schema v20)."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.core.infra.chat_database import ChatDatabase, _SCHEMA_VERSION
from app.core.conversation.topic_cluster_store import ClusterRow, TopicClusterStore


def _build() -> tuple[ChatDatabase, TopicClusterStore, Path]:
    tmp = tempfile.mkdtemp()
    path = Path(tmp) / "test.db"
    db = ChatDatabase(path)
    return db, TopicClusterStore(db), path


class SchemaTests(unittest.TestCase):
    def test_fresh_db_has_v20_tables(self) -> None:
        _, _, path = _build()
        conn = sqlite3.connect(str(path))
        ver = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        self.assertEqual(ver[0], _SCHEMA_VERSION)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn("topic_clusters", tables)
        self.assertIn("memory_topic_assignments", tables)


class RoundTripTests(unittest.TestCase):
    def test_upsert_and_load(self) -> None:
        _, store, _ = _build()
        c = np.array([0.6, 0.8], dtype=np.float32)
        store.upsert_cluster(ClusterRow(cluster_id=1, label="cats", centroid=c, size=3))
        store.set_assignment(1, 1)
        store.set_assignment(2, 1)
        clusters, assignments = store.load_all()
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].label, "cats")
        self.assertEqual(clusters[0].size, 3)
        np.testing.assert_allclose(clusters[0].centroid, c, rtol=1e-5)
        self.assertEqual(assignments, {1: 1, 2: 1})

    def test_next_cluster_id(self) -> None:
        _, store, _ = _build()
        self.assertEqual(store.next_cluster_id(), 1)
        store.upsert_cluster(ClusterRow(cluster_id=5, label="x"))
        self.assertEqual(store.next_cluster_id(), 6)

    def test_reassignment_overwrites(self) -> None:
        _, store, _ = _build()
        store.set_assignment(1, 10)
        store.set_assignment(1, 20)
        _, assignments = store.load_all()
        self.assertEqual(assignments, {1: 20})

    def test_delete_assignment_and_cascade(self) -> None:
        _, store, _ = _build()
        store.upsert_cluster(ClusterRow(cluster_id=1, label="x"))
        store.set_assignment(1, 1)
        store.set_assignment(2, 1)
        store.delete_for_memory(1)
        _, assignments = store.load_all()
        self.assertEqual(assignments, {2: 1})

    def test_delete_cluster_removes_assignments(self) -> None:
        _, store, _ = _build()
        store.upsert_cluster(ClusterRow(cluster_id=1, label="x"))
        store.set_assignment(1, 1)
        store.set_assignment(2, 1)
        store.delete_cluster(1)
        clusters, assignments = store.load_all()
        self.assertEqual(clusters, [])
        self.assertEqual(assignments, {})

    def test_replace_all_is_atomic_swap(self) -> None:
        _, store, _ = _build()
        store.upsert_cluster(ClusterRow(cluster_id=1, label="old"))
        store.set_assignment(99, 1)
        new_clusters = [
            ClusterRow(cluster_id=1, label="a", centroid=np.array([1.0, 0.0], dtype=np.float32), size=2),
            ClusterRow(cluster_id=2, label="b", centroid=np.array([0.0, 1.0], dtype=np.float32), size=2),
        ]
        store.replace_all(new_clusters, {1: 1, 2: 1, 3: 2, 4: 2})
        clusters, assignments = store.load_all()
        self.assertEqual(len(clusters), 2)
        labels = {c.cluster_id: c.label for c in clusters}
        self.assertEqual(labels, {1: "a", 2: "b"})
        self.assertEqual(assignments, {1: 1, 2: 1, 3: 2, 4: 2})
        self.assertNotIn(99, assignments)

    def test_empty_centroid_survives_roundtrip(self) -> None:
        _, store, _ = _build()
        store.upsert_cluster(ClusterRow(cluster_id=1, label="x"))
        clusters, _ = store.load_all()
        self.assertEqual(clusters[0].centroid.size, 0)


if __name__ == "__main__":
    unittest.main()
