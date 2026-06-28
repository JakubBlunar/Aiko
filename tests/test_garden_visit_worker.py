"""Tests for :class:`GardenVisitWorker` — outbound + inbound phases."""
from __future__ import annotations

import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.infra.chat_database import ChatDatabase
from app.core.world.garden_visit_worker import GardenVisitWorker
from app.core.world.world_store import WorldStore


class _TempWorld:
    def __enter__(self) -> WorldStore:
        self._dir = tempfile.TemporaryDirectory()
        path = Path(self._dir.name) / "world.db"
        ChatDatabase(path)
        store = WorldStore(path)
        store.seed_default()
        self.store = store
        return store

    def __exit__(self, *exc) -> None:
        try:
            self.store.close()
            self._dir.cleanup()
        except PermissionError:
            pass


def _make_worker(
    store: WorldStore,
    *,
    period: str = "morning",
    notify=None,
    intentional_hold_seconds: float = 0.0,
    enabled: bool = True,
    relax_ratio: float = 0.0,
    need_dry_days: float = 2.0,
    need_visit_floor_seconds: float = 0.75 * 3600,
    seed: int = 0,
):
    return GardenVisitWorker(
        store,
        notify=notify,
        rng=random.Random(seed),
        circadian_period_provider=lambda: period,
        intentional_hold_seconds=intentional_hold_seconds,
        enabled_provider=lambda: enabled,
        relax_ratio=relax_ratio,
        need_dry_days=need_dry_days,
        need_visit_floor_seconds=need_visit_floor_seconds,
    )


class GardenVisitWorkerOutboundTests(unittest.TestCase):
    def test_outbound_moves_to_garden_and_waters(self) -> None:
        with _TempWorld() as store:
            patches: list[dict] = []
            worker = _make_worker(store, notify=patches.append)
            now = datetime.now(timezone.utc)
            # Worker should be ready: never ran + daylight + not in garden.
            self.assertTrue(worker.is_ready(now=now, last_run_at=None))
            result = worker.run()
            self.assertEqual(result["phase"], "outbound")
            self.assertTrue(result["watered"] or result["harvested"])
            # State now points at the garden.
            garden = store.get_location("garden")
            self.assertEqual(store.get_state().location_id, garden.id)
            # Patches included a state change.
            self.assertTrue(any("state" in p for p in patches))

    def test_outbound_auto_harvests_mature_plants(self) -> None:
        with _TempWorld() as store:
            # Force one plant mature so the visit triggers a harvest.
            plant = next(
                i for i in store.list_items(kind="plant")
                if (i.state or {}).get("species") == "tomato"
            )
            store.update_item(
                plant.id,
                state={**(plant.state or {}), "stage": "mature"},
            )
            patches: list[dict] = []
            worker = _make_worker(store, notify=patches.append)
            result = worker.run()
            self.assertTrue(result["harvested"])
            # The annual tomato plant should be gone now.
            self.assertIsNone(store.get_item(plant.id))
            # Produce arrived in the kitchen.
            kitchen = store.get_location("kitchenette")
            kitchen_food = [
                i for i in store.list_items(location_id=kitchen.id)
                if i.kind == "food"
            ]
            self.assertTrue(any("tomato" in i.slug for i in kitchen_food))

    def test_skipped_outside_daylight(self) -> None:
        with _TempWorld() as store:
            worker = _make_worker(store, period="late_night")
            self.assertFalse(
                worker.is_ready(now=datetime.now(timezone.utc), last_run_at=None)
            )

    def test_cooldown_blocks_repeat_visits(self) -> None:
        with _TempWorld() as store:
            worker = _make_worker(store)
            now = datetime.now(timezone.utc)
            worker.run()  # outbound, stamps next_eligible 1.5-3.5h ahead.
            # Pull her back home so she's no longer in the garden.
            desk = store.get_location("desk")
            store.set_state(location_id=desk.id)
            # Worker shouldn't fire again immediately even with elapsed interval.
            soon = now + timedelta(seconds=worker.interval_seconds + 60)
            self.assertFalse(worker.is_ready(now=soon, last_run_at=now))


class GardenVisitWorkerIntentionalHoldTests(unittest.TestCase):
    def test_outbound_deferred_during_intentional_hold(self) -> None:
        with _TempWorld() as store:
            worker = _make_worker(store, intentional_hold_seconds=7200.0)
            now = datetime.now(timezone.utc)
            # Brain/user placed Aiko a moment ago.
            worker._mem_kv["world.intentional_state_at"] = (
                now - timedelta(seconds=30)
            ).isoformat()
            self.assertFalse(worker.is_ready(now=now, last_run_at=None))

    def test_inbound_auto_return_cancelled_if_she_chose_to_stay(self) -> None:
        with _TempWorld() as store:
            worker = _make_worker(store, intentional_hold_seconds=7200.0)
            outbound = worker.run()  # walks her to the garden, stamps return_at
            return_at = datetime.fromisoformat(outbound["return_at"])
            garden = store.get_location("garden")
            # She (brain) deliberately re-sets her state mid-visit.
            worker._mem_kv["world.intentional_state_at"] = (
                return_at - timedelta(minutes=2)
            ).isoformat()
            past = return_at + timedelta(minutes=1)
            self.assertTrue(worker.is_ready(now=past, last_run_at=None))
            result = worker.run()
            self.assertTrue(result.get("cancelled_intentional"))
            # She stays in the garden, not yanked back to the desk.
            self.assertEqual(store.get_state().location_id, garden.id)


