"""Tests for :class:`app.core.world.idle_activity_worker.IdleAwayActivityWorker`.

Exercises activity selection (tied to a fake world inventory), the world
mutation it triggers (``set_state`` / ``consume_item`` / ``update_item``),
the kv journal ring, and the pacing gates (cooldown, daily cap, enabled
switch, garden-visit guard). All fakes — no real WorldStore, LLM, or DB.
The worker composes its line via the deterministic fallback
(``ollama=None``) so assertions don't depend on a model.
"""
from __future__ import annotations

import json
import random
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.world.idle_activity_worker import (
    AWAY_ACTIVITIES_JOURNAL_KEY,
    IdleAwayActivityWorker,
    load_journal,
)


class _FakeKV:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value


class _FakeItem:
    def __init__(
        self,
        id_: int,
        name: str,
        *,
        kind: str = "object",
        consumable: bool = False,
        quantity: int = 1,
        location_id: int | None = None,
    ) -> None:
        self.id = id_
        self.name = name
        self.kind = kind
        self.consumable = consumable
        self.quantity = quantity
        self.location_id = location_id

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "quantity": self.quantity}


class _FakeLoc:
    def __init__(self, id_: int, name: str, slug: str = "") -> None:
        self.id = id_
        self.name = name
        self.slug = slug or name.lower().replace(" ", "_")


class _FakeRoomState:
    def __init__(
        self, posture: str, activity: str, location_id: int | None = None,
    ) -> None:
        self.posture = posture
        self.activity = activity
        self.location_id = location_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "posture": self.posture,
            "activity": self.activity,
            "location_id": self.location_id,
        }


class _FakeWorldStore:
    def __init__(
        self,
        *,
        items: list[_FakeItem] | None = None,
        locations: list[_FakeLoc] | None = None,
    ) -> None:
        self._items = items if items is not None else []
        self._locations = (
            locations
            if locations is not None
            else [_FakeLoc(1, "the desk", "desk")]
        )
        self.set_state_calls: list[dict[str, Any]] = []
        self.consumed: list[int] = []
        self.moved: list[tuple[int, int]] = []

    def list_items(self) -> list[_FakeItem]:
        return list(self._items)

    def list_locations(self) -> list[_FakeLoc]:
        return list(self._locations)

    def set_state(
        self,
        *,
        posture: str,
        activity: str,
        location_id: int | None = None,
    ) -> _FakeRoomState:
        self.set_state_calls.append(
            {
                "posture": posture,
                "activity": activity,
                "location_id": location_id,
            }
        )
        return _FakeRoomState(posture, activity, location_id)

    def consume_item(self, item_id: int, *, amount: int = 1):
        self.consumed.append(item_id)
        item = next((i for i in self._items if i.id == item_id), None)
        if item is None:
            return None, 0
        item.quantity -= amount
        if item.quantity <= 0:
            self._items = [i for i in self._items if i.id != item_id]
            return None, amount
        return item, amount

    def update_item(self, item_id: int, *, location_id: int):
        self.moved.append((item_id, location_id))
        item = next((i for i in self._items if i.id == item_id), None)
        if item is not None:
            item.location_id = location_id
        return item


def _make_worker(
    *,
    world: _FakeWorldStore,
    kv: _FakeKV,
    enabled: bool = True,
    cooldown: float = 5400.0,
    daily_cap: int = 6,
    seed: int = 0,
    notify: Any = None,
    intentional_hold_seconds: float = 0.0,
    outings_enabled: bool = True,
    outing_cooldown_seconds: float = 6.0 * 3600,
    outing_daily_cap: int = 2,
    period: str | None = None,
) -> IdleAwayActivityWorker:
    return IdleAwayActivityWorker(
        world_store=world,
        kv_get=kv.get,
        kv_set=kv.set,
        user_display_name_provider=lambda: "Jacob",
        enabled_provider=lambda: enabled,
        notify=notify,
        ollama=None,  # deterministic fallback
        model=None,
        interval_seconds=1200.0,
        cooldown_seconds=cooldown,
        daily_cap=daily_cap,
        journal_max=8,
        intentional_hold_seconds=intentional_hold_seconds,
        outings_enabled_provider=lambda: outings_enabled,
        outing_cooldown_seconds=outing_cooldown_seconds,
        outing_daily_cap=outing_daily_cap,
        circadian_period_provider=(
            (lambda: period) if period is not None else None
        ),
        rng=random.Random(seed),
    )


