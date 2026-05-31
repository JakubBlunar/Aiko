"""Loader-level tests for :mod:`app.core.infra.settings`.

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

from app.core.infra import settings as settings_mod
from app.core.infra.settings import AvatarSettings, load_settings


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


class CuriositySeedSettingsTests(unittest.TestCase):
    """K9: new agent + memory knobs default-load from missing config keys."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.user_json = Path(self._tmp.name) / "user.json"
        patcher = mock.patch.object(
            settings_mod, "USER_CONFIG_PATH", self.user_json,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_config(self, agent_extra: dict | None = None, memory_extra: dict | None = None) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(json.loads(default_path.read_text(encoding="utf-8")))
        # Strip the new K9 keys to verify the defaults kick in.
        for k in (
            "topic_graph_enabled",
            "curiosity_seed_enabled",
            "curiosity_seed_max_active",
            "curiosity_seed_max_per_run",
            "curiosity_seed_min_novelty",
            "curiosity_seed_resolve_threshold",
            "topic_graph_filter_threshold",
        ):
            cfg.get("agent", {}).pop(k, None)
        cfg.get("memory", {}).pop("curiosity_seed_interval_seconds", None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        if memory_extra is not None:
            cfg["memory"] = {**cfg.get("memory", {}), **memory_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_load_when_keys_missing(self) -> None:
        path = self._write_config()
        result = load_settings(config_path=path)
        self.assertTrue(result.agent.topic_graph_enabled)
        self.assertTrue(result.agent.curiosity_seed_enabled)
        self.assertEqual(result.agent.curiosity_seed_max_active, 6)
        self.assertEqual(result.agent.curiosity_seed_max_per_run, 2)
        self.assertAlmostEqual(result.agent.curiosity_seed_min_novelty, 0.85)
        self.assertAlmostEqual(
            result.agent.curiosity_seed_resolve_threshold, 0.50,
        )
        self.assertAlmostEqual(
            result.agent.topic_graph_filter_threshold, 0.65,
        )
        self.assertEqual(result.memory.curiosity_seed_interval_seconds, 3600)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={
                "curiosity_seed_max_active": 12,
                "curiosity_seed_min_novelty": 0.9,
            },
            memory_extra={"curiosity_seed_interval_seconds": 1800},
        )
        result = load_settings(config_path=path)
        self.assertEqual(result.agent.curiosity_seed_max_active, 12)
        self.assertAlmostEqual(result.agent.curiosity_seed_min_novelty, 0.9)
        self.assertEqual(result.memory.curiosity_seed_interval_seconds, 1800)

    def test_clamps_out_of_range_thresholds(self) -> None:
        path = self._write_config(
            agent_extra={
                "curiosity_seed_min_novelty": 99.0,
                "curiosity_seed_resolve_threshold": -1.0,
                "topic_graph_filter_threshold": 1.5,
            },
        )
        result = load_settings(config_path=path)
        self.assertAlmostEqual(result.agent.curiosity_seed_min_novelty, 1.0)
        self.assertAlmostEqual(
            result.agent.curiosity_seed_resolve_threshold, 0.0,
        )
        self.assertAlmostEqual(
            result.agent.topic_graph_filter_threshold, 1.0,
        )


class CallbackDetectorSettingsTests(unittest.TestCase):
    """K22: agent master switch + 6 memory knobs round-trip with clamps."""

    _CALLBACK_AGENT_KEYS = ("callback_detector_enabled",)
    _CALLBACK_MEMORY_KEYS = (
        "callback_age_floor_days",
        "callback_similarity_threshold",
        "callback_max_hits_per_turn",
        "callback_cooldown_hours",
        "callback_salience_bump",
        "callback_revival_bump",
    )

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.user_json = Path(self._tmp.name) / "user.json"
        patcher = mock.patch.object(
            settings_mod, "USER_CONFIG_PATH", self.user_json,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_config(
        self,
        agent_extra: dict | None = None,
        memory_extra: dict | None = None,
        strip_keys: bool = True,
    ) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(
            json.loads(default_path.read_text(encoding="utf-8"))
        )
        if strip_keys:
            for k in self._CALLBACK_AGENT_KEYS:
                cfg.get("agent", {}).pop(k, None)
            for k in self._CALLBACK_MEMORY_KEYS:
                cfg.get("memory", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        if memory_extra is not None:
            cfg["memory"] = {**cfg.get("memory", {}), **memory_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_load_when_keys_missing(self) -> None:
        path = self._write_config()
        result = load_settings(config_path=path)
        self.assertTrue(result.agent.callback_detector_enabled)
        self.assertEqual(result.memory.callback_age_floor_days, 3)
        self.assertAlmostEqual(
            result.memory.callback_similarity_threshold, 0.55,
        )
        self.assertEqual(result.memory.callback_max_hits_per_turn, 3)
        self.assertEqual(result.memory.callback_cooldown_hours, 24)
        self.assertAlmostEqual(result.memory.callback_salience_bump, 0.05)
        self.assertAlmostEqual(result.memory.callback_revival_bump, 0.10)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={"callback_detector_enabled": False},
            memory_extra={
                "callback_age_floor_days": 7,
                "callback_similarity_threshold": 0.70,
                "callback_max_hits_per_turn": 5,
                "callback_cooldown_hours": 48,
                "callback_salience_bump": 0.08,
                "callback_revival_bump": 0.20,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.callback_detector_enabled)
        self.assertEqual(result.memory.callback_age_floor_days, 7)
        self.assertAlmostEqual(
            result.memory.callback_similarity_threshold, 0.70,
        )
        self.assertEqual(result.memory.callback_max_hits_per_turn, 5)
        self.assertEqual(result.memory.callback_cooldown_hours, 48)
        self.assertAlmostEqual(result.memory.callback_salience_bump, 0.08)
        self.assertAlmostEqual(result.memory.callback_revival_bump, 0.20)

    def test_clamps_out_of_range_values(self) -> None:
        # Each numeric knob has a documented floor / ceiling. Verify
        # the parser enforces them so a buggy user.json can't push
        # the detector into a degenerate state.
        path = self._write_config(
            memory_extra={
                "callback_age_floor_days": 0,            # min 1
                "callback_similarity_threshold": 99.0,    # max 1.0
                "callback_max_hits_per_turn": 0,         # min 1
                "callback_cooldown_hours": 0,            # min 1
                "callback_salience_bump": -5.0,          # min 0.0
                "callback_revival_bump": 2.0,            # max 1.0
            },
        )
        result = load_settings(config_path=path)
        self.assertEqual(result.memory.callback_age_floor_days, 1)
        self.assertAlmostEqual(
            result.memory.callback_similarity_threshold, 1.0,
        )
        self.assertEqual(result.memory.callback_max_hits_per_turn, 1)
        self.assertEqual(result.memory.callback_cooldown_hours, 1)
        self.assertAlmostEqual(result.memory.callback_salience_bump, 0.0)
        self.assertAlmostEqual(result.memory.callback_revival_bump, 1.0)


class CalibrationDetectorSettingsTests(unittest.TestCase):
    """K20: agent master switch + 7 memory knobs round-trip with clamps."""

    _CAL_AGENT_KEYS = ("calibration_detection_enabled",)
    _CAL_MEMORY_KEYS = (
        "calibration_baseline",
        "calibration_global_low_threshold",
        "calibration_topic_low_threshold",
        "calibration_half_life_days",
        "calibration_topic_merge_threshold",
        "calibration_softening_threshold",
        "calibration_max_topic_slots",
    )

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.user_json = Path(self._tmp.name) / "user.json"
        patcher = mock.patch.object(
            settings_mod, "USER_CONFIG_PATH", self.user_json,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_config(
        self,
        agent_extra: dict | None = None,
        memory_extra: dict | None = None,
        strip_keys: bool = True,
    ) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(
            json.loads(default_path.read_text(encoding="utf-8"))
        )
        if strip_keys:
            for k in self._CAL_AGENT_KEYS:
                cfg.get("agent", {}).pop(k, None)
            for k in self._CAL_MEMORY_KEYS:
                cfg.get("memory", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        if memory_extra is not None:
            cfg["memory"] = {**cfg.get("memory", {}), **memory_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_load_when_keys_missing(self) -> None:
        path = self._write_config()
        result = load_settings(config_path=path)
        self.assertTrue(result.agent.calibration_detection_enabled)
        self.assertAlmostEqual(result.memory.calibration_baseline, 0.80)
        self.assertAlmostEqual(
            result.memory.calibration_global_low_threshold, 0.55,
        )
        self.assertAlmostEqual(
            result.memory.calibration_topic_low_threshold, 0.50,
        )
        self.assertAlmostEqual(result.memory.calibration_half_life_days, 5.0)
        self.assertAlmostEqual(
            result.memory.calibration_topic_merge_threshold, 0.78,
        )
        self.assertAlmostEqual(
            result.memory.calibration_softening_threshold, 0.70,
        )
        self.assertEqual(result.memory.calibration_max_topic_slots, 8)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={"calibration_detection_enabled": False},
            memory_extra={
                "calibration_baseline": 0.65,
                "calibration_global_low_threshold": 0.40,
                "calibration_topic_low_threshold": 0.35,
                "calibration_half_life_days": 14.0,
                "calibration_topic_merge_threshold": 0.85,
                "calibration_softening_threshold": 0.60,
                "calibration_max_topic_slots": 12,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.calibration_detection_enabled)
        self.assertAlmostEqual(result.memory.calibration_baseline, 0.65)
        self.assertAlmostEqual(
            result.memory.calibration_global_low_threshold, 0.40,
        )
        self.assertAlmostEqual(
            result.memory.calibration_topic_low_threshold, 0.35,
        )
        self.assertAlmostEqual(
            result.memory.calibration_half_life_days, 14.0,
        )
        self.assertAlmostEqual(
            result.memory.calibration_topic_merge_threshold, 0.85,
        )
        self.assertAlmostEqual(
            result.memory.calibration_softening_threshold, 0.60,
        )
        self.assertEqual(result.memory.calibration_max_topic_slots, 12)

    def test_clamps_out_of_range_values(self) -> None:
        # Each numeric knob has a documented floor / ceiling. Verify
        # the parser enforces them so a buggy user.json can't push
        # the detector into a degenerate state.
        path = self._write_config(
            memory_extra={
                "calibration_baseline": 5.0,                    # max 1.0
                "calibration_global_low_threshold": -0.5,       # min 0.0
                "calibration_topic_low_threshold": 9.0,         # max 1.0
                "calibration_half_life_days": -10.0,            # min 0.1
                "calibration_topic_merge_threshold": -1.0,      # min 0.0
                "calibration_softening_threshold": 50.0,        # max 1.0
                "calibration_max_topic_slots": 0,               # min 1
            },
        )
        result = load_settings(config_path=path)
        self.assertAlmostEqual(result.memory.calibration_baseline, 1.0)
        self.assertAlmostEqual(
            result.memory.calibration_global_low_threshold, 0.0,
        )
        self.assertAlmostEqual(
            result.memory.calibration_topic_low_threshold, 1.0,
        )
        self.assertAlmostEqual(
            result.memory.calibration_half_life_days, 0.1,
        )
        self.assertAlmostEqual(
            result.memory.calibration_topic_merge_threshold, 0.0,
        )
        self.assertAlmostEqual(
            result.memory.calibration_softening_threshold, 1.0,
        )
        self.assertEqual(result.memory.calibration_max_topic_slots, 1)


class SensoryAnchorSettingsTests(unittest.TestCase):
    """K24: agent master switch + 4 memory knobs round-trip with clamps."""

    _SA_AGENT_KEYS = ("sensory_anchor_enabled",)
    _SA_MEMORY_KEYS = (
        "sensory_anchor_min_turn_gap",
        "sensory_anchor_probability_scale",
        "sensory_anchor_max_recent_items",
        "sensory_anchor_max_window_items",
    )

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.user_json = Path(self._tmp.name) / "user.json"
        patcher = mock.patch.object(
            settings_mod, "USER_CONFIG_PATH", self.user_json,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_config(
        self,
        agent_extra: dict | None = None,
        memory_extra: dict | None = None,
        strip_keys: bool = True,
    ) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(
            json.loads(default_path.read_text(encoding="utf-8"))
        )
        if strip_keys:
            for k in self._SA_AGENT_KEYS:
                cfg.get("agent", {}).pop(k, None)
            for k in self._SA_MEMORY_KEYS:
                cfg.get("memory", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        if memory_extra is not None:
            cfg["memory"] = {**cfg.get("memory", {}), **memory_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_load_when_keys_missing(self) -> None:
        path = self._write_config()
        result = load_settings(config_path=path)
        self.assertTrue(result.agent.sensory_anchor_enabled)
        self.assertEqual(result.memory.sensory_anchor_min_turn_gap, 4)
        self.assertAlmostEqual(
            result.memory.sensory_anchor_probability_scale, 1.0,
        )
        self.assertEqual(result.memory.sensory_anchor_max_recent_items, 4)
        self.assertEqual(result.memory.sensory_anchor_max_window_items, 6)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={"sensory_anchor_enabled": False},
            memory_extra={
                "sensory_anchor_min_turn_gap": 12,
                "sensory_anchor_probability_scale": 0.5,
                "sensory_anchor_max_recent_items": 8,
                "sensory_anchor_max_window_items": 24,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.sensory_anchor_enabled)
        self.assertEqual(result.memory.sensory_anchor_min_turn_gap, 12)
        self.assertAlmostEqual(
            result.memory.sensory_anchor_probability_scale, 0.5,
        )
        self.assertEqual(result.memory.sensory_anchor_max_recent_items, 8)
        self.assertEqual(result.memory.sensory_anchor_max_window_items, 24)

    def test_clamps_out_of_range_values(self) -> None:
        # ``probability_scale`` is the only knob with both a floor
        # (0.0) and a ceiling (2.0); the three int knobs have a
        # min-1 floor and no ceiling. Verify the parser holds.
        path = self._write_config(
            memory_extra={
                "sensory_anchor_min_turn_gap": 0,             # min 1
                "sensory_anchor_probability_scale": -1.0,     # min 0.0
                "sensory_anchor_max_recent_items": -5,        # min 1
                "sensory_anchor_max_window_items": 0,         # min 1
            },
        )
        result = load_settings(config_path=path)
        self.assertEqual(result.memory.sensory_anchor_min_turn_gap, 1)
        self.assertAlmostEqual(
            result.memory.sensory_anchor_probability_scale, 0.0,
        )
        self.assertEqual(result.memory.sensory_anchor_max_recent_items, 1)
        self.assertEqual(result.memory.sensory_anchor_max_window_items, 1)

        # Now hammer the ceiling on the probability scale.
        path = self._write_config(
            memory_extra={
                "sensory_anchor_probability_scale": 999.0,    # max 2.0
            },
        )
        result = load_settings(config_path=path)
        self.assertAlmostEqual(
            result.memory.sensory_anchor_probability_scale, 2.0,
        )


class MisattunementSettingsTests(unittest.TestCase):
    """K23: agent master switch + 4 threshold knobs round-trip with clamps."""

    _M_AGENT_KEYS = (
        "misattunement_detection_enabled",
        "misattunement_shrink_min_prev_words",
        "misattunement_shrink_max_user_words",
        "misattunement_pivot_max_user_words",
        "misattunement_cooldown_turns",
    )

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.user_json = Path(self._tmp.name) / "user.json"
        patcher = mock.patch.object(
            settings_mod, "USER_CONFIG_PATH", self.user_json,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_config(
        self,
        agent_extra: dict | None = None,
        strip_keys: bool = True,
    ) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(
            json.loads(default_path.read_text(encoding="utf-8"))
        )
        if strip_keys:
            for k in self._M_AGENT_KEYS:
                cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_load_when_keys_missing(self) -> None:
        path = self._write_config()
        result = load_settings(config_path=path)
        self.assertTrue(result.agent.misattunement_detection_enabled)
        self.assertEqual(result.agent.misattunement_shrink_min_prev_words, 30)
        self.assertEqual(result.agent.misattunement_shrink_max_user_words, 8)
        self.assertEqual(result.agent.misattunement_pivot_max_user_words, 8)
        self.assertEqual(result.agent.misattunement_cooldown_turns, 3)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={
                "misattunement_detection_enabled": False,
                "misattunement_shrink_min_prev_words": 50,
                "misattunement_shrink_max_user_words": 5,
                "misattunement_pivot_max_user_words": 4,
                "misattunement_cooldown_turns": 5,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.misattunement_detection_enabled)
        self.assertEqual(result.agent.misattunement_shrink_min_prev_words, 50)
        self.assertEqual(result.agent.misattunement_shrink_max_user_words, 5)
        self.assertEqual(result.agent.misattunement_pivot_max_user_words, 4)
        self.assertEqual(result.agent.misattunement_cooldown_turns, 5)

    def test_clamps_negative_to_zero(self) -> None:
        # All four int knobs have a ``max(0, int(...))`` floor; a
        # negative value clamps to 0 (which effectively disables
        # that gate -- shrink with prev_words >= 0 always satisfies
        # the floor, but ``this_user_words <= 0`` is itself blocked
        # by the ``user_words <= 0`` short-circuit in detect()).
        path = self._write_config(
            agent_extra={
                "misattunement_shrink_min_prev_words": -10,
                "misattunement_shrink_max_user_words": -1,
                "misattunement_pivot_max_user_words": -1,
                "misattunement_cooldown_turns": -7,
            },
        )
        result = load_settings(config_path=path)
        self.assertEqual(result.agent.misattunement_shrink_min_prev_words, 0)
        self.assertEqual(result.agent.misattunement_shrink_max_user_words, 0)
        self.assertEqual(result.agent.misattunement_pivot_max_user_words, 0)
        self.assertEqual(result.agent.misattunement_cooldown_turns, 0)


class ConfidenceDecaySettingsTests(unittest.TestCase):
    """K25: agent master switch + 3 memory knobs round-trip with clamps."""

    _CD_AGENT_KEYS = ("confidence_time_decay_enabled",)
    _CD_MEMORY_KEYS = (
        "confidence_decay_horizon_days",
        "confidence_decay_floor",
        "confidence_decay_distant_threshold",
    )

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.user_json = Path(self._tmp.name) / "user.json"
        patcher = mock.patch.object(
            settings_mod, "USER_CONFIG_PATH", self.user_json,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_config(
        self,
        agent_extra: dict | None = None,
        memory_extra: dict | None = None,
        strip_keys: bool = True,
    ) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(
            json.loads(default_path.read_text(encoding="utf-8"))
        )
        if strip_keys:
            for k in self._CD_AGENT_KEYS:
                cfg.get("agent", {}).pop(k, None)
            for k in self._CD_MEMORY_KEYS:
                cfg.get("memory", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        if memory_extra is not None:
            cfg["memory"] = {**cfg.get("memory", {}), **memory_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_load_when_keys_missing(self) -> None:
        path = self._write_config()
        result = load_settings(config_path=path)
        self.assertTrue(result.agent.confidence_time_decay_enabled)
        self.assertEqual(result.memory.confidence_decay_horizon_days, 365)
        self.assertAlmostEqual(result.memory.confidence_decay_floor, 0.3)
        self.assertAlmostEqual(
            result.memory.confidence_decay_distant_threshold, 0.5,
        )

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={"confidence_time_decay_enabled": False},
            memory_extra={
                "confidence_decay_horizon_days": 90,
                "confidence_decay_floor": 0.1,
                "confidence_decay_distant_threshold": 0.4,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.confidence_time_decay_enabled)
        self.assertEqual(result.memory.confidence_decay_horizon_days, 90)
        self.assertAlmostEqual(result.memory.confidence_decay_floor, 0.1)
        self.assertAlmostEqual(
            result.memory.confidence_decay_distant_threshold, 0.4,
        )

    def test_horizon_days_clamped_to_one(self) -> None:
        # horizon_days <= 0 would zero-divide in the helper. Parser
        # floors at 1.
        path = self._write_config(
            memory_extra={
                "confidence_decay_horizon_days": 0,
            },
        )
        result = load_settings(config_path=path)
        self.assertEqual(result.memory.confidence_decay_horizon_days, 1)

        # Negative inputs clamp to 1 too.
        path = self._write_config(
            memory_extra={
                "confidence_decay_horizon_days": -50,
            },
        )
        result = load_settings(config_path=path)
        self.assertEqual(result.memory.confidence_decay_horizon_days, 1)

    def test_floor_and_threshold_clamp_unit_interval(self) -> None:
        # Both float knobs sit in [0, 1] with the standard parser
        # clamp pattern.
        path = self._write_config(
            memory_extra={
                "confidence_decay_floor": -0.5,
                "confidence_decay_distant_threshold": -0.2,
            },
        )
        result = load_settings(config_path=path)
        self.assertAlmostEqual(result.memory.confidence_decay_floor, 0.0)
        self.assertAlmostEqual(
            result.memory.confidence_decay_distant_threshold, 0.0,
        )

        path = self._write_config(
            memory_extra={
                "confidence_decay_floor": 5.0,
                "confidence_decay_distant_threshold": 99.0,
            },
        )
        result = load_settings(config_path=path)
        self.assertAlmostEqual(result.memory.confidence_decay_floor, 1.0)
        self.assertAlmostEqual(
            result.memory.confidence_decay_distant_threshold, 1.0,
        )


class OpinionInjectionSettingsTests(unittest.TestCase):
    """K29: 2 agent flags + 6 memory knobs round-trip with clamps."""

    _OI_AGENT_KEYS = (
        "opinion_injection_enabled",
        "opinion_injection_require_definite",
    )
    _OI_MEMORY_KEYS = (
        "opinion_injection_min_cosine",
        "opinion_injection_min_user_words",
        "opinion_injection_cooldown_turns",
        "opinion_injection_per_session_cap",
        "opinion_injection_per_hour_cap",
        "opinion_injection_per_day_cap",
    )

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.user_json = Path(self._tmp.name) / "user.json"
        patcher = mock.patch.object(
            settings_mod, "USER_CONFIG_PATH", self.user_json,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_config(
        self,
        agent_extra: dict | None = None,
        memory_extra: dict | None = None,
        strip_keys: bool = True,
    ) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(
            json.loads(default_path.read_text(encoding="utf-8"))
        )
        if strip_keys:
            for k in self._OI_AGENT_KEYS:
                cfg.get("agent", {}).pop(k, None)
            for k in self._OI_MEMORY_KEYS:
                cfg.get("memory", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        if memory_extra is not None:
            cfg["memory"] = {**cfg.get("memory", {}), **memory_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_load_when_keys_missing(self) -> None:
        path = self._write_config()
        result = load_settings(config_path=path)
        self.assertTrue(result.agent.opinion_injection_enabled)
        self.assertFalse(result.agent.opinion_injection_require_definite)
        self.assertAlmostEqual(
            result.memory.opinion_injection_min_cosine, 0.55,
        )
        self.assertEqual(result.memory.opinion_injection_min_user_words, 4)
        self.assertEqual(result.memory.opinion_injection_cooldown_turns, 5)
        self.assertEqual(result.memory.opinion_injection_per_session_cap, 3)
        self.assertEqual(result.memory.opinion_injection_per_hour_cap, 6)
        self.assertEqual(result.memory.opinion_injection_per_day_cap, 30)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={
                "opinion_injection_enabled": False,
                "opinion_injection_require_definite": True,
            },
            memory_extra={
                "opinion_injection_min_cosine": 0.70,
                "opinion_injection_min_user_words": 6,
                "opinion_injection_cooldown_turns": 8,
                "opinion_injection_per_session_cap": 1,
                "opinion_injection_per_hour_cap": 12,
                "opinion_injection_per_day_cap": 50,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.opinion_injection_enabled)
        self.assertTrue(result.agent.opinion_injection_require_definite)
        self.assertAlmostEqual(
            result.memory.opinion_injection_min_cosine, 0.70,
        )
        self.assertEqual(result.memory.opinion_injection_min_user_words, 6)
        self.assertEqual(result.memory.opinion_injection_cooldown_turns, 8)
        self.assertEqual(result.memory.opinion_injection_per_session_cap, 1)
        self.assertEqual(result.memory.opinion_injection_per_hour_cap, 12)
        self.assertEqual(result.memory.opinion_injection_per_day_cap, 50)

    def test_min_cosine_clamps_unit_interval(self) -> None:
        path = self._write_config(
            memory_extra={"opinion_injection_min_cosine": -0.4},
        )
        result = load_settings(config_path=path)
        self.assertAlmostEqual(
            result.memory.opinion_injection_min_cosine, 0.0,
        )
        path = self._write_config(
            memory_extra={"opinion_injection_min_cosine": 5.0},
        )
        result = load_settings(config_path=path)
        self.assertAlmostEqual(
            result.memory.opinion_injection_min_cosine, 1.0,
        )

    def test_integer_knobs_clamp_negative_to_zero(self) -> None:
        # All five integer knobs floor at 0; setting them all to
        # negative inputs effectively disables the corresponding
        # gate (per_session_cap=0 means "fire unboundedly per
        # session" by the provider's interpretation; the other
        # knobs degrade to similarly-permissive states).
        path = self._write_config(
            memory_extra={
                "opinion_injection_min_user_words": -3,
                "opinion_injection_cooldown_turns": -10,
                "opinion_injection_per_session_cap": -1,
                "opinion_injection_per_hour_cap": -5,
                "opinion_injection_per_day_cap": -50,
            },
        )
        result = load_settings(config_path=path)
        self.assertEqual(result.memory.opinion_injection_min_user_words, 0)
        self.assertEqual(result.memory.opinion_injection_cooldown_turns, 0)
        self.assertEqual(result.memory.opinion_injection_per_session_cap, 0)
        self.assertEqual(result.memory.opinion_injection_per_hour_cap, 0)
        self.assertEqual(result.memory.opinion_injection_per_day_cap, 0)


if __name__ == "__main__":
    unittest.main()
