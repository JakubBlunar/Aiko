"""Tests for :mod:`app.core.relationship.affection_style_worker` (J11).

The worker is the only path that pulls the learned weights back toward
uniform. Uses a tiny in-memory kv_meta stub so the reads/writes can be
pinned without a real SQLite database.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.relationship import affection_style as _af
from app.core.relationship.affection_style_worker import (
    AffectionStyleDecayWorker,
)


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
    affection_style_enabled: bool = True
    affection_style_decay_interval_seconds: int = 21600
    affection_style_decay_half_life_days: float = 30.0
    affection_style_floor: float = 0.05


class IsReadyTests(unittest.TestCase):
    def test_disabled_blocks(self) -> None:
        w = AffectionStyleDecayWorker(
            chat_db=_FakeChatDB(),
            settings=_FakeSettings(affection_style_enabled=False),
        )
        self.assertFalse(
            w.is_ready(now=datetime.now(timezone.utc), last_run_at=None)
        )

    def test_first_run_ready(self) -> None:
        w = AffectionStyleDecayWorker(
            chat_db=_FakeChatDB(), settings=_FakeSettings(),
        )
        self.assertTrue(
            w.is_ready(now=datetime.now(timezone.utc), last_run_at=None)
        )

    def test_timing_is_no_longer_a_veto(self) -> None:
        # The interval moved into demand() at migration; is_ready() is
        # the master switch alone.
        w = AffectionStyleDecayWorker(
            chat_db=_FakeChatDB(), settings=_FakeSettings(),
        )
        now = datetime.now(timezone.utc)
        self.assertTrue(
            w.is_ready(now=now, last_run_at=now - timedelta(seconds=5))
        )


class DemandTests(unittest.TestCase):
    def _probe(self, worker: AffectionStyleDecayWorker):
        return worker.demand(
            now=datetime.now(timezone.utc), last_run_at=None,
        )

    def test_a_skewed_state_left_alone_for_a_month_wants_decay(self) -> None:
        old = datetime.now(timezone.utc) - timedelta(days=30)
        skewed = _af.apply_observation(
            _af.uniform_state(old), ["touch"], 1.0, old,
            learning_rate=0.5, floor=0.05,
        )
        db = _FakeChatDB({_af.KV_AFFECTION_STYLE: _af.serialize(skewed)})
        w = AffectionStyleDecayWorker(chat_db=db, settings=_FakeSettings())
        signal = self._probe(w)
        self.assertEqual(signal.pressure, 1.0)
        self.assertEqual(signal.reason, "decay due")
        self.assertFalse(signal.needs_llm)

    def test_a_state_updated_just_now_reports_no_change(self) -> None:
        now = datetime.now(timezone.utc)
        fresh = _af.apply_observation(
            _af.uniform_state(now), ["teasing"], 1.0, now,
            learning_rate=0.5, floor=0.05,
        )
        db = _FakeChatDB({_af.KV_AFFECTION_STYLE: _af.serialize(fresh)})
        w = AffectionStyleDecayWorker(chat_db=db, settings=_FakeSettings())
        signal = self._probe(w)
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "no_change")

    def test_a_brand_new_install_reports_empty(self) -> None:
        w = AffectionStyleDecayWorker(
            chat_db=_FakeChatDB(), settings=_FakeSettings(),
        )
        signal = self._probe(w)
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "empty")

    def test_disabled_and_zero_half_life_both_report_no_pressure(
        self,
    ) -> None:
        off = AffectionStyleDecayWorker(
            chat_db=_FakeChatDB(),
            settings=_FakeSettings(affection_style_enabled=False),
        )
        self.assertEqual(self._probe(off).reason, "disabled")
        frozen = AffectionStyleDecayWorker(
            chat_db=_FakeChatDB(),
            settings=_FakeSettings(affection_style_decay_half_life_days=0.0),
        )
        self.assertEqual(self._probe(frozen).reason, "decay_disabled")

    def test_probing_never_writes_the_decayed_state_back(self) -> None:
        old = datetime.now(timezone.utc) - timedelta(days=30)
        skewed = _af.apply_observation(
            _af.uniform_state(old), ["touch"], 1.0, old,
            learning_rate=0.5, floor=0.05,
        )
        serialized = _af.serialize(skewed)
        db = _FakeChatDB({_af.KV_AFFECTION_STYLE: serialized})
        w = AffectionStyleDecayWorker(chat_db=db, settings=_FakeSettings())
        self._probe(w)
        self._probe(w)
        self.assertEqual(db.kv_set_calls, 0)
        self.assertEqual(db._store[_af.KV_AFFECTION_STYLE], serialized)


class RunTests(unittest.TestCase):
    def test_empty_kv_is_noop(self) -> None:
        db = _FakeChatDB()
        w = AffectionStyleDecayWorker(chat_db=db, settings=_FakeSettings())
        out = w.run()
        self.assertFalse(out.get("decayed"))
        self.assertEqual(out.get("reason"), "empty")
        self.assertEqual(db.kv_set_calls, 0)

    def test_disabled_skips(self) -> None:
        w = AffectionStyleDecayWorker(
            chat_db=_FakeChatDB(),
            settings=_FakeSettings(affection_style_enabled=False),
        )
        self.assertTrue(w.run().get("skipped"))

    def test_zero_half_life_skips(self) -> None:
        w = AffectionStyleDecayWorker(
            chat_db=_FakeChatDB(),
            settings=_FakeSettings(affection_style_decay_half_life_days=0.0),
        )
        self.assertEqual(w.run().get("reason"), "decay_disabled")

    def test_decays_learned_state_toward_uniform(self) -> None:
        # Seed a skewed state set 30 days ago so a 30-day half-life
        # produces a real, detectable move toward uniform.
        old = datetime.now(timezone.utc) - timedelta(days=30)
        skewed = _af.apply_observation(
            _af.uniform_state(old), ["touch"], 1.0, old,
            learning_rate=0.5, floor=0.05,
        )
        db = _FakeChatDB({_af.KV_AFFECTION_STYLE: _af.serialize(skewed)})
        w = AffectionStyleDecayWorker(chat_db=db, settings=_FakeSettings())
        out = w.run()
        self.assertTrue(out.get("decayed"))
        self.assertEqual(db.kv_set_calls, 1)
        after = _af.deserialize(db._store[_af.KV_AFFECTION_STYLE])
        uniform = 1.0 / len(_af.AFFECTION_KINDS)
        # touch moved closer to uniform than it was.
        self.assertLess(
            after.weight_of("touch") - uniform,
            skewed.weight_of("touch") - uniform,
        )

    def test_no_elapsed_no_change(self) -> None:
        now = datetime.now(timezone.utc)
        fresh = _af.apply_observation(
            _af.uniform_state(now), ["teasing"], 1.0, now,
            learning_rate=0.5, floor=0.05,
        )
        db = _FakeChatDB({_af.KV_AFFECTION_STYLE: _af.serialize(fresh)})
        w = AffectionStyleDecayWorker(chat_db=db, settings=_FakeSettings())
        out = w.run()
        self.assertFalse(out.get("decayed"))
        self.assertEqual(db.kv_set_calls, 0)


if __name__ == "__main__":
    unittest.main()
