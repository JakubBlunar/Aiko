"""Worker-level tests for K73 SharedRitualWorker."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.cue_store import CueStore
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


def _worker(db, *, cues=None, **kw):
    kw.setdefault("min_messages", 3)
    return SharedRitualWorker(
        chat_db=db,
        clock=_now,
        cue_store_provider=(lambda: cues) if cues is not None else None,
        user_name_provider=lambda: "Jacob",
        **kw,
    )


def _cue_store() -> tuple[CueStore, TemporaryDirectory]:
    tmp = TemporaryDirectory(ignore_cleanup_errors=True)
    return CueStore(ChatDatabase(Path(tmp.name) / "chat.db")), tmp


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


class PoolProductionTests(unittest.TestCase):
    """The offer lives in the pool; the ritual store only records that
    one was made."""

    def setUp(self) -> None:
        self.cues, tmp = _cue_store()
        self.addCleanup(tmp.cleanup)
        self.db = FakeDB(_weekly(4, 20, 3, "hey how's it going"))

    def test_a_named_ritual_is_offered_through_the_pool(self) -> None:
        result = _worker(self.db, cues=self.cues).run()
        self.assertEqual(result["drafted"], 1)
        rows = self.cues.pending("shared_ritual")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].subject, "our friday-evening check-ins")
        self.assertIn("jacob", rows[0].text.lower())
        self.assertEqual(
            rows[0].payload["key"], "friday:evening:casual_check_in",
        )

    def test_publishing_is_what_flips_acknowledged(self) -> None:
        _worker(self.db, cues=self.cues).run()
        stored = sr.load_rituals(self.db.kv_get)
        self.assertTrue(stored[0]["acknowledged"])

    def test_a_second_sweep_does_not_re_offer(self) -> None:
        worker = _worker(self.db, cues=self.cues)
        worker.run()
        self.assertEqual(worker.run()["drafted"], 0)
        self.assertEqual(len(self.cues.pending("shared_ritual")), 1)

    def test_a_used_offer_is_still_spoken_for(self) -> None:
        """Broader than the acknowledged flag: even after the row leaves
        the pending shelf its subject must not come back."""
        _worker(self.db, cues=self.cues).run()
        row = self.cues.pending("shared_ritual")[0]
        self.cues.mark_used(row.id, evidence="test")
        # Forget the acknowledgment so only the pool can rule it out.
        sr.save_rituals(
            self.db.kv_set,
            [{**r, "acknowledged": False} for r in sr.load_rituals(self.db.kv_get)],
        )
        self.assertEqual(_worker(self.db, cues=self.cues).run()["drafted"], 0)

    def test_pressure_falls_once_an_offer_is_waiting(self) -> None:
        worker = _worker(self.db, cues=self.cues)
        self.assertEqual(worker.demand(now=_now(), last_run_at=None).pressure, 1.0)
        worker.run()
        self.assertEqual(worker.demand(now=_now(), last_run_at=None).pressure, 0.0)

    def test_a_disabled_worker_reports_no_pressure(self) -> None:
        signal = _worker(
            self.db, cues=self.cues, enabled_provider=lambda: False,
        ).demand(now=_now(), last_run_at=None)
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "disabled")

    def test_no_pool_leaves_the_worker_on_plain_intervals(self) -> None:
        self.assertIsNone(
            _worker(self.db).demand(now=_now(), last_run_at=None)
        )


if __name__ == "__main__":
    unittest.main()
