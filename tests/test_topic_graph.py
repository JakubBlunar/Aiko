"""Tests for :mod:`app.core.conversation.topic_graph` (K9 personality backlog).

The graph is a thin wrapper over the in-memory ``MemoryStore._mirror``
so the tests build a fake mirror with synthetic two-cluster
embeddings and assert clustering, the "is this fresh?" filter, and
the cache invalidation behaviour.
"""
from __future__ import annotations

import threading
import unittest
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.core.conversation.topic_graph import (
    TopicGraph,
    _normalise,
    build_topic_graph_snapshot,
)


# ── stub mirror ──────────────────────────────────────────────────────


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
    """Bare-bones stand-in for :class:`MemoryStore` with the surface
    :class:`TopicGraph` actually touches: ``_mirror`` and ``_lock``."""

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


def _build_two_cluster_store() -> _StubMemoryStore:
    """Returns a store with two well-separated 4-D clusters."""
    store = _StubMemoryStore()
    cluster_a = [
        _StubMemory(id=1, content="cat naps in sunbeams", embedding=_vec([0.95, 0.30, 0.0, 0.0])),
        _StubMemory(id=2, content="kittens like windowsills", embedding=_vec([0.92, 0.39, 0.0, 0.0])),
        _StubMemory(id=3, content="cats and warm spots", embedding=_vec([0.97, 0.25, 0.0, 0.0])),
    ]
    cluster_b = [
        _StubMemory(id=10, content="basil seedlings", embedding=_vec([0.0, 0.0, 0.95, 0.30])),
        _StubMemory(id=11, content="watering rosemary", embedding=_vec([0.0, 0.0, 0.92, 0.39])),
        _StubMemory(id=12, content="herbs in pots", embedding=_vec([0.0, 0.0, 0.97, 0.25])),
    ]
    for mem in cluster_a + cluster_b:
        store.add(mem)
    return store


# ── tests ────────────────────────────────────────────────────────────


class ClusteringTests(unittest.TestCase):
    def test_finds_two_clusters(self) -> None:
        store = _build_two_cluster_store()
        graph = TopicGraph(
            store, similarity=0.55, min_cluster_size=2, filter_threshold=0.65,
        )
        clusters = graph.topic_clusters()
        self.assertEqual(len(clusters), 2)
        # Each cluster must contain only members from one of the two
        # disjoint synthetic groups.
        cluster_ids = {tuple(sorted(c.member_ids)) for c in clusters}
        self.assertIn((1, 2, 3), cluster_ids)
        self.assertIn((10, 11, 12), cluster_ids)

    def test_below_min_cluster_size_drops_singletons(self) -> None:
        store = _StubMemoryStore()
        store.add(
            _StubMemory(
                id=1, content="lonely fact", embedding=_vec([1.0, 0.0]),
            )
        )
        graph = TopicGraph(
            store, similarity=0.5, min_cluster_size=2, filter_threshold=0.5,
        )
        self.assertEqual(graph.topic_clusters(), [])


class FilterTests(unittest.TestCase):
    def test_inside_cluster_is_close(self) -> None:
        store = _build_two_cluster_store()
        graph = TopicGraph(
            store, similarity=0.55, min_cluster_size=2, filter_threshold=0.65,
        )
        # A vector that's very close to cluster A's first member.
        candidate = _vec([0.95, 0.31, 0.0, 0.0])
        self.assertTrue(graph.is_close_to_any_cluster(candidate))

    def test_outside_clusters_is_fresh(self) -> None:
        store = _build_two_cluster_store()
        graph = TopicGraph(
            store, similarity=0.55, min_cluster_size=2, filter_threshold=0.85,
        )
        # Orthogonal axis -> nothing in the store covers this.
        candidate = _vec([0.0, 1.0, 0.0, 0.0])
        self.assertFalse(graph.is_close_to_any_cluster(candidate))

    def test_empty_mirror_returns_false(self) -> None:
        store = _StubMemoryStore()
        graph = TopicGraph(
            store, similarity=0.5, min_cluster_size=2, filter_threshold=0.5,
        )
        self.assertFalse(
            graph.is_close_to_any_cluster(_vec([1.0, 0.0])),
        )

    def test_threshold_override_respected(self) -> None:
        store = _build_two_cluster_store()
        graph = TopicGraph(
            store, similarity=0.55, min_cluster_size=2, filter_threshold=0.4,
        )
        # Mid-cosine candidate. Sits roughly 0.5 away from every
        # member -- below the per-call strict threshold, above the
        # default looser one.
        candidate = _vec([0.6, 0.0, 0.6, 0.0])
        self.assertTrue(graph.is_close_to_any_cluster(candidate))
        self.assertFalse(
            graph.is_close_to_any_cluster(candidate, threshold=0.99),
        )

    def test_best_match_returns_id(self) -> None:
        store = _build_two_cluster_store()
        graph = TopicGraph(
            store, similarity=0.55, min_cluster_size=2, filter_threshold=0.65,
        )
        sim, mid = graph.best_match(_vec([0.95, 0.30, 0.0, 0.0]))
        self.assertGreater(sim, 0.99)
        # The nearest member is one of cluster A.
        self.assertIn(mid, {1, 2, 3})


