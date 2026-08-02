"""Tests for :mod:`app.core.conversation.topic_graph_rebuild_worker` (K9).

The worker itself is a thin wrapper around :meth:`TopicGraph.rebuild`,
so a stub graph is enough to pin the scheduling contract: what
``is_ready`` still vetoes, and what the ``demand()`` probe reports for a
given unclustered backlog.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.core.conversation.topic_graph_rebuild_worker import (
    TopicGraphRebuildWorker,
)


_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _FakeGraph:
    def __init__(
        self,
        *,
        pending: int = 0,
        persistent: bool = True,
        pending_raises: bool = False,
    ) -> None:
        self.persistent = persistent
        self._pending = int(pending)
        self._pending_raises = pending_raises
        self.rebuilds = 0

    def pending_count(self) -> int:
        if self._pending_raises:
            raise RuntimeError("graph locked")
        return self._pending

    def rebuild(self) -> int:
        self.rebuilds += 1
        self._pending = 0
        return 7


def _worker(graph: _FakeGraph, *, threshold: int = 25) -> TopicGraphRebuildWorker:
    return TopicGraphRebuildWorker(
        graph, interval_seconds=86_400.0, pending_threshold=threshold,
    )


class IsReadyTests(unittest.TestCase):
    def test_a_non_persistent_graph_is_vetoed(self) -> None:
        w = _worker(_FakeGraph(persistent=False, pending=100))
        self.assertFalse(w.is_ready(now=_NOW, last_run_at=None))

    def test_timing_is_no_longer_a_veto(self) -> None:
        # A daily interval used to keep is_ready() false for 24h unless
        # the pending threshold tripped. Both now live in demand().
        w = _worker(_FakeGraph())
        self.assertTrue(w.is_ready(now=_NOW, last_run_at=None))
        self.assertTrue(
            w.is_ready(now=_NOW, last_run_at=_NOW - timedelta(seconds=60))
        )

    def test_interval_is_floored(self) -> None:
        w = TopicGraphRebuildWorker(_FakeGraph(), interval_seconds=1.0)
        self.assertEqual(w.interval_seconds, 60.0)


class DemandTests(unittest.TestCase):
    def _probe(self, worker: TopicGraphRebuildWorker):
        return worker.demand(now=_NOW, last_run_at=None)

    def test_a_settled_graph_asks_for_nothing(self) -> None:
        signal = self._probe(_worker(_FakeGraph(pending=0)))
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "0 unclustered")
        self.assertFalse(signal.needs_llm)

    def test_the_old_pending_threshold_is_now_the_top_of_the_ramp(
        self,
    ) -> None:
        # Crossing the threshold used to short-circuit the interval.
        # It now saturates pressure, which outranks everything else in
        # the lane -- same outcome, general mechanism.
        signal = self._probe(_worker(_FakeGraph(pending=25), threshold=25))
        self.assertEqual(signal.pressure, 1.0)
        signal = self._probe(_worker(_FakeGraph(pending=90), threshold=25))
        self.assertEqual(signal.pressure, 1.0)

    def test_a_partial_backlog_ranks_between(self) -> None:
        signal = self._probe(_worker(_FakeGraph(pending=5), threshold=25))
        self.assertGreaterEqual(signal.pressure, 0.5)
        self.assertLess(signal.pressure, 1.0)
        self.assertEqual(signal.reason, "5 unclustered")

    def test_a_broken_count_defers_to_the_interval(self) -> None:
        w = _worker(_FakeGraph(pending_raises=True))
        self.assertIsNone(self._probe(w))

    def test_probing_never_rebuilds(self) -> None:
        graph = _FakeGraph(pending=100)
        w = _worker(graph)
        self._probe(w)
        self._probe(w)
        self.assertEqual(graph.rebuilds, 0)
        self.assertEqual(graph.pending_count(), 100)


class RunTests(unittest.TestCase):
    def test_run_rebuilds_and_reports_the_prior_backlog(self) -> None:
        graph = _FakeGraph(pending=30)
        out = _worker(graph).run()
        self.assertEqual(graph.rebuilds, 1)
        self.assertEqual(out["clusters"], 7)
        self.assertEqual(out["pending_before"], 30)

    def test_run_skips_a_non_persistent_graph(self) -> None:
        graph = _FakeGraph(persistent=False)
        out = _worker(graph).run()
        self.assertTrue(out.get("skipped"))
        self.assertEqual(graph.rebuilds, 0)


class WorkerShapeTests(unittest.TestCase):
    def test_name_is_stable(self) -> None:
        self.assertEqual(TopicGraphRebuildWorker.name, "topic_graph_rebuild")


if __name__ == "__main__":
    unittest.main()
