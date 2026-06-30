"""Worker-level tests for K73 SharedRitualWorker."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from app.core.proactive.shared_ritual_worker import SharedRitualWorker
from app.core.relationship import shared_ritual as sr


_LOCAL_TZ = datetime.now().astimezone().tzinfo


def _weekday_at(target_weekday: int, hour: int):
    """First date on/after 2026-01-01 falling on ``target_weekday``."""
    d = datetime(2026, 1, 1, hour, 0, tzinfo=_LOCAL_TZ)
    while d.weekday() != target_weekday:
        d += timedelta(days=1)
    return d


def _weekly(target_weekday: int, hour: int, weeks: int, text: str):
    base = _weekday_at(target_weekday, hour)
    return [
        ((base + timedelta(weeks=w)).isoformat(), text) for w in range(weeks)
    ]


class FakeDB:
    def __init__(self, rows, kv=None) -> None:
        self._rows = rows
        self.kv = dict(kv or {})

    def execute_fetchall(self, sql, params):  # noqa: ANN001
        return list(self._rows)

    def kv_get(self, key):  # noqa: ANN001
        return self.kv.get(key)

    def kv_set(self, key, value):  # noqa: ANN001
        self.kv[key] = value


def _now() -> datetime:
    return datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


def _worker(db, **kw):
    kw.setdefault("min_messages", 3)
    return SharedRitualWorker(chat_db=db, clock=_now, **kw)


class WorkerTests(unittest.TestCase):
    def test_names_friday_evening_check_ins(self) -> None:
        # Friday (weekday 4) at 20:00 across 3 distinct weeks.
        rows = _weekly(4, 20, 3, "hey how's it going")
        db = FakeDB(rows)
        result = _worker(db).run()
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["candidates"], 1)
        stored = sr.load_rituals(db.kv_get)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["label"], "our Friday-evening check-ins")
        self.assertFalse(stored[0]["acknowledged"])

    def test_support_shape(self) -> None:
        rows = _weekly(4, 20, 3, "i feel so stressed and exhausted")
        db = FakeDB(rows)
        _worker(db).run()
        stored = sr.load_rituals(db.kv_get)
        self.assertEqual(stored[0]["shape"], "support")
        self.assertIn("heart-to-hearts", stored[0]["label"])

    def test_min_messages_floor(self) -> None:
        rows = _weekly(4, 20, 3, "hi")
        db = FakeDB(rows)
        result = SharedRitualWorker(
            chat_db=db, clock=_now, min_messages=30,
        ).run()
        self.assertTrue(result.get("below_min_messages"))
        self.assertEqual(sr.load_rituals(db.kv_get), [])

    def test_force_bypasses_floor(self) -> None:
        rows = _weekly(4, 20, 3, "hi")
        db = FakeDB(rows)
        w = SharedRitualWorker(chat_db=db, clock=_now, min_messages=30)
        w.force_next()
        result = w.run()
        self.assertEqual(result["updated"], 1)

    def test_two_weeks_no_ritual(self) -> None:
        rows = _weekly(4, 20, 2, "hello there")
        db = FakeDB(rows)
        result = SharedRitualWorker(
            chat_db=db, clock=_now, min_messages=1,
        ).run()
        self.assertEqual(result["candidates"], 0)
        self.assertEqual(sr.load_rituals(db.kv_get), [])

    def test_disabled_returns_early(self) -> None:
        rows = _weekly(4, 20, 3, "hi")
        db = FakeDB(rows)
        w = SharedRitualWorker(
            chat_db=db, clock=_now, min_messages=3,
            enabled_provider=lambda: False,
        )
        result = w.run()
        self.assertTrue(result.get("disabled"))

    def test_acknowledged_preserved_across_runs(self) -> None:
        rows = _weekly(4, 20, 3, "hey")
        db = FakeDB(rows)
        w = _worker(db)
        w.run()
        # Simulate the provider acknowledging it.
        stored = sr.mark_acknowledged(
            sr.load_rituals(db.kv_get), "friday:evening:casual_check_in",
        )
        sr.save_rituals(db.kv_set, stored)
        # Second sweep keeps the acknowledged flag.
        w.run()
        again = sr.load_rituals(db.kv_get)
        self.assertTrue(again[0]["acknowledged"])


if __name__ == "__main__":
    unittest.main()
