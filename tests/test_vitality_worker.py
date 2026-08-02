"""Tests for :mod:`app.core.affect.vitality_worker` (K68).

The worker relaxes body-energy toward the circadian baseline during
quiet windows. These tests pin the scheduler contract — what
``is_ready`` vetoes, what the ``demand()`` probe reports, and above all
that probing never rolls today's rhythm or writes energy back.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.affect import vitality as _vit
from app.core.affect import vitality_rhythm as _vr
from app.core.affect.vitality_worker import VitalityWorker
from app.core.proactive.idle_worker import compute_staleness, compute_urgency

# The scheduler's ``urgency_threshold`` default. Mirrored rather than
# imported because it is a constructor argument, not a module constant.
_THRESHOLD = 0.35


class _FakeChatDB:
    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._store: dict[str, str] = dict(initial or {})
        self.kv_set_calls = 0

    def kv_get(self, key: str) -> str | None:
        return self._store.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.kv_set_calls += 1
        self._store[key] = value


@dataclass
class _FakeAgent:
    vitality_enabled: bool = True
    vitality_rhythm_enabled: bool = False
    vitality_check_interval_seconds: int = 900


@dataclass
class _FakeMemory:
    vitality_recover_half_life_hours: float = 2.0
    vitality_rhythm_exception_chance: float = 0.3


def _worker(
    db: _FakeChatDB, *, notify=None, **agent_kw
) -> VitalityWorker:
    return VitalityWorker(
        chat_db=db,
        agent_settings=_FakeAgent(**agent_kw),
        memory_settings=_FakeMemory(),
        notify=notify,
    )


def _store_energy(db: _FakeChatDB, energy: float, *, hours_ago: float) -> None:
    stamp = datetime.now().astimezone() - timedelta(hours=hours_ago)
    db._store[_vit.KV_VITALITY] = _vit.serialize(
        _vit.VitalityState(energy=energy, last_update_at=stamp.isoformat())
    )


class IsReadyTests(unittest.TestCase):
    def test_disabled_blocks(self) -> None:
        w = _worker(_FakeChatDB(), vitality_enabled=False)
        self.assertFalse(
            w.is_ready(now=datetime.now(timezone.utc), last_run_at=None)
        )

    def test_timing_is_no_longer_a_veto(self) -> None:
        w = _worker(_FakeChatDB())
        now = datetime.now(timezone.utc)
        self.assertTrue(w.is_ready(now=now, last_run_at=None))
        self.assertTrue(
            w.is_ready(now=now, last_run_at=now - timedelta(seconds=10))
        )


class DemandTests(unittest.TestCase):
    def _probe(self, worker: VitalityWorker):
        return worker.demand(
            now=datetime.now(timezone.utc), last_run_at=None,
        )

    def test_energy_far_below_baseline_wants_a_run(self) -> None:
        # Flat on the floor for six hours -- recovery has real work.
        db = _FakeChatDB()
        _store_energy(db, 0.02, hours_ago=6.0)
        signal = self._probe(_worker(db))
        self.assertGreaterEqual(signal.pressure, 0.5)
        self.assertFalse(signal.needs_llm)
        self.assertEqual(signal.lane, "compute")

    def test_a_cold_install_sits_at_baseline(self) -> None:
        # deserialize() seeds a missing key at the baseline itself, so
        # there is nothing to recover toward.
        signal = self._probe(_worker(_FakeChatDB()))
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "at baseline")

    def test_no_elapsed_time_means_no_pressure(self) -> None:
        db = _FakeChatDB()
        _store_energy(db, 0.02, hours_ago=0.0)
        self.assertEqual(self._probe(_worker(db)).pressure, 0.0)

    def _deepest_deficit(self, db: _FakeChatDB, *, seconds_ago: float) -> float:
        """Store the worst possible deficit and return its urgency.

        The circadian baseline moves through the day, so the test pins
        the *deficit* rather than the energy: whichever end of the scale
        is further from where she should be resting right now.
        """
        baseline, _rhythm = _vr.peek_baseline(
            db, datetime.now().astimezone(), enabled=False,
        )
        energy = 0.0 if baseline >= 0.5 else 1.0
        _store_energy(db, energy, hours_ago=seconds_ago / 3600.0)
        signal = self._probe(_worker(db))
        return compute_urgency(
            signal.pressure, compute_staleness(seconds_ago, 900),
        )

    def test_a_deep_deficit_just_after_a_run_stays_off_the_floor(self) -> None:
        """The P44 regression: 91 s cadence against a 900 s heartbeat.

        The old probe floored at 0.5 whenever recovery would move the
        level *at all*, which — recovery being asymptotic — was always.
        Full pressure plus any staleness clears the 0.35 threshold, so
        the worker sat on the anti-thrash floor and ran ten times more
        often than configured to move a few thousandths each time.
        """
        urgency = self._deepest_deficit(_FakeChatDB(), seconds_ago=90.0)
        self.assertLess(urgency, _THRESHOLD)

    def test_a_deep_deficit_outranks_the_heartbeat_once_it_matters(self) -> None:
        # Same deficit, half a heartbeat later: now worth jumping ahead
        # of the workers that are merely due.
        urgency = self._deepest_deficit(_FakeChatDB(), seconds_ago=450.0)
        self.assertGreater(urgency, _THRESHOLD)

    def test_sitting_at_baseline_never_gains_pressure_from_age(self) -> None:
        """What separates this probe from staleness-as-pressure."""
        db = _FakeChatDB()
        baseline, _rhythm = _vr.peek_baseline(
            db, datetime.now().astimezone(), enabled=False,
        )
        for hours in (1.0, 12.0, 96.0):
            _store_energy(db, baseline, hours_ago=hours)
            with self.subTest(hours=hours):
                self.assertEqual(self._probe(_worker(db)).pressure, 0.0)

    def test_disabled_reports_no_pressure(self) -> None:
        db = _FakeChatDB()
        _store_energy(db, 0.02, hours_ago=6.0)
        signal = self._probe(_worker(db, vitality_enabled=False))
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "disabled")

    def test_probing_never_writes_energy_or_notifies(self) -> None:
        seen: list[float] = []
        db = _FakeChatDB()
        _store_energy(db, 0.02, hours_ago=6.0)
        before = db._store[_vit.KV_VITALITY]
        w = _worker(db, notify=seen.append)
        self._probe(w)
        self._probe(w)
        self.assertEqual(db.kv_set_calls, 0)
        self.assertEqual(db._store[_vit.KV_VITALITY], before)
        self.assertEqual(seen, [])

    def test_probing_never_rolls_todays_rhythm(self) -> None:
        # resolve_daily_rhythm() persists its roll on first touch. A
        # probe must not be the thing that decides the rhythm, so the
        # worker reads through peek_baseline instead.
        db = _FakeChatDB()
        _store_energy(db, 0.02, hours_ago=6.0)
        w = _worker(db, vitality_rhythm_enabled=True)
        self._probe(w)
        self.assertNotIn(_vr.KV_RHYTHM, db._store)
        self.assertNotIn(_vr.KV_RHYTHM_SET_AT, db._store)
        self.assertEqual(db.kv_set_calls, 0)

    def test_a_broken_kv_defers_to_the_interval(self) -> None:
        class _Exploding(_FakeChatDB):
            def kv_get(self, key: str) -> str | None:
                raise RuntimeError("db locked")

        self.assertIsNone(self._probe(_worker(_Exploding())))


class RunTests(unittest.TestCase):
    def test_run_recovers_and_notifies(self) -> None:
        seen: list[float] = []
        db = _FakeChatDB()
        _store_energy(db, 0.02, hours_ago=6.0)
        out = _worker(db, notify=seen.append).run()
        self.assertTrue(out["recovered"])
        self.assertEqual(len(seen), 1)
        self.assertGreater(out["energy"], 0.02)

    def test_run_skips_when_disabled(self) -> None:
        db = _FakeChatDB()
        out = _worker(db, vitality_enabled=False).run()
        self.assertTrue(out.get("skipped"))
        self.assertEqual(db.kv_set_calls, 0)


class PeekBaselineTests(unittest.TestCase):
    """The read-only sibling of ``current_baseline``."""

    def test_peek_matches_current_once_the_roll_exists(self) -> None:
        db = _FakeChatDB()
        now = datetime.now().astimezone()
        rolled, rhythm = _vr.current_baseline(db, now, enabled=True)
        peeked, peeked_rhythm = _vr.peek_baseline(db, now, enabled=True)
        self.assertAlmostEqual(rolled, peeked, places=9)
        self.assertEqual(rhythm.name, peeked_rhythm.name)

    def test_peek_assumes_normal_before_the_days_roll(self) -> None:
        db = _FakeChatDB()
        now = datetime.now().astimezone()
        _baseline, rhythm = _vr.peek_baseline(db, now, enabled=True)
        self.assertEqual(rhythm.name, "normal")
        self.assertEqual(db.kv_set_calls, 0)

    def test_peek_disabled_is_the_plain_circadian_curve(self) -> None:
        now = datetime.now().astimezone()
        baseline, rhythm = _vr.peek_baseline(
            _FakeChatDB(), now, enabled=False,
        )
        self.assertEqual(rhythm.name, "normal")
        self.assertAlmostEqual(
            baseline, _vit.circadian_baseline(now), places=9,
        )


class WorkerShapeTests(unittest.TestCase):
    def test_name_is_stable(self) -> None:
        self.assertEqual(VitalityWorker.name, "vitality")


if __name__ == "__main__":
    unittest.main()
