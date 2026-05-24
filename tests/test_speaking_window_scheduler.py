"""Tests for the SpeakingWindowScheduler.

Covers submit/run/cancel semantics, soft/hard close, urgent user-speech
cancellation, priority ordering, dedupe, and idle fallback.
"""
from __future__ import annotations

import threading
import time
import unittest

from app.core.speaking_window_scheduler import (
    ScheduledJob,
    SpeakingWindowScheduler,
    StopFlag,
)


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    """Poll ``predicate`` until True or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class SpeakingWindowSchedulerTests(unittest.TestCase):
    def tearDown(self) -> None:
        sched = getattr(self, "_sched", None)
        if sched is not None:
            sched.stop()

    def _make(self, **kwargs) -> SpeakingWindowScheduler:
        sched = SpeakingWindowScheduler(**kwargs)
        self._sched = sched
        return sched

    # ── basic execution ────────────────────────────────────────────────

    def test_submit_runs_on_window_open(self) -> None:
        sched = self._make(speaking_window_grace_ms=50)
        ran = threading.Event()

        def job(stop: StopFlag) -> None:
            ran.set()

        sched.submit(ScheduledJob(
            name="test", priority=10, estimated_seconds=0.1, callable=job,
        ))
        sched.on_tts_state("start")
        self.assertTrue(_wait_for(ran.is_set, timeout=2.0))

    def test_priority_ordering(self) -> None:
        sched = self._make()
        order: list[str] = []
        lock = threading.Lock()
        all_done = threading.Event()

        def make_job(name: str, expected_idx: int):
            def _run(stop: StopFlag) -> None:
                with lock:
                    order.append(name)
                    if len(order) == 3:
                        all_done.set()
            return _run

        # Submit out of priority order.
        sched.submit(ScheduledJob(
            name="low", priority=50, estimated_seconds=0.1,
            callable=make_job("low", 2),
        ))
        sched.submit(ScheduledJob(
            name="high", priority=10, estimated_seconds=0.1,
            callable=make_job("high", 0),
        ))
        sched.submit(ScheduledJob(
            name="mid", priority=30, estimated_seconds=0.1,
            callable=make_job("mid", 1),
        ))
        sched.on_tts_state("start")
        self.assertTrue(_wait_for(all_done.is_set, timeout=2.0))
        self.assertEqual(order, ["high", "mid", "low"])

    def test_soft_close_stops_after_current_job(self) -> None:
        sched = self._make(speaking_window_grace_ms=100)
        first_started = threading.Event()
        first_finished = threading.Event()
        second_ran = threading.Event()

        def slow(stop: StopFlag) -> None:
            first_started.set()
            # Sleep in slices so we'd see urgent cancel if present.
            for _ in range(10):
                if stop.is_urgent():
                    return
                time.sleep(0.02)
            first_finished.set()

        def quick(stop: StopFlag) -> None:
            second_ran.set()

        sched.submit(ScheduledJob(
            name="slow", priority=10, estimated_seconds=0.5, callable=slow,
        ))
        sched.submit(ScheduledJob(
            name="quick", priority=20, estimated_seconds=0.1, callable=quick,
        ))
        sched.on_tts_state("start")
        self.assertTrue(_wait_for(first_started.is_set, timeout=1.0))
        sched.on_tts_state("end")
        self.assertTrue(_wait_for(first_finished.is_set, timeout=1.0))
        # Soft close: current job allowed to finish, but the next does NOT
        # start in this window.
        time.sleep(0.2)
        self.assertFalse(second_ran.is_set())

    def test_user_speech_urgent_cancels_in_flight(self) -> None:
        sched = self._make()
        started = threading.Event()
        cancelled = threading.Event()

        def long_job(stop: StopFlag) -> None:
            started.set()
            for _ in range(200):
                if stop.is_urgent():
                    cancelled.set()
                    return
                time.sleep(0.01)

        sched.submit(ScheduledJob(
            name="long", priority=10, estimated_seconds=2.0, callable=long_job,
        ))
        sched.on_tts_state("start")
        self.assertTrue(_wait_for(started.is_set, timeout=1.0))
        sched.on_user_speech()
        self.assertTrue(_wait_for(cancelled.is_set, timeout=1.0))

    def test_dedupe_drops_stale_entries(self) -> None:
        sched = self._make()
        seen: list[int] = []
        all_done = threading.Event()

        def job_factory(idx: int):
            def _run(stop: StopFlag) -> None:
                seen.append(idx)
                if seen and seen[-1] == 3:
                    all_done.set()
            return _run

        # Submit three jobs sharing the same dedupe key. Only the latest
        # should execute.
        sched.submit(ScheduledJob(
            name="d", priority=10, estimated_seconds=0.1,
            callable=job_factory(1), dedupe_key="profile",
        ))
        sched.submit(ScheduledJob(
            name="d", priority=10, estimated_seconds=0.1,
            callable=job_factory(2), dedupe_key="profile",
        ))
        sched.submit(ScheduledJob(
            name="d", priority=10, estimated_seconds=0.1,
            callable=job_factory(3), dedupe_key="profile",
        ))
        sched.on_tts_state("start")
        self.assertTrue(_wait_for(all_done.is_set, timeout=1.0))
        # Only the last one ran.
        self.assertEqual(seen, [3])

    def test_idle_drain_fires_when_quiet(self) -> None:
        sched = self._make(idle_seconds=2.0)
        ran = threading.Event()

        def job(stop: StopFlag) -> None:
            ran.set()

        sched.submit(ScheduledJob(
            name="idle", priority=10, estimated_seconds=0.1, callable=job,
        ))
        sched.start_idle_loop()
        # Give the idle loop one cycle (idle_seconds=2.0 -> ~3s wall time).
        self.assertTrue(_wait_for(ran.is_set, timeout=5.0))

    def test_snapshot_reports_pending_count(self) -> None:
        sched = self._make()
        sched.submit(ScheduledJob(
            name="a", priority=10, estimated_seconds=0.1, callable=lambda s: None,
        ))
        sched.submit(ScheduledJob(
            name="b", priority=20, estimated_seconds=0.1, callable=lambda s: None,
        ))
        snap = sched.snapshot()
        self.assertEqual(snap["pending"], 2)
        self.assertEqual(snap["submitted"], 2)
        self.assertFalse(snap["window_open"])


if __name__ == "__main__":
    unittest.main()
