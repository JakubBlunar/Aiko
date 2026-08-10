"""Scheduler behaviour for demand-driven admission and lane budgets (P36).

Drives ``_tick()`` directly rather than through the daemon thread so
each case is deterministic. Complements
``tests/test_idle_worker_demand.py`` (pure math) and
``tests/test_idle_worker_scheduler.py`` (pre-existing framework).
"""
from __future__ import annotations

import time
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.proactive.idle_worker import WorkSignal
from app.core.proactive.idle_worker_scheduler import IdleWorkerScheduler
from app.core.proactive.llm_contention import (
    CONTENTION_NONE,
    CONTENTION_SWAPPING,
)


class _DemandWorker:
    """Worker with a controllable demand() probe and simulated cost."""

    def __init__(
        self,
        name: str,
        *,
        interval_seconds: float = 300.0,
        pressure: float = 1.0,
        needs_llm: bool = False,
        sleep_ms: float = 0.0,
        signal: WorkSignal | None | str = "auto",
        probe_sleep_ms: float = 0.0,
        probe_raises: Exception | None = None,
    ) -> None:
        self._name = name
        self._interval = float(interval_seconds)
        self._pressure = pressure
        self._needs_llm = needs_llm
        self._sleep_ms = sleep_ms
        self._signal = signal
        self._probe_sleep_ms = probe_sleep_ms
        self._probe_raises = probe_raises
        self.runs = 0
        self.probes = 0
        self.order: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def is_ready(self, *, now: datetime, last_run_at: datetime | None) -> bool:
        return True

    def demand(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> WorkSignal | None:
        self.probes += 1
        if self._probe_sleep_ms:
            time.sleep(self._probe_sleep_ms / 1000.0)
        if self._probe_raises is not None:
            raise self._probe_raises
        if self._signal != "auto":
            return self._signal  # type: ignore[return-value]
        return WorkSignal(
            pressure=self._pressure,
            reason="test",
            needs_llm=self._needs_llm,
        )

    def run(self) -> dict[str, Any] | None:
        self.runs += 1
        if self._sleep_ms:
            time.sleep(self._sleep_ms / 1000.0)
        return {"runs": self.runs}


class _LegacyWorker:
    """No demand() -- must keep behaving as it does today."""

    def __init__(self, name: str, *, interval_seconds: float = 0.0) -> None:
        self._name = name
        self._interval = float(interval_seconds)
        self.runs = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def is_ready(self, *, now: datetime, last_run_at: datetime | None) -> bool:
        return True

    def run(self) -> dict[str, Any] | None:
        self.runs += 1
        return {"runs": self.runs}


def _sched(**kw: Any) -> IdleWorkerScheduler:
    base: dict[str, Any] = dict(
        wake_seconds=15.0,
        tick_budget_ms=6000,
        compute_budget_ms=6000,
        pressure_enabled=True,
        urgency_threshold=0.35,
        min_interval_ratio=0.1,
        contention_provider=lambda: CONTENTION_NONE,
        idle_depth_provider=lambda: 0.0,
    )
    base.update(kw)
    return IdleWorkerScheduler(**base)


def _age(sched: IdleWorkerScheduler, name: str, seconds: float) -> None:
    """Backdate a worker's last_run_at so it looks overdue."""
    record = sched._records[name]
    record.last_run_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)


