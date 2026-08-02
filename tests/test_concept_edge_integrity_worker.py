"""Tests for :mod:`app.core.concepts.concept_edge_integrity_worker` (L25)."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app.core.concepts.concept_edge_integrity_worker import (
    ConceptEdgeIntegrityWorker,
)


class _FakeReconciler:
    def __init__(self, *, orphans: int = 0) -> None:
        self.calls: list[int] = []
        self.backlog_calls: list[int] = []
        self._orphans = int(orphans)

    def sweep(self, limit: int) -> dict[str, Any]:
        self.calls.append(int(limit))
        return {"orphans_dropped": 0, "concepts_reconciled": 0}

    def orphan_backlog(self, limit: int) -> int:
        self.backlog_calls.append(int(limit))
        return min(self._orphans, int(limit))


def _worker(
    reconciler: _FakeReconciler,
    *,
    enabled: bool = True,
    concepts_enabled: bool = True,
    interval: float = 3600.0,
    batch: int = 200,
) -> ConceptEdgeIntegrityWorker:
    mem = SimpleNamespace(
        concept_edge_integrity_enabled=enabled,
        concept_edge_integrity_interval_seconds=interval,
        concept_edge_integrity_batch_size=batch,
    )
    agent = SimpleNamespace(concepts_enabled=concepts_enabled)
    return ConceptEdgeIntegrityWorker(
        reconciler=reconciler, memory_settings=mem, agent_settings=agent
    )


class IntegrityWorkerTests(unittest.TestCase):
    def test_run_delegates_to_sweep_with_batch(self) -> None:
        rec = _FakeReconciler()
        w = _worker(rec, batch=42)
        out = w.run()
        self.assertEqual(rec.calls, [42])
        self.assertIn("orphans_dropped", out)

    def test_run_skips_when_disabled(self) -> None:
        rec = _FakeReconciler()
        w = _worker(rec, enabled=False)
        out = w.run()
        self.assertEqual(rec.calls, [])
        self.assertTrue(out.get("skipped"))

    def test_run_skips_when_concepts_disabled(self) -> None:
        rec = _FakeReconciler()
        w = _worker(rec, concepts_enabled=False)
        w.run()
        self.assertEqual(rec.calls, [])

    def test_is_ready_is_the_feature_flag_alone(self) -> None:
        # Timing moved into demand() at migration, so a recent run no
        # longer vetoes readiness -- the probe reports no pressure
        # instead.
        rec = _FakeReconciler()
        w = _worker(rec, interval=3600.0)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertTrue(w.is_ready(now=now, last_run_at=None))
        self.assertTrue(
            w.is_ready(now=now, last_run_at=now - timedelta(seconds=60))
        )

    def test_is_ready_false_when_disabled(self) -> None:
        rec = _FakeReconciler()
        w = _worker(rec, enabled=False)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertFalse(w.is_ready(now=now, last_run_at=None))

    def test_batch_size_floored_at_one(self) -> None:
        rec = _FakeReconciler()
        w = _worker(rec, batch=0)
        w.run()
        self.assertEqual(rec.calls, [1])


class IntegrityWorkerDemandTests(unittest.TestCase):
    _NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def _probe(self, worker: ConceptEdgeIntegrityWorker):
        return worker.demand(now=self._NOW, last_run_at=None)

    def test_a_clean_graph_asks_for_nothing(self) -> None:
        rec = _FakeReconciler(orphans=0)
        signal = self._probe(_worker(rec))
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "0 orphan edges")
        self.assertFalse(signal.needs_llm)

    def test_a_single_orphan_already_clears_the_threshold(self) -> None:
        rec = _FakeReconciler(orphans=1)
        self.assertGreaterEqual(self._probe(_worker(rec)).pressure, 0.5)

    def test_a_full_batch_saturates(self) -> None:
        rec = _FakeReconciler(orphans=500)
        signal = self._probe(_worker(rec, batch=200))
        self.assertEqual(signal.pressure, 1.0)
        self.assertEqual(rec.backlog_calls, [200])

    def test_disabled_reports_no_pressure_without_probing(self) -> None:
        rec = _FakeReconciler(orphans=99)
        signal = self._probe(_worker(rec, enabled=False))
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "disabled")
        self.assertEqual(rec.backlog_calls, [])

    def test_the_probe_never_sweeps(self) -> None:
        rec = _FakeReconciler(orphans=7)
        w = _worker(rec)
        self._probe(w)
        self._probe(w)
        self.assertEqual(rec.calls, [])

    def test_a_broken_probe_defers_to_the_interval(self) -> None:
        class _Exploding(_FakeReconciler):
            def orphan_backlog(self, limit: int) -> int:
                raise RuntimeError("db locked")

        self.assertIsNone(self._probe(_worker(_Exploding())))


if __name__ == "__main__":
    unittest.main()
