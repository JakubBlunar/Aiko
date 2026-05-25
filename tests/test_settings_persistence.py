"""Tests for ``app.core.settings.persist_user_overrides``.

The helper is used by ``SessionController`` whenever a user-tunable
knob (avatar scale, auto-outfit, …) changes at runtime so the value
survives an app restart. These tests verify the deep-merge / atomic
write / cache-bust contract without touching the real
``config/user.json``.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core import settings as settings_mod
from app.core.settings import persist_user_overrides


class PersistUserOverridesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "user.json"

    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_creates_file_on_first_write(self) -> None:
        self.assertFalse(self.path.exists())
        persist_user_overrides({"avatar": {"scale_multiplier": 1.6}}, path=self.path)
        self.assertTrue(self.path.exists())
        self.assertEqual(self._read(), {"avatar": {"scale_multiplier": 1.6}})

    def test_deep_merges_into_existing_unrelated_keys(self) -> None:
        # Pretend the user already has a tts override saved.
        self.path.write_text(
            json.dumps({"tts": {"voice": "aiko1.safetensors"}}),
            encoding="utf-8",
        )
        persist_user_overrides({"avatar": {"scale_multiplier": 2.5}}, path=self.path)
        self.assertEqual(
            self._read(),
            {
                "tts": {"voice": "aiko1.safetensors"},
                "avatar": {"scale_multiplier": 2.5},
            },
        )

    def test_deep_merges_into_existing_same_section(self) -> None:
        # An existing avatar block keeps unrelated keys intact when a
        # different field is patched in.
        self.path.write_text(
            json.dumps({"avatar": {"auto_outfit": "day"}}),
            encoding="utf-8",
        )
        persist_user_overrides({"avatar": {"scale_multiplier": 0.8}}, path=self.path)
        self.assertEqual(
            self._read(),
            {"avatar": {"auto_outfit": "day", "scale_multiplier": 0.8}},
        )

    def test_overwrites_scalar_in_same_key(self) -> None:
        self.path.write_text(
            json.dumps({"avatar": {"scale_multiplier": 1.0}}),
            encoding="utf-8",
        )
        persist_user_overrides({"avatar": {"scale_multiplier": 1.75}}, path=self.path)
        self.assertEqual(self._read(), {"avatar": {"scale_multiplier": 1.75}})

    def test_empty_or_invalid_patch_is_noop(self) -> None:
        self.assertFalse(self.path.exists())
        persist_user_overrides({}, path=self.path)
        persist_user_overrides("not a dict", path=self.path)  # type: ignore[arg-type]
        self.assertFalse(self.path.exists())

    def test_invalidates_in_process_cache(self) -> None:
        # Prime the cache through the public read path.
        self.path.write_text(
            json.dumps({"avatar": {"scale_multiplier": 1.0}}),
            encoding="utf-8",
        )
        first = settings_mod._read_config(self.path)
        self.assertEqual(first["avatar"]["scale_multiplier"], 1.0)
        # Persist a new value; the next read must reflect it instead of
        # returning the cached pre-patch dict.
        persist_user_overrides({"avatar": {"scale_multiplier": 3.0}}, path=self.path)
        second = settings_mod._read_config(self.path)
        self.assertEqual(second["avatar"]["scale_multiplier"], 3.0)

    def test_temp_file_is_cleaned_up(self) -> None:
        persist_user_overrides({"avatar": {"scale_multiplier": 2.0}}, path=self.path)
        # No leftover ``user.json.tmp`` from the atomic-rename dance.
        leftover = self.path.with_suffix(self.path.suffix + ".tmp")
        self.assertFalse(leftover.exists())


if __name__ == "__main__":
    unittest.main()
