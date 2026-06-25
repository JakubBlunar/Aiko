"""Tests for K64b — the interest-drift worker + surfacing provider.

Covers the cue producer
(:class:`~app.core.proactive.interest_drift_worker.InterestDriftWorker`),
its pure classifier (``classify_drift``), the journal helpers, and the
inner-life consumer
(:meth:`InnerLifePart2Mixin._render_interest_drift_block`).
"""
from __future__ import annotations

import json
import unittest
from dataclasses import dataclass

from app.core.proactive.interest_drift_worker import (
    INTEREST_DRIFT_JOURNAL_KEY,
    InterestDriftWorker,
    append_drift,
    classify_drift,
    drift_relevant,
    load_drifts,
)
from app.core.proactive.knowledge_gap_notice_worker import topic_key
from app.core.session.inner_life_part2 import InnerLifePart2Mixin


# ── fakes ───────────────────────────────────────────────────────────────


@dataclass
class _Entry:
    label: str
    size: int


class _FakeGraph:
    """Returns a scripted sequence of interest_map results, one per tick."""

    def __init__(self, frames: list[list[_Entry]]) -> None:
        self._frames = frames
        self._i = 0

    def interest_map(self, *, top_n, min_size=None):
        frame = self._frames[min(self._i, len(self._frames) - 1)]
        self._i += 1
        return [e for e in frame if e.size >= (min_size or 0)][:top_n]


class _KV:
    def __init__(self) -> None:
        self.d: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self.d.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.d[key] = value


def _make_worker(graph, kv, **kw) -> InterestDriftWorker:
    params: dict = {
        "interval_seconds": 21600.0,
        "daily_cap": 5,
        "journal_max": 6,
        "min_size": 4,
        "max_clusters": 40,
        "window_samples": 8,
        "min_samples": 3,
        "rise_ratio": 0.5,
        "fade_max_growth_ratio": 0.05,
        "topic_cooldown_hours": 72.0,
    }
    params.update(kw)
    return InterestDriftWorker(
        topic_graph_provider=lambda: graph,
        kv_get=kv.kv_get,
        kv_set=kv.kv_set,
        **params,
    )


# ── pure classifier ──────────────────────────────────────────────────────


class ClassifyTests(unittest.TestCase):
    def test_rising_on_fast_growth(self) -> None:
        self.assertEqual(
            classify_drift(
                [4, 6, 9], rise_ratio=0.5, fade_max_growth_ratio=0.05, min_size=4
            ),
            "rising",
        )

    def test_fading_on_stagnant_large_cluster(self) -> None:
        self.assertEqual(
            classify_drift(
                [20, 20, 20], rise_ratio=0.5, fade_max_growth_ratio=0.05, min_size=4
            ),
            "fading",
        )

    def test_small_absolute_gain_not_rising(self) -> None:
        # +1 over the window is below _RISE_MIN_DELTA even if ratio is high.
        self.assertIsNone(
            classify_drift(
                [2, 3], rise_ratio=0.1, fade_max_growth_ratio=0.0, min_size=4
            )
        )

    def test_below_min_size_is_neutral(self) -> None:
        self.assertIsNone(
            classify_drift(
                [1, 2, 3], rise_ratio=0.5, fade_max_growth_ratio=0.05, min_size=4
            )
        )

    def test_too_few_samples(self) -> None:
        self.assertIsNone(
            classify_drift(
                [10], rise_ratio=0.5, fade_max_growth_ratio=0.05, min_size=4
            )
        )

    def test_moderate_growth_is_neutral(self) -> None:
        # Grew 8->10 (25%, +2): below rise_ratio 0.5 and above fade ceiling.
        self.assertIsNone(
            classify_drift(
                [8, 9, 10], rise_ratio=0.5, fade_max_growth_ratio=0.05, min_size=4
            )
        )

    def test_drift_relevant(self) -> None:
        entry = {"topic": "weekend hiking plans"}
        self.assertTrue(drift_relevant(entry, "thinking about hiking soon"))
        self.assertFalse(drift_relevant(entry, "let's talk about wine"))


# ── worker ───────────────────────────────────────────────────────────────


