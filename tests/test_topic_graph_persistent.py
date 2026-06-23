"""Tests for the persistent / incremental TopicGraph mode (schema v20).

These exercise the path that activates when a ``TopicClusterStore`` is
injected: warm-start from SQLite, incremental add/delete, and the full
batch rebuild. The matmul batch path is used (no rag_store), so no
LanceDB is required.
"""
from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.conversation.topic_cluster_store import TopicClusterStore
from app.core.conversation.topic_graph import (
    TopicGraph,
    _normalise,
    build_topic_graph_snapshot,
)


@dataclass
class _StubMemory:
    id: int
    content: str
    embedding: np.ndarray
    kind: str = "fact"
    salience: float = 0.5
    use_count: int = 0
    created_at: str = "2026-01-01T00:00:00+00:00"
    last_used_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    tier: str = "long_term"


class _StubMemoryStore:
    def __init__(self) -> None:
        self._mirror: dict[int, _StubMemory] = {}
        self._lock = threading.Lock()

    def add(self, mem: _StubMemory) -> None:
        with self._lock:
            self._mirror[mem.id] = mem

    def get(self, memory_id: int) -> _StubMemory | None:
        with self._lock:
            return self._mirror.get(int(memory_id))


def _vec(seed: list[float]) -> np.ndarray:
    return _normalise(np.asarray(seed, dtype=np.float32))


def _two_cluster_store() -> _StubMemoryStore:
    store = _StubMemoryStore()
    for mem in [
        _StubMemory(1, "cat naps", _vec([0.95, 0.30, 0.0, 0.0])),
        _StubMemory(2, "kittens", _vec([0.92, 0.39, 0.0, 0.0])),
        _StubMemory(3, "warm cats", _vec([0.97, 0.25, 0.0, 0.0])),
        _StubMemory(10, "basil", _vec([0.0, 0.0, 0.95, 0.30])),
        _StubMemory(11, "rosemary", _vec([0.0, 0.0, 0.92, 0.39])),
        _StubMemory(12, "herbs", _vec([0.0, 0.0, 0.97, 0.25])),
    ]:
        store.add(mem)
    return store


def _cluster_store() -> tuple[ChatDatabase, TopicClusterStore]:
    tmp = tempfile.mkdtemp()
    db = ChatDatabase(Path(tmp) / "t.db")
    return db, TopicClusterStore(db)


