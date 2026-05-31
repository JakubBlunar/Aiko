"""Tests for the P8 IdleWorker scheduler features.

Covers:
  * Per-worker duration EMA on :class:`IdleWorkerRecord` (alpha=0.3).
  * Multi-worker drain stays within the tick budget.
  * Anti-starvation: at least one due worker always runs even when
    its estimate exceeds the remaining budget.
  * The most-overdue worker (oldest ``last_run_at``) is picked first.
  * Errors increment ``error_count`` while ``last_error`` is set.
  * ``get_status()`` shape: scheduler-level config + per-worker
    next_due_at / overdue_seconds.
  * ``idle_workers tick:`` summary log line is emitted when at least
    one due worker exists.
"""
from __future__ import annotations

import logging
import time
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.proactive.idle_worker import IdleWorkerRecord
from app.core.proactive.idle_worker_scheduler import IdleWorkerScheduler


class _SizedWorker:
    """Tiny IdleWorker stand-in that sleeps a fixed wall time per run."""

    def __init__(
        self,
        name: str,
        *,
        sleep_ms: float = 5.0,
        interval_seconds: float = 0.0,
        ready: bool = True,
        raises: Exception | None = None,
    ) -> None:
        self._name = name
        self._sleep_ms = float(sleep_ms)
        self._interval = float(interval_seconds)
        self._ready = ready
        self._raises = raises
        self.runs = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def is_ready(self, *, now: datetime, last_run_at: datetime | None) -> bool:
        return self._ready

    def run(self) -> dict[str, Any] | None:
        if self._sleep_ms > 0:
            time.sleep(self._sleep_ms / 1000.0)
        if self._raises is not None:
            raise self._raises
        self.runs += 1
        return {"runs": self.runs}


class IdleWorkerRecordEMATests(unittest.TestCase):
    """Per-record duration accounting (alpha=0.3 EMA)."""

    def test_first_run_seeds_average(self) -> None:
        rec = IdleWorkerRecord(name="x")
        rec.update_after_run(100.0)
        self.assertAlmostEqual(rec.last_duration_ms or 0.0, 100.0)
        self.assertAlmostEqual(rec.avg_duration_ms or 0.0, 100.0)
        self.assertAlmostEqual(rec.total_duration_ms, 100.0)

    def test_ema_converges_with_alpha_0_3(self) -> None:
        rec = IdleWorkerRecord(name="x")
        rec.update_after_run(100.0)
        rec.update_after_run(200.0)
        # 0.3*200 + 0.7*100 = 60 + 70 = 130
        self.assertAlmostEqual(rec.avg_duration_ms or 0.0, 130.0, places=4)
        rec.update_after_run(200.0)
        # 0.3*200 + 0.7*130 = 60 + 91 = 151
        self.assertAlmostEqual(rec.avg_duration_ms or 0.0, 151.0, places=4)

    def test_negative_duration_is_clamped(self) -> None:
        rec = IdleWorkerRecord(name="x")
        rec.update_after_run(-50.0)
        self.assertAlmostEqual(rec.avg_duration_ms or 0.0, 0.0)
        self.assertAlmostEqual(rec.total_duration_ms, 0.0)

    def test_to_dict_round_trip(self) -> None:
        rec = IdleWorkerRecord(name="x")
        rec.update_after_run(123.4567)
        rec.update_after_error()
        d = rec.to_dict()
        self.assertEqual(d["name"], "x")
        self.assertAlmostEqual(float(d["last_duration_ms"]), 123.46, places=2)
        self.assertAlmostEqual(float(d["avg_duration_ms"]), 123.46, places=2)
        self.assertEqual(d["error_count"], 1)


