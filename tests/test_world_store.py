"""Tests for :mod:`app.core.world_store` (Aiko's room).

Exercises the SQLite-backed world model end-to-end: schema migration,
default seed, location/item CRUD, consume semantics, and the
``render_block`` shape that lands in the prompt.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.chat_database import ChatDatabase
from app.core.world_store import (
    VALID_KINDS,
    VALID_POSTURES,
    WorldStore,
)


class _TempDb:
    def __enter__(self) -> tuple[Path, ChatDatabase]:
        self._dir = tempfile.TemporaryDirectory()
        path = Path(self._dir.name) / "world.db"
        db = ChatDatabase(path)
        return path, db

    def __exit__(self, *exc):
        try:
            self._dir.cleanup()
        except PermissionError:
            pass


class SchemaTests(unittest.TestCase):
    def test_world_tables_created_at_v6(self) -> None:
        with _TempDb() as (path, db):
            conn = db._get_conn()
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            self.assertIn("world_locations", tables)
            self.assertIn("world_items", tables)
            self.assertIn("world_state", tables)
            version = conn.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
            self.assertGreaterEqual(version[0], 6)

    def test_world_store_loads_empty(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            self.assertTrue(store.is_empty())
            self.assertEqual(store.list_locations(), [])
            self.assertEqual(store.list_items(), [])


class SeedTests(unittest.TestCase):
    def test_seed_default_populates_rich_room(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            seeded = store.seed_default()
            self.assertTrue(seeded)
            locations = store.list_locations()
            items = store.list_items()
            self.assertGreaterEqual(len(locations), 5)
            self.assertGreaterEqual(len(items), 8)
            slugs = {l.slug for l in locations}
            self.assertIn("desk", slugs)
            self.assertIn("kitchenette", slugs)
            # Cookies should be a stackable consumable.
            cookies = next((i for i in items if i.slug == "cookie_jar"), None)
            self.assertIsNotNone(cookies)
            self.assertTrue(cookies.consumable)
            self.assertGreaterEqual(cookies.quantity, 1)

    def test_seed_default_idempotent(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            self.assertTrue(store.seed_default())
            initial_locations = len(store.list_locations())
            initial_items = len(store.list_items())
            # Second call without force should be a no-op.
            self.assertFalse(store.seed_default())
            self.assertEqual(len(store.list_locations()), initial_locations)
            self.assertEqual(len(store.list_items()), initial_items)

    def test_seed_default_force_resets_room(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            store.add_item(name="extra rock", kind="other")
            n_before = len(store.list_items())
            store.seed_default(force=True)
            self.assertNotEqual(
                {i.name for i in store.list_items()},
                {"extra rock"} | {i.name for i in store.list_items()},
            )
            # The forced reseed should re-create exactly the default room
            # contents — the user-added "extra rock" is gone.
            self.assertNotIn(
                "extra rock", {i.name for i in store.list_items()},
            )
            # And the count matches a fresh seed.
            self.assertGreater(n_before, len(store.list_items()) - 5)

    def test_seed_state_anchored_at_desk(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            state = store.get_state()
            self.assertEqual(state.posture, "sitting")
            desk = store.get_location("desk")
            self.assertIsNotNone(desk)
            self.assertEqual(state.location_id, desk.id)


class LocationTests(unittest.TestCase):
    def test_add_and_remove_cascades_items(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            loc = store.add_location(name="the balcony")
            self.assertIsNotNone(loc)
            result = store.add_item(
                name="potted plant", kind="decor", location_id=loc.id,
            )
            self.assertIsNotNone(result)
            item, _ = result
            self.assertEqual(item.location_id, loc.id)
            removed = store.remove_location(loc.id)
            self.assertTrue(removed)
            # Item still exists, but its location is now NULL (carried).
            survivors = store.list_items()
            self.assertEqual(len(survivors), 1)
            self.assertIsNone(survivors[0].location_id)

    def test_remove_location_clears_aiko_state_pointer(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            desk = store.get_location("desk")
            self.assertIsNotNone(desk)
            self.assertEqual(store.get_state().location_id, desk.id)
            store.remove_location(desk.id)
            self.assertIsNone(store.get_state().location_id)

    def test_find_location_fuzzy_match(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            # By slug.
            self.assertEqual(store.find_location("bed").slug, "bed")
            # By substring of name.
            self.assertEqual(store.find_location("kitch").slug, "kitchenette")
            # Unknown.
            self.assertIsNone(store.find_location("dungeon"))


class ItemTests(unittest.TestCase):
    def test_stacking_consumable_bumps_quantity(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            loc = store.add_location(name="kitchenette")
            first = store.add_item(
                slug="cookie",
                name="cookie",
                kind="food",
                location_id=loc.id,
                consumable=True,
                quantity=2,
                given_by="user",
            )
            self.assertIsNotNone(first)
            second = store.add_item(
                slug="cookie",
                name="cookie",
                kind="food",
                location_id=loc.id,
                consumable=True,
                quantity=3,
                given_by="user",
            )
            self.assertIsNotNone(second)
            item_a, created_a = first
            item_b, created_b = second
            self.assertTrue(created_a)
            self.assertFalse(created_b)
            self.assertEqual(item_a.id, item_b.id)
            self.assertEqual(item_b.quantity, 5)
            self.assertEqual(len(store.list_items()), 1)

    def test_consume_decrements_and_deletes_at_zero(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            loc = store.add_location(name="kitchenette")
            result = store.add_item(
                slug="cookie",
                name="cookie",
                kind="food",
                location_id=loc.id,
                consumable=True,
                quantity=2,
            )
            assert result is not None
            item, _ = result
            still_there, consumed = store.consume_item(item.id, amount=1)
            self.assertEqual(consumed, 1)
            self.assertIsNotNone(still_there)
            self.assertEqual(still_there.quantity, 1)
            gone, consumed = store.consume_item(item.id, amount=5)
            # Overshoot is clipped to remaining quantity.
            self.assertEqual(consumed, 1)
            self.assertIsNone(gone)
            self.assertEqual(store.get_item(item.id), None)

    def test_consume_non_consumable_clamps_quantity(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            loc = store.add_location(name="desk")
            result = store.add_item(
                slug="lamp",
                name="lamp",
                kind="decor",
                location_id=loc.id,
                quantity=1,
                consumable=False,
            )
            assert result is not None
            item, _ = result
            still, consumed = store.consume_item(item.id, amount=1)
            self.assertEqual(consumed, 1)
            self.assertIsNotNone(still)
            self.assertEqual(still.quantity, 0)
            # Non-consumable rows remain even at qty 0.
            self.assertIsNotNone(store.get_item(item.id))

    def test_update_item_changes_location_and_quantity(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            kitchen = store.add_location(name="kitchenette")
            beanbag = store.add_location(name="beanbag")
            result = store.add_item(
                slug="snack",
                name="snack",
                kind="food",
                location_id=kitchen.id,
                consumable=True,
                quantity=3,
            )
            assert result is not None
            item, _ = result
            updated = store.update_item(
                item.id,
                location_id=beanbag.id,
                quantity=1,
            )
            self.assertIsNotNone(updated)
            self.assertEqual(updated.location_id, beanbag.id)
            self.assertEqual(updated.quantity, 1)

    def test_find_item_fuzzy_match(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            self.assertEqual(store.find_item("cookies").slug, "cookie_jar")
            self.assertEqual(store.find_item("scifi").slug, "scifi_paperback")
            self.assertIsNone(store.find_item("dragon"))


class StateTests(unittest.TestCase):
    def test_get_state_lazy_creates_singleton(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            state = store.get_state()
            self.assertIsNotNone(state)
            self.assertEqual(state.posture, "sitting")
            self.assertEqual(state.activity, "idle")

    def test_set_state_clamps_to_vocabulary(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.set_state(posture="zooming", activity="banana")
            state = store.get_state()
            # Invalid values are dropped — defaults survive.
            self.assertIn(state.posture, VALID_POSTURES)
            self.assertNotEqual(state.posture, "zooming")

    def test_set_state_accepts_explicit_none_location(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            loc = store.add_location(name="bed")
            store.set_state(location_id=loc.id)
            self.assertEqual(store.get_state().location_id, loc.id)
            # Explicit None clears the pointer (sentinel-aware).
            store.set_state(location_id=None)
            self.assertIsNone(store.get_state().location_id)


class RenderBlockTests(unittest.TestCase):
    def test_render_block_mentions_location_and_nudge(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            block = store.render_block()
            self.assertIn("desk", block.lower())
            self.assertIn("sitting", block.lower())
            # Tonal nudge must be present so Aiko doesn't force-mention.
            self.assertIn("natural", block.lower())
            self.assertIn("force", block.lower())

    def test_render_block_surfaces_user_gift(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            kitchen = store.get_location("kitchenette")
            self.assertIsNotNone(kitchen)
            store.add_item(
                slug="user_cookie",
                name="cookies",
                kind="food",
                location_id=kitchen.id,
                consumable=True,
                quantity=3,
                given_by="user",
            )
            block = store.render_block()
            self.assertIn("Jacob", block)
            self.assertIn("cookies", block.lower())

    def test_render_block_empty_when_no_world(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            self.assertEqual(store.render_block(), "")


class SnapshotTests(unittest.TestCase):
    def test_snapshot_shape_for_rest_consumers(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            snap = store.snapshot()
            self.assertIn("state", snap)
            self.assertIn("locations", snap)
            self.assertIn("items", snap)
            self.assertIsInstance(snap["locations"], list)
            self.assertIsInstance(snap["items"], list)
            # Each location dict has the expected keys.
            self.assertEqual(
                set(snap["locations"][0].keys()),
                {"id", "slug", "name", "description", "position"},
            )
            # Each item dict mirrors the WorldItem TypeScript interface.
            item = snap["items"][0]
            self.assertIn("kind", item)
            self.assertIn("quantity", item)
            self.assertIn("consumable", item)
            self.assertIn("state", item)


class VocabularyTests(unittest.TestCase):
    def test_kind_clamped_to_valid_set(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            result = store.add_item(name="thing", kind="rocket_fuel")
            assert result is not None
            item, _ = result
            self.assertIn(item.kind, VALID_KINDS)
            self.assertEqual(item.kind, "other")


if __name__ == "__main__":
    unittest.main()