class LaneAdmissionTests(unittest.TestCase):
    def test_zero_pressure_worker_is_not_run(self) -> None:
        sched = _sched()
        worker = _DemandWorker("idle_one", pressure=0.0)
        sched.register(worker)
        _age(sched, "idle_one", 60.0)
        sched._tick()
        self.assertEqual(worker.runs, 0)
        self.assertEqual(worker.probes, 1)
        self.assertEqual(
            sched._records["idle_one"].last_admit_reason, "idle",
        )

    def test_pressure_admits_long_before_the_heartbeat(self) -> None:
        sched = _sched()
        worker = _DemandWorker("busy", interval_seconds=300.0, pressure=1.0)
        sched.register(worker)
        _age(sched, "busy", 60.0)  # 60s into a 300s heartbeat
        sched._tick()
        self.assertEqual(worker.runs, 1)
        self.assertEqual(sched._records["busy"].last_admit_reason, "pressure")

    def test_floor_blocks_a_worker_that_just_ran(self) -> None:
        sched = _sched()
        worker = _DemandWorker("busy", interval_seconds=300.0, pressure=1.0)
        sched.register(worker)
        _age(sched, "busy", 5.0)  # floor is max(15, 30) = 30s
        sched._tick()
        self.assertEqual(worker.runs, 0)
        self.assertEqual(sched._records["busy"].last_admit_reason, "floor")

    def test_legacy_worker_keeps_running_without_a_probe(self) -> None:
        sched = _sched()
        worker = _LegacyWorker("old")
        sched.register(worker)
        sched._tick()
        self.assertEqual(worker.runs, 1)
        self.assertEqual(sched._records["old"].last_admit_reason, "legacy")
        # Charged to the LLM lane, since we cannot know it is cheap.
        self.assertEqual(sched._records["old"].last_lane, "llm")

    def test_pressure_disabled_restores_the_legacy_path(self) -> None:
        sched = _sched(pressure_enabled=False)
        worker = _DemandWorker("busy", pressure=0.0)
        sched.register(worker)
        sched._tick()
        # Legacy path never probes and never consults pressure.
        self.assertEqual(worker.probes, 0)
        self.assertEqual(worker.runs, 1)


class LegacyIntervalTests(unittest.TestCase):
    """The escape hatch has to hold the interval for migrated workers.

    A migrated worker's ``is_ready()`` is hard vetoes only -- its timing
    check lives in the probe, which the legacy path never calls. Without
    re-imposing the interval here, turning the escape hatch on would run
    every migrated worker on every wake tick.
    """

    def test_a_migrated_worker_does_not_rerun_inside_its_interval(
        self,
    ) -> None:
        sched = _sched(pressure_enabled=False)
        worker = _DemandWorker("busy", interval_seconds=300.0, pressure=1.0)
        sched.register(worker)
        sched._tick()
        self.assertEqual(worker.runs, 1)
        sched._tick()
        sched._tick()
        self.assertEqual(worker.runs, 1)

    def test_a_migrated_worker_runs_again_once_its_interval_elapses(
        self,
    ) -> None:
        sched = _sched(pressure_enabled=False)
        worker = _DemandWorker("busy", interval_seconds=300.0, pressure=1.0)
        sched.register(worker)
        sched._tick()
        _age(sched, "busy", 301.0)
        sched._tick()
        self.assertEqual(worker.runs, 2)
        # Still no probing on this path.
        self.assertEqual(worker.probes, 0)

    def test_an_unmigrated_worker_keeps_its_own_timing(self) -> None:
        # No demand() means is_ready() still owns the interval, so a
        # worker that says "always ready" keeps running every tick
        # exactly as it did pre-P36.
        sched = _sched(pressure_enabled=False)
        worker = _LegacyWorker("old")
        sched.register(worker)
        sched._tick()
        sched._tick()
        self.assertEqual(worker.runs, 2)

    def test_a_hard_veto_still_wins_over_the_interval(self) -> None:
        sched = _sched(pressure_enabled=False)
        worker = _DemandWorker("busy", interval_seconds=300.0, pressure=1.0)
        worker.is_ready = lambda **_kw: False  # type: ignore[assignment]
        sched.register(worker)
        sched._tick()
        self.assertEqual(worker.runs, 0)