class WorkerTests(unittest.TestCase):
    def test_no_graph(self) -> None:
        kv = _KV()
        worker = InterestDriftWorker(
            topic_graph_provider=lambda: None,
            kv_get=kv.kv_get,
            kv_set=kv.kv_set,
        )
        self.assertTrue(worker.run().get("no_graph"))

    def test_disabled(self) -> None:
        graph = _FakeGraph([[_Entry("hiking", 10)]])
        worker = _make_worker(graph, _KV(), enabled_provider=lambda: False)
        self.assertTrue(worker.run().get("disabled"))

    def test_builds_series_and_stays_silent_until_warm(self) -> None:
        # min_samples=3 → first two ticks only sample, no draft.
        graph = _FakeGraph(
            [
                [_Entry("rust debugging", 4)],
                [_Entry("rust debugging", 6)],
                [_Entry("rust debugging", 9)],
            ]
        )
        kv = _KV()
        worker = _make_worker(graph, kv)
        self.assertEqual(worker.run().get("drafted", 0), 0)  # 1 sample
        self.assertEqual(worker.run().get("drafted", 0), 0)  # 2 samples
        result = worker.run()  # 3 samples → classify rising
        self.assertEqual(result["drafted"], 1)
        self.assertEqual(result["direction"], "rising")
        ring = load_drifts(kv.kv_get)
        self.assertEqual(ring[0]["topic"], "rust debugging")
        self.assertEqual(ring[0]["topic_key"], topic_key("rust debugging"))

    def test_topic_cooldown_blocks_redraft(self) -> None:
        graph = _FakeGraph(
            [
                [_Entry("rust", 4)],
                [_Entry("rust", 6)],
                [_Entry("rust", 9)],
                [_Entry("rust", 12)],
            ]
        )
        kv = _KV()
        worker = _make_worker(graph, kv)
        worker.run(); worker.run()
        self.assertEqual(worker.run()["drafted"], 1)
        # Next tick: still growing but the topic is on cooldown.
        self.assertTrue(worker.run().get("no_candidate"))

    def test_force_next_bypasses_cooldown(self) -> None:
        graph = _FakeGraph(
            [
                [_Entry("rust", 4)],
                [_Entry("rust", 6)],
                [_Entry("rust", 9)],
                [_Entry("rust", 12)],
            ]
        )
        kv = _KV()
        worker = _make_worker(graph, kv)
        worker.run(); worker.run(); worker.run()
        worker.force_next()
        self.assertEqual(worker.run()["drafted"], 1)
        self.assertEqual(len(load_drifts(kv.kv_get)), 2)

    def test_journal_trims_to_max(self) -> None:
        kv = _KV()
        for i in range(10):
            append_drift(
                kv.kv_get, kv.kv_set,
                {"at": str(i), "topic": f"t{i}", "topic_key": f"k{i}",
                 "direction": "rising", "from_size": 1, "to_size": 2},
                max_entries=6,
            )
        self.assertEqual(len(load_drifts(kv.kv_get)), 6)


# ── provider ─────────────────────────────────────────────────────────────


class _Agent:
    interest_drift_enabled = True


class _Settings:
    def __init__(self) -> None:
        self.agent = _Agent()


class _Host(InnerLifePart2Mixin):
    def __init__(self) -> None:
        self._settings = _Settings()
        self._chat_db = _KV()
        self._interest_drift_force_next = False


class ProviderTests(unittest.TestCase):
    def _seed(self, host: _Host, *, topic="weekend hiking", direction="rising") -> None:
        append_drift(
            host._chat_db.kv_get,
            host._chat_db.kv_set,
            {
                "at": "2026-01-01T00:00:00+00:00",
                "topic": topic,
                "topic_key": topic_key(topic),
                "direction": direction,
                "from_size": 4,
                "to_size": 12,
            },
            max_entries=6,
        )

    def test_empty_ring_returns_blank(self) -> None:
        self.assertEqual(
            _Host()._render_interest_drift_block("going hiking"), ""
        )

    def test_disabled_returns_blank(self) -> None:
        host = _Host()
        host._settings.agent.interest_drift_enabled = False
        self._seed(host)
        self.assertEqual(host._render_interest_drift_block("going hiking"), "")

    def test_rising_surfaces_on_relevant_turn(self) -> None:
        host = _Host()
        self._seed(host, direction="rising")
        out = host._render_interest_drift_block("planning a hiking trip")
        self.assertIn("hiking", out.lower())
        self.assertIn("drawn to", out)

    def test_fading_copy(self) -> None:
        host = _Host()
        self._seed(host, direction="fading")
        out = host._render_interest_drift_block("anything about hiking?")
        self.assertIn("drifted out", out)

    def test_not_relevant_returns_blank(self) -> None:
        host = _Host()
        self._seed(host)
        self.assertEqual(
            host._render_interest_drift_block("tell me about wine"), ""
        )

    def test_surfaced_once_only(self) -> None:
        host = _Host()
        self._seed(host)
        self.assertTrue(host._render_interest_drift_block("hiking plans"))
        self.assertEqual(host._render_interest_drift_block("hiking plans"), "")

    def test_force_next_bypasses_relevance(self) -> None:
        host = _Host()
        self._seed(host)
        host._interest_drift_force_next = True
        self.assertIn("hiking", host._render_interest_drift_block("").lower())


if __name__ == "__main__":
    unittest.main()
