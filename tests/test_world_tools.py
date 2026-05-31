"""Tests for the agent-facing world tools (look_around / move_to / ...)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from app.core.infra.chat_database import ChatDatabase
from app.core.world.world_store import WorldStore
from app.llm.tools.world import (
    ChangePostureTool,
    ConsumeItemTool,
    HarvestPlantTool,
    InspectItemTool,
    LookAroundTool,
    MoveToTool,
    PlantSeedTool,
    WaterPlantTool,
    build_world_tools,
)


class _Harness:
    """Lightweight stand-in for ``SessionController`` exposing only the
    methods world tools actually call."""

    def __init__(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        path = Path(self._dir.name) / "tools.db"
        ChatDatabase(path)
        self._world_store = WorldStore(path)
        self._world_store.seed_default()

    def update_world_state(self, **kwargs):
        state = self._world_store.set_state(**kwargs)
        return state.to_dict()

    def consume_world_item(self, item_id, *, amount=1):
        item, consumed = self._world_store.consume_item(item_id, amount=amount)
        if consumed <= 0:
            return None
        if item is None:
            return {"deleted_item_id": item_id, "consumed": consumed}
        return {"item": item.to_dict(), "consumed": consumed}

    # The garden tools call session-level helpers (add_world_item /
    # delete_world_item / _notify_world). Provide thin stand-ins that
    # touch the real store and record world patches for the tests.
    def __init_world_listeners(self) -> None:
        if not hasattr(self, "world_patches"):
            self.world_patches: list[dict] = []

    def _notify_world(self, patch):
        self.__init_world_listeners()
        self.world_patches.append(dict(patch))

    def add_world_item(self, **kwargs):
        self.__init_world_listeners()
        result = self._world_store.add_item(**kwargs)
        if result is None:
            return None
        item, _ = result
        snap = item.to_dict()
        self._notify_world({"item": snap})
        return snap

    def delete_world_item(self, item_id):
        self.__init_world_listeners()
        ok = self._world_store.remove_item(int(item_id))
        if ok:
            self._notify_world({"deleted_item_id": int(item_id)})
        return ok

    def cleanup(self) -> None:
        self._world_store.close()
        try:
            self._dir.cleanup()
        except PermissionError:
            pass


class BuildToolsTests(unittest.TestCase):
    def test_build_returns_eight_tools(self) -> None:
        h = _Harness()
        tools = build_world_tools(h)
        self.assertEqual(len(tools), 8)
        names = {t.schema().name for t in tools}
        self.assertEqual(
            names,
            {
                "look_around",
                "move_to",
                "change_posture",
                "inspect_item",
                "consume_item",
                "water_plant",
                "plant_seed",
                "harvest_plant",
            },
        )
        h.cleanup()


class LookAroundTests(unittest.TestCase):
    def test_look_around_includes_current_location(self) -> None:
        h = _Harness()
        tool = LookAroundTool(h)
        result = json.loads(tool.run({}))
        self.assertIn("here", result)
        self.assertIsNotNone(result["here"])
        self.assertEqual(result["here"]["name"], "the desk")
        self.assertGreater(len(result["other_locations"]), 0)
        h.cleanup()


class MoveToTests(unittest.TestCase):
    def test_move_to_known_slug(self) -> None:
        h = _Harness()
        tool = MoveToTool(h)
        result = json.loads(tool.run({"location": "bed"}))
        self.assertEqual(result["slug"], "bed")
        bed = h._world_store.get_location("bed")
        self.assertEqual(h._world_store.get_state().location_id, bed.id)
        h.cleanup()

    def test_move_to_unknown_raises(self) -> None:
        from app.llm.tools.base import ToolError

        h = _Harness()
        tool = MoveToTool(h)
        with self.assertRaises(ToolError):
            tool.run({"location": "dungeon"})
        h.cleanup()

    def test_move_to_missing_arg_raises(self) -> None:
        from app.llm.tools.base import ToolError

        h = _Harness()
        tool = MoveToTool(h)
        with self.assertRaises(ToolError):
            tool.run({})
        h.cleanup()


class ChangePostureTests(unittest.TestCase):
    def test_change_posture_valid(self) -> None:
        h = _Harness()
        tool = ChangePostureTool(h)
        result = json.loads(tool.run({"posture": "lying"}))
        self.assertEqual(result["state"]["posture"], "lying")
        h.cleanup()

    def test_change_posture_invalid_raises(self) -> None:
        from app.llm.tools.base import ToolError

        h = _Harness()
        tool = ChangePostureTool(h)
        with self.assertRaises(ToolError):
            tool.run({"posture": "zooming"})
        h.cleanup()


class InspectItemTests(unittest.TestCase):
    def test_inspect_known_item(self) -> None:
        h = _Harness()
        tool = InspectItemTool(h)
        result = json.loads(tool.run({"item": "cookies"}))
        # The seeded "cookies" item has slug "cookie_jar".
        self.assertIn("name", result)
        self.assertIn("location", result)
        self.assertTrue(result.get("consumable", False))
        h.cleanup()

    def test_inspect_unknown_item_raises(self) -> None:
        from app.llm.tools.base import ToolError

        h = _Harness()
        tool = InspectItemTool(h)
        with self.assertRaises(ToolError):
            tool.run({"item": "dragon"})
        h.cleanup()


class ConsumeItemTests(unittest.TestCase):
    def test_consume_cookie_decrements(self) -> None:
        h = _Harness()
        tool = ConsumeItemTool(h)
        result = json.loads(tool.run({"item": "cookies", "amount": 1}))
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["remaining"], 0)
        h.cleanup()

    def test_consume_non_consumable_returns_refusal(self) -> None:
        h = _Harness()
        tool = ConsumeItemTool(h)
        result = json.loads(tool.run({"item": "warm_lamp"}))
        self.assertFalse(result["ok"])
        h.cleanup()

    def test_consume_until_empty(self) -> None:
        from app.llm.tools.base import ToolError

        h = _Harness()
        tool = ConsumeItemTool(h)
        # Seed has 3 cookies; eat them all then verify the tool errors
        # when there's nothing left to consume.
        seen_last_message = False
        for _ in range(4):
            try:
                payload = json.loads(tool.run({"item": "cookies", "amount": 1}))
            except ToolError:
                # Once cookies disappear, find_item raises -- that's an
                # acceptable terminal state.
                break
            if "note" in payload and "last" in str(payload.get("note", "")):
                seen_last_message = True
        # Either we hit the "last cookie" branch or exhausted into a
        # ToolError via the loop break above. Both are valid.
        self.assertTrue(seen_last_message or h._world_store.find_item("cookies") is None)
        h.cleanup()


class WaterPlantTests(unittest.TestCase):
    def test_water_plant_updates_state(self) -> None:
        h = _Harness()
        # seed_default already installs the garden so basil_seedling exists.
        tool = WaterPlantTool(h)
        # Move Aiko to the garden so the "must be in same location" check passes.
        garden = h._world_store.get_location("garden")
        h._world_store.set_state(location_id=garden.id)
        result = json.loads(tool.run({"plant": "basil"}))
        self.assertTrue(result["ok"])
        plant = h._world_store.find_item("basil_seedling")
        self.assertIn("last_watered_at", plant.state)
        h.cleanup()

    def test_water_plant_rejects_non_plant(self) -> None:
        from app.llm.tools.base import ToolError

        h = _Harness()
        tool = WaterPlantTool(h)
        with self.assertRaises(ToolError):
            tool.run({"plant": "warm_lamp"})
        h.cleanup()


class PlantSeedTests(unittest.TestCase):
    def test_plant_seed_consumes_and_creates_plant(self) -> None:
        h = _Harness()
        tool = PlantSeedTool(h)
        # seed_default puts a sunflower seed packet in inventory.
        before = h._world_store.find_item("seed_packet_sunflower")
        self.assertIsNotNone(before)
        result = json.loads(
            tool.run({"seed": "sunflower seed packet", "where": "garden"})
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "sprout")
        # Original seed is gone.
        self.assertIsNone(h._world_store.find_item("seed_packet_sunflower"))
        # New sunflower sprout exists in the garden.
        garden = h._world_store.get_location("garden")
        plants_in_garden = [
            i for i in h._world_store.list_items(location_id=garden.id)
            if i.kind == "plant"
            and (i.state or {}).get("species") == "sunflower"
        ]
        self.assertTrue(plants_in_garden)
        h.cleanup()

    def test_plant_seed_unknown_seed_raises(self) -> None:
        from app.llm.tools.base import ToolError

        h = _Harness()
        tool = PlantSeedTool(h)
        with self.assertRaises(ToolError):
            tool.run({"seed": "magic bean"})
        h.cleanup()


class HarvestPlantTests(unittest.TestCase):
    def _mature(self, h, species):
        plant = next(
            i for i in h._world_store.list_items(kind="plant")
            if (i.state or {}).get("species") == species
        )
        h._world_store.update_item(
            plant.id,
            state={**(plant.state or {}), "stage": "mature"},
        )
        return plant

    def test_harvest_refuses_non_mature(self) -> None:
        from app.llm.tools.base import ToolError

        h = _Harness()
        tool = HarvestPlantTool(h)
        with self.assertRaises(ToolError):
            tool.run({"plant": "basil_seedling"})
        h.cleanup()

    def test_harvest_perennial_resets_plant(self) -> None:
        h = _Harness()
        tool = HarvestPlantTool(h)
        plant = self._mature(h, "basil")
        result = json.loads(tool.run({"plant": plant.name}))
        self.assertTrue(result["ok"])
        self.assertEqual(result["lifecycle"], "perennial")
        self.assertFalse(result["plant_deleted"])
        self.assertTrue(result["plant_reset"])
        refreshed = h._world_store.get_item(plant.id)
        self.assertEqual(refreshed.state["stage"], "growing")
        # Produce in kitchenette.
        kitchen = h._world_store.get_location("kitchenette")
        kitchen_food = [
            i for i in h._world_store.list_items(location_id=kitchen.id)
            if i.kind == "food"
        ]
        self.assertTrue(any("basil" in i.slug for i in kitchen_food))
        h.cleanup()

    def test_harvest_annual_deletes_and_drops_seed(self) -> None:
        h = _Harness()
        tool = HarvestPlantTool(h)
        plant = self._mature(h, "tomato")
        result = json.loads(tool.run({"plant": plant.name}))
        self.assertTrue(result["plant_deleted"])
        self.assertIsNone(h._world_store.get_item(plant.id))
        new_seeds = [
            i for i in h._world_store.list_items(kind="seed")
            if (i.state or {}).get("species") == "tomato"
        ]
        self.assertTrue(new_seeds)
        h.cleanup()


if __name__ == "__main__":
    unittest.main()
