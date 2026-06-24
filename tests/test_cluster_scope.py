"""Tests for F10j — :func:`app.core.memory.cluster_scope.partition_by_cluster`."""
from __future__ import annotations

import unittest
from dataclasses import dataclass

from app.core.memory.cluster_scope import partition_by_cluster


@dataclass
class _Mem:
    id: int
    created_at: str = ""


class _FakeGraph:
    def __init__(self, assignment: dict[int, int | None], *, persistent=True):
        self._assignment = assignment
        self.persistent = persistent

    def cluster_id_for(self, memory_id: int) -> int | None:
        return self._assignment.get(int(memory_id))


class PartitionByClusterTests(unittest.TestCase):
    def test_empty_returns_empty(self) -> None:
        self.assertEqual(partition_by_cluster([], _FakeGraph({})), [])

    def test_disabled_single_group(self) -> None:
        rows = [_Mem(1), _Mem(2), _Mem(3)]
        out = partition_by_cluster(rows, _FakeGraph({}), enabled=False)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0], rows)

    def test_no_graph_single_group(self) -> None:
        rows = [_Mem(1), _Mem(2)]
        out = partition_by_cluster(rows, None, enabled=True)
        self.assertEqual(out, [rows])

    def test_non_persistent_single_group(self) -> None:
        rows = [_Mem(1), _Mem(2)]
        out = partition_by_cluster(
            rows, _FakeGraph({1: 0, 2: 0}, persistent=False), enabled=True,
        )
        self.assertEqual(out, [rows])

    def test_partitions_by_cluster_and_drops_singletons(self) -> None:
        rows = [
            _Mem(1, "2026-01-01"),
            _Mem(2, "2026-01-02"),  # cluster A
            _Mem(3, "2026-02-01"),
            _Mem(4, "2026-02-02"),
            _Mem(5, "2026-02-03"),  # cluster B (newest)
            _Mem(6, "2026-01-15"),  # cluster C — singleton, dropped
            _Mem(7, "2026-01-10"),
            _Mem(8, "2026-01-11"),  # unclustered (None) bucket
        ]
        graph = _FakeGraph(
            {1: 10, 2: 10, 3: 20, 4: 20, 5: 20, 6: 30, 7: None, 8: None}
        )
        out = partition_by_cluster(rows, graph)
        # C (singleton) dropped; A, B, and the None bucket remain.
        self.assertEqual(len(out), 3)
        id_sets = [{m.id for m in g} for g in out]
        self.assertIn({1, 2}, id_sets)
        self.assertIn({3, 4, 5}, id_sets)
        self.assertIn({7, 8}, id_sets)
        # Ordered by newest member desc → cluster B first (2026-02-03).
        self.assertEqual({m.id for m in out[0]}, {3, 4, 5})

    def test_cluster_id_for_failure_buckets_unclustered(self) -> None:
        class _BoomGraph(_FakeGraph):
            def cluster_id_for(self, memory_id: int):
                raise RuntimeError("boom")

        rows = [_Mem(1), _Mem(2)]
        out = partition_by_cluster(rows, _BoomGraph({}))
        # Both fall into the None bucket → one group of 2.
        self.assertEqual(len(out), 1)
        self.assertEqual({m.id for m in out[0]}, {1, 2})


if __name__ == "__main__":
    unittest.main()
