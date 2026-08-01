"""Tests for K64b — the interest-drift worker + surfacing provider.

Covers the cue producer
(:class:`~app.core.proactive.interest_drift_worker.InterestDriftWorker`),
its pure classifier (``classify_drift``), the journal helpers, and the
inner-life consumer
(:meth:`InnerLifePart2Mixin._render_interest_drift_block`).
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.core.proactive.interest_drift_worker import (
    InterestDriftWorker,
    _KV_MASS_SERIES,
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


@dataclass
class _Activity:
    label: str
    size: int
    representative_id: int = 0


class _FakeGraphWithActivity(_FakeGraph):
    """Adds the L28 rep-id seam on top of the scripted interest_map."""

    def __init__(
        self, frames: list[list[_Entry]], activity: list[_Activity],
    ) -> None:
        super().__init__(frames)
        self._activity = activity
        self.activity_calls = 0

    def cluster_activity(self, *, top_n, min_size=None):
        self.activity_calls += 1
        return list(self._activity)[:top_n]


@dataclass
class _Concept:
    label: str
    confidence: float = 0.5


class _FakeView:
    def __init__(
        self,
        by_rep: dict[int, list[_Concept]] | None = None,
        *,
        enabled: bool = True,
    ) -> None:
        self._by_rep = by_rep or {}
        self.enabled = enabled
        self.calls: list[int] = []

    def for_cluster(self, rep_id) -> list[_Concept]:
        self.calls.append(int(rep_id))
        return list(self._by_rep.get(int(rep_id), []))


class _KV:
    def __init__(self) -> None:
        self.d: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self.d.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.d[key] = value


_NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)


def _cue_store():
    """A real CueStore on a throwaway database."""
    from tempfile import TemporaryDirectory

    from app.core.infra.chat_database import ChatDatabase
    from app.core.proactive.cue_store import CueStore

    tmp = TemporaryDirectory(ignore_cleanup_errors=True)
    store = CueStore(ChatDatabase(Path(tmp.name) / "chat.db"))
    return store, tmp


def _make_worker(graph, kv, **kw) -> InterestDriftWorker:
    params: dict = {
        "interval_seconds": 21600.0,
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
        worker.run()
        worker.run()
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
        worker.run()
        worker.run()
        worker.run()
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


class PoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store, tmp = _cue_store()
        self.addCleanup(tmp.cleanup)

    def _worker(self, graph, kv=None, **kw) -> InterestDriftWorker:
        return _make_worker(
            graph,
            kv or _KV(),
            cue_store_provider=lambda: self.store,
            **kw,
        )

    @staticmethod
    def _rising(topic: str = "rust debugging") -> _FakeGraph:
        return _FakeGraph(
            [[_Entry(topic, 4)], [_Entry(topic, 6)], [_Entry(topic, 9)]]
        )

    def test_run_queues_the_topic_into_the_pool(self) -> None:
        worker = self._worker(self._rising())
        worker.run()
        worker.run()
        result = worker.run()
        self.assertGreater(result["cue_id"], 0)
        rows = self.store.pending("interest_drift")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].subject, "rust debugging")
        self.assertEqual(rows[0].payload["direction"], "rising")

    def test_an_empty_shelf_reports_full_pressure(self) -> None:
        signal = self._worker(_FakeGraph([])).demand(
            now=_NOW, last_run_at=None,
        )
        self.assertEqual(signal.pressure, 1.0)

    def test_a_pooled_topic_is_not_re_drafted(self) -> None:
        kv = _KV()
        graph = _FakeGraph(
            [
                [_Entry("rust", 4)],
                [_Entry("rust", 6)],
                [_Entry("rust", 9)],
                [_Entry("rust", 12)],
            ]
        )
        worker = self._worker(graph, kv, topic_cooldown_hours=0.0)
        worker.run()
        worker.run()
        self.assertEqual(worker.run()["drafted"], 1)
        self.assertTrue(worker.run().get("no_candidate"))

    def test_a_full_shelf_still_samples_the_time_series(self) -> None:
        """Drift's other job is the mass series, and it must not go dark.

        The heartbeat keeps admitting it at ``interval_seconds`` even at
        zero pressure, so a stocked worker still records samples -- which
        is what the classifier needs to stay warm.
        """
        kv = _KV()
        worker = self._worker(self._rising(), kv)
        worker.run()
        self.assertIn(_KV_MASS_SERIES, kv.d)

    def test_demand_is_none_without_a_pool(self) -> None:
        worker = _make_worker(_FakeGraph([]), _KV())
        self.assertIsNone(worker.demand(now=_NOW, last_run_at=None))

    def test_disabled_worker_reports_zero_not_none(self) -> None:
        worker = self._worker(_FakeGraph([]), enabled_provider=lambda: False)
        self.assertEqual(
            worker.demand(now=_NOW, last_run_at=None).pressure, 0.0,
        )


class BeliefAnnotationTests(unittest.TestCase):
    """L28: a drafted drift carries the concept she holds about the topic,
    resolved through ``ConceptView.for_cluster``."""

    @staticmethod
    def _frames() -> list[list[_Entry]]:
        return [
            [_Entry("rust debugging", 4)],
            [_Entry("rust debugging", 6)],
            [_Entry("rust debugging", 9)],
        ]

    def _graph(self) -> _FakeGraphWithActivity:
        return _FakeGraphWithActivity(
            self._frames(),
            [_Activity("rust debugging", 9, representative_id=7)],
        )

    def _warm(self, worker) -> dict:
        worker.run()
        worker.run()
        return worker.run()

    def test_belief_lands_in_the_journal(self) -> None:
        view = _FakeView({7: [
            _Concept("she likes the puzzle of a stubborn bug", 0.8),
            _Concept("a weaker hunch", 0.2),
        ]})
        kv = _KV()
        worker = _make_worker(
            self._graph(), kv, view_provider=lambda: view,
        )
        self.assertEqual(self._warm(worker)["drafted"], 1)
        entry = load_drifts(kv.kv_get)[0]
        self.assertEqual(
            entry["belief"], "she likes the puzzle of a stubborn bug"
        )

    def test_view_only_consulted_when_drafting(self) -> None:
        view = _FakeView({7: [_Concept("something")]})
        graph = self._graph()
        worker = _make_worker(graph, _KV(), view_provider=lambda: view)
        worker.run()
        worker.run()
        # Two sampling ticks, no draft -> the mirror-joining read is unpaid.
        self.assertEqual(graph.activity_calls, 0)
        worker.run()
        self.assertEqual(graph.activity_calls, 1)

    def test_no_view_leaves_the_entry_unannotated(self) -> None:
        kv = _KV()
        worker = _make_worker(self._graph(), kv)
        self._warm(worker)
        self.assertNotIn("belief", load_drifts(kv.kv_get)[0])

    def test_cold_view_leaves_the_entry_unannotated(self) -> None:
        kv = _KV()
        worker = _make_worker(
            self._graph(), kv, view_provider=lambda: _FakeView(enabled=False),
        )
        self._warm(worker)
        self.assertNotIn("belief", load_drifts(kv.kv_get)[0])

    def test_graph_without_activity_still_drafts(self) -> None:
        kv = _KV()
        worker = _make_worker(
            _FakeGraph(self._frames()), kv,
            view_provider=lambda: _FakeView({7: [_Concept("x")]}),
        )
        self.assertEqual(self._warm(worker)["drafted"], 1)
        self.assertNotIn("belief", load_drifts(kv.kv_get)[0])

    def test_unresolved_representative_is_not_queried(self) -> None:
        view = _FakeView({7: [_Concept("x")]})
        graph = _FakeGraphWithActivity(
            self._frames(),
            [_Activity("rust debugging", 9, representative_id=0)],
        )
        kv = _KV()
        worker = _make_worker(graph, kv, view_provider=lambda: view)
        self._warm(worker)
        self.assertEqual(view.calls, [])
        self.assertNotIn("belief", load_drifts(kv.kv_get)[0])

    def test_provider_raising_still_drafts(self) -> None:
        def boom():
            raise RuntimeError("no view")

        kv = _KV()
        worker = _make_worker(self._graph(), kv, view_provider=boom)
        self.assertEqual(self._warm(worker)["drafted"], 1)
        self.assertNotIn("belief", load_drifts(kv.kv_get)[0])


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
        self.debug_overrides.disarm("interest_drift_force_next")


class ProviderTests(unittest.TestCase):
    def _seed(
        self,
        host: _Host,
        *,
        topic="weekend hiking",
        direction="rising",
        belief: str | None = None,
    ) -> None:
        entry = {
            "at": "2026-01-01T00:00:00+00:00",
            "topic": topic,
            "topic_key": topic_key(topic),
            "direction": direction,
            "from_size": 4,
            "to_size": 12,
        }
        if belief is not None:
            entry["belief"] = belief
        append_drift(
            host._chat_db.kv_get,
            host._chat_db.kv_set,
            entry,
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
        host.debug_overrides.arm("interest_drift_force_next")
        self.assertIn("hiking", host._render_interest_drift_block("").lower())

    def test_belief_reaches_the_cue(self) -> None:
        host = _Host()
        self._seed(host, belief="the quiet of a long climb suits you")
        out = host._render_interest_drift_block("planning a hiking trip")
        self.assertIn(
            "What you hold about it: the quiet of a long climb suits you.",
            out,
        )

    def test_cue_without_belief_is_unchanged(self) -> None:
        host = _Host()
        self._seed(host)
        out = host._render_interest_drift_block("planning a hiking trip")
        self.assertNotIn("What you hold about it", out)
        self.assertIn("budding interest of yours", out)


if __name__ == "__main__":
    unittest.main()
