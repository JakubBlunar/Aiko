"""Tests for :class:`app.core.world.world_notice_worker.WorldNoticeWorker`.

Exercises the two triggers (fresh gift, stale room) and the pacing
gates (cooldown, daily cap, enabled switch) with lightweight fakes —
no real WorldStore, LLM, or DB. The worker composes its line via the
deterministic fallback (``ollama=None``) so the assertions don't depend
on a model.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.world.world_notice_worker import (
    WORLD_LAST_USER_GIFT_KEY,
    WorldNoticeWorker,
)


class _FakeKV:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value


class _FakeNudgeStore:
    def __init__(self) -> None:
        self.upserts: list[dict[str, Any]] = []

    def upsert(self, user_id: str, **kw: Any) -> dict[str, Any]:
        row = {"user_id": user_id, **kw}
        self.upserts.append(row)
        return row


class _FakeState:
    posture = "curled_up"
    activity = "reading"
    location_id = 1


class _FakeLoc:
    def __init__(self, id_: int, name: str) -> None:
        self.id = id_
        self.name = name


class _FakeWorldStore:
    def get_state(self) -> _FakeState:
        return _FakeState()

    def list_locations(self) -> list[_FakeLoc]:
        return [_FakeLoc(1, "the window seat")]


def _make_worker(
    *,
    kv: _FakeKV,
    nudges: _FakeNudgeStore,
    enabled: bool = True,
    cooldown: float = 3600.0,
    daily_cap: int = 4,
) -> WorldNoticeWorker:
    return WorldNoticeWorker(
        world_store=_FakeWorldStore(),
        prepared_nudge_store=nudges,
        kv_get=kv.get,
        kv_set=kv.set,
        user_id_provider=lambda: "jacob",
        user_display_name_provider=lambda: "Jacob",
        enabled_provider=lambda: enabled,
        ollama=None,  # force deterministic fallback
        model=None,
        interval_seconds=300.0,
        cooldown_seconds=cooldown,
        daily_cap=daily_cap,
        ttl_seconds=1800.0,
    )


def _stamp_gift(kv: _FakeKV, name: str = "cookies", at: str | None = None) -> None:
    kv.set(
        WORLD_LAST_USER_GIFT_KEY,
        json.dumps({
            "id": 7,
            "name": name,
            "at": at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }),
    )


class GiftTriggerTests(unittest.TestCase):
    def test_fresh_gift_primes_world_nudge(self) -> None:
        kv, nudges = _FakeKV(), _FakeNudgeStore()
        _stamp_gift(kv, name="green tea")
        worker = _make_worker(kv=kv, nudges=nudges)
        result = worker.run()
        self.assertEqual(result["fired"], 1)
        self.assertEqual(result["kind"], "gift")
        self.assertEqual(len(nudges.upserts), 1)
        up = nudges.upserts[0]
        self.assertEqual(up["source_kind"], "world")
        self.assertIn("green tea", up["text"])

    def test_handled_gift_does_not_refire(self) -> None:
        kv, nudges = _FakeKV(), _FakeNudgeStore()
        _stamp_gift(kv)
        worker = _make_worker(kv=kv, nudges=nudges)
        self.assertEqual(worker.run()["fired"], 1)
        # Second run sees the same watermark already handled.
        second = worker.run()
        self.assertEqual(second["fired"], 0)
        self.assertEqual(len(nudges.upserts), 1)

    def test_gift_bypasses_daily_cap(self) -> None:
        kv, nudges = _FakeKV(), _FakeNudgeStore()
        # Exhaust the daily cap counter for today.
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        kv.set("world_notice.day", today)
        kv.set("world_notice.day_count", "99")
        _stamp_gift(kv)
        worker = _make_worker(kv=kv, nudges=nudges, daily_cap=4)
        self.assertEqual(worker.run()["fired"], 1)


class StaleRoomTriggerTests(unittest.TestCase):
    def test_stale_room_fires_when_cooldown_elapsed(self) -> None:
        kv, nudges = _FakeKV(), _FakeNudgeStore()
        worker = _make_worker(kv=kv, nudges=nudges, cooldown=0.0)
        result = worker.run()
        self.assertEqual(result["fired"], 1)
        self.assertEqual(result["kind"], "room")
        self.assertEqual(nudges.upserts[0]["source_kind"], "world")

    def test_stale_room_blocked_by_cooldown(self) -> None:
        kv, nudges = _FakeKV(), _FakeNudgeStore()
        recent = datetime.now(timezone.utc) - timedelta(seconds=60)
        kv.set("world_notice.last_fired_at", recent.isoformat())
        worker = _make_worker(kv=kv, nudges=nudges, cooldown=3600.0)
        result = worker.run()
        self.assertEqual(result["fired"], 0)
        self.assertTrue(result.get("skipped_cooldown"))

    def test_stale_room_blocked_by_daily_cap(self) -> None:
        kv, nudges = _FakeKV(), _FakeNudgeStore()
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        kv.set("world_notice.day", today)
        kv.set("world_notice.day_count", "4")
        worker = _make_worker(kv=kv, nudges=nudges, cooldown=0.0, daily_cap=4)
        result = worker.run()
        self.assertEqual(result["fired"], 0)
        self.assertTrue(result.get("skipped_daily_cap"))


class GateTests(unittest.TestCase):
    def test_disabled_short_circuits(self) -> None:
        kv, nudges = _FakeKV(), _FakeNudgeStore()
        _stamp_gift(kv)
        worker = _make_worker(kv=kv, nudges=nudges, enabled=False)
        result = worker.run()
        self.assertTrue(result.get("disabled"))
        self.assertEqual(len(nudges.upserts), 0)

    def test_no_user_skips(self) -> None:
        kv, nudges = _FakeKV(), _FakeNudgeStore()
        _stamp_gift(kv)
        worker = WorldNoticeWorker(
            world_store=_FakeWorldStore(),
            prepared_nudge_store=nudges,
            kv_get=kv.get,
            kv_set=kv.set,
            user_id_provider=lambda: "",
            user_display_name_provider=lambda: "Jacob",
            ollama=None,
            model=None,
        )
        result = worker.run()
        self.assertTrue(result.get("skipped_no_user"))


if __name__ == "__main__":
    unittest.main()
