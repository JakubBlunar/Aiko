"""Tests for :mod:`app.core.relationship.humor_style_worker` (K74).

The worker is the only path that pulls the learned humor weights back
toward uniform. Mirrors ``tests/test_affection_style_worker.py`` -- a
tiny in-memory kv_meta stub so reads and writes can be pinned without a
real SQLite database.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.relationship import humor_style as _hs
from app.core.relationship.humor_style_worker import HumorStyleDecayWorker


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
class _FakeSettings:
    humor_style_enabled: bool = True
    humor_style_decay_interval_seconds: int = 21600
    humor_style_decay_half_life_days: float = 30.0
    humor_style_floor: float = 0.05


def _skewed(days_ago: float) -> str:
    """A state that leans on one register, last touched ``days_ago``."""
    stamp = datetime.now(timezone.utc) - timedelta(days=days_ago)
    state = _hs.apply_observation(
        _hs.uniform_state(stamp), ["absurdist"], 1.0, stamp,
        learning_rate=0.5, floor=0.05,
    )
    return _hs.serialize(state)


def _worker(db: _FakeChatDB, **kw) -> HumorStyleDecayWorker:
    return HumorStyleDecayWorker(chat_db=db, settings=_FakeSettings(**kw))


class IsReadyTests(unittest.TestCase):
    def test_disabled_blocks(self) -> None:
        w = _worker(_FakeChatDB(), humor_style_enabled=False)
        self.assertFalse(
            w.is_ready(now=datetime.now(timezone.utc), last_run_at=None)
        )

    def test_enabled_is_ready_regardless_of_timing(self) -> None:
        # The interval moved into demand() at migration, so is_ready()
        # is the master switch and nothing else.
        w = _worker(_FakeChatDB())
        now = datetime.now(timezone.utc)
        self.assertTrue(w.is_ready(now=now, last_run_at=None))
        self.assertTrue(
            w.is_ready(now=now, last_run_at=now - timedelta(seconds=5))
        )

    def test_interval_seconds_reads_settings(self) -> None:
        w = _worker(_FakeChatDB(), humor_style_decay_interval_seconds=777)
        self.assertEqual(w.interval_seconds, 777.0)


class DemandTests(unittest.TestCase):
    def _probe(self, worker: HumorStyleDecayWorker):
        return worker.demand(
            now=datetime.now(timezone.utc), last_run_at=None,
        )

    def test_a_month_old_lean_wants_decay(self) -> None:
        db = _FakeChatDB({_hs.KV_HUMOR_STYLE: _skewed(30.0)})
        signal = self._probe(_worker(db))
        self.assertEqual(signal.pressure, 1.0)
        self.assertEqual(signal.reason, "decay due")

    def test_a_lean_learned_just_now_reports_no_change(self) -> None:
        db = _FakeChatDB({_hs.KV_HUMOR_STYLE: _skewed(0.0)})
        signal = self._probe(_worker(db))
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "no_change")

    def test_a_brand_new_install_reports_empty(self) -> None:
        signal = self._probe(_worker(_FakeChatDB()))
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "empty")

    def test_the_probe_never_claims_the_llm_lane(self) -> None:
        signal = self._probe(_worker(_FakeChatDB()))
        self.assertFalse(signal.needs_llm)
        self.assertEqual(signal.lane, "compute")

    def test_disabled_and_zero_half_life_report_no_pressure(self) -> None:
        off = _worker(_FakeChatDB(), humor_style_enabled=False)
        self.assertEqual(self._probe(off).reason, "disabled")
        frozen = _worker(
            _FakeChatDB(), humor_style_decay_half_life_days=0.0,
        )
        self.assertEqual(self._probe(frozen).reason, "decay_disabled")

    def test_probing_never_writes_the_decayed_state_back(self) -> None:
        stored = _skewed(30.0)
        db = _FakeChatDB({_hs.KV_HUMOR_STYLE: stored})
        w = _worker(db)
        self._probe(w)
        self._probe(w)
        self.assertEqual(db.kv_set_calls, 0)
        self.assertEqual(db._store[_hs.KV_HUMOR_STYLE], stored)


class RunTests(unittest.TestCase):
    def test_empty_kv_is_a_noop(self) -> None:
        db = _FakeChatDB()
        out = _worker(db).run()
        self.assertFalse(out.get("decayed"))
        self.assertEqual(out.get("reason"), "empty")
        self.assertEqual(db.kv_set_calls, 0)

    def test_disabled_skips(self) -> None:
        out = _worker(_FakeChatDB(), humor_style_enabled=False).run()
        self.assertTrue(out.get("skipped"))
        self.assertEqual(out.get("reason"), "disabled")

    def test_zero_half_life_skips(self) -> None:
        out = _worker(
            _FakeChatDB(), humor_style_decay_half_life_days=0.0,
        ).run()
        self.assertTrue(out.get("skipped"))
        self.assertEqual(out.get("reason"), "decay_disabled")

    def test_decays_a_stale_lean_toward_uniform(self) -> None:
        db = _FakeChatDB({_hs.KV_HUMOR_STYLE: _skewed(30.0)})
        before = _hs.deserialize(db._store[_hs.KV_HUMOR_STYLE])
        out = _worker(db).run()
        self.assertTrue(out.get("decayed"))
        self.assertEqual(db.kv_set_calls, 1)
        after = _hs.deserialize(db._store[_hs.KV_HUMOR_STYLE])
        uniform = 1.0 / len(_hs.HUMOR_KINDS)
        self.assertLess(
            after.weight_of("absurdist") - uniform,
            before.weight_of("absurdist") - uniform,
        )

    def test_no_elapsed_time_means_no_write(self) -> None:
        db = _FakeChatDB({_hs.KV_HUMOR_STYLE: _skewed(0.0)})
        out = _worker(db).run()
        self.assertFalse(out.get("decayed"))
        self.assertEqual(out.get("reason"), "no_change")
        self.assertEqual(db.kv_set_calls, 0)


class WorkerShapeTests(unittest.TestCase):
    def test_name_is_stable(self) -> None:
        self.assertEqual(HumorStyleDecayWorker.name, "humor_style_decay")


if __name__ == "__main__":
    unittest.main()
