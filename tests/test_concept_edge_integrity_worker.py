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
    def __init__(self) -> None:
        self.calls: list[int] = []

    def sweep(self, limit: int) -> dict[str, Any]:
        self.calls.append(int(limit))
        return {"orphans_dropped": 0, "concepts_reconciled": 0}


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

    def test_is_ready_gating(self) -> None:
        rec = _FakeReconciler()
        w = _worker(rec, interval=3600.0)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # never ran -> ready
        self.assertTrue(w.is_ready(now=now, last_run_at=None))
        # ran recently -> not ready
        self.assertFalse(
            w.is_ready(now=now, last_run_at=now - timedelta(seconds=60))
        )
        # interval elapsed -> ready
        self.assertTrue(
            w.is_ready(now=now, last_run_at=now - timedelta(seconds=3601))
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


if __name__ == "__main__":
    unittest.main()
