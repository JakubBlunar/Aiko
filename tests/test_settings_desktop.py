"""Loader-level tests for :class:`app.core.settings.DesktopSettings`.

Mirrors :mod:`tests.test_settings`. Covers default values, the clamps in
``clamp_persona_window_width`` / ``clamp_persona_window_height``, and
graceful handling of legacy configs that predate the desktop block.
"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from app.core import settings as settings_mod
from app.core.settings import (
    DesktopSettings,
    PersonaWindowSettings,
    PERSONA_WINDOW_MAX_HEIGHT,
    PERSONA_WINDOW_MAX_WIDTH,
    PERSONA_WINDOW_MIN_HEIGHT,
    PERSONA_WINDOW_MIN_WIDTH,
    clamp_persona_window_height,
    clamp_persona_window_width,
    load_settings,
)


class DesktopSettingsLoaderTests(unittest.TestCase):
    """``desktop.persona_window`` round-trips through the loader and is
    clamped into the documented range."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.user_json = Path(self._tmp.name) / "user.json"
        patcher = mock.patch.object(
            settings_mod, "USER_CONFIG_PATH", self.user_json,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        self._base_config = json.loads(default_path.read_text(encoding="utf-8"))

    def _write_config(
        self, *, persona_window: dict | None = None, drop_desktop: bool = False
    ) -> Path:
        cfg = copy.deepcopy(self._base_config)
        if drop_desktop:
            cfg.pop("desktop", None)
        elif persona_window is not None:
            cfg.setdefault("desktop", {})["persona_window"] = persona_window
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_dataclass_defaults_match_documented(self) -> None:
        # Sanity-check the dataclass defaults so a fresh install (no
        # config files) still produces sensible values.
        defaults = DesktopSettings()
        self.assertEqual(defaults.persona_window.width, 320)
        self.assertEqual(defaults.persona_window.height, 480)
        self.assertTrue(defaults.persona_window.always_on_top)
        self.assertEqual(PersonaWindowSettings().width, 320)

    def test_default_config_round_trips(self) -> None:
        path = self._write_config()
        result = load_settings(config_path=path)
        self.assertEqual(result.desktop.persona_window.width, 320)
        self.assertEqual(result.desktop.persona_window.height, 480)
        self.assertTrue(result.desktop.persona_window.always_on_top)

    def test_in_range_value_passes_through(self) -> None:
        path = self._write_config(
            persona_window={"width": 400, "height": 600, "always_on_top": False},
        )
        result = load_settings(config_path=path)
        self.assertEqual(result.desktop.persona_window.width, 400)
        self.assertEqual(result.desktop.persona_window.height, 600)
        self.assertFalse(result.desktop.persona_window.always_on_top)

    def test_width_below_min_clamps(self) -> None:
        path = self._write_config(
            persona_window={"width": 50, "height": 480, "always_on_top": True},
        )
        result = load_settings(config_path=path)
        self.assertEqual(result.desktop.persona_window.width, PERSONA_WINDOW_MIN_WIDTH)

    def test_width_above_max_clamps(self) -> None:
        path = self._write_config(
            persona_window={"width": 9999, "height": 480, "always_on_top": True},
        )
        result = load_settings(config_path=path)
        self.assertEqual(result.desktop.persona_window.width, PERSONA_WINDOW_MAX_WIDTH)

    def test_height_below_min_clamps(self) -> None:
        path = self._write_config(
            persona_window={"width": 320, "height": 1, "always_on_top": True},
        )
        result = load_settings(config_path=path)
        self.assertEqual(
            result.desktop.persona_window.height, PERSONA_WINDOW_MIN_HEIGHT,
        )

    def test_height_above_max_clamps(self) -> None:
        path = self._write_config(
            persona_window={"width": 320, "height": 100_000, "always_on_top": True},
        )
        result = load_settings(config_path=path)
        self.assertEqual(
            result.desktop.persona_window.height, PERSONA_WINDOW_MAX_HEIGHT,
        )

    def test_missing_desktop_block_falls_back_to_defaults(self) -> None:
        # A pre-Tauri config file (no ``desktop`` key) must still load
        # and produce the dataclass defaults instead of raising.
        path = self._write_config(drop_desktop=True)
        result = load_settings(config_path=path)
        self.assertEqual(result.desktop.persona_window.width, 320)
        self.assertEqual(result.desktop.persona_window.height, 480)
        self.assertTrue(result.desktop.persona_window.always_on_top)

    def test_garbage_value_falls_back_to_default(self) -> None:
        # A user hand-edits ``user.json`` and stuffs in a non-numeric
        # width by accident. We coerce silently so the app still
        # launches.
        path = self._write_config(
            persona_window={
                "width": "wide",
                "height": None,
                "always_on_top": True,
            },
        )
        result = load_settings(config_path=path)
        self.assertEqual(result.desktop.persona_window.width, 320)
        self.assertEqual(result.desktop.persona_window.height, 480)


class ClampHelperTests(unittest.TestCase):
    """``clamp_persona_window_*`` are pure functions used by both the
    settings loader and ``SessionController.update_desktop_settings``.
    Cover them directly so regressions surface even without the loader."""

    def test_width_clamp_in_range(self) -> None:
        self.assertEqual(clamp_persona_window_width(400), 400)

    def test_width_clamp_min(self) -> None:
        self.assertEqual(clamp_persona_window_width(0), PERSONA_WINDOW_MIN_WIDTH)

    def test_width_clamp_max(self) -> None:
        self.assertEqual(clamp_persona_window_width(99_999), PERSONA_WINDOW_MAX_WIDTH)

    def test_width_clamp_garbage_uses_fallback(self) -> None:
        self.assertEqual(
            clamp_persona_window_width("nope", fallback=400), 400,
        )

    def test_height_clamp_in_range(self) -> None:
        self.assertEqual(clamp_persona_window_height(800), 800)

    def test_height_clamp_min(self) -> None:
        self.assertEqual(
            clamp_persona_window_height(10), PERSONA_WINDOW_MIN_HEIGHT,
        )

    def test_height_clamp_max(self) -> None:
        self.assertEqual(
            clamp_persona_window_height(99_999), PERSONA_WINDOW_MAX_HEIGHT,
        )

    def test_height_clamp_garbage_uses_fallback(self) -> None:
        self.assertEqual(
            clamp_persona_window_height(None, fallback=600), 600,
        )


if __name__ == "__main__":
    unittest.main()
