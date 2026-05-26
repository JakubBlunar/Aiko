"""Loader-level tests for :mod:`app.core.settings`.

The full settings stack is exercised across the rest of the test
suite via the modules that consume it. This file focuses on the
small clamps + defaults that are easy to forget when adding a new
user-tunable knob -- specifically the ``avatar.expressiveness``
slider introduced for the continuous-expressiveness pass.
"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from app.core import settings as settings_mod
from app.core.settings import AvatarSettings, load_settings


class AvatarExpressivenessLoaderTests(unittest.TestCase):
    """``avatar.expressiveness`` round-trips through the loader and
    is clamped into the documented [0.0, 1.5] range."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Pin the user-overrides file at an empty path so the loader
        # only sees what we hand-build below. Otherwise a developer's
        # local ``config/user.json`` could leak into these assertions.
        self.user_json = Path(self._tmp.name) / "user.json"
        patcher = mock.patch.object(
            settings_mod, "USER_CONFIG_PATH", self.user_json,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # Start from the real ``config/default.json`` so we don't have
        # to enumerate every required field. We only mutate the avatar
        # sub-block per test.
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        self._base_config = json.loads(default_path.read_text(encoding="utf-8"))

    def _write_config(self, avatar_extra: dict | None = None) -> Path:
        cfg = copy.deepcopy(self._base_config)
        if avatar_extra is not None:
            cfg["avatar"] = {**cfg.get("avatar", {}), **avatar_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_default_value_round_trips(self) -> None:
        path = self._write_config()
        result = load_settings(config_path=path)
        self.assertAlmostEqual(result.avatar.expressiveness, 1.0)

    def test_dataclass_default_matches_config_default(self) -> None:
        # Belt-and-braces: an ``AvatarSettings()`` constructed without
        # arguments must agree with what the JSON loader produces for
        # an absent ``expressiveness`` key. Otherwise a fresh install
        # without ``user.json`` would pick up a different value than
        # the JSON-driven one.
        self.assertAlmostEqual(AvatarSettings().expressiveness, 1.0)

    def test_value_below_zero_clamps_to_zero(self) -> None:
        path = self._write_config({"expressiveness": -0.5})
        result = load_settings(config_path=path)
        self.assertEqual(result.avatar.expressiveness, 0.0)

    def test_value_above_one_point_five_clamps(self) -> None:
        path = self._write_config({"expressiveness": 9.9})
        result = load_settings(config_path=path)
        self.assertEqual(result.avatar.expressiveness, 1.5)

    def test_missing_key_falls_back_to_default(self) -> None:
        # An older config without ``expressiveness`` (e.g. surviving
        # from before the slider was introduced) must still load and
        # default to ``1.0`` rather than blowing up on KeyError.
        cfg = copy.deepcopy(self._base_config)
        cfg.get("avatar", {}).pop("expressiveness", None)
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        result = load_settings(config_path=path)
        self.assertAlmostEqual(result.avatar.expressiveness, 1.0)

    def test_in_range_value_passes_through_unchanged(self) -> None:
        path = self._write_config({"expressiveness": 0.6})
        result = load_settings(config_path=path)
        self.assertAlmostEqual(result.avatar.expressiveness, 0.6)


if __name__ == "__main__":
    unittest.main()
