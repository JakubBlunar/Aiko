"""Tests for :class:`PlantGrowthWorker` — hourly stage promotion."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.chat_database import ChatDatabase
from app.core.plant_growth_worker import PlantGrowthWorker
from app.core.world_store import WorldStore


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

    def test_is_ready_interval_gate(self) -> None:
        with _TempWorld() as store:
            worker = PlantGrowthWorker(store, interval_seconds=3600)
            now = datetime.now(timezone.utc)
            self.assertTrue(worker.is_ready(now=now, last_run_at=None))
            # Just ran — should not be ready again until interval elapses.
            recent = now - timedelta(seconds=120)
            self.assertFalse(worker.is_ready(now=now, last_run_at=recent))
            older = now - timedelta(seconds=3700)
            self.assertTrue(worker.is_ready(now=now, last_run_at=older))


if __name__ == "__main__":
    unittest.main()