class LaneOrderingTests(unittest.TestCase):
    def test_compute_drains_before_llm(self) -> None:
        sched = _sched()
        order: list[str] = []

        class _Recording(_DemandWorker):
            def run(self) -> dict[str, Any] | None:
                order.append(self.name)
                return super().run()

        # The LLM worker is strictly more urgent, yet compute goes first.
        llm = _Recording("synth", pressure=1.0, needs_llm=True)
        cheap = _Recording("lifecycle", pressure=0.6, needs_llm=False)
        sched.register(llm)
        sched.register(cheap)
        _age(sched, "synth", 290.0)
        _age(sched, "lifecycle", 60.0)
        sched._tick()
        self.assertEqual(order, ["lifecycle", "synth"])

    def test_exhausted_llm_lane_does_not_stall_compute(self) -> None:
        # LLM lane is tiny; compute lane is normal. The cheap worker
        # must still run even though the expensive one cannot.
        sched = _sched(tick_budget_ms=1, compute_budget_ms=6000)
        llm = _DemandWorker("synth", pressure=1.0, needs_llm=True)
        cheap = _DemandWorker("lifecycle", pressure=1.0)
        sched.register(llm)
        sched.register(cheap)
        for name in ("synth", "lifecycle"):
            _age(sched, name, 60.0)
        # Give both a large EMA so neither fits its lane on estimate.
        sched._records["synth"].avg_duration_ms = 5000.0
        sched._records["lifecycle"].avg_duration_ms = 100.0
        sched._tick()
        self.assertEqual(cheap.runs, 1)
        self.assertEqual(llm.runs, 0)


class FitAndStarvationTests(unittest.TestCase):
    def test_slot_one_must_fit_at_just_left(self) -> None:
        # The pre-P36 hole: ran>=1 exempted the first worker entirely,
        # so a 45s worker was admitted on every tick regardless of budget.
        sched = _sched(idle_depth_provider=lambda: 10.0)  # just_left
        worker = _DemandWorker("heavy", pressure=1.0, needs_llm=True)
        sched.register(worker)
        _age(sched, "heavy", 60.0)
        sched._records["heavy"].avg_duration_ms = 45_000.0
        sched._tick()
        self.assertEqual(worker.runs, 0)
        self.assertEqual(sched._records["heavy"].last_admit_reason, "lane_full")

    def test_slot_one_is_exempt_once_the_user_is_away(self) -> None:
        sched = _sched(idle_depth_provider=lambda: 900.0)  # away
        worker = _DemandWorker("heavy", pressure=1.0, needs_llm=True)
        sched.register(worker)
        _age(sched, "heavy", 60.0)
        sched._records["heavy"].avg_duration_ms = 45_000.0
        sched._tick()
        self.assertEqual(worker.runs, 1)

    def test_a_worker_that_never_ran_is_exempt(self) -> None:
        # Its estimate is a guess (_DEFAULT_ESTIMATE_MS). Refusing it on
        # that guess would mean never measuring it, i.e. excluding it
        # forever on a tight budget.
        sched = _sched(
            tick_budget_ms=0, compute_budget_ms=0,
            idle_depth_provider=lambda: 10.0,
        )
        worker = _DemandWorker("fresh", pressure=1.0)
        sched.register(worker)
        sched._tick()
        self.assertEqual(worker.runs, 1)

    def test_escape_valve_fires_past_three_heartbeats(self) -> None:
        # A user who returns every few minutes pins depth at just_left
        # forever; without the valve a long worker would never run.
        sched = _sched(idle_depth_provider=lambda: 10.0)
        worker = _DemandWorker(
            "heavy", interval_seconds=300.0, pressure=1.0, needs_llm=True,
        )
        sched.register(worker)
        _age(sched, "heavy", 1000.0)  # > 3 * 300
        sched._records["heavy"].avg_duration_ms = 45_000.0
        sched._tick()
        self.assertEqual(worker.runs, 1)


