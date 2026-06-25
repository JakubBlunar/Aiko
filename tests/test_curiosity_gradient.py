"""Tests for K64c — the curiosity-gradient worker + surfacing provider.

Covers the cue producer
(:class:`~app.core.proactive.curiosity_gradient_worker.CuriosityGradientWorker`),
its pure edge finder (``find_gradient_edges``), the journal helpers, and the
inner-life consumer
(:meth:`InnerLifePart2Mixin._render_curiosity_gradient_block`).
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass, field

import numpy as np

from app.core.proactive.associative_wander_worker import pair_key
from app.core.proactive.curiosity_gradient_worker import (
    CuriosityGradientWorker,
    append_gradient,
    find_gradient_edges,
    gradient_relevant,
    load_gradients,
)
from app.core.session.inner_life_part2 import InnerLifePart2Mixin


# ── fakes ───────────────────────────────────────────────────────────────


@dataclass
class _Cluster:
    cluster_id: int
    summary: str
    size: int
    centroid: np.ndarray
    member_ids: tuple[int, ...] = field(default_factory=tuple)


class _FakeGraph:
    def __init__(self, clusters: list[_Cluster]) -> None:
        self._clusters = clusters

    def topic_clusters(self) -> list[_Cluster]:
        return list(self._clusters)


class _KV:
    def __init__(self) -> None:
        self.d: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self.d.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.d[key] = value


def _vec(*xs: float) -> np.ndarray:
    return np.asarray(xs, dtype=np.float32)


def _make_worker(graph, kv, **kw) -> CuriosityGradientWorker:
    params: dict = {
        "interval_seconds": 5400.0,
        "daily_cap": 5,
        "journal_max": 6,
        "dense_min_size": 8,
        "thin_min_size": 2,
        "thin_max_size": 4,
        "adjacency_min_cosine": 0.40,
        "adjacency_max_cosine": 0.90,
        "edge_cooldown_hours": 96.0,
    }
    params.update(kw)
    return CuriosityGradientWorker(
        topic_graph_provider=lambda: graph,
        kv_get=kv.kv_get,
        kv_set=kv.kv_set,
        **params,
    )


# ── pure edge finder ──────────────────────────────────────────────────────


class FindEdgesTests(unittest.TestCase):
    def test_thin_adjacent_to_dense_is_edge(self) -> None:
        # Thin cluster at ~0.6 cosine to the dense anchor → adjacent edge.
        clusters = [
            _Cluster(1, "hiking gear", 12, _vec(1, 0, 0)),
            _Cluster(2, "trail navigation", 3, _vec(0.6, 0.8, 0)),
        ]
        edges = find_gradient_edges(
            clusters, dense_min_size=8, thin_min_size=2, thin_max_size=4,
            adjacency_min=0.40, adjacency_max=0.90,
        )
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].dense_label, "hiking gear")
        self.assertEqual(edges[0].thin_label, "trail navigation")

    def test_far_thin_cluster_excluded(self) -> None:
        clusters = [
            _Cluster(1, "hiking gear", 12, _vec(1, 0, 0)),
            _Cluster(2, "tax law", 3, _vec(0, 1, 0)),  # cos 0 → not adjacent
        ]
        self.assertEqual(
            find_gradient_edges(
                clusters, dense_min_size=8, thin_min_size=2, thin_max_size=4,
                adjacency_min=0.40, adjacency_max=0.90,
            ),
            [],
        )

    def test_near_duplicate_excluded(self) -> None:
        clusters = [
            _Cluster(1, "hiking gear", 12, _vec(1, 0, 0)),
            _Cluster(2, "hiking boots", 3, _vec(0.99, 0.14, 0)),  # ~dup
        ]
        self.assertEqual(
            find_gradient_edges(
                clusters, dense_min_size=8, thin_min_size=2, thin_max_size=4,
                adjacency_min=0.40, adjacency_max=0.90,
            ),
            [],
        )

    def test_no_dense_anchor(self) -> None:
        clusters = [
            _Cluster(1, "a", 3, _vec(1, 0, 0)),
            _Cluster(2, "b", 3, _vec(0.6, 0.8, 0)),
        ]
        self.assertEqual(
            find_gradient_edges(
                clusters, dense_min_size=8, thin_min_size=2, thin_max_size=4,
                adjacency_min=0.40, adjacency_max=0.90,
            ),
            [],
        )

    def test_gradient_relevant_either_side(self) -> None:
        entry = {"dense_topic": "hiking gear", "thin_topic": "trail navigation"}
        self.assertTrue(gradient_relevant(entry, "my hiking gear is old"))
        self.assertTrue(gradient_relevant(entry, "how does navigation work"))
        self.assertFalse(gradient_relevant(entry, "let's talk about wine"))


# ── worker ───────────────────────────────────────────────────────────────


def _edge_graph() -> _FakeGraph:
    return _FakeGraph(
        [
            _Cluster(1, "hiking gear", 12, _vec(1, 0, 0)),
            _Cluster(2, "trail navigation", 3, _vec(0.6, 0.8, 0)),
        ]
    )


class WorkerTests(unittest.TestCase):
    def test_drafts_edge(self) -> None:
        kv = _KV()
        result = _make_worker(_edge_graph(), kv).run()
        self.assertEqual(result["drafted"], 1)
        self.assertEqual(result["dense_topic"], "hiking gear")
        self.assertEqual(result["thin_topic"], "trail navigation")
        ring = load_gradients(kv.kv_get)
        self.assertEqual(
            ring[0]["edge_key"], pair_key("hiking gear", "trail navigation")
        )

    def test_no_graph(self) -> None:
        kv = _KV()
        worker = CuriosityGradientWorker(
            topic_graph_provider=lambda: None,
            kv_get=kv.kv_get,
            kv_set=kv.kv_set,
        )
        self.assertTrue(worker.run().get("no_graph"))

    def test_no_edge(self) -> None:
        graph = _FakeGraph(
            [
                _Cluster(1, "hiking", 12, _vec(1, 0, 0)),
                _Cluster(2, "tax law", 3, _vec(0, 1, 0)),
            ]
        )
        self.assertTrue(_make_worker(graph, _KV()).run().get("no_edge"))

    def test_disabled(self) -> None:
        worker = _make_worker(_edge_graph(), _KV(), enabled_provider=lambda: False)
        self.assertTrue(worker.run().get("disabled"))

    def test_edge_cooldown_blocks_redraft(self) -> None:
        kv = _KV()
        worker = _make_worker(_edge_graph(), kv)
        self.assertEqual(worker.run()["drafted"], 1)
        self.assertTrue(worker.run().get("all_on_cooldown"))

    def test_force_next_bypasses_cooldown(self) -> None:
        kv = _KV()
        worker = _make_worker(_edge_graph(), kv)
        worker.run()
        worker.force_next()
        self.assertEqual(worker.run()["drafted"], 1)
        self.assertEqual(len(load_gradients(kv.kv_get)), 2)

    def test_daily_cap_blocks(self) -> None:
        kv = _KV()
        worker = _make_worker(
            _edge_graph(), kv, daily_cap=1, edge_cooldown_hours=0.0
        )
        self.assertEqual(worker.run()["drafted"], 1)
        self.assertTrue(worker.run().get("skipped_daily_cap"))

    def test_journal_trims_to_max(self) -> None:
        kv = _KV()
        for i in range(10):
            append_gradient(
                kv.kv_get, kv.kv_set,
                {"at": str(i), "dense_topic": f"d{i}", "thin_topic": f"t{i}",
                 "edge_key": f"k{i}", "cosine": 0.5},
                max_entries=6,
            )
        self.assertEqual(len(load_gradients(kv.kv_get)), 6)


# ── provider ─────────────────────────────────────────────────────────────


class _Agent:
    curiosity_gradient_enabled = True


class _Settings:
    def __init__(self) -> None:
        self.agent = _Agent()


class _Host(InnerLifePart2Mixin):
    def __init__(self) -> None:
        self._settings = _Settings()
        self._chat_db = _KV()
        self._curiosity_gradient_force_next = False


class ProviderTests(unittest.TestCase):
    def _seed(self, host: _Host) -> None:
        append_gradient(
            host._chat_db.kv_get,
            host._chat_db.kv_set,
            {
                "at": "2026-01-01T00:00:00+00:00",
                "dense_topic": "hiking gear",
                "thin_topic": "trail navigation",
                "edge_key": pair_key("hiking gear", "trail navigation"),
                "cosine": 0.6,
            },
            max_entries=6,
        )

    def test_empty_ring_returns_blank(self) -> None:
        self.assertEqual(
            _Host()._render_curiosity_gradient_block("hiking gear"), ""
        )

    def test_disabled_returns_blank(self) -> None:
        host = _Host()
        host._settings.agent.curiosity_gradient_enabled = False
        self._seed(host)
        self.assertEqual(
            host._render_curiosity_gradient_block("hiking gear"), ""
        )

    def test_surfaces_on_dense_topic(self) -> None:
        host = _Host()
        self._seed(host)
        out = host._render_curiosity_gradient_block("my hiking gear broke")
        self.assertIn("trail navigation", out)
        self.assertIn("curious", out.lower())

    def test_surfaces_on_thin_topic(self) -> None:
        host = _Host()
        self._seed(host)
        out = host._render_curiosity_gradient_block("how does navigation work")
        self.assertIn("trail navigation", out)

    def test_not_relevant_returns_blank(self) -> None:
        host = _Host()
        self._seed(host)
        self.assertEqual(
            host._render_curiosity_gradient_block("tell me about wine"), ""
        )

    def test_surfaced_once_only(self) -> None:
        host = _Host()
        self._seed(host)
        self.assertTrue(host._render_curiosity_gradient_block("hiking gear"))
        self.assertEqual(host._render_curiosity_gradient_block("hiking gear"), "")

    def test_force_next_bypasses_relevance(self) -> None:
        host = _Host()
        self._seed(host)
        host._curiosity_gradient_force_next = True
        self.assertIn(
            "trail navigation", host._render_curiosity_gradient_block("")
        )


if __name__ == "__main__":
    unittest.main()
