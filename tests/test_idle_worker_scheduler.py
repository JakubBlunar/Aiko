"""Tests for the IdleWorker framework (schema v8 / G1).

Covers:
  * ``force_run`` runs the worker once and updates the record.
  * The scheduler skips workers when ``is_quiet_callback`` returns False.
  * Only one worker fires per tick (cap so heavy workers can't pile).
  * Workers report errors on the record but don't kill the scheduler.
  * Worker last_run_at survives a fresh scheduler if a ``kv_get`` is wired.
"""
from __future__ import annotations

import time
import unittest
from datetime import datetime, timezone
from typing import Any

from app.core.proactive.idle_worker import IdleWorkerRecord, default_is_ready
from app.core.proactive.idle_worker_scheduler import IdleWorkerScheduler


class _Worker:
    """Tiny IdleWorker stand-in. Counts runs + supports custom is_ready."""

    def __init__(
        self,
        name: str,
        *,
        interval_seconds: float = 0.0,
        ready: bool = True,
        raises: Exception | None = None,
    ) -> None:
        self._name = name
        self._interval = float(interval_seconds)
        self._ready = ready
        self._raises = raises
        self.runs = 0
        self.last_now: datetime | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def is_ready(self, *, now: datetime, last_run_at: datetime | None) -> bool:
        return self._ready

    def run(self) -> dict[str, Any] | None:
        if self._raises is not None:
            raise self._raises
        self.runs += 1
        self.last_now = datetime.now(timezone.utc)
        return {"runs": self.runs}


class TestIdleWorkerScheduler(unittest.TestCase):
    def test_force_run_records_result(self) -> None:
        sched = IdleWorkerScheduler()
        w = _Worker("alpha")
        sched.register(w)
        result = sched.force_run("alpha")
        self.assertEqual(result, {"runs": 1})
        recs = sched.get_records()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["name"], "alpha")
        self.assertEqual(recs[0]["run_count"], 1)
        self.assertIsNone(recs[0]["last_error"])

    def test_force_run_unknown_raises(self) -> None:
        sched = IdleWorkerScheduler()
        with self.assertRaises(KeyError):
            sched.force_run("nope")

    def test_force_run_propagates_error_and_records_it(self) -> None:
        sched = IdleWorkerScheduler()
        w = _Worker("bad", raises=RuntimeError("boom"))
        sched.register(w)
        with self.assertRaises(RuntimeError):
            sched.force_run("bad")
        recs = sched.get_records()
        self.assertEqual(recs[0]["run_count"], 0)
        self.assertIn("boom", recs[0]["last_error"] or "")

    def test_scheduler_loop_runs_multiple_due_workers_per_tick(self) -> None:
        # P8: tick-budget drain runs as many due workers as fit. With a
        # generous budget and tiny no-op workers, both register a run
        # on the very first tick rather than rotating one-per-tick.
        sched = IdleWorkerScheduler(
            wake_seconds=0.5, tick_budget_ms=2000,
        )
        a = _Worker("a")
        b = _Worker("b")
        sched.register(a)
        sched.register(b)
        sched.start()
        try:
            time.sleep(1.2)
        finally:
            sched.stop()
        # Both workers run inside the single first tick (anti-starvation
        # forces #1, budget admits #2). Subsequent ticks may add more.
        self.assertGreaterEqual(a.runs, 1)
        self.assertGreaterEqual(b.runs, 1)

    def test_scheduler_caps_per_tick_when_max_per_tick_set(self) -> None:
        # max_per_tick=1 reproduces the legacy one-per-tick behaviour.
        sched = IdleWorkerScheduler(
            wake_seconds=0.5, tick_budget_ms=2000, max_per_tick=1,
        )
        a = _Worker("a")
        b = _Worker("b")
        sched.register(a)
        sched.register(b)
        sched.start()
        try:
            time.sleep(1.6)
        finally:
            sched.stop()
        # Two ticks, one worker each, rotating by oldest last_run.
        total = a.runs + b.runs
        self.assertGreaterEqual(total, 2)
        self.assertLessEqual(abs(a.runs - b.runs), 2)

    def test_is_quiet_callback_skips_tick(self) -> None:
        sched = IdleWorkerScheduler(
            wake_seconds=0.5,
            is_quiet_callback=lambda: False,
        )
        w = _Worker("a")
        sched.register(w)
        sched.start()
        try:
            time.sleep(1.2)
        finally:
            sched.stop()
        self.assertEqual(w.runs, 0)

    def test_kv_persistence_round_trip(self) -> None:
        store: dict[str, str] = {}
        sched_a = IdleWorkerScheduler(
            kv_get=lambda k: store.get(k),
            kv_set=lambda k, v: store.__setitem__(k, v),
        )
        sched_a.register(_Worker("a"))
        sched_a.force_run("a")
        self.assertTrue(any(k.endswith(".last_run_at") for k in store))

        # Fresh scheduler reads the persisted timestamp on register().
        sched_b = IdleWorkerScheduler(
            kv_get=lambda k: store.get(k),
            kv_set=lambda k, v: store.__setitem__(k, v),
        )
        sched_b.register(_Worker("a"))
        rec = sched_b.get_records()[0]
        self.assertIsNotNone(rec["last_run_at"])

    def test_default_is_ready(self) -> None:
        now = datetime.now(timezone.utc)
        self.assertTrue(default_is_ready(60.0, now=now, last_run_at=None))
        self.assertFalse(
            default_is_ready(
                60.0, now=now, last_run_at=now,
            )
        )

    def test_get_records_to_dict_shape(self) -> None:
        rec = IdleWorkerRecord(name="x")
        d = rec.to_dict()
        self.assertEqual(d["name"], "x")
        self.assertIsNone(d["last_run_at"])
        self.assertEqual(d["run_count"], 0)


if __name__ == "__main__":
    unittest.main()
