"""Tests for H16 :class:`CircadianSettleWorker` — gentle resting default."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.world.circadian_settle_worker import (
    CircadianSettleWorker,
    settle_target,
)


class _FakeLoc:
    def __init__(self, id_: int, slug: str) -> None:
        self.id = id_
        self.slug = slug
        self.name = slug.replace("_", " ")


class _FakeState:
    def __init__(self, location_id: int | None, updated_at: str) -> None:
        self.location_id = location_id
        self.posture = "sitting"
        self.activity = "idle"
        self.updated_at = updated_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "location_id": self.location_id,
            "posture": self.posture,
            "activity": self.activity,
            "updated_at": self.updated_at,
        }


class _FakeStore:
    def __init__(self, *, location_id: int | None, updated_at: str) -> None:
        self._locs = {
            "bed": _FakeLoc(1, "bed"),
            "desk": _FakeLoc(2, "desk"),
            "beanbag": _FakeLoc(3, "beanbag"),
        }
        self._state = _FakeState(location_id, updated_at)
        self.set_state_calls: list[dict[str, Any]] = []

    def get_state(self) -> _FakeState:
        return self._state

    def get_location(self, slug: str):
        return self._locs.get(slug)

    def set_state(self, *, location_id, posture, activity) -> _FakeState:
        self.set_state_calls.append(
            {"location_id": location_id, "posture": posture, "activity": activity}
        )
        self._state = _FakeState(
            location_id, datetime.now(timezone.utc).isoformat()
        )
        return self._state


def _stale_ts() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()


def _make_worker(store, *, period="night", kv=None, hold=0.0, settle_after=7200.0):
    return CircadianSettleWorker(
        store,
        kv_get=(kv.get if kv is not None else None),
        circadian_period_provider=lambda: period,
        settle_after_seconds=settle_after,
        intentional_hold_seconds=hold,
    )


class SettleTargetTests(unittest.TestCase):
    def test_night_is_bed(self) -> None:
        self.assertEqual(settle_target("late_night")[0], "bed")
        self.assertEqual(settle_target("night")[0], "bed")

    def test_morning_is_desk(self) -> None:
        self.assertEqual(settle_target("morning")[0], "desk")

    def test_afternoon_is_beanbag(self) -> None:
        self.assertEqual(settle_target("afternoon")[0], "beanbag")

    def test_unknown_period_none(self) -> None:
        self.assertIsNone(settle_target("zzz"))


class WorkerTests(unittest.TestCase):
    def test_settles_to_bed_at_night_when_stale(self) -> None:
        store = _FakeStore(location_id=2, updated_at=_stale_ts())  # at desk
        worker = _make_worker(store, period="night")
        result = worker.run()
        self.assertEqual(result["fired"], 1)
        self.assertEqual(result["slug"], "bed")
        self.assertEqual(store.set_state_calls[-1]["location_id"], 1)

    def test_skips_when_recently_active(self) -> None:
        fresh = datetime.now(timezone.utc).isoformat()
        store = _FakeStore(location_id=2, updated_at=fresh)
        worker = _make_worker(store, period="night")
        result = worker.run()
        self.assertEqual(result["fired"], 0)
        self.assertTrue(result.get("skipped_recent_activity"))

    def test_skips_when_already_there(self) -> None:
        store = _FakeStore(location_id=1, updated_at=_stale_ts())  # at bed
        worker = _make_worker(store, period="night")
        result = worker.run()
        self.assertEqual(result["fired"], 0)
        self.assertTrue(result.get("already_there"))

    def test_respects_intentional_hold(self) -> None:
        class _KV:
            def __init__(self) -> None:
                self.s: dict[str, str] = {}

            def get(self, k):
                return self.s.get(k)

        kv = _KV()
        kv.s["world.intentional_state_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=30)
        ).isoformat()
        store = _FakeStore(location_id=2, updated_at=_stale_ts())
        worker = _make_worker(store, period="night", kv=kv, hold=7200.0)
        result = worker.run()
        self.assertEqual(result["fired"], 0)
        self.assertTrue(result.get("skipped_intentional_hold"))

    def test_respects_garden_visit_outstanding(self) -> None:
        class _KV:
            def __init__(self) -> None:
                self.s: dict[str, str] = {}

            def get(self, k):
                return self.s.get(k)

        kv = _KV()
        kv.s["garden_visit.return_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat()
        store = _FakeStore(location_id=2, updated_at=_stale_ts())
        worker = _make_worker(store, period="night", kv=kv)
        result = worker.run()
        self.assertEqual(result["fired"], 0)
        self.assertTrue(result.get("skipped_garden_visit"))


if __name__ == "__main__":
    unittest.main()
