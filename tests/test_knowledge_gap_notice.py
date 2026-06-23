"""Tests for F10f — the knowledge-gap notice worker + surfacing provider.

Covers the cue producer
(:class:`~app.core.proactive.knowledge_gap_notice_worker.KnowledgeGapNoticeWorker`),
its pure helpers (``topic_key`` / ``topic_relevant``), and the inner-life
consumer
(:meth:`InnerLifePart2Mixin._render_knowledge_gap_notice_block`).
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.proactive.knowledge_gap_notice_worker import (
    KnowledgeGapNoticeWorker,
    append_notice,
    load_notices,
    topic_key,
    topic_relevant,
)
from app.core.session.inner_life_part2 import InnerLifePart2Mixin


# ── fakes ───────────────────────────────────────────────────────────────


@dataclass
class _GapCluster:
    cluster_id: int
    label: str
    size: int
    knowledge_count: int
    knowledge_fraction: float


class _FakeGraph:
    def __init__(self, clusters: list[_GapCluster]) -> None:
        self._clusters = clusters
        self.calls: list[dict] = []

    def knowledge_gap_clusters(self, *, min_size, max_knowledge_fraction, top_n):
        self.calls.append(
            {
                "min_size": min_size,
                "max_knowledge_fraction": max_knowledge_fraction,
                "top_n": top_n,
            }
        )
        return list(self._clusters)[:top_n]


class _KV:
    def __init__(self) -> None:
        self.d: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self.d.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.d[key] = value


def _make_worker(
    graph, kv, **kw
) -> KnowledgeGapNoticeWorker:
    params: dict = {
        "interval_seconds": 3600.0,
        "min_size": 5,
        "max_knowledge_fraction": 0.15,
        "topic_cooldown_hours": 72.0,
        "journal_max": 6,
    }
    params.update(kw)
    return KnowledgeGapNoticeWorker(
        topic_graph_provider=lambda: graph,
        kv_get=kv.kv_get,
        kv_set=kv.kv_set,
        **params,
    )


# ── pure helpers ─────────────────────────────────────────────────────────


class HelperTests(unittest.TestCase):
    def test_topic_key_stable_and_normalised(self) -> None:
        self.assertEqual(topic_key("Python  Debugging"), topic_key("python debugging"))
        self.assertNotEqual(topic_key("python"), topic_key("guitar"))

    def test_topic_relevant_overlap(self) -> None:
        self.assertTrue(topic_relevant("python debugging", "how do I debug python?"))
        self.assertFalse(topic_relevant("python debugging", "let's talk about wine"))
        # Sub-3-char words are ignored on both sides.
        self.assertFalse(topic_relevant("a to", "to a"))


# ── worker ───────────────────────────────────────────────────────────────


class WorkerTests(unittest.TestCase):
    def test_drafts_strongest_gap(self) -> None:
        graph = _FakeGraph(
            [_GapCluster(1, "python debugging", 8, 0, 0.0)]
        )
        kv = _KV()
        worker = _make_worker(graph, kv)
        result = worker.run()
        self.assertEqual(result["drafted"], 1)
        self.assertEqual(result["topic"], "python debugging")
        ring = load_notices(kv.kv_get)
        self.assertEqual(len(ring), 1)
        self.assertEqual(ring[0]["topic"], "python debugging")
        self.assertEqual(ring[0]["cluster_key"], topic_key("python debugging"))

    def test_no_candidate(self) -> None:
        worker = _make_worker(_FakeGraph([]), _KV())
        self.assertEqual(worker.run()["drafted"], 0)

    def test_no_graph(self) -> None:
        kv = _KV()
        worker = KnowledgeGapNoticeWorker(
            topic_graph_provider=lambda: None,
            kv_get=kv.kv_get,
            kv_set=kv.kv_set,
        )
        self.assertTrue(worker.run().get("no_graph"))

    def test_disabled(self) -> None:
        graph = _FakeGraph([_GapCluster(1, "python", 8, 0, 0.0)])
        worker = _make_worker(graph, _KV(), enabled_provider=lambda: False)
        self.assertTrue(worker.run().get("disabled"))

    def test_per_topic_cooldown_blocks_redraft(self) -> None:
        graph = _FakeGraph([_GapCluster(1, "python", 8, 0, 0.0)])
        kv = _KV()
        worker = _make_worker(graph, kv)
        self.assertEqual(worker.run()["drafted"], 1)
        # Immediately re-running: same topic on cooldown → skipped.
        self.assertTrue(worker.run().get("all_on_cooldown"))

    def test_force_next_bypasses_cooldown(self) -> None:
        graph = _FakeGraph([_GapCluster(1, "python", 8, 0, 0.0)])
        kv = _KV()
        worker = _make_worker(graph, kv)
        worker.run()
        worker.force_next()
        self.assertEqual(worker.run()["drafted"], 1)
        self.assertEqual(len(load_notices(kv.kv_get)), 2)

    def test_cooldown_expiry_allows_redraft(self) -> None:
        graph = _FakeGraph([_GapCluster(1, "python", 8, 0, 0.0)])
        kv = _KV()
        worker = _make_worker(graph, kv, topic_cooldown_hours=1.0)
        worker.run()
        # Backdate the cooldown stamp beyond the window.
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        import json

        kv.d["knowledge_gap_notice.topic_cooldowns"] = json.dumps(
            {topic_key("python"): old}
        )
        self.assertEqual(worker.run()["drafted"], 1)

    def test_journal_trims_to_max(self) -> None:
        kv = _KV()
        for i in range(10):
            append_notice(
                kv.kv_get, kv.kv_set,
                {"at": str(i), "topic": f"t{i}", "cluster_key": f"k{i}"},
                max_entries=6,
            )
        self.assertEqual(len(load_notices(kv.kv_get)), 6)


# ── provider ─────────────────────────────────────────────────────────────


class _Agent:
    knowledge_gap_notice_enabled = True


class _Settings:
    def __init__(self) -> None:
        self.agent = _Agent()


class _Host(InnerLifePart2Mixin):
    def __init__(self) -> None:
        self._settings = _Settings()
        self._chat_db = _KV()
        self._knowledge_gap_notice_force_next = False


class ProviderTests(unittest.TestCase):
    def _seed(self, host: _Host, topic: str = "python debugging") -> None:
        append_notice(
            host._chat_db.kv_get,
            host._chat_db.kv_set,
            {
                "at": "2026-01-01T00:00:00+00:00",
                "topic": topic,
                "cluster_key": topic_key(topic),
                "size": 8,
                "knowledge_count": 0,
            },
            max_entries=6,
        )

    def test_empty_ring_returns_blank(self) -> None:
        host = _Host()
        self.assertEqual(
            host._render_knowledge_gap_notice_block("debugging python"), ""
        )

    def test_disabled_returns_blank(self) -> None:
        host = _Host()
        host._settings.agent.knowledge_gap_notice_enabled = False
        self._seed(host)
        self.assertEqual(
            host._render_knowledge_gap_notice_block("debugging python"), ""
        )

    def test_surfaces_on_topic_relevant_turn(self) -> None:
        host = _Host()
        self._seed(host)
        out = host._render_knowledge_gap_notice_block("how do I debug python?")
        self.assertIn("python", out.lower())
        self.assertIn("keeps coming up", out)

    def test_not_relevant_returns_blank(self) -> None:
        host = _Host()
        self._seed(host)
        self.assertEqual(
            host._render_knowledge_gap_notice_block("tell me about wine"), ""
        )

    def test_surfaced_once_only(self) -> None:
        host = _Host()
        self._seed(host)
        first = host._render_knowledge_gap_notice_block("debug python")
        self.assertTrue(first)
        second = host._render_knowledge_gap_notice_block("debug python")
        self.assertEqual(second, "")

    def test_force_next_bypasses_relevance(self) -> None:
        host = _Host()
        self._seed(host)
        host._knowledge_gap_notice_force_next = True
        # Empty user_text + irrelevant: force still surfaces newest.
        out = host._render_knowledge_gap_notice_block("")
        self.assertIn("python", out.lower())


if __name__ == "__main__":
    unittest.main()