class PersistentModeTests(unittest.TestCase):
    def _graph(self, mem_store, cluster_store) -> TopicGraph:
        return TopicGraph(
            mem_store, similarity=0.55, min_cluster_size=2,
            filter_threshold=0.65, cluster_store=cluster_store,
        )

    def test_first_read_rebuilds_and_persists(self) -> None:
        mem = _two_cluster_store()
        _, cs = _cluster_store()
        g = self._graph(mem, cs)
        self.assertTrue(g.persistent)
        clusters = g.topic_clusters()
        self.assertEqual(len(clusters), 2)
        # Persisted now.
        rows, assignments = cs.load_all()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(assignments), 6)

    def test_second_instance_warm_starts_without_rebuild(self) -> None:
        mem = _two_cluster_store()
        db, cs = _cluster_store()
        self._graph(mem, cs).topic_clusters()  # builds + persists
        # Fresh graph over the same store; should load from SQLite.
        g2 = TopicGraph(
            mem, similarity=0.55, min_cluster_size=2,
            filter_threshold=0.65, cluster_store=TopicClusterStore(db),
        )
        clusters = g2.topic_clusters()
        self.assertEqual(len(clusters), 2)
        sizes = sorted(len(c.member_ids) for c in clusters)
        self.assertEqual(sizes, [3, 3])

    def test_incremental_add_close_joins_cluster(self) -> None:
        mem = _two_cluster_store()
        _, cs = _cluster_store()
        g = self._graph(mem, cs)
        g.topic_clusters()  # warm
        new = _StubMemory(4, "more cats", _vec([0.93, 0.36, 0.0, 0.0]))
        mem.add(new)
        g.on_memory_added(new)
        clusters = g.topic_clusters()
        self.assertEqual(len(clusters), 2)  # no new cluster
        # The new memory should now be a member of the cat cluster.
        cat = next(c for c in clusters if 1 in c.member_ids)
        self.assertIn(4, cat.member_ids)
        # Persisted assignment.
        _, assignments = cs.load_all()
        self.assertIn(4, assignments)

    def test_incremental_add_far_stays_pending(self) -> None:
        mem = _two_cluster_store()
        _, cs = _cluster_store()
        g = self._graph(mem, cs)
        g.topic_clusters()
        far = _StubMemory(99, "quantum chromodynamics", _vec([0.0, 1.0, 0.0, 0.0]))
        mem.add(far)
        g.on_memory_added(far)
        self.assertEqual(g.pending_count(), 1)
        _, assignments = cs.load_all()
        self.assertNotIn(99, assignments)

    def test_delete_removes_member(self) -> None:
        mem = _two_cluster_store()
        _, cs = _cluster_store()
        g = self._graph(mem, cs)
        g.topic_clusters()
        g.on_memory_deleted(3)
        clusters = g.topic_clusters()
        cat = next(c for c in clusters if 1 in c.member_ids)
        self.assertNotIn(3, cat.member_ids)
        _, assignments = cs.load_all()
        self.assertNotIn(3, assignments)

    def test_delete_drops_empty_cluster(self) -> None:
        mem = _two_cluster_store()
        _, cs = _cluster_store()
        g = self._graph(mem, cs)
        g.topic_clusters()
        # Wipe one whole family (min_cluster_size=2 -> falls below after 2 dels).
        for mid in (10, 11, 12):
            g.on_memory_deleted(mid)
        clusters = g.topic_clusters()
        self.assertEqual(len(clusters), 1)

    def test_rebuild_returns_cluster_count_and_resets_pending(self) -> None:
        mem = _two_cluster_store()
        _, cs = _cluster_store()
        g = self._graph(mem, cs)
        g.topic_clusters()
        far = _StubMemory(99, "x", _vec([0.0, 1.0, 0.0, 0.0]))
        mem.add(far)
        g.on_memory_added(far)
        self.assertEqual(g.pending_count(), 1)
        n = g.rebuild()
        self.assertGreaterEqual(n, 2)
        self.assertEqual(g.pending_count(), 0)

    def test_snapshot_reports_persistent(self) -> None:
        mem = _two_cluster_store()
        _, cs = _cluster_store()
        g = self._graph(mem, cs)
        snap = build_topic_graph_snapshot(g, mem)
        self.assertTrue(snap["persistent"])
        self.assertEqual(snap["algorithm"], "mutual_knn_louvain")
        self.assertIn("pending_unclustered", snap)


class ListenerIntegrationTests(unittest.TestCase):
    """Add/delete propagate through real MemoryStore listeners into the
    persisted graph."""

    def test_memory_store_listeners_drive_graph(self) -> None:
        from app.core.memory.memory_store import MemoryStore

        tmp = tempfile.mkdtemp()
        path = Path(tmp) / "chat.db"
        db = ChatDatabase(path)  # creates the schema (memories + v20 tables)
        store = MemoryStore(path)
        cs = TopicClusterStore(db)
        g = TopicGraph(
            store, similarity=0.55, min_cluster_size=2,
            filter_threshold=0.65, cluster_store=cs,
        )
        store.add_memory_listener(g.on_memory_added)
        store.add_delete_listener(g.on_memory_deleted)

        # Seed a cat cluster directly so the centroid exists.
        seeds = [
            ("cat naps", _vec([0.95, 0.30, 0.0, 0.0])),
            ("kittens", _vec([0.92, 0.39, 0.0, 0.0])),
        ]
        for content, vec in seeds:
            store.add(content, "fact", vec, skip_dedupe=True)
        # The batch refit (what TopicGraphRebuildWorker runs) forms the
        # initial cluster from the accumulated memories. Incremental adds
        # can only *join* an existing cluster, not bootstrap one.
        g.rebuild()
        clusters = g.topic_clusters()
        self.assertEqual(len(clusters), 1)

        # A new close memory must be incrementally absorbed (listener path).
        m3 = store.add("warm cats", "fact", _vec([0.97, 0.25, 0.0, 0.0]), skip_dedupe=True)
        clusters = g.topic_clusters()
        cat = clusters[0]
        self.assertIn(int(m3.id), cat.member_ids)

        # Deleting drops it from the graph (delete-listener path).
        store.delete(int(m3.id))
        clusters = g.topic_clusters()
        self.assertNotIn(int(m3.id), clusters[0].member_ids if clusters else ())


