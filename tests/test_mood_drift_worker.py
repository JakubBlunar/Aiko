"""H3 mood-drift sampler worker + ``record_daily_sample`` tests.

Uses a tiny in-memory kv stub + fake affect/axes stores so we can pin
the once-per-day dedupe and the (valence + four axes) capture without a
real SQLite database.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.affect import mood_drift as md
from app.core.affect.mood_drift_worker import (
    MoodDriftSampleWorker,
    record_daily_sample,
)


class _FakeChatDB:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self.kv_set_calls = 0

    def kv_get(self, key: str) -> str | None:
        return self._store.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.kv_set_calls += 1
        self._store[key] = value


@dataclass
class _Affect:
    valence: float = 0.0


class _FakeAffectStore:
    def __init__(self, valence: float) -> None:
        self._v = valence

    def get(self, _user_id: str) -> _Affect:
        return _Affect(valence=self._v)


@dataclass
class _Axes:
    closeness: float = 0.0
    humor: float = 0.0
    trust: float = 0.0
    comfort: float = 0.0


class _FakeAxesStore:
    def __init__(self, **kw: float) -> None:
        self._axes = _Axes(**kw)

    def get(self, _user_id: str) -> _Axes:
        return self._axes


@dataclass
class _Settings:
    mood_drift_enabled: bool = True
    mood_drift_check_interval_seconds: int = 3600


class RecordDailySampleTests(unittest.TestCase):
    def test_records_first_sample(self) -> None:
        db = _FakeChatDB()
        now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        samples, wrote = record_daily_sample(
            chat_db=db,
            affect_store=_FakeAffectStore(-0.3),
            axes_store=_FakeAxesStore(closeness=0.4),
            user_id="u",
            now=now,
        )
        self.assertTrue(wrote)
        self.assertEqual(len(samples), 1)
        self.assertAlmostEqual(samples[0].valence, -0.3)
        self.assertAlmostEqual(samples[0].closeness, 0.4)
        # persisted
        self.assertEqual(
            len(md.deserialize_samples(db.kv_get(md.KV_SAMPLES))), 1,
        )

    def test_same_day_no_duplicate(self) -> None:
        db = _FakeChatDB()
        now = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
        record_daily_sample(
            chat_db=db, affect_store=_FakeAffectStore(-0.3),
            axes_store=None, user_id="u", now=now,
        )
        samples, wrote = record_daily_sample(
            chat_db=db, affect_store=_FakeAffectStore(0.5),
            axes_store=None, user_id="u",
            now=now + timedelta(hours=3),
        )
        self.assertFalse(wrote)
        self.assertEqual(len(samples), 1)

    def test_next_day_appends(self) -> None:
        db = _FakeChatDB()
        d1 = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
        d2 = datetime(2026, 6, 2, 9, 0, tzinfo=timezone.utc)
        record_daily_sample(
            chat_db=db, affect_store=_FakeAffectStore(-0.3),
            axes_store=None, user_id="u", now=d1,
        )
        samples, wrote = record_daily_sample(
            chat_db=db, affect_store=_FakeAffectStore(0.2),
            axes_store=None, user_id="u", now=d2,
        )
        self.assertTrue(wrote)
        self.assertEqual(len(samples), 2)

    def test_no_axes_store_zeros(self) -> None:
        db = _FakeChatDB()
        samples, _ = record_daily_sample(
            chat_db=db, affect_store=_FakeAffectStore(0.1),
            axes_store=None, user_id="u",
            now=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(samples[0].closeness, 0.0)
        self.assertEqual(samples[0].trust, 0.0)


class WorkerTests(unittest.TestCase):
    def _worker(self, **settings_kw) -> MoodDriftSampleWorker:
        return MoodDriftSampleWorker(
            chat_db=_FakeChatDB(),
            settings=_Settings(**settings_kw),
            affect_store=_FakeAffectStore(-0.4),
            axes_store=_FakeAxesStore(),
            user_id="u",
        )

    def test_disabled_not_ready(self) -> None:
        w = self._worker(mood_drift_enabled=False)
        self.assertFalse(
            w.is_ready(now=datetime.now(timezone.utc), last_run_at=None)
        )

    def test_first_run_ready(self) -> None:
        w = self._worker()
        self.assertTrue(
            w.is_ready(now=datetime.now(timezone.utc), last_run_at=None)
        )

    def test_run_samples_then_noop(self) -> None:
        w = self._worker()
        first = w.run()
        self.assertTrue(first.get("sampled"))
        second = w.run()
        self.assertFalse(second.get("sampled"))
        self.assertEqual(second.get("reason"), "fresh")

    def test_run_disabled_skips(self) -> None:
        w = self._worker(mood_drift_enabled=False)
        self.assertTrue(w.run().get("skipped"))


if __name__ == "__main__":
    unittest.main()