class CacheInvalidationTests(unittest.TestCase):
    def test_cache_reused_when_mirror_unchanged(self) -> None:
        store = _build_two_cluster_store()
        graph = TopicGraph(
            store, similarity=0.55, min_cluster_size=2, filter_threshold=0.5,
        )
        first = graph.topic_clusters()
        second = graph.topic_clusters()
        # Same instances if the cache held — TopicCluster is frozen
        # so identity is the cleanest check.
        self.assertEqual(len(first), len(second))
        for a, b in zip(first, second):
            self.assertIs(a, b)

    def test_cache_rebuilds_on_new_memory(self) -> None:
        store = _build_two_cluster_store()
        graph = TopicGraph(
            store, similarity=0.55, min_cluster_size=2, filter_threshold=0.5,
        )
        before = graph.topic_clusters()
        store.add(
            _StubMemory(
                id=99,
                content="extra cat fact",
                embedding=_vec([0.93, 0.36, 0.0, 0.0]),
                last_used_at="2030-01-01T00:00:00+00:00",
            )
        )
        after = graph.topic_clusters()
        # Old snapshot didn't contain id=99; new one must.
        before_ids = {mid for c in before for mid in c.member_ids}
        after_ids = {mid for c in after for mid in c.member_ids}
        self.assertNotIn(99, before_ids)
        self.assertIn(99, after_ids)

    def test_cache_invalidates_on_threshold_update(self) -> None:
        store = _build_two_cluster_store()
        graph = TopicGraph(
            store, similarity=0.55, min_cluster_size=2, filter_threshold=0.5,
        )
        first = graph.topic_clusters()
        graph.update_runtime(filter_threshold=0.7)
        # After the update, a fresh build must happen — the cluster
        # objects can't be the same identities.
        second = graph.topic_clusters()
        if first and second:
            self.assertIsNot(first[0], second[0])


class SnapshotTests(unittest.TestCase):
    """K9 browser surface: ``build_topic_graph_snapshot`` shape + joins."""

    def test_disabled_when_topic_graph_none(self) -> None:
        snap = build_topic_graph_snapshot(None, _StubMemoryStore())
        self.assertFalse(snap["enabled"])
        self.assertEqual(snap["clusters"], [])
        self.assertEqual(snap["total_clusters"], 0)

    def test_disabled_when_memory_store_none(self) -> None:
        store = _build_two_cluster_store()
        graph = TopicGraph(store, similarity=0.55, min_cluster_size=2)
        snap = build_topic_graph_snapshot(graph, None)
        self.assertFalse(snap["enabled"])

    def test_snapshot_shape_and_joins(self) -> None:
        store = _build_two_cluster_store()
        graph = TopicGraph(
            store, similarity=0.55, min_cluster_size=2, filter_threshold=0.65,
        )
        snap = build_topic_graph_snapshot(graph, store)
        self.assertTrue(snap["enabled"])
        self.assertEqual(snap["total_clusters"], 2)
        self.assertEqual(snap["total_memories"], 6)
        self.assertEqual(snap["clustered_memories"], 6)
        self.assertAlmostEqual(snap["similarity"], 0.55)
        self.assertEqual(snap["min_cluster_size"], 2)
        # Each cluster carries joined member details.
        for cluster in snap["clusters"]:
            self.assertEqual(cluster["size"], len(cluster["members"]))
            self.assertIn("kind_counts", cluster)
            for member in cluster["members"]:
                self.assertIn("id", member)
                self.assertIn("content", member)
                self.assertIn("kind", member)
                self.assertIn("tier", member)
                self.assertIn("salience", member)

    def test_clusters_sorted_by_size_desc(self) -> None:
        store = _StubMemoryStore()
        # Cluster A: 4 members; cluster B: 2 members.
        for i, off in enumerate([0.30, 0.32, 0.28, 0.34]):
            store.add(
                _StubMemory(
                    id=i + 1,
                    content=f"cat fact {i}",
                    embedding=_vec([0.95, off, 0.0, 0.0]),
                )
            )
        for i, off in enumerate([0.30, 0.34]):
            store.add(
                _StubMemory(
                    id=100 + i,
                    content=f"herb fact {i}",
                    embedding=_vec([0.0, 0.0, 0.95, off]),
                )
            )
        graph = TopicGraph(store, similarity=0.55, min_cluster_size=2)
        snap = build_topic_graph_snapshot(graph, store)
        sizes = [c["size"] for c in snap["clusters"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual(sizes[0], 4)

    def test_member_content_trimmed(self) -> None:
        store = _StubMemoryStore()
        long_text = "x" * 500
        for i in range(2):
            store.add(
                _StubMemory(
                    id=i + 1,
                    content=long_text,
                    embedding=_vec([0.95, 0.30 + i * 0.01, 0.0, 0.0]),
                )
            )
        graph = TopicGraph(store, similarity=0.55, min_cluster_size=2)
        snap = build_topic_graph_snapshot(graph, store, max_member_chars=160)
        member = snap["clusters"][0]["members"][0]
        self.assertLessEqual(len(member["content"]), 160)


if __name__ == "__main__":
    unittest.main()
