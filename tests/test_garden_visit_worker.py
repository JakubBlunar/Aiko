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
):
    return GardenVisitWorker(
        store,
        notify=notify,
        rng=random.Random(0),
        circadian_period_provider=lambda: period,
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
            desk = store.get_location("desk")
            self.assertEqual(store.get_state().location_id, desk.id)


if __name__ == "__main__":
    unittest.main()
