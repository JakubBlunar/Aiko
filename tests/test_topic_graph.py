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
    _adaptive_k,
    _cluster_memories_adaptive,
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

    def test_bridge_memory_does_not_chain_families(self) -> None:
        """The mutual-k-NN clusterer must NOT fuse two dense families
        through a single weak bridge memory (the single-link failure
        mode that produces "one huge cluster")."""
        store = _StubMemoryStore()
        # Family A: tight knot on axis 0.
        for i in range(5):
            store.add(
                _StubMemory(
                    id=i + 1,
                    content=f"A-{i}",
                    embedding=_vec([1.0, 0.04 * i, 0.0]),
                )
            )
        # Family B: tight knot on axis 2.
        for i in range(5):
            store.add(
                _StubMemory(
                    id=20 + i,
                    content=f"B-{i}",
                    embedding=_vec([0.0, 0.04 * i, 1.0]),
                )
            )
        # A single bridge memory sitting roughly between the two knots.
        # Single-link at a modest threshold would union A and B through
        # it; mutual-k-NN should not, because the bridge cannot be in
        # the mutual top-k of members on *both* dense sides.
        store.add(
            _StubMemory(id=99, content="bridge", embedding=_vec([0.7, 0.0, 0.7])),
        )
        graph = TopicGraph(
            store, similarity=0.55, min_cluster_size=3, filter_threshold=0.65,
        )
        clusters = graph.topic_clusters()
        # The two families stay as separate clusters (the bridge may or
        # may not attach to one of them, but it must never merge them).
        family_ids = [
            set(c.member_ids) for c in clusters if len(c.member_ids) >= 3
        ]
        merged = any(
            ({1, 2, 3, 4, 5} <= ids) and ({20, 21, 22, 23, 24} <= ids)
            for ids in family_ids
        )
        self.assertFalse(merged, "bridge memory chained the two families")
        # Both dense families survive as their own clusters.
        self.assertTrue(any({1, 2, 3, 4, 5} <= ids for ids in family_ids))
        self.assertTrue(any({20, 21, 22, 23, 24} <= ids for ids in family_ids))

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

    def test_snapshot_reports_algorithm_and_k(self) -> None:
        store = _build_two_cluster_store()
        graph = TopicGraph(store, similarity=0.55, min_cluster_size=2)
        snap = build_topic_graph_snapshot(graph, store)
        self.assertEqual(snap["algorithm"], "mutual_knn_louvain")
        # k is derived from the corpus size and recorded on build.
        self.assertEqual(snap["neighbors_k"], _adaptive_k(6))
        self.assertGreaterEqual(snap["neighbors_k"], 2)

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


class LouvainPartitionTests(unittest.TestCase):
    """The reason for the upgrade: connectivity merges a densely-linked
    blob into one cluster; Louvain modularity splits it into topics."""

    def test_splits_connected_blob_into_communities(self) -> None:
        from app.core.conversation.topic_graph import (
            _connected_components,
            _partition_graph,
        )

        # Three dense triangles (0-1-2, 3-4-5, 6-7-8) linked by two weak
        # bridge edges (2-3, 5-6): ONE connected component, but THREE
        # modular communities.
        strong, weak = 0.95, 0.56
        edges = [
            (0, 1, strong), (1, 2, strong), (0, 2, strong),
            (3, 4, strong), (4, 5, strong), (3, 5, strong),
            (6, 7, strong), (7, 8, strong), (6, 8, strong),
            (2, 3, weak), (5, 6, weak),
        ]
        # Connected components collapses the whole thing into one blob.
        cc_big = [c for c in _connected_components(9, edges) if len(c) >= 3]
        self.assertEqual(len(cc_big), 1)
        # Louvain recovers the three dense triangles.
        meta: dict = {}
        comms = _partition_graph(9, edges, meta=meta)
        big = [c for c in comms if len(c) >= 3]
        self.assertGreaterEqual(len(big), 3)
        self.assertEqual(meta["algorithm"], "mutual_knn_louvain")
        self.assertGreater(meta["resolution"], 0.0)

    def test_empty_edges_yields_singletons(self) -> None:
        from app.core.conversation.topic_graph import _partition_graph

        comms = _partition_graph(4, [])
        self.assertEqual(sorted(len(c) for c in comms), [1, 1, 1, 1])

    def test_adaptive_resolution_grows_and_clamps(self) -> None:
        from app.core.conversation.topic_graph import _adaptive_resolution

        self.assertEqual(_adaptive_resolution(5), 1.0)
        self.assertGreater(_adaptive_resolution(1000), _adaptive_resolution(50))
        self.assertLessEqual(_adaptive_resolution(10_000_000), 2.5)


class AdaptiveKTests(unittest.TestCase):
    def test_scales_logarithmically_and_clamps(self) -> None:
        # Tiny corpora: "everyone is a neighbour".
        self.assertEqual(_adaptive_k(2), 1)
        self.assertEqual(_adaptive_k(3), 2)
        # Grows ~log2(n)+1.
        self.assertEqual(_adaptive_k(6), 4)
        self.assertGreaterEqual(_adaptive_k(100), 7)
        # Clamped to the upper bound on huge corpora.
        self.assertLessEqual(_adaptive_k(1_000_000), 12)

    def test_empty_input_returns_no_clusters(self) -> None:
        self.assertEqual(
            _cluster_memories_adaptive([], min_size=3, floor=0.55), []
        )


if __name__ == "__main__":
    unittest.main()