class GardenVisitWorkerReturnTests(unittest.TestCase):
    def test_inbound_returns_after_visit_duration(self) -> None:
        with _TempWorld() as store:
            worker = _make_worker(store)
            # First call: outbound + stamps return_at ~6 min ahead.
            outbound = worker.run()
            return_at = datetime.fromisoformat(outbound["return_at"])
            # is_ready false while visit duration hasn't elapsed.
            mid = return_at - timedelta(minutes=1)
            self.assertFalse(worker.is_ready(now=mid, last_run_at=None))
            # After the visit duration: ready, run flips her back to desk.
            past = return_at + timedelta(minutes=1)
            self.assertTrue(worker.is_ready(now=past, last_run_at=None))
            inbound = worker.run()
            self.assertEqual(inbound["phase"], "inbound")
            # H13 — she settles into one of the cozy spots (no longer always
            # the desk), but never stays in the garden.
            garden = store.get_location("garden")
            self.assertNotEqual(store.get_state().location_id, garden.id)
            self.assertIn(
                inbound["returned_to_slug"],
                {"desk", "beanbag", "window_seat", "bookshelf", "bed"},
            )


class GardenVisitWorkerH15Tests(unittest.TestCase):
    """H15 — need-driven trigger, relax flavour, journal trace, switch."""

    def test_disabled_switch_blocks_visit(self) -> None:
        with _TempWorld() as store:
            worker = _make_worker(store, enabled=False)
            self.assertFalse(
                worker.is_ready(
                    now=datetime.now(timezone.utc), last_run_at=None,
                )
            )

    def test_need_driven_visit_bypasses_long_cooldown(self) -> None:
        with _TempWorld() as store:
            # Force a mature plant so the garden "needs attention".
            plant = next(iter(store.list_items(kind="plant")))
            store.update_item(
                plant.id, state={**(plant.state or {}), "stage": "mature"},
            )
            worker = _make_worker(store)
            now = datetime.now(timezone.utc)
            # Pretend the long cooldown is still in effect, but the last
            # need-driven visit was longer ago than the short need floor.
            worker._mem_kv[worker._NEXT_KEY] = (
                now + timedelta(hours=2)
            ).isoformat()
            worker._mem_kv[worker._LAST_VISIT_KEY] = (
                now - timedelta(hours=1)
            ).isoformat()
            self.assertTrue(worker.is_ready(now=now, last_run_at=None))

    def test_need_floor_blocks_back_to_back_need_visits(self) -> None:
        with _TempWorld() as store:
            plant = next(iter(store.list_items(kind="plant")))
            store.update_item(
                plant.id, state={**(plant.state or {}), "stage": "mature"},
            )
            worker = _make_worker(store)
            now = datetime.now(timezone.utc)
            worker._mem_kv[worker._NEXT_KEY] = (
                now + timedelta(hours=2)
            ).isoformat()
            # Last visit just a few minutes ago — inside the need floor.
            worker._mem_kv[worker._LAST_VISIT_KEY] = (
                now - timedelta(minutes=5)
            ).isoformat()
            self.assertFalse(worker.is_ready(now=now, last_run_at=None))

    def test_tend_visit_writes_away_journal(self) -> None:
        from app.core.world.idle_activity_worker import load_journal

        with _TempWorld() as store:
            worker = _make_worker(store, relax_ratio=0.0)
            result = worker.run()
            self.assertEqual(result["flavour"], "tend")
            journal = load_journal(lambda k: worker._kv_read(k))
            self.assertTrue(journal)
            self.assertEqual(journal[-1]["key"], "garden")
            self.assertTrue(journal[-1]["summary"])

    def test_relax_flavour_skips_watering(self) -> None:
        from app.core.world.idle_activity_worker import load_journal

        with _TempWorld() as store:
            # relax_ratio=1.0 forces the non-gardening beat; no mature/dry
            # plant so the garden doesn't "need" tending.
            worker = _make_worker(store, relax_ratio=1.0)
            result = worker.run()
            self.assertEqual(result["flavour"], "relax")
            self.assertEqual(result["watered"], [])
            self.assertEqual(result["harvested"], [])
            journal = load_journal(lambda k: worker._kv_read(k))
            self.assertEqual(journal[-1]["key"], "garden")

    def test_force_visit_bypasses_daylight(self) -> None:
        with _TempWorld() as store:
            worker = _make_worker(store, period="late_night")
            now = datetime.now(timezone.utc)
            self.assertFalse(worker.is_ready(now=now, last_run_at=None))
            worker.force_visit()
            self.assertTrue(worker.is_ready(now=now, last_run_at=None))


if __name__ == "__main__":
    unittest.main()
