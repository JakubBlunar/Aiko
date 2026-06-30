"""Tests for K68 off-rhythm days (app/core/affect/vitality_rhythm.py).

Pure roll distribution + the kv lazy-resolve shell (fake db). No real
chat database, no controller -- runs in milliseconds.
"""
from __future__ import annotations

import random
import unittest
from datetime import datetime, timedelta

from app.core.affect import vitality_rhythm as vr


class _FakeKvDb:
    """Minimal kv_meta stand-in with the two methods the resolver uses."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.set_calls = 0

    def kv_get(self, key: str):
        return self.store.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.set_calls += 1
        self.store[key] = value


class RollRhythmTests(unittest.TestCase):
    def test_zero_chance_always_normal(self) -> None:
        rng = random.Random(1)
        for _ in range(50):
            self.assertEqual(
                vr.roll_rhythm(exception_chance=0.0, rng=rng).name, "normal",
            )

    def test_full_chance_never_normal(self) -> None:
        rng = random.Random(2)
        for _ in range(50):
            self.assertNotEqual(
                vr.roll_rhythm(exception_chance=1.0, rng=rng).name, "normal",
            )

    def test_returns_known_names(self) -> None:
        rng = random.Random(3)
        valid = {r.name for r in vr.RHYTHMS}
        for _ in range(100):
            self.assertIn(vr.roll_rhythm(exception_chance=0.5, rng=rng).name, valid)

    def test_distribution_mostly_normal_at_default(self) -> None:
        rng = random.Random(4)
        names = [
            vr.roll_rhythm(exception_chance=0.3, rng=rng).name for _ in range(2000)
        ]
        normal_share = names.count("normal") / len(names)
        # ~70% normal at the default chance; loose bounds for the seed.
        self.assertGreater(normal_share, 0.6)
        self.assertLess(normal_share, 0.8)

    def test_nocturnal_is_rarest_exception(self) -> None:
        rng = random.Random(5)
        names = [
            vr.roll_rhythm(exception_chance=1.0, rng=rng).name for _ in range(3000)
        ]
        # nocturnal has the lowest weight -> rarer than night_owl.
        self.assertLess(names.count("nocturnal"), names.count("night_owl"))


class GetByNameTests(unittest.TestCase):
    def test_case_insensitive(self) -> None:
        self.assertEqual(vr.get_rhythm_by_name("NIGHT_OWL").name, "night_owl")

    def test_unknown_is_none(self) -> None:
        self.assertIsNone(vr.get_rhythm_by_name("does_not_exist"))
        self.assertIsNone(vr.get_rhythm_by_name(None))


class ResolveDailyRhythmTests(unittest.TestCase):
    def test_disabled_returns_normal_no_write(self) -> None:
        db = _FakeKvDb()
        now = datetime.now().astimezone()
        out = vr.resolve_daily_rhythm(db, now, enabled=False)
        self.assertEqual(out.name, "normal")
        self.assertEqual(db.set_calls, 0)

    def test_none_db_returns_normal(self) -> None:
        out = vr.resolve_daily_rhythm(None, datetime.now().astimezone())
        self.assertEqual(out.name, "normal")

    def test_fresh_roll_persists(self) -> None:
        db = _FakeKvDb()
        now = datetime.now().astimezone()
        out = vr.resolve_daily_rhythm(
            db, now, exception_chance=1.0, rng=random.Random(7),
        )
        self.assertNotEqual(out.name, "normal")
        self.assertEqual(db.store[vr.KV_RHYTHM], out.name)
        self.assertIn(vr.KV_RHYTHM_SET_AT, db.store)

    def test_same_day_is_stable(self) -> None:
        db = _FakeKvDb()
        now = datetime.now().astimezone()
        first = vr.resolve_daily_rhythm(
            db, now, exception_chance=1.0, rng=random.Random(8),
        )
        writes_after_first = db.set_calls
        # Second call same day: no re-roll, no extra write, same rhythm.
        second = vr.resolve_daily_rhythm(
            db, now, exception_chance=1.0, rng=random.Random(99),
        )
        self.assertEqual(second.name, first.name)
        self.assertEqual(db.set_calls, writes_after_first)

    def test_new_day_rerolls(self) -> None:
        db = _FakeKvDb()
        yesterday = datetime.now().astimezone() - timedelta(days=1)
        vr.resolve_daily_rhythm(
            db, yesterday, exception_chance=1.0, rng=random.Random(10),
        )
        writes = db.set_calls
        today = datetime.now().astimezone()
        vr.resolve_daily_rhythm(
            db, today, exception_chance=1.0, rng=random.Random(11),
        )
        # A fresh day rolls again (and rewrites the set_at).
        self.assertGreater(db.set_calls, writes)
        from app.core.affect import day_color as dc

        self.assertFalse(dc.is_stale(db.store[vr.KV_RHYTHM_SET_AT], today))

    def test_unknown_stored_name_self_heals(self) -> None:
        db = _FakeKvDb()
        now = datetime.now().astimezone()
        db.store[vr.KV_RHYTHM] = "from_an_old_palette"
        db.store[vr.KV_RHYTHM_SET_AT] = now.isoformat()
        out = vr.resolve_daily_rhythm(
            db, now, exception_chance=1.0, rng=random.Random(12),
        )
        self.assertIn(out.name, {r.name for r in vr.RHYTHMS})
        self.assertEqual(db.store[vr.KV_RHYTHM], out.name)


class CurrentBaselineTests(unittest.TestCase):
    def test_returns_baseline_and_rhythm(self) -> None:
        db = _FakeKvDb()
        now = datetime(2026, 6, 30, 12, 0, 0).astimezone()
        baseline, rhythm = vr.current_baseline(db, now, enabled=False)
        self.assertEqual(rhythm.name, "normal")
        self.assertGreaterEqual(baseline, 0.0)
        self.assertLessEqual(baseline, 1.0)

    def test_flipped_day_lowers_midday_baseline(self) -> None:
        db = _FakeKvDb()
        # Pin nocturnal for the day.
        now = datetime(2026, 6, 30, 12, 0, 0).astimezone()
        db.store[vr.KV_RHYTHM] = "nocturnal"
        db.store[vr.KV_RHYTHM_SET_AT] = now.isoformat()
        flipped, rhythm = vr.current_baseline(db, now, enabled=True)
        self.assertEqual(rhythm.name, "nocturnal")
        plain, _ = vr.current_baseline(db, now, enabled=False)
        self.assertLess(flipped, plain)


if __name__ == "__main__":
    unittest.main()