class AnnClustererTests(unittest.TestCase):
    """``_cluster_memories_ann`` over a real LanceDB-backed RagStore."""

    def test_ann_finds_two_clusters(self) -> None:
        import shutil
        from app.core.conversation.topic_graph import _cluster_memories_ann
        from app.core.rag.rag_store import RagStore

        tmp = Path(tempfile.mkdtemp(prefix="aiko-tg-ann-"))
        try:
            rag = RagStore(tmp, embedding_model="x", vector_dim=4)
            mem = _two_cluster_store()
            for m in mem._mirror.values():
                rag.add_memory(
                    record_id=str(m.id), content=m.content,
                    kind=m.kind, embedding=m.embedding,
                )
            mems = list(mem._mirror.values())
            clusters = _cluster_memories_ann(mems, rag, min_size=2, floor=0.55)
            self.assertEqual(len(clusters), 2)
            ids = {tuple(sorted(int(x.id) for x in g)) for g in clusters}
            self.assertIn((1, 2, 3), ids)
            self.assertIn((10, 11, 12), ids)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class InterestMapTests(unittest.TestCase):
    """F10e ``TopicGraph.interest_map``: top-N clusters by size, cheap (no
    mirror join), persistent-only, renders the F10a label (clean name) over
    the heuristic fallback."""

    def _graph(self, mem, cs) -> TopicGraph:
        return TopicGraph(
            mem, similarity=0.55, min_cluster_size=2,
            filter_threshold=0.65, cluster_store=cs,
        )

    def test_non_persistent_returns_empty(self) -> None:
        g = TopicGraph(_two_cluster_store(), min_cluster_size=2)
        self.assertFalse(g.persistent)
        self.assertEqual(g.interest_map(), [])

    def test_clusters_sorted_by_size_with_f10a_labels(self) -> None:
        mem = _two_cluster_store()
        # Make the cat cluster bigger so size ordering is determinate.
        mem.add(_StubMemory(4, "more cats", _vec([0.93, 0.36, 0.0, 0.0])))
        mem.add(_StubMemory(5, "a cat", _vec([0.94, 0.33, 0.0, 0.0])))
        _, cs = _cluster_store()
        g = self._graph(mem, cs)
        clusters = g.topic_clusters()  # warm + build (2 clusters)
        self.assertEqual(len(clusters), 2)
        # After a build every cluster carries a heuristic label, so the
        # map is already populated (sizes 5 and 3, largest first).
        sizes = [e.size for e in g.interest_map(top_n=5, min_size=2)]
        self.assertEqual(sizes, [5, 3])
        # The F10a worker's clean label replaces the heuristic one.
        cat = next(c for c in clusters if 1 in c.member_ids)
        herb = next(c for c in clusters if 10 in c.member_ids)
        g.set_cluster_label(cat.cluster_id, "cats")
        g.set_cluster_label(herb.cluster_id, "herbs")
        entries = g.interest_map(top_n=5, min_size=2)
        self.assertEqual(
            [(e.label, e.size) for e in entries],
            [("cats", 5), ("herbs", 3)],
        )

    def test_blank_label_is_skipped(self) -> None:
        # Defensive: a cluster whose label is somehow blank never surfaces.
        mem = _two_cluster_store()
        _, cs = _cluster_store()
        g = self._graph(mem, cs)
        g.topic_clusters()
        with g._lock:  # type: ignore[attr-defined]
            for cluster in g._live.values():  # type: ignore[attr-defined]
                cluster.label = ""
        self.assertEqual(g.interest_map(top_n=5, min_size=2), [])

    def test_top_n_cap(self) -> None:
        mem = _two_cluster_store()
        mem.add(_StubMemory(4, "more cats", _vec([0.93, 0.36, 0.0, 0.0])))
        _, cs = _cluster_store()
        g = self._graph(mem, cs)
        clusters = g.topic_clusters()
        for c in clusters:
            g.set_cluster_label(
                c.cluster_id, "cats" if 1 in c.member_ids else "herbs",
            )
        entries = g.interest_map(top_n=1, min_size=2)
        self.assertEqual([e.label for e in entries], ["cats"])

    def test_min_size_floor_excludes_small_clusters(self) -> None:
        mem = _two_cluster_store()
        mem.add(_StubMemory(4, "more cats", _vec([0.93, 0.36, 0.0, 0.0])))
        _, cs = _cluster_store()
        g = self._graph(mem, cs)
        clusters = g.topic_clusters()  # cats=4, herbs=3
        for c in clusters:
            g.set_cluster_label(
                c.cluster_id, "cats" if 1 in c.member_ids else "herbs",
            )
        # min_size=4 drops the 3-member herb cluster.
        entries = g.interest_map(top_n=5, min_size=4)
        self.assertEqual([e.label for e in entries], ["cats"])


if __name__ == "__main__":
    unittest.main()
