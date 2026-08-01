"""Tests for :class:`PlantGrowthWorker` — hourly stage promotion."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.infra.chat_database import ChatDatabase
from app.core.world.plant_growth_worker import PlantGrowthWorker
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


def _age_plant(store: WorldStore, item_id: int, *, hours_ago: float) -> None:
    item = store.get_item(item_id)
    new_state = dict(item.state or {})
    past = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    new_state["last_promotion_at"] = past.isoformat()
    new_state["planted_at"] = past.isoformat()
    new_state["last_watered_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat()
    store.update_item(item_id, state=new_state)


class PlantGrowthWorkerTests(unittest.TestCase):
    def test_promotes_due_sprouts(self) -> None:
        with _TempWorld() as store:
            sprouts = [
                i for i in store.list_items(kind="plant")
                if (i.state or {}).get("stage") == "sprout"
            ]
            self.assertTrue(sprouts)
            for plant in sprouts:
                _age_plant(store, plant.id, hours_ago=48)
            patches: list[dict] = []
            worker = PlantGrowthWorker(store, notify=patches.append)
            result = worker.run()
            self.assertGreaterEqual(result["promoted"], len(sprouts))
            # Patches were broadcast for each promoted plant.
            self.assertGreaterEqual(len(patches), len(sprouts))
            for plant in sprouts:
                refreshed = store.get_item(plant.id)
                self.assertEqual(refreshed.state["stage"], "sapling")

    def test_no_promotion_when_under_min_age(self) -> None:
        with _TempWorld() as store:
            worker = PlantGrowthWorker(store)
            # Default seed plants were just inserted — not due yet.
            result = worker.run()
            self.assertEqual(result["promoted"], 0)

    def test_is_ready_is_no_longer_an_interval_gate(self) -> None:
        # P36: the interval moved out of is_ready and became the
        # heartbeat. Timing is now the scheduler's anti-thrash floor plus
        # demand(); this worker has no hard veto of its own.
        with _TempWorld() as store:
            worker = PlantGrowthWorker(store, interval_seconds=3600)
            now = datetime.now(timezone.utc)
            self.assertTrue(worker.is_ready(now=now, last_run_at=None))
            recent = now - timedelta(seconds=120)
            self.assertTrue(worker.is_ready(now=now, last_run_at=recent))

    def test_demand_is_zero_when_nothing_would_promote(self) -> None:
        with _TempWorld() as store:
            worker = PlantGrowthWorker(store)
            now = datetime.now(timezone.utc)
            signal = worker.demand(now=now, last_run_at=None)
            # Seed plants were just inserted, so none is due.
            self.assertIsNotNone(signal)
            self.assertEqual(signal.pressure, 0.0)
            self.assertFalse(signal.needs_llm)

    def test_demand_does_not_mutate_plant_state(self) -> None:
        # list_items hands out live mirror references, so a probe that
        # called promote_stage would advance a stage without persisting.
        with _TempWorld() as store:
            sprouts = [
                i for i in store.list_items(kind="plant")
                if (i.state or {}).get("stage") == "sprout"
            ]
            self.assertTrue(sprouts)
            for plant in sprouts:
                _age_plant(store, plant.id, hours_ago=48)
            worker = PlantGrowthWorker(store)
            now = datetime.now(timezone.utc)
            before = [
                dict(p.state or {}) for p in store.list_items(kind="plant")
            ]
            signal = worker.demand(now=now, last_run_at=None)
            self.assertGreater(signal.pressure, 0.0)
            after = [
                dict(p.state or {}) for p in store.list_items(kind="plant")
            ]
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
