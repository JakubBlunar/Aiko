"""Tests for the L4 cluster co-activation signal (``cluster_coactivation``).

Exercise co-firing detection across conversation sessions, the
support / Jaccard thresholds, connected-component mode grouping, the
session bucket strategy (members without a session are unbucketable),
the non-persistent no-op, and the coarse result cache. Uses the same
persistent-mode harness shape as ``test_topic_graph_persistent`` with
``source_session`` added to the stub memory.
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
from app.core.conversation.topic_graph import TopicGraph, _normalise


@dataclass
class _StubMemory:
    id: int
    content: str
    embedding: np.ndarray
    kind: str = "fact"
    salience: float = 0.5
    use_count: int = 0
    source_session: str | None = None
    source_message_id: int | None = None
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


# Four well-separated cluster directions in R^4.
_DIRS = {
    "A": [1.0, 0.0, 0.0, 0.0],
    "B": [0.0, 1.0, 0.0, 0.0],
    "C": [0.0, 0.0, 1.0, 0.0],
    "D": [0.0, 0.0, 0.0, 1.0],
}


def _vec(family: str, jitter: float) -> np.ndarray:
    base = np.asarray(_DIRS[family], dtype=np.float32).copy()
    # Small in-family jitter on a second axis so members aren't identical
    # but stay far from the other families.
    idx = (list(_DIRS).index(family) + 1) % 4
    base[idx] += jitter
    return _normalise(base)


def _cluster_store() -> tuple[ChatDatabase, TopicClusterStore]:
    tmp = tempfile.mkdtemp()
    db = ChatDatabase(Path(tmp) / "t.db")
    return db, TopicClusterStore(db)


def _graph(mem_store, cluster_store) -> TopicGraph:
    return TopicGraph(
        mem_store,
        similarity=0.5,
        min_cluster_size=2,
        filter_threshold=0.6,
        cluster_store=cluster_store,
    )


def _rep_to_family(graph: TopicGraph, mem: _StubMemoryStore) -> dict[int, str]:
    """Map each cluster's representative_id to its family letter by looking
    at the family of any member (all members of a cluster share a family)."""
    out: dict[int, str] = {}
    for cluster in graph.topic_clusters():
        first = next(iter(cluster.member_ids))
        content = mem.get(int(first)).content  # type: ignore[union-attr]
        out[int(cluster.representative_id)] = content.split(":")[0]
    return out


class CoactivationDetectionTests(unittest.TestCase):
    def _two_mode_store(self) -> _StubMemoryStore:
        """A/B co-fire in sessions s1+s2; C/D co-fire in s3+s4."""
        mem = _StubMemoryStore()
        rows = [
            # id, family, session
            (1, "A", "s1"), (2, "A", "s2"),
            (3, "B", "s1"), (4, "B", "s2"),
            (5, "C", "s3"), (6, "C", "s4"),
            (7, "D", "s3"), (8, "D", "s4"),
        ]
        for i, (mid, fam, sess) in enumerate(rows):
            mem.add(
                _StubMemory(
                    mid,
                    f"{fam}:memory {mid}",
                    _vec(fam, 0.05 * (i + 1)),
                    source_session=sess,
                )
            )
        return mem

    def test_detects_two_modes(self) -> None:
        mem = self._two_mode_store()
        _, cs = _cluster_store()
        g = _graph(mem, cs)
        self.assertEqual(len(g.topic_clusters()), 4)
        modes = g.cluster_coactivation(
            min_pair_support=2, min_strength=0.5, max_modes=4,
        )
        self.assertEqual(len(modes), 2)
        rep_family = _rep_to_family(g, mem)
        mode_families = {
            frozenset(rep_family[r] for r in mode.reps) for mode in modes
        }
        self.assertEqual(
            mode_families, {frozenset({"A", "B"}), frozenset({"C", "D"})}
        )
        for mode in modes:
            self.assertEqual(mode.bucket_by, "session")
            self.assertAlmostEqual(mode.strength, 1.0, places=3)
            self.assertEqual(len(mode.labels), len(mode.reps))

    def test_min_pair_support_threshold(self) -> None:
        mem = self._two_mode_store()
        _, cs = _cluster_store()
        g = _graph(mem, cs)
        # Every pair only co-occurs in 2 sessions; support 3 => nothing.
        modes = g.cluster_coactivation(min_pair_support=3, min_strength=0.1)
        self.assertEqual(modes, [])

    def test_min_strength_threshold(self) -> None:
        # A fires alone in many sessions but co-fires with B only twice ->
        # low Jaccard, filtered out by a high min_strength.
        mem = _StubMemoryStore()
        rows = [
            (1, "A", "s1"), (2, "A", "s2"),
            (3, "A", "s3"), (4, "A", "s4"),  # A also fires solo
            (5, "B", "s1"), (6, "B", "s2"),  # B only ever with A in s1,s2
        ]
        for i, (mid, fam, sess) in enumerate(rows):
            mem.add(
                _StubMemory(
                    mid, f"{fam}:m{mid}", _vec(fam, 0.05 * (i + 1)),
                    source_session=sess,
                )
            )
        _, cs = _cluster_store()
        g = _graph(mem, cs)
        # inter=2, |A buckets|=4, |B buckets|=2 -> Jaccard 2/4 = 0.5.
        self.assertEqual(
            g.cluster_coactivation(min_pair_support=2, min_strength=0.6), []
        )
        kept = g.cluster_coactivation(min_pair_support=2, min_strength=0.4)
        self.assertEqual(len(kept), 1)
        self.assertAlmostEqual(kept[0].strength, 0.5, places=3)

    def test_connected_component_grouping(self) -> None:
        # A-B co-fire (s1,s2), B-C co-fire (s3,s4) -> one mode {A,B,C}.
        mem = _StubMemoryStore()
        rows = [
            (1, "A", "s1"), (2, "A", "s2"),
            (3, "B", "s1"), (4, "B", "s2"),
            (5, "B", "s3"), (6, "B", "s4"),
            (7, "C", "s3"), (8, "C", "s4"),
        ]
        for i, (mid, fam, sess) in enumerate(rows):
            mem.add(
                _StubMemory(
                    mid, f"{fam}:m{mid}", _vec(fam, 0.05 * (i + 1)),
                    source_session=sess,
                )
            )
        _, cs = _cluster_store()
        g = _graph(mem, cs)
        modes = g.cluster_coactivation(
            min_pair_support=2, min_strength=0.4, max_reps_per_mode=4,
        )
        self.assertEqual(len(modes), 1)
        rep_family = _rep_to_family(g, mem)
        fams = {rep_family[r] for r in modes[0].reps}
        self.assertEqual(fams, {"A", "B", "C"})

    def test_members_without_session_are_ignored(self) -> None:
        # Same embedding families but no session ids -> unbucketable -> no
        # co-firing at all.
        mem = _StubMemoryStore()
        rows = [
            (1, "A"), (2, "A"), (3, "B"), (4, "B"),
        ]
        for i, (mid, fam) in enumerate(rows):
            mem.add(
                _StubMemory(
                    mid, f"{fam}:m{mid}", _vec(fam, 0.05 * (i + 1)),
                    source_session=None,
                )
            )
        _, cs = _cluster_store()
        g = _graph(mem, cs)
        self.assertEqual(len(g.topic_clusters()), 2)
        self.assertEqual(
            g.cluster_coactivation(min_pair_support=1, min_strength=0.0), []
        )

    def test_non_persistent_is_empty(self) -> None:
        mem = self._two_mode_store()
        g = TopicGraph(mem, similarity=0.5, min_cluster_size=2)
        self.assertFalse(g.persistent)
        self.assertEqual(g.cluster_coactivation(), [])

    def test_unknown_bucket_by_is_empty(self) -> None:
        mem = self._two_mode_store()
        _, cs = _cluster_store()
        g = _graph(mem, cs)
        self.assertEqual(g.cluster_coactivation(bucket_by="nope"), [])

    def test_result_is_cached(self) -> None:
        mem = self._two_mode_store()
        _, cs = _cluster_store()
        g = _graph(mem, cs)
        first = g.cluster_coactivation(min_pair_support=2, min_strength=0.5)
        self.assertTrue(g._coact_cache)  # cache populated
        second = g.cluster_coactivation(min_pair_support=2, min_strength=0.5)
        self.assertEqual(
            [m.reps for m in first], [m.reps for m in second]
        )


if __name__ == "__main__":
    unittest.main()
