"""Tests for the world-related slice of :class:`SessionController`.

Bypasses ``__init__`` and wires only the world store + listeners so we
can exercise:
  - ``add_world_listener`` / ``_notify_world`` fan-out
  - ``update_world_state`` / ``add_world_item`` / ``consume_world_item``
    snapshot shapes and listener triggers
  - ``give_item`` defaults (kitchenette + given_by="user")
  - ``_render_world_block`` graceful fallback when the store is missing
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from dataclasses import dataclass

from app.core.chat_database import ChatDatabase
from app.core.session_controller import SessionController
from app.core.world_store import WorldStore


@dataclass
class _AssistantStub:
    user_display_name: str = "Jacob"


@dataclass
class _SettingsStub:
    assistant: _AssistantStub


def _make_controller(*, seed: bool = True) -> tuple[SessionController, Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "session_world.db"
    ChatDatabase(db_path)
    store = WorldStore(db_path)
    if seed:
        store.seed_default()
    controller = SessionController.__new__(SessionController)
    controller._world_store = store
    controller._world_listeners = []
    # ``user_display_name`` (and therefore ``reseed_world`` /
    # ``_render_world_block``) reads ``self._settings.assistant``.
    controller._settings = _SettingsStub(assistant=_AssistantStub())  # type: ignore[attr-defined]
    return controller, db_path, tmp


def _cleanup(tmp: tempfile.TemporaryDirectory, controller: SessionController) -> None:
    try:
        if controller._world_store is not None:
            controller._world_store.close()
    except Exception:
        pass
    try:
        tmp.cleanup()
    except PermissionError:
        pass


class ListenerTests(unittest.TestCase):
    def test_listener_fires_on_state_update(self) -> None:
        controller, _, tmp = _make_controller()
        captured: list[dict[str, Any]] = []
        controller.add_world_listener(lambda patch: captured.append(dict(patch)))
        snap = controller.update_world_state(posture="lying")
        self.assertIsNotNone(snap)
        self.assertEqual(len(captured), 1)
        self.assertIn("state", captured[0])
        self.assertEqual(captured[0]["state"]["posture"], "lying")
        _cleanup(tmp, controller)

    def test_listener_fires_on_item_add(self) -> None:
        controller, _, tmp = _make_controller(seed=False)
        controller._world_store.seed_default()
        captured: list[dict[str, Any]] = []
        controller.add_world_listener(lambda patch: captured.append(dict(patch)))
        snap = controller.add_world_item(
            name="extra cookie", kind="food", consumable=True, quantity=1,
        )
        self.assertIsNotNone(snap)
        self.assertEqual(len(captured), 1)
        self.assertIn("item", captured[0])
        _cleanup(tmp, controller)

    def test_consume_to_zero_emits_deletion(self) -> None:
        controller, _, tmp = _make_controller(seed=False)
        controller._world_store.seed_default()
        result = controller.add_world_item(
            name="last_cookie", kind="food", consumable=True, quantity=1,
        )
        captured: list[dict[str, Any]] = []
        controller.add_world_listener(lambda patch: captured.append(dict(patch)))
        outcome = controller.consume_world_item(result["id"], amount=1)
        self.assertIsNotNone(outcome)
        self.assertIn("deleted_item_id", outcome)
        deletion_events = [p for p in captured if "deleted_item_id" in p]
        self.assertEqual(len(deletion_events), 1)
        _cleanup(tmp, controller)


class GiveItemTests(unittest.TestCase):
    def test_give_item_default_lands_in_kitchenette(self) -> None:
        controller, _, tmp = _make_controller()
        snap = controller.give_item(
            name="cookies", kind="food", quantity=2,
        )
        self.assertIsNotNone(snap)
        self.assertEqual(snap["given_by"], "user")
        kitchen = controller._world_store.get_location("kitchenette")
        # The seeded default already has a "cookie_jar" stack — the give
        # path should merge into it (since it's the same slug). Confirm
        # the resulting row is in the kitchenette regardless.
        self.assertEqual(snap["location_id"], kitchen.id)
        _cleanup(tmp, controller)

    def test_give_item_with_explicit_location(self) -> None:
        controller, _, tmp = _make_controller()
        bed = controller._world_store.get_location("bed")
        self.assertIsNotNone(bed)
        snap = controller.give_item(
            name="teddy",
            kind="toy",
            location_slug="bed",
        )
        self.assertEqual(snap["location_id"], bed.id)
        self.assertFalse(snap["consumable"])  # toys aren't consumable by default
        _cleanup(tmp, controller)

    def test_give_food_is_consumable_by_default(self) -> None:
        controller, _, tmp = _make_controller()
        snap = controller.give_item(name="apple", kind="food")
        self.assertTrue(snap["consumable"])
        _cleanup(tmp, controller)

    def test_give_with_unknown_location_falls_back_to_first(self) -> None:
        controller, _, tmp = _make_controller()
        snap = controller.give_item(
            name="wandering gift", kind="other", location_slug="dungeon",
        )
        self.assertIsNotNone(snap)
        # Should have landed in *some* real location.
        self.assertIsNotNone(snap["location_id"])
        _cleanup(tmp, controller)


class RenderBlockTests(unittest.TestCase):
    def test_renders_when_store_present(self) -> None:
        controller, _, tmp = _make_controller()
        block = controller._render_world_block()
        self.assertNotEqual(block, "")
        self.assertIn("desk", block.lower())
        _cleanup(tmp, controller)

    def test_returns_empty_when_store_missing(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._world_store = None
        self.assertEqual(controller._render_world_block(), "")

    def test_world_snapshot_disabled_when_store_missing(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._world_store = None
        snap = controller.world_snapshot()
        self.assertFalse(snap["enabled"])
        self.assertEqual(snap["locations"], [])
        self.assertEqual(snap["items"], [])


class ResetTests(unittest.TestCase):
    def test_reseed_world_emits_snapshot(self) -> None:
        controller, _, tmp = _make_controller()
        # Add a custom item the reseed should wipe.
        controller.add_world_item(name="extra rock", kind="other")
        captured: list[dict[str, Any]] = []
        controller.add_world_listener(lambda patch: captured.append(dict(patch)))
        result = controller.reseed_world(force=True)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(len(captured), 1)
        names = {i["name"] for i in result["items"]}
        self.assertNotIn("extra rock", names)
        _cleanup(tmp, controller)


if __name__ == "__main__":
    unittest.main()
