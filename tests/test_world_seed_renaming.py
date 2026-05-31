"""Tests for the renamed world seed (Phase 4e).

``WorldStore.seed_default`` accepts a ``user_display_name`` keyword and
threads it through the ``"photo of Jacob"`` seed item + its slug.
``render_block`` similarly accepts the name and uses it in the
``"<name> gave you ..."`` line.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.infra.chat_database import ChatDatabase
from app.core.world.world_store import (
    WorldStore,
    _slug_from_user_name,
)


class _Sandbox:
    def __enter__(self) -> Path:
        self._dir = tempfile.TemporaryDirectory()
        path = Path(self._dir.name) / "world.db"
        ChatDatabase(path)  # bootstrap schema
        return path

    def __exit__(self, *exc):
        try:
            self._dir.cleanup()
        except PermissionError:
            pass


class SlugDerivationTests(unittest.TestCase):
    def test_empty_name_falls_back(self) -> None:
        self.assertEqual(_slug_from_user_name(""), "photo_of_you")
        self.assertEqual(_slug_from_user_name("   "), "photo_of_you")

    def test_alpha_name_becomes_lowercase_slug(self) -> None:
        self.assertEqual(_slug_from_user_name("Bea"), "photo_of_bea")
        self.assertEqual(_slug_from_user_name("BEA"), "photo_of_bea")

    def test_special_chars_collapse(self) -> None:
        self.assertEqual(_slug_from_user_name("Ada Lovelace"), "photo_of_ada_lovelace")
        self.assertEqual(_slug_from_user_name("@@@"), "photo_of_you")

    def test_unicode_strips_to_fallback(self) -> None:
        # Emoji-only / non-ASCII names produce no [a-z0-9] characters so
        # the helper falls back to ``photo_of_you``.
        self.assertEqual(_slug_from_user_name("🦊🦊🦊"), "photo_of_you")


class SeedNamingTests(unittest.TestCase):
    def test_seed_uses_configured_name(self) -> None:
        with _Sandbox() as path:
            store = WorldStore(path)
            seeded = store.seed_default(user_display_name="Bea")
            self.assertTrue(seeded)
            items = {item.slug: item for item in store.list_items()}
            self.assertIn("photo_of_bea", items)
            self.assertEqual(items["photo_of_bea"].name, "photo of Bea")
            self.assertNotIn("photo_of_jacob", items)
            self.assertNotIn("photo_of_user", items)

    def test_seed_without_name_falls_back_to_you(self) -> None:
        with _Sandbox() as path:
            store = WorldStore(path)
            self.assertTrue(store.seed_default(user_display_name=""))
            items = {item.slug: item for item in store.list_items()}
            self.assertIn("photo_of_you", items)
            self.assertEqual(items["photo_of_you"].name, "photo of you")

    def test_seed_is_idempotent(self) -> None:
        with _Sandbox() as path:
            store = WorldStore(path)
            self.assertTrue(store.seed_default(user_display_name="Bea"))
            # Second seed is a no-op (world is non-empty).
            self.assertFalse(store.seed_default(user_display_name="Bea"))

    def test_force_reseed_renames_photo(self) -> None:
        with _Sandbox() as path:
            store = WorldStore(path)
            store.seed_default(user_display_name="Bea")
            store.seed_default(force=True, user_display_name="Carl")
            slugs = {item.slug for item in store.list_items()}
            self.assertIn("photo_of_carl", slugs)
            self.assertNotIn("photo_of_bea", slugs)


class RenderBlockTests(unittest.TestCase):
    def test_gift_line_uses_user_display_name(self) -> None:
        with _Sandbox() as path:
            store = WorldStore(path)
            store.seed_default(user_display_name="Bea")
            cookie = store.add_item(
                slug="hot_cookie",
                name="cookie",
                description="a warm chocolate-chip cookie",
                kind="food",
                quantity=1,
                consumable=True,
                given_by="user",
            )
            self.assertIsNotNone(cookie)
            block = store.render_block(user_display_name="Bea")
            self.assertIn("Bea gave you", block)
            self.assertNotIn("Jacob gave you", block)

    def test_gift_line_falls_back_when_name_blank(self) -> None:
        with _Sandbox() as path:
            store = WorldStore(path)
            store.seed_default()
            store.add_item(
                slug="hot_tea",
                name="tea",
                description="jasmine tea",
                kind="food",
                quantity=1,
                consumable=True,
                given_by="user",
            )
            block = store.render_block(user_display_name="")
            self.assertIn("the user gave you", block)


if __name__ == "__main__":
    unittest.main()
