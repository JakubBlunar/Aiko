"""Tests for K67 — the dormant-interest re-opener worker + provider.

Covers the cue producer
(:class:`~app.core.proactive.dormant_interest_worker.DormantInterestWorker`),
its pure classifier (``classify_dormant``), the journal helpers, and the
inner-life consumer
(:meth:`InnerLifePart2Mixin._render_dormant_interest_block`).
"""
from __future__ import annotations

import json
import unittest
from dataclasses import dataclass

from app.core.proactive.dormant_interest_worker import (
    DORMANT_INTEREST_JOURNAL_KEY,
    DormantInterestWorker,
    append_dormant,
    classify_dormant,
    load_dormant,
)
from app.core.proactive.knowledge_gap_notice_worker import topic_key
from app.core.session.inner_life_part2 import InnerLifePart2Mixin


# ── fakes ───────────────────────────────────────────────────────────────


@dataclass
class _Entry:
    label: str
    size: int
    days_since: float | None = None
    last_active: str = ""


class _FakeGraph:
    """Returns a scripted cluster_activity result."""

    def __init__(self, rows: list[_Entry]) -> None:
        self._rows = rows

    def cluster_activity(self, *, top_n, min_size=None):
        rows = [e for e in self._rows if e.size >= (min_size or 0)]
        rows = sorted(rows, key=lambda e: e.size, reverse=True)
        return rows[:top_n]


class _KV:
    def __init__(self) -> None:
        self.d: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self.d.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.d[key] = value


def _make_worker(graph, kv, **kw) -> DormantInterestWorker:
    params: dict = {
        "interval_seconds": 21600.0,
        "daily_cap": 5,
        "journal_max": 6,
        "min_size": 6,
        "max_clusters": 40,
        "dormant_days": 21.0,
        "topic_cooldown_hours": 336.0,
    }
    params.update(kw)
    return DormantInterestWorker(
        topic_graph_provider=lambda: graph,
        kv_get=kv.kv_get,
        kv_set=kv.kv_set,
        **params,
    )


# ── pure classifier ──────────────────────────────────────────────────────


class ClassifyTests(unittest.TestCase):
    def test_dormant_when_big_and_old(self) -> None:
        self.assertTrue(
            classify_dormant(10, 40.0, min_size=6, dormant_days=21.0)
        )

    def test_not_dormant_when_recent(self) -> None:
        self.assertFalse(
            classify_dormant(10, 5.0, min_size=6, dormant_days=21.0)
        )

    def test_not_dormant_when_small(self) -> None:
        self.assertFalse(
            classify_dormant(3, 90.0, min_size=6, dormant_days=21.0)
        )

    def test_none_days_since_not_dormant(self) -> None:
        self.assertFalse(
            classify_dormant(10, None, min_size=6, dormant_days=21.0)
        )

    def test_exactly_at_threshold_counts(self) -> None:
        self.assertTrue(
            classify_dormant(6, 21.0, min_size=6, dormant_days=21.0)
        )


# ── worker ───────────────────────────────────────────────────────────────


class WorkerTests(unittest.TestCase):
    def test_no_graph(self) -> None:
        kv = _KV()
        worker = DormantInterestWorker(
            topic_graph_provider=lambda: None,
            kv_get=kv.kv_get,
            kv_set=kv.kv_set,
        )
        self.assertTrue(worker.run().get("no_graph"))

    def test_disabled(self) -> None:
        graph = _FakeGraph([_Entry("hiking", 10, 40.0)])
        worker = _make_worker(graph, _KV(), enabled_provider=lambda: False)
        self.assertTrue(worker.run().get("disabled"))

    def test_drafts_dormant_interest(self) -> None:
        graph = _FakeGraph([_Entry("garage band", 8, 45.0)])
        kv = _KV()
        result = _make_worker(graph, kv).run()
        self.assertEqual(result["drafted"], 1)
        self.assertEqual(result["topic"], "garage band")
        ring = load_dormant(kv.kv_get)
        self.assertEqual(ring[0]["topic_key"], topic_key("garage band"))
        self.assertEqual(ring[0]["size"], 8)

    def test_recent_topic_is_no_candidate(self) -> None:
        graph = _FakeGraph([_Entry("garage band", 8, 3.0)])
        self.assertTrue(_make_worker(graph, _KV()).run().get("no_candidate"))

    def test_small_topic_is_no_candidate(self) -> None:
        # Below min_size (6) — also below cluster_activity's own floor, so it
        # never even reaches the classifier; either way: no candidate.
        graph = _FakeGraph([_Entry("garage band", 3, 90.0)])
        self.assertTrue(_make_worker(graph, _KV()).run().get("no_candidate"))

    def test_none_days_since_is_no_candidate(self) -> None:
        graph = _FakeGraph([_Entry("garage band", 8, None)])
        self.assertTrue(_make_worker(graph, _KV()).run().get("no_candidate"))

    def test_most_dormant_ranked_first(self) -> None:
        graph = _FakeGraph(
            [
                _Entry("guitar", 8, 30.0),
                _Entry("painting", 7, 120.0),
            ]
        )
        result = _make_worker(graph, _KV()).run()
        self.assertEqual(result["topic"], "painting")

    def test_topic_cooldown_blocks_redraft(self) -> None:
        graph = _FakeGraph([_Entry("garage band", 8, 45.0)])
        kv = _KV()
        worker = _make_worker(graph, kv)
        self.assertEqual(worker.run()["drafted"], 1)
        self.assertTrue(worker.run().get("no_candidate"))

    def test_force_next_bypasses_cooldown(self) -> None:
        graph = _FakeGraph([_Entry("garage band", 8, 45.0)])
        kv = _KV()
        worker = _make_worker(graph, kv)
        worker.run()
        worker.force_next()
        self.assertEqual(worker.run()["drafted"], 1)
        self.assertEqual(len(load_dormant(kv.kv_get)), 2)

    def test_daily_cap_blocks(self) -> None:
        graph = _FakeGraph(
            [
                _Entry("guitar", 8, 45.0),
                _Entry("painting", 7, 50.0),
                _Entry("climbing", 9, 60.0),
            ]
        )
        kv = _KV()
        worker = _make_worker(graph, kv, daily_cap=2)
        self.assertEqual(worker.run()["drafted"], 1)
        self.assertEqual(worker.run()["drafted"], 1)
        self.assertTrue(worker.run().get("skipped_daily_cap"))

    def test_journal_trims_to_max(self) -> None:
        kv = _KV()
        for i in range(10):
            append_dormant(
                kv.kv_get, kv.kv_set,
                {"at": str(i), "topic": f"t{i}", "topic_key": f"k{i}",
                 "days_since": 30.0, "size": 8},
                max_entries=6,
            )
        self.assertEqual(len(load_dormant(kv.kv_get)), 6)