class IdleWorkerSchedulerBudgetTests(unittest.TestCase):
    """Tick-budget arithmetic without spinning the daemon thread."""

    def test_anti_starvation_runs_one_even_with_zero_budget(self) -> None:
        # tick_budget_ms=0 still admits the most-overdue worker, otherwise
        # nothing would ever run on tight machines.
        sched = IdleWorkerScheduler(tick_budget_ms=0)
        a = _SizedWorker("a", sleep_ms=1)
        b = _SizedWorker("b", sleep_ms=1)
        sched.register(a)
        sched.register(b)
        sched._tick()  # type: ignore[attr-defined]
        # Only the lead (anti-starvation) runs; the other is deferred.
        self.assertEqual(a.runs + b.runs, 1)

    def test_budget_admits_multiple_due_workers(self) -> None:
        sched = IdleWorkerScheduler(tick_budget_ms=500)
        workers = [_SizedWorker(f"w{i}", sleep_ms=1) for i in range(5)]
        for w in workers:
            sched.register(w)
        sched._tick()  # type: ignore[attr-defined]
        # 5 workers, 1ms each, 500ms budget -> all five fit.
        self.assertEqual(sum(w.runs for w in workers), 5)

    def test_budget_skips_when_estimate_exceeds_remaining(self) -> None:
        # Pre-seed avg_duration_ms via force_run with a slow worker so
        # the EMA learns ~50ms, then re-run with a small budget.
        sched = IdleWorkerScheduler(tick_budget_ms=30)
        slow = _SizedWorker("slow", sleep_ms=50)
        fast = _SizedWorker("fast", sleep_ms=1)
        sched.register(slow)
        sched.register(fast)
        # Warm up the slow worker's EMA. force_run bypasses readiness
        # but still records duration.
        sched.force_run("slow")
        sched.force_run("fast")
        # Now both have averages: slow ≈ 50ms, fast ≈ 1ms. Reset
        # last_run_at so they are due again on the next tick.
        records = {r["name"]: r for r in sched.get_records()}
        self.assertGreater(records["slow"]["avg_duration_ms"] or 0.0, 10.0)
        self.assertLess(records["fast"]["avg_duration_ms"] or 99.0, 30.0)

        # Force both rows back to 'never run' state so they're due on
        # the next tick, but keep the EMA we just learned.
        for rec in sched._records.values():  # type: ignore[attr-defined]
            rec.last_run_at = None

        slow.runs = 0
        fast.runs = 0
        sched._tick()  # type: ignore[attr-defined]
        # Slow runs first (older last_run_at = None equals the other,
        # registration order tie-breaks). After it consumes ~50ms the
        # 30ms budget is exhausted, so fast is deferred.
        # Anti-starvation lets the FIRST (slow) run; budget then blocks fast.
        self.assertEqual(slow.runs, 1)
        self.assertEqual(fast.runs, 0)

    def test_max_per_tick_caps_runs(self) -> None:
        sched = IdleWorkerScheduler(tick_budget_ms=10_000, max_per_tick=2)
        workers = [_SizedWorker(f"w{i}", sleep_ms=1) for i in range(5)]
        for w in workers:
            sched.register(w)
        sched._tick()  # type: ignore[attr-defined]
        self.assertEqual(sum(w.runs for w in workers), 2)

    def test_oldest_last_run_picks_first(self) -> None:
        sched = IdleWorkerScheduler(tick_budget_ms=10_000, max_per_tick=1)
        a = _SizedWorker("a", sleep_ms=1)
        b = _SizedWorker("b", sleep_ms=1)
        sched.register(a)
        sched.register(b)
        # Mark `b` as having run more recently than `a`.
        now = datetime.now(timezone.utc)
        sched._records["a"].last_run_at = now - timedelta(seconds=120)  # type: ignore[attr-defined]
        sched._records["b"].last_run_at = now - timedelta(seconds=10)  # type: ignore[attr-defined]
        sched._tick()  # type: ignore[attr-defined]
        self.assertEqual(a.runs, 1)
        self.assertEqual(b.runs, 0)

    def test_error_bumps_error_count(self) -> None:
        sched = IdleWorkerScheduler(tick_budget_ms=10_000)
        bad = _SizedWorker("bad", sleep_ms=1, raises=RuntimeError("boom"))
        sched.register(bad)
        with self.assertRaises(RuntimeError):
            sched.force_run("bad")
        rec = sched.get_records()[0]
        self.assertEqual(rec["error_count"], 1)
        self.assertIn("boom", rec["last_error"] or "")
        # last_duration_ms stays None on error (we don't fold a partial
        # run into the EMA).
        self.assertIsNone(rec["last_duration_ms"])

    def test_quiet_callback_returning_false_skips_tick(self) -> None:
        sched = IdleWorkerScheduler(
            tick_budget_ms=10_000,
            is_quiet_callback=lambda: False,
        )
        a = _SizedWorker("a", sleep_ms=1)
        sched.register(a)
        sched._tick()  # type: ignore[attr-defined]
        self.assertEqual(a.runs, 0)


