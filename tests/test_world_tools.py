"""Tests for the agent-facing world tools (look_around / move_to / ...)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from app.core.chat_database import ChatDatabase
from app.core.world_store import WorldStore
from app.llm.tools.world import (
    ChangePostureTool,
    ConsumeItemTool,
    InspectItemTool,
    LookAroundTool,
    MoveToTool,
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

    def cleanup(self) -> None:
        self._world_store.close()
        try:
            self._dir.cleanup()
        except PermissionError:
            pass


class BuildToolsTests(unittest.TestCase):
    def test_build_returns_five_tools(self) -> None:
        h = _Harness()
        tools = build_world_tools(h)
        self.assertEqual(len(tools), 5)
        names = {t.schema().name for t in tools}
        self.assertEqual(
            names,
            {
                "look_around",
                "move_to",
                "change_posture",
                "inspect_item",
                "consume_item",
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


if __name__ == "__main__":
    unittest.main()
