"""Unit tests for L46 meta-graph depth / cycle / descendant walks."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.concepts.concept_meta_depth import (
    descendant_ids,
    meta_depth,
    would_cycle,
)


class _Edge:
    def __init__(self, src_id: int) -> None:
        self.src_type = "concept"
        self.src_id = str(src_id)


class _Store:
    def __init__(self, models: dict[int, str], children: dict[int, list[int]]):
        self._models = models
        self._children = children

    def get(self, cid: int):
        model = self._models.get(int(cid))
        if model is None:
            return None
        return SimpleNamespace(evidence_model=model, concept_id=int(cid))

    def evidence_of(self, cid: int):
        return [_Edge(c) for c in self._children.get(int(cid), [])]


class MetaDepthTests(unittest.TestCase):
    def test_non_meta_is_zero(self) -> None:
        store = _Store({1: "set"}, {})
        self.assertEqual(meta_depth(store, 1), 0)

    def test_missing_is_zero(self) -> None:
        store = _Store({}, {})
        self.assertEqual(meta_depth(store, 99), 0)

    def test_meta_without_children_is_one(self) -> None:
        store = _Store({2: "meta"}, {})
        self.assertEqual(meta_depth(store, 2), 1)

    def test_l1_over_bases_is_one(self) -> None:
        store = _Store({1: "set", 2: "set", 10: "meta"}, {10: [1, 2]})
        self.assertEqual(meta_depth(store, 10), 1)

    def test_l2_over_l1s_is_two(self) -> None:
        store = _Store(
            {1: "set", 2: "set", 3: "set", 10: "meta", 11: "meta", 20: "meta"},
            {10: [1, 2], 11: [3], 20: [10, 11]},
        )
        self.assertEqual(meta_depth(store, 20), 2)
        self.assertEqual(meta_depth(store, 10), 1)

    def test_memo_shared_across_calls(self) -> None:
        store = _Store({1: "set", 10: "meta", 20: "meta"}, {10: [1], 20: [10]})
        memo: dict[int, int] = {}
        self.assertEqual(meta_depth(store, 20, memo=memo), 2)
        self.assertIn(10, memo)
        self.assertEqual(memo[10], 1)

    def test_cycle_returns_cap_not_hang(self) -> None:
        store = _Store({10: "meta", 11: "meta"}, {10: [11], 11: [10]})
        self.assertGreaterEqual(meta_depth(store, 10, max_walk=4), 4)


class DescendantAndCycleTests(unittest.TestCase):
    def test_cone_includes_grandchildren(self) -> None:
        store = _Store(
            {1: "set", 2: "set", 10: "meta", 20: "meta"},
            {10: [1, 2], 20: [10]},
        )
        self.assertEqual(descendant_ids(store, 20), {10, 1, 2})
        self.assertNotIn(20, descendant_ids(store, 20))

    def test_new_parent_cannot_cycle(self) -> None:
        store = _Store({10: "meta"}, {})
        self.assertFalse(would_cycle(store, None, 10))
        self.assertFalse(would_cycle(store, 0, 10))

    def test_self_edge_is_cycle(self) -> None:
        store = _Store({10: "meta"}, {})
        self.assertTrue(would_cycle(store, 10, 10))

    def test_citing_own_descendant_is_cycle(self) -> None:
        store = _Store(
            {1: "set", 10: "meta", 20: "meta"},
            {10: [1], 20: [10]},
        )
        # Adding 20 as a child of 10 would loop 20 -> 10 -> 20.
        self.assertTrue(would_cycle(store, 10, 20))
        self.assertFalse(would_cycle(store, 20, 10))


if __name__ == "__main__":
    unittest.main()