class IdleWorkerSchedulerStatusTests(unittest.TestCase):
    """Shape/content of the new ``get_status`` snapshot used by MCP."""

    def test_status_includes_scheduler_config(self) -> None:
        sched = IdleWorkerScheduler(
            wake_seconds=42.0, tick_budget_ms=1500, max_per_tick=3,
        )
        sched.register(_SizedWorker("a"))
        snap = sched.get_status()
        self.assertEqual(snap["wake_seconds"], 42.0)
        self.assertEqual(snap["tick_budget_ms"], 1500)
        self.assertEqual(snap["max_per_tick"], 3)
        self.assertIn("workers", snap)
        self.assertEqual(len(snap["workers"]), 1)

    def test_never_run_worker_has_none_overdue_and_sorts_first(self) -> None:
        sched = IdleWorkerScheduler(tick_budget_ms=10_000)
        a = _SizedWorker("a", interval_seconds=120)
        b = _SizedWorker("b", interval_seconds=120)
        sched.register(a)
        sched.register(b)
        # b ran 200s ago -> overdue=80s; a never ran.
        sched._records["b"].last_run_at = datetime.now(  # type: ignore[attr-defined]
            timezone.utc
        ) - timedelta(seconds=200)

        snap = sched.get_status()
        names = [w["name"] for w in snap["workers"]]
        self.assertEqual(names[0], "a")  # never-run sorts first
        self.assertIsNone(snap["workers"][0]["overdue_seconds"])
        self.assertGreater(snap["workers"][1]["overdue_seconds"], 70.0)
        self.assertEqual(snap["workers"][1]["name"], "b")

    def test_overdue_sort_descending_for_run_workers(self) -> None:
        sched = IdleWorkerScheduler(tick_budget_ms=10_000)
        a = _SizedWorker("a", interval_seconds=60)
        b = _SizedWorker("b", interval_seconds=60)
        sched.register(a)
        sched.register(b)
        now = datetime.now(timezone.utc)
        sched._records["a"].last_run_at = now - timedelta(seconds=80)  # type: ignore[attr-defined]
        sched._records["b"].last_run_at = now - timedelta(seconds=300)  # type: ignore[attr-defined]
        snap = sched.get_status()
        # b is more overdue than a, so it sorts first.
        self.assertEqual(snap["workers"][0]["name"], "b")
        self.assertEqual(snap["workers"][1]["name"], "a")

    def test_status_carries_avg_duration_after_run(self) -> None:
        sched = IdleWorkerScheduler(tick_budget_ms=10_000)
        sched.register(_SizedWorker("a", sleep_ms=10))
        sched.force_run("a")
        snap = sched.get_status()
        avg = snap["workers"][0]["avg_duration_ms"]
        self.assertIsNotNone(avg)
        self.assertGreater(avg, 0.0)


class IdleWorkerSchedulerLoggingTests(unittest.TestCase):
    """The ``idle_workers tick:`` summary log line."""

    def test_summary_line_emitted_when_due_workers_exist(self) -> None:
        sched = IdleWorkerScheduler(tick_budget_ms=2000)
        sched.register(_SizedWorker("a", sleep_ms=1))
        sched.register(_SizedWorker("b", sleep_ms=1))
        with self.assertLogs("app.idle_worker_scheduler", level="INFO") as cap:
            sched._tick()  # type: ignore[attr-defined]
        joined = "\n".join(cap.output)
        self.assertIn("idle_workers tick:", joined)
        self.assertIn("ran=2", joined)
        self.assertIn("due=2", joined)
        self.assertIn("budget_ms=2000", joined)

    def test_no_summary_when_nothing_due(self) -> None:
        sched = IdleWorkerScheduler(tick_budget_ms=2000)
        # Worker registered but pretend it isn't ready.
        sched.register(_SizedWorker("a", ready=False))
        # Logger may emit nothing at INFO; verify by capturing at DEBUG
        # to make any unexpected info-line failures explicit.
        logger = logging.getLogger("app.idle_worker_scheduler")
        prev_disabled = logger.disabled
        logger.disabled = False
        try:
            with self.assertLogs("app.idle_worker_scheduler", level="DEBUG") as cap:
                logger.debug("sentinel-for-cap")  # ensure cap is non-empty
                sched._tick()  # type: ignore[attr-defined]
        finally:
            logger.disabled = prev_disabled
        joined = "\n".join(cap.output)
        self.assertNotIn("idle_workers tick:", joined)


if __name__ == "__main__":
    unittest.main()
