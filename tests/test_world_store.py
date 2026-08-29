"""Tests for :mod:`app.core.world.world_store` (Aiko's room).

Exercises the SQLite-backed world model end-to-end: schema migration,
default seed, location/item CRUD, consume semantics, and the
``render_block`` shape that lands in the prompt.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from datetime import datetime, timedelta, timezone

from app.core.infra.chat_database import ChatDatabase
from app.core.world.world_store import (
    VALID_KINDS,
    VALID_POSTURES,
    WorldStore,
    promote_stage,
    species_fact,
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
            self.assertIn("world_scenes", tables)
            version = conn.execute(
                "SELECT version FROM schema_version LIMIT 1"
            ).fetchone()
            self.assertGreaterEqual(version[0], 39)

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
            slugs = {loc.slug for loc in locations}
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
            self.assertIsNotNone(store.get_location("desk"))
            self.assertEqual(store.get_state().location_id, desk.id)
            extra = store.add_scene(name="the loft")
            self.assertIsNotNone(extra)
            spot = store.add_location(
                name="the couch", scene_id=extra.id, locked=False,
            )
            self.assertIsNotNone(spot)
            store.set_state(location_id=spot.id)
            self.assertEqual(store.get_state().location_id, spot.id)
            store.remove_location(spot.id)
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


class FindItemTolerationTests(unittest.TestCase):
    """The matcher has to survive how Aiko actually phrases things.

    She asks for what she just narrated, not what the row is called: the
    ambient block says "9 cookies" but ``inspect_item`` hands her the
    description ("warm, fish-shaped chocolate-chip cookies"), so she asks
    to eat a "fish-shaped cookie" and the old substring matcher returned
    nothing, three turns running.
    """

    def _store(self, path: Path) -> WorldStore:
        store = WorldStore(path)
        store.seed_default()
        return store

    def test_extra_adjectives_still_find_the_row(self) -> None:
        with _TempDb() as (path, _db):
            store = self._store(path)
            for query in (
                "fish-shaped cookie",
                "warm fish-shaped chocolate-chip cookie",
                "chocolate chip cookies",
            ):
                with self.subTest(query=query):
                    found = store.find_item(query)
                    self.assertIsNotNone(found, f"{query!r} matched nothing")
                    self.assertEqual(found.slug, "cookie_jar")

    def test_articles_and_plurals_are_ignored(self) -> None:
        with _TempDb() as (path, _db):
            store = self._store(path)
            for query in ("a cookie", "the cookies", "one more cookie", "cookie"):
                with self.subTest(query=query):
                    found = store.find_item(query)
                    self.assertIsNotNone(found, f"{query!r} matched nothing")
                    self.assertEqual(found.slug, "cookie_jar")

    def test_hyphenation_drift_still_matches_the_slug(self) -> None:
        with _TempDb() as (path, _db):
            store = self._store(path)
            self.assertEqual(
                store.find_item("sci-fi paperback").slug, "scifi_paperback"
            )

    def test_nonsense_still_matches_nothing(self) -> None:
        with _TempDb() as (path, _db):
            store = self._store(path)
            for query in ("dragon", "the user's tax return", "", "   "):
                with self.subTest(query=query):
                    self.assertIsNone(store.find_item(query))

    def test_one_shared_generic_word_is_not_a_match(self) -> None:
        """"chocolate-chip cookie" must not resolve to "potato chips"."""
        with _TempDb() as (path, _db):
            store = self._store(path)
            store.add_item(
                name="Potato chips", kind="food", consumable=True, quantity=20
            )
            found = store.find_item("chocolate-chip cookie")
            self.assertIsNotNone(found)
            self.assertEqual(found.slug, "cookie_jar")

    def test_the_pile_she_is_sitting_next_to_wins(self) -> None:
        """Several rows share a name; proximity and freshness break the tie."""
        with _TempDb() as (path, _db):
            store = self._store(path)
            beanbag = store.find_location("beanbag")
            self.assertIsNotNone(beanbag)
            fresh = store.add_item(
                name="cookies",
                kind="food",
                slug="gift_cookies",
                consumable=True,
                quantity=9,
                location_id=beanbag.id,
                given_by="user",
            )
            self.assertIsNotNone(fresh)
            store.set_state(location_id=beanbag.id)
            self.assertEqual(store.find_item("cookies").slug, "gift_cookies")
            self.assertEqual(store.find_item("a cookie").slug, "gift_cookies")

    def test_kind_filter_keeps_the_garden_out_of_the_kitchen(self) -> None:
        with _TempDb() as (path, _db):
            store = self._store(path)
            store.add_item(
                name="lavender sprigs",
                kind="food",
                consumable=True,
                quantity=12,
            )
            plant = store.find_item("lavender", kinds=("plant",))
            self.assertIsNotNone(plant)
            self.assertEqual(plant.kind, "plant")
            self.assertIsNone(store.find_item("warm lamp", kinds=("plant",)))

    def test_prefer_consumable_reaches_past_the_seed_packet(self) -> None:
        with _TempDb() as (path, _db):
            store = self._store(path)
            store.add_item(
                name="ripe tomatoes",
                kind="food",
                slug="tomatoes",
                consumable=True,
                quantity=4,
            )
            store.add_item(
                name="tomato seed packet",
                kind="seed",
                slug="seed_packet_tomato",
                quantity=1,
            )
            found = store.find_item("tomato", prefer_consumable=True)
            self.assertEqual(found.slug, "tomatoes")

    def test_rank_items_returns_every_candidate_best_first(self) -> None:
        with _TempDb() as (path, _db):
            store = self._store(path)
            ranked = store.rank_items("cookie")
            self.assertTrue(ranked)
            self.assertEqual(ranked[0].slug, "cookie_jar")

    def test_summarize_available_names_what_she_could_have_meant(self) -> None:
        with _TempDb() as (path, _db):
            store = self._store(path)
            summary = store.summarize_available(consumable_only=True)
            self.assertIn("cookies", summary)
            # Non-consumables stay out of a consumable-only summary.
            self.assertNotIn("monitors", summary)

    def test_summarize_available_is_empty_with_nothing_to_offer(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            self.assertEqual(store.summarize_available(), "")


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
            self.assertIn("scenes", snap)
            self.assertIn("locations", snap)
            self.assertIn("items", snap)
            self.assertIsInstance(snap["locations"], list)
            self.assertIsInstance(snap["items"], list)
            self.assertGreaterEqual(len(snap["scenes"]), 1)
            self.assertEqual(snap["scenes"][0]["slug"], "apartment")
            # Each location dict has the expected keys.
            self.assertEqual(
                set(snap["locations"][0].keys()),
                {
                    "id", "slug", "name", "description", "position",
                    "scene_id", "locked",
                },
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

    def test_plant_and_seed_kinds_accepted(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            loc = store.add_location(name="garden")
            plant = store.add_item(
                name="basil",
                kind="plant",
                location_id=loc.id,
                state={"species": "basil", "stage": "sprout"},
            )
            seed = store.add_item(
                name="sunflower seed",
                kind="seed",
                state={"species": "sunflower"},
            )
            assert plant is not None and seed is not None
            self.assertEqual(plant[0].kind, "plant")
            self.assertEqual(seed[0].kind, "seed")


class GardenSeedAndGrowthTests(unittest.TestCase):
    def test_ensure_garden_seed_idempotent(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            # First call from a blank world inserts the garden + items.
            self.assertTrue(store.ensure_garden_seed())
            first_count = len(store.list_items())
            # Second call is a no-op.
            self.assertFalse(store.ensure_garden_seed())
            self.assertEqual(len(store.list_items()), first_count)
            garden = store.get_location("garden")
            self.assertIsNotNone(garden)
            plants = [
                i for i in store.list_items(location_id=garden.id)
                if i.kind == "plant"
            ]
            self.assertGreaterEqual(len(plants), 1)

    def test_seed_default_installs_garden(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            self.assertIsNotNone(store.get_location("garden"))
            seeds = [i for i in store.list_items() if i.kind == "seed"]
            self.assertGreaterEqual(len(seeds), 1)

    def test_promote_stage_advances_after_min_age(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.ensure_garden_seed()
            sprouts = [
                i for i in store.list_items(kind="plant")
                if (i.state or {}).get("stage") == "sprout"
            ]
            self.assertTrue(sprouts)
            item = sprouts[0]
            # Pretend the plant was promoted 48h ago so the sprout
            # stage's 24h gate is fully elapsed and watering is fresh.
            now = datetime.now(timezone.utc)
            past = now - timedelta(hours=48)
            new_state = dict(item.state or {})
            new_state["last_promotion_at"] = past.isoformat()
            new_state["last_watered_at"] = (now - timedelta(hours=1)).isoformat()
            store.update_item(item.id, state=new_state)
            refreshed = store.get_item(item.id)
            advanced = promote_stage(refreshed, now=now)
            self.assertEqual(advanced, "sapling")

    def test_promote_stage_blocked_when_dry(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.ensure_garden_seed()
            sprouts = [
                i for i in store.list_items(kind="plant")
                if (i.state or {}).get("stage") == "sprout"
            ]
            assert sprouts
            item = sprouts[0]
            now = datetime.now(timezone.utc)
            ancient = now - timedelta(hours=48)
            very_dry = now - timedelta(hours=200)  # well past 96h tolerance
            new_state = dict(item.state or {})
            new_state["last_promotion_at"] = ancient.isoformat()
            new_state["last_watered_at"] = very_dry.isoformat()
            store.update_item(item.id, state=new_state)
            refreshed = store.get_item(item.id)
            advanced = promote_stage(refreshed, now=now)
            self.assertIsNone(advanced)
            # Drought stress flag should be set so the UI can show it.
            self.assertGreater(float((refreshed.state or {}).get("days_dry", 0)), 0)

    def test_water_plant_refreshes_last_watered_at(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.ensure_garden_seed()
            plant = next(i for i in store.list_items(kind="plant"))
            before = (plant.state or {}).get("last_watered_at", "")
            store.update_item(plant.id, state={
                **(plant.state or {}),
                "last_watered_at": "1970-01-01T00:00:00+00:00",
                "days_dry": 10,
            })
            updated = store.water_plant(plant.id)
            assert updated is not None
            after = (updated.state or {}).get("last_watered_at", "")
            self.assertNotEqual(before, after)
            self.assertEqual(updated.state.get("days_dry"), 0)


class HarvestTests(unittest.TestCase):
    def _make_mature(self, store: WorldStore, *, species: str) -> None:
        store.seed_default()
        plant = next(
            i for i in store.list_items(kind="plant")
            if (i.state or {}).get("species") == species
        )
        new_state = dict(plant.state or {})
        new_state["stage"] = "mature"
        store.update_item(plant.id, state=new_state)
        return plant

    def test_harvest_refuses_non_mature(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            plant = next(
                i for i in store.list_items(kind="plant")
                if (i.state or {}).get("stage") == "sprout"
            )
            self.assertIsNone(store.harvest_plant(plant.id))

    def test_harvest_perennial_resets_to_growing(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            plant = self._make_mature(store, species="basil")
            result = store.harvest_plant(plant.id)
            self.assertIsNotNone(result)
            self.assertTrue(result["plant"]["reset"])
            self.assertFalse(result["plant"]["deleted"])
            refreshed = store.get_item(plant.id)
            self.assertIsNotNone(refreshed)
            self.assertEqual(refreshed.state.get("stage"), "growing")
            # Produce shows up in the kitchenette.
            kitchen = store.get_location("kitchenette")
            kitchen_items = (
                store.list_items(location_id=kitchen.id) if kitchen else []
            )
            self.assertTrue(any(i.slug == "basil_leaves" for i in kitchen_items))

    def test_harvest_annual_deletes_and_drops_seed(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            plant = self._make_mature(store, species="tomato")
            result = store.harvest_plant(plant.id)
            self.assertIsNotNone(result)
            self.assertTrue(result["plant"]["deleted"])
            self.assertIsNone(store.get_item(plant.id))
            # A fresh seed lands in inventory (location_id is None).
            seeds = [
                i for i in store.list_items(kind="seed")
                if (i.state or {}).get("species") == "tomato"
            ]
            self.assertTrue(seeds)
            self.assertIsNone(seeds[0].location_id)

    def test_species_fact_falls_back_to_generic(self) -> None:
        fact = species_fact("dragonfruit")
        self.assertEqual(fact["produce_species"], "harvest")
        self.assertEqual(fact["lifecycle"], "perennial")

    def test_widened_catalog_covers_the_kitchen_garden(self) -> None:
        """K91 — four species made every harvest one of four lines."""
        for species in (
            "lettuce", "mint", "strawberry", "chili", "rosemary",
            "spring_onion", "radish", "peas",
        ):
            fact = species_fact(species)
            self.assertNotEqual(
                fact["produce_species"], "harvest", species,
            )
            self.assertIn(fact["lifecycle"], ("annual", "perennial"), species)
            low, high = fact["produce_quantity_range"]
            self.assertLessEqual(low, high, species)

    def test_a_widened_species_harvests_its_own_produce(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            garden = store.get_location("garden")
            created = store.add_item(
                name="lettuce",
                kind="plant",
                location_id=garden.id,
                state={"species": "lettuce", "stage": "mature"},
            )
            assert created is not None
            plant, _new = created
            result = store.harvest_plant(plant.id)
            assert result is not None
            self.assertEqual(result["produce"]["name"], "crisp lettuce")


class ConsolidateConsumablesTests(unittest.TestCase):
    """K91 — gifts accrete duplicate food stacks across rooms."""

    def _stock(self, store: WorldStore) -> None:
        for loc_slug, qty in (("desk", 9), ("bed", 19), ("kitchenette", 39)):
            loc = store.get_location(loc_slug)
            store.add_item(
                name="cookies",
                slug="cookies",
                kind="food",
                location_id=loc.id if loc else None,
                consumable=True,
                quantity=qty,
            )

    def test_same_slug_stacks_fold_into_the_biggest(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            self._stock(store)
            merged = store.consolidate_consumables()
            self.assertTrue(merged)
            rows = [i for i in store.list_items() if i.slug == "cookies"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].quantity, 67)

    def test_distinct_slugs_are_left_alone(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            before = len(store.list_items())
            self.assertEqual(store.consolidate_consumables(), [])
            self.assertEqual(len(store.list_items()), before)

    def test_non_food_consumables_are_untouched(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            for slug in ("desk", "bed"):
                loc = store.get_location(slug)
                store.add_item(
                    name="incense stick",
                    slug="incense",
                    kind="decor",
                    location_id=loc.id if loc else None,
                    consumable=True,
                    quantity=2,
                )
            store.consolidate_consumables()
            self.assertEqual(
                len([i for i in store.list_items() if i.slug == "incense"]), 2
            )

    def test_state_keys_survive_the_merge(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            desk = store.get_location("desk")
            kitchen = store.get_location("kitchenette")
            store.add_item(
                name="cookies", slug="cookies", kind="food", consumable=True,
                quantity=2, location_id=desk.id if desk else None,
                state={"freshness": "stale"},
            )
            store.add_item(
                name="cookies", slug="cookies", kind="food", consumable=True,
                quantity=9, location_id=kitchen.id if kitchen else None,
                state={"flavor": "ginger snap"},
            )
            store.consolidate_consumables()
            row = next(i for i in store.list_items() if i.slug == "cookies")
            self.assertEqual(row.state.get("flavor"), "ginger snap")
            self.assertEqual(row.state.get("freshness"), "stale")


class OutdoorRenderTests(unittest.TestCase):
    def test_render_block_uses_outdoor_phrasing_in_garden(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            garden = store.get_location("garden")
            self.assertIsNotNone(garden)
            store.set_state(location_id=garden.id, activity="stretching")
            block = store.render_block()
            self.assertIn("outside", block.lower())
            self.assertIn("garden", block.lower())

    def test_render_block_plant_stage_suffix(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            garden = store.get_location("garden")
            store.set_state(location_id=garden.id)
            # Force one plant to mature so the (ready to harvest) cue surfaces.
            plant = next(i for i in store.list_items(kind="plant"))
            store.update_item(
                plant.id,
                state={**(plant.state or {}), "stage": "mature"},
            )
            block = store.render_block()
            self.assertIn("ready to harvest", block.lower())


class SceneTests(unittest.TestCase):
    def test_seed_creates_builtin_apartment(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            home = store.home_scene()
            self.assertIsNotNone(home)
            self.assertEqual(home.slug, "apartment")
            self.assertEqual(home.origin, "builtin")
            desk = store.get_location("desk")
            self.assertIsNotNone(desk)
            self.assertTrue(desk.locked)
            self.assertEqual(desk.scene_id, home.id)
            self.assertEqual(store.get_state().scene_id, home.id)

    def test_cannot_remove_or_rename_seeded_spots(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            desk = store.get_location("desk")
            self.assertFalse(store.remove_location(desk.id))
            updated = store.update_location(desk.id, name="the floor")
            self.assertEqual(updated.name, desk.name)

    def test_custom_scene_travel_and_scoped_lookups(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            home = store.home_scene()
            scene = store.add_scene(
                name="Jacob's room", description="his actual room",
            )
            self.assertIsNotNone(scene)
            self.assertEqual(scene.origin, "custom")
            spots = store.list_locations(scene_id=scene.id)
            self.assertGreaterEqual(len(spots), 1)
            store.add_location(
                name="the desk", scene_id=scene.id, description="his desk",
            )
            result = store.travel_to_scene(scene.id)
            self.assertIsNotNone(result)
            self.assertEqual(store.current_scene().id, scene.id)
            self.assertIsNone(store.get_location("kitchenette"))
            self.assertIsNotNone(
                store.get_location("kitchenette", scene_id=home.id),
            )
            desk = store.find_location("desk")
            self.assertIsNotNone(desk)
            self.assertEqual(desk.scene_id, scene.id)
            block = store.render_block()
            self.assertIn("Jacob's room", block)
            self.assertNotIn("You are in your room.", block)

    def test_items_stay_put_carried_travel(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            kitchen = store.get_location("kitchenette")
            cookies = store.add_item(
                name="cookies", kind="food", location_id=kitchen.id,
                consumable=True, quantity=2,
            )[0]
            lamp = store.add_item(
                name="pocket lamp", kind="gadget", location_id=None,
            )[0]
            scene = store.add_scene(name="the loft")
            store.travel_to_scene(scene.id)
            visible = {i.id for i in store._visible_items()}
            self.assertNotIn(cookies.id, visible)
            self.assertIn(lamp.id, visible)
            self.assertEqual(store.get_item(cookies.id).location_id, kitchen.id)

    def test_force_reseed_keeps_custom_scene(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            scene = store.add_scene(name="Jacob's room")
            mug = store.add_item(
                name="mug",
                kind="keepsake",
                location_id=store.list_locations(scene_id=scene.id)[0].id,
            )[0]
            store.seed_default(force=True)
            self.assertIsNotNone(store.get_scene(scene.id))
            self.assertIsNotNone(store.get_item(mug.id))
            self.assertIsNotNone(store.get_location("desk"))

    def test_cannot_delete_apartment(self) -> None:
        with _TempDb() as (path, _db):
            store = WorldStore(path)
            store.seed_default()
            home = store.home_scene()
            self.assertFalse(store.remove_scene(home.id))


if __name__ == "__main__":
    unittest.main()