class DepthAndContentionTests(unittest.TestCase):
    def test_swapping_pins_the_llm_lane_while_compute_scales(self) -> None:
        sched = _sched(
            contention_provider=lambda: CONTENTION_SWAPPING,
            idle_depth_provider=lambda: 600.0,  # away -> 3x
        )
        status = sched.get_status()
        self.assertEqual(status["idle_depth"], "away")
        self.assertEqual(status["contention"], "swapping")
        # Compute follows depth; the LLM lane stays pinned because a
        # model swap would cost the returning user a reload.
        self.assertEqual(status["effective_compute_budget_ms"], 18000.0)
        self.assertEqual(status["effective_llm_budget_ms"], 6000.0)

    def test_split_backends_let_the_llm_lane_follow_depth(self) -> None:
        sched = _sched(
            contention_provider=lambda: CONTENTION_NONE,
            idle_depth_provider=lambda: 600.0,  # away -> 3x
        )
        status = sched.get_status()
        self.assertEqual(status["effective_llm_budget_ms"], 18000.0)

    def test_depth_multiplier_of_one_disables_scaling(self) -> None:
        sched = _sched(
            depth_max_multiplier=1.0, idle_depth_provider=lambda: 50000.0,
        )
        status = sched.get_status()
        self.assertEqual(status["effective_compute_budget_ms"], 6000.0)


class MidTickQuietTests(unittest.TestCase):
    def test_user_returning_mid_tick_stops_further_admissions(self) -> None:
        quiet = {"value": True}
        sched = _sched(is_quiet_callback=lambda: quiet["value"])

        class _Returner(_DemandWorker):
            def run(self) -> dict[str, Any] | None:
                quiet["value"] = False  # user comes back during this run
                return super().run()

        first = _Returner("first", pressure=1.0)
        second = _DemandWorker("second", pressure=0.9)
        sched.register(first)
        sched.register(second)
        _age(sched, "first", 100.0)
        _age(sched, "second", 60.0)
        sched._tick()
        self.assertEqual(first.runs, 1)
        # The already-running worker finished; nothing new was started.
        self.assertEqual(second.runs, 0)


class ProbeSafetyTests(unittest.TestCase):
    def test_probe_error_falls_back_to_legacy_admission(self) -> None:
        sched = _sched()
        worker = _DemandWorker(
            "flaky", probe_raises=RuntimeError("boom"),
        )
        sched.register(worker)
        sched._tick()
        self.assertEqual(worker.runs, 1)
        self.assertEqual(sched._records["flaky"].last_admit_reason, "legacy")
        self.assertEqual(
            sched._records["flaky"].last_probe_reason, "probe_error",
        )

    def test_probe_returning_none_falls_back_to_legacy(self) -> None:
        sched = _sched()
        worker = _DemandWorker("shrug", signal=None)
        sched.register(worker)
        sched._tick()
        self.assertEqual(worker.runs, 1)
        self.assertEqual(sched._records["shrug"].last_admit_reason, "legacy")

    def test_expensive_probe_is_abandoned(self) -> None:
        sched = _sched()
        worker = _DemandWorker("slow_probe", pressure=0.0, probe_sleep_ms=70.0)
        sched.register(worker)
        _age(sched, "slow_probe", 60.0)
        sched._tick()
        self.assertEqual(worker.probes, 1)
        self.assertGreater(sched._records["slow_probe"].avg_probe_ms or 0.0, 50.0)
        # Second tick: the probe is skipped and the worker reverts to
        # interval behaviour rather than paying the tax every tick.
        _age(sched, "slow_probe", 60.0)
        sched._tick()
        self.assertEqual(worker.probes, 1)
        self.assertEqual(
            sched._records["slow_probe"].last_probe_reason, "probe_too_slow",
        )


class StatusTests(unittest.TestCase):
    def test_status_exposes_demand_fields(self) -> None:
        sched = _sched()
        sched.register(_DemandWorker("busy", pressure=0.8))
        sched.register(_LegacyWorker("old"))
        _age(sched, "busy", 60.0)
        sched._tick()
        rows = {r["name"]: r for r in sched.get_status()["workers"]}
        self.assertTrue(rows["busy"]["demand_aware"])
        self.assertFalse(rows["old"]["demand_aware"])
        self.assertAlmostEqual(rows["busy"]["last_pressure"], 0.8)
        self.assertEqual(rows["busy"]["last_lane"], "compute")
        self.assertEqual(rows["busy"]["min_interval_seconds"], 30.0)


if __name__ == "__main__":
    unittest.main()