class ActivitySelectionTests(unittest.TestCase):
    def test_forced_snack_consumes_food_and_journals(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore(
            items=[
                _FakeItem(
                    7, "cookies", kind="food", consumable=True, quantity=2
                )
            ]
        )
        worker = _make_worker(world=world, kv=kv, cooldown=0.0)
        worker.force_activity("snack")
        result = worker.run()
        self.assertEqual(result["fired"], 1)
        self.assertEqual(result["key"], "snack")
        self.assertIn(7, world.consumed)
        journal = load_journal(kv.get)
        self.assertEqual(len(journal), 1)
        self.assertIn("cookies", journal[0]["summary"])

    def test_forced_move_cat_moves_item(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore(
            items=[_FakeItem(3, "the cat", kind="pet", location_id=1)],
            locations=[_FakeLoc(1, "the desk"), _FakeLoc(2, "the bed")],
        )
        worker = _make_worker(world=world, kv=kv, cooldown=0.0)
        worker.force_activity("move_cat")
        result = worker.run()
        self.assertEqual(result["key"], "move_cat")
        self.assertEqual(len(world.moved), 1)
        self.assertEqual(world.moved[0][0], 3)

    def test_beat_moves_aiko_to_matching_location(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore(
            locations=[
                _FakeLoc(1, "the desk", "desk"),
                _FakeLoc(2, "the window seat", "window_seat"),
            ],
        )
        worker = _make_worker(world=world, kv=kv, cooldown=0.0)
        worker.force_activity("look_outside")
        worker.run()
        # set_state was called with the window-seat location id.
        self.assertTrue(world.set_state_calls)
        last = world.set_state_calls[-1]
        self.assertEqual(last["location_id"], 2)

    def test_wander_always_available_with_empty_room(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore(items=[], locations=[])
        worker = _make_worker(world=world, kv=kv, cooldown=0.0)
        result = worker.run()
        self.assertEqual(result["fired"], 1)
        # set_state always called -> world mutated.
        self.assertTrue(world.set_state_calls)

    def test_world_mutation_broadcasts(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore()
        patches: list[dict[str, Any]] = []
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, notify=patches.append
        )
        worker.force_activity("doodle")
        worker.run()
        self.assertTrue(any("state" in p for p in patches))


class JournalTests(unittest.TestCase):
    def test_journal_ring_trims_to_max(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore()
        worker = _make_worker(world=world, kv=kv, cooldown=0.0, daily_cap=999)
        for _ in range(12):
            worker.force_activity("doodle")
            worker.run()
        journal = load_journal(kv.get)
        self.assertEqual(len(journal), 8)  # journal_max

    def test_load_journal_handles_garbage(self) -> None:
        kv = _FakeKV()
        kv.set(AWAY_ACTIVITIES_JOURNAL_KEY, "not json")
        self.assertEqual(load_journal(kv.get), [])


class GateTests(unittest.TestCase):
    def test_disabled_short_circuits(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore()
        worker = _make_worker(world=world, kv=kv, enabled=False)
        result = worker.run()
        self.assertTrue(result.get("disabled"))
        self.assertFalse(world.set_state_calls)

    def test_cooldown_blocks(self) -> None:
        kv = _FakeKV()
        recent = datetime.now(timezone.utc) - timedelta(seconds=60)
        kv.set("away_activity.last_fired_at", recent.isoformat())
        world = _FakeWorldStore()
        worker = _make_worker(world=world, kv=kv, cooldown=5400.0)
        result = worker.run()
        self.assertEqual(result["fired"], 0)
        self.assertTrue(result.get("skipped_cooldown"))

    def test_daily_cap_blocks(self) -> None:
        kv = _FakeKV()
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        kv.set("away_activity.day", today)
        kv.set("away_activity.day_count", "6")
        world = _FakeWorldStore()
        worker = _make_worker(world=world, kv=kv, cooldown=0.0, daily_cap=6)
        result = worker.run()
        self.assertEqual(result["fired"], 0)
        self.assertTrue(result.get("skipped_daily_cap"))

    def test_garden_visit_outstanding_defers(self) -> None:
        kv = _FakeKV()
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        kv.set("garden_visit.return_at", future.isoformat())
        world = _FakeWorldStore()
        worker = _make_worker(world=world, kv=kv, cooldown=0.0)
        result = worker.run()
        self.assertEqual(result["fired"], 0)
        self.assertTrue(result.get("skipped_garden_visit"))

    def test_intentional_hold_defers(self) -> None:
        kv = _FakeKV()
        # Brain/user placed Aiko 1 min ago; hold window is 2h.
        recent = datetime.now(timezone.utc) - timedelta(seconds=60)
        kv.set("world.intentional_state_at", recent.isoformat())
        world = _FakeWorldStore()
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, intentional_hold_seconds=7200.0
        )
        result = worker.run()
        self.assertEqual(result["fired"], 0)
        self.assertTrue(result.get("skipped_intentional_hold"))
        self.assertFalse(world.set_state_calls)

    def test_intentional_hold_expired_allows_beat(self) -> None:
        kv = _FakeKV()
        # Placed 3h ago; outside the 2h hold window -> worker free again.
        old = datetime.now(timezone.utc) - timedelta(hours=3)
        kv.set("world.intentional_state_at", old.isoformat())
        world = _FakeWorldStore()
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, intentional_hold_seconds=7200.0
        )
        result = worker.run()
        self.assertEqual(result["fired"], 1)
        self.assertTrue(world.set_state_calls)

    def test_intentional_hold_disabled_ignores_stamp(self) -> None:
        kv = _FakeKV()
        recent = datetime.now(timezone.utc) - timedelta(seconds=10)
        kv.set("world.intentional_state_at", recent.isoformat())
        world = _FakeWorldStore()
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, intentional_hold_seconds=0.0
        )
        result = worker.run()
        self.assertEqual(result["fired"], 1)


class OutingTests(unittest.TestCase):
    """H22 — the rare 'I stepped out for a bit' away-beat."""

    def test_forced_outing_journals_and_stamps(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore()
        worker = _make_worker(world=world, kv=kv, cooldown=0.0, period="afternoon")
        worker.force_activity("outing")
        result = worker.run()
        self.assertEqual(result["fired"], 1)
        self.assertEqual(result["key"], "outing")
        journal = load_journal(kv.get)
        self.assertEqual(journal[-1]["key"], "outing")
        # Outing watermarks were stamped so the next one is gated.
        self.assertIsNotNone(kv.get("outing.last_fired_at"))
        self.assertEqual(kv.get("outing.day_count"), "1")

    def test_outing_not_offered_when_disabled(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore()
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, outings_enabled=False,
            period="afternoon",
        )
        # Even forced, a disabled outing is not added to candidates, so the
        # pick falls back to another beat (never key == "outing").
        worker.force_activity("outing")
        result = worker.run()
        self.assertNotEqual(result.get("key"), "outing")

    def test_outing_cooldown_blocks_repeat(self) -> None:
        kv = _FakeKV()
        recent = datetime.now(timezone.utc) - timedelta(minutes=30)
        kv.set("outing.last_fired_at", recent.isoformat())
        world = _FakeWorldStore()
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0,
            outing_cooldown_seconds=6.0 * 3600, period="afternoon",
        )
        now = datetime.now(timezone.utc)
        self.assertFalse(worker._outing_eligible(now))

    def test_outing_daily_cap_blocks(self) -> None:
        kv = _FakeKV()
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        kv.set("outing.day", today)
        kv.set("outing.day_count", "2")
        world = _FakeWorldStore()
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, outing_daily_cap=2,
            outing_cooldown_seconds=0.0, period="afternoon",
        )
        self.assertFalse(worker._outing_eligible(datetime.now(timezone.utc)))

    def test_outing_blocked_at_night(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore()
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, outing_cooldown_seconds=0.0,
            period="late_night",
        )
        self.assertFalse(worker._outing_eligible(datetime.now(timezone.utc)))

    def test_outing_eligible_in_daylight(self) -> None:
        kv = _FakeKV()
        world = _FakeWorldStore()
        worker = _make_worker(
            world=world, kv=kv, cooldown=0.0, outing_cooldown_seconds=0.0,
            period="morning",
        )
        self.assertTrue(worker._outing_eligible(datetime.now(timezone.utc)))


if __name__ == "__main__":
    unittest.main()