# ── provider ─────────────────────────────────────────────────────────────


class _Agent:
    dormant_interest_enabled = True


class _MemSettings:
    stagnation_mild_threshold = 0.18
    dormant_interest_surface_cooldown_hours = 24.0


class _Settings:
    def __init__(self) -> None:
        self.agent = _Agent()


class _Detector:
    def __init__(self, last_mean=None) -> None:
        self.last_mean = last_mean


class _Host(InnerLifePart2Mixin):
    def __init__(self, *, last_mean=0.05, surface_cooldown=0.0) -> None:
        self._settings = _Settings()
        self._memory_settings = _MemSettings()
        self._memory_settings.dormant_interest_surface_cooldown_hours = (
            surface_cooldown
        )
        self._chat_db = _KV()
        self._topic_stagnation_detector = _Detector(last_mean)
        self._dormant_interest_force_next = False


class ProviderTests(unittest.TestCase):
    def _seed(self, host: _Host, *, topic="garage band") -> None:
        append_dormant(
            host._chat_db.kv_get,
            host._chat_db.kv_set,
            {
                "at": "2026-01-01T00:00:00+00:00",
                "topic": topic,
                "topic_key": topic_key(topic),
                "days_since": 45.0,
                "size": 8,
            },
            max_entries=6,
        )

    def test_empty_ring_returns_blank(self) -> None:
        self.assertEqual(_Host()._render_dormant_interest_block(), "")

    def test_disabled_returns_blank(self) -> None:
        host = _Host()
        host._settings.agent.dormant_interest_enabled = False
        self._seed(host)
        self.assertEqual(host._render_dormant_interest_block(), "")

    def test_no_lull_returns_blank(self) -> None:
        host = _Host(last_mean=0.40)  # conversation still moving
        self._seed(host)
        self.assertEqual(host._render_dormant_interest_block(), "")

    def test_none_lull_returns_blank(self) -> None:
        host = _Host(last_mean=None)  # window not warm yet
        self._seed(host)
        self.assertEqual(host._render_dormant_interest_block(), "")

    def test_surfaces_on_lull(self) -> None:
        host = _Host(last_mean=0.05)
        self._seed(host)
        out = host._render_dormant_interest_block()
        self.assertIn("garage band", out)
        self.assertIn("gone quiet", out)

    def test_surfaced_once_only(self) -> None:
        # surface cooldown disabled → isolates the per-topic surfaced gate.
        host = _Host(last_mean=0.05, surface_cooldown=0.0)
        self._seed(host)
        self.assertTrue(host._render_dormant_interest_block())
        self.assertEqual(host._render_dormant_interest_block(), "")

    def test_surface_cooldown_blocks_second(self) -> None:
        host = _Host(last_mean=0.05, surface_cooldown=24.0)
        self._seed(host, topic="garage band")
        self._seed(host, topic="oil painting")
        self.assertTrue(host._render_dormant_interest_block())
        # Second distinct topic queued, but the wall-clock cooldown holds.
        self.assertEqual(host._render_dormant_interest_block(), "")

    def test_force_next_bypasses_all_gates(self) -> None:
        host = _Host(last_mean=0.40)  # no lull
        self._seed(host)
        host._dormant_interest_force_next = True
        self.assertIn(
            "garage band", host._render_dormant_interest_block()
        )


if __name__ == "__main__":
    unittest.main()
