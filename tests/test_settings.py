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


class ForwardCuriositySettingsTests(unittest.TestCase):
    """K34: agent master switch + memory cadence/cap knobs round-trip + clamps."""

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
        self, agent_extra: dict | None = None, memory_extra: dict | None = None,
    ) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(json.loads(default_path.read_text(encoding="utf-8")))
        cfg.get("agent", {}).pop("forward_curiosity_enabled", None)
        for k in (
            "forward_curiosity_interval_seconds",
            "forward_curiosity_cooldown_seconds",
            "forward_curiosity_daily_cap",
            "forward_curiosity_min_gap_hours",
            "forward_curiosity_journal_max",
        ):
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
        self.assertTrue(result.agent.forward_curiosity_enabled)
        self.assertEqual(
            result.memory.forward_curiosity_interval_seconds, 1800,
        )
        self.assertEqual(
            result.memory.forward_curiosity_cooldown_seconds, 3600,
        )
        self.assertEqual(result.memory.forward_curiosity_daily_cap, 4)
        self.assertAlmostEqual(
            result.memory.forward_curiosity_min_gap_hours, 4.0,
        )
        self.assertEqual(result.memory.forward_curiosity_journal_max, 8)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={"forward_curiosity_enabled": False},
            memory_extra={
                "forward_curiosity_interval_seconds": 900,
                "forward_curiosity_daily_cap": 2,
                "forward_curiosity_min_gap_hours": 6.0,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.forward_curiosity_enabled)
        self.assertEqual(
            result.memory.forward_curiosity_interval_seconds, 900,
        )
        self.assertEqual(result.memory.forward_curiosity_daily_cap, 2)
        self.assertAlmostEqual(
            result.memory.forward_curiosity_min_gap_hours, 6.0,
        )

    def test_clamps_out_of_range_values(self) -> None:
        path = self._write_config(
            memory_extra={
                "forward_curiosity_interval_seconds": 1,  # floor 30
                "forward_curiosity_min_gap_hours": -5.0,  # floor 0.0
                "forward_curiosity_journal_max": 0,  # floor 1
            },
        )
        result = load_settings(config_path=path)
        self.assertEqual(
            result.memory.forward_curiosity_interval_seconds, 30,
        )
        self.assertAlmostEqual(
            result.memory.forward_curiosity_min_gap_hours, 0.0,
        )
        self.assertEqual(result.memory.forward_curiosity_journal_max, 1)


class PromiseFollowthroughSettingsTests(unittest.TestCase):
    """K43: agent master switch + memory cadence/age knobs round-trip + clamps."""

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
        self, agent_extra: dict | None = None, memory_extra: dict | None = None,
    ) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(json.loads(default_path.read_text(encoding="utf-8")))
        cfg.get("agent", {}).pop("promise_followthrough_enabled", None)
        for k in (
            "promise_followthrough_interval_seconds",
            "promise_followthrough_min_age_hours",
            "promise_followthrough_cooldown_hours",
            "promise_followthrough_drop_after_days",
            "promise_fulfil_min_overlap",
        ):
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
        self.assertTrue(result.agent.promise_followthrough_enabled)
        self.assertEqual(
            result.memory.promise_followthrough_interval_seconds, 1800,
        )
        self.assertAlmostEqual(
            result.memory.promise_followthrough_min_age_hours, 4.0,
        )
        self.assertAlmostEqual(
            result.memory.promise_followthrough_cooldown_hours, 6.0,
        )
        self.assertAlmostEqual(
            result.memory.promise_followthrough_drop_after_days, 14.0,
        )
        self.assertEqual(result.memory.promise_fulfil_min_overlap, 3)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={"promise_followthrough_enabled": False},
            memory_extra={
                "promise_followthrough_interval_seconds": 600,
                "promise_followthrough_min_age_hours": 1.0,
                "promise_followthrough_cooldown_hours": 2.5,
                "promise_followthrough_drop_after_days": 7.0,
                "promise_fulfil_min_overlap": 4,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.promise_followthrough_enabled)
        self.assertEqual(
            result.memory.promise_followthrough_interval_seconds, 600,
        )
        self.assertAlmostEqual(
            result.memory.promise_followthrough_min_age_hours, 1.0,
        )
        self.assertAlmostEqual(
            result.memory.promise_followthrough_cooldown_hours, 2.5,
        )
        self.assertAlmostEqual(
            result.memory.promise_followthrough_drop_after_days, 7.0,
        )
        self.assertEqual(result.memory.promise_fulfil_min_overlap, 4)

    def test_clamps_out_of_range_values(self) -> None:
        path = self._write_config(
            memory_extra={
                "promise_followthrough_interval_seconds": 1,  # floor 30
                "promise_followthrough_min_age_hours": -2.0,  # floor 0.0
                "promise_followthrough_cooldown_hours": -1.0,  # floor 0.0
                "promise_followthrough_drop_after_days": 0.1,  # floor 1.0
                "promise_fulfil_min_overlap": 0,  # floor 1
            },
        )
        result = load_settings(config_path=path)
        self.assertEqual(
            result.memory.promise_followthrough_interval_seconds, 30,
        )
        self.assertAlmostEqual(
            result.memory.promise_followthrough_min_age_hours, 0.0,
        )
        self.assertAlmostEqual(
            result.memory.promise_followthrough_cooldown_hours, 0.0,
        )
        self.assertAlmostEqual(
            result.memory.promise_followthrough_drop_after_days, 1.0,
        )
        self.assertEqual(result.memory.promise_fulfil_min_overlap, 1)


class SelfCorrectionSettingsTests(unittest.TestCase):
    """K38: agent master switch + memory threshold knobs round-trip + clamps."""

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
        self, agent_extra: dict | None = None, memory_extra: dict | None = None,
    ) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(json.loads(default_path.read_text(encoding="utf-8")))
        cfg.get("agent", {}).pop("self_correction_enabled", None)
        for k in (
            "self_correction_min_confidence",
            "self_correction_min_overlap",
            "self_correction_max_candidates",
            "self_correction_cooldown_turns",
        ):
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
        self.assertTrue(result.agent.self_correction_enabled)
        self.assertAlmostEqual(
            result.memory.self_correction_min_confidence, 0.6,
        )
        self.assertEqual(result.memory.self_correction_min_overlap, 2)
        self.assertEqual(result.memory.self_correction_max_candidates, 50)
        self.assertEqual(result.memory.self_correction_cooldown_turns, 3)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={"self_correction_enabled": False},
            memory_extra={
                "self_correction_min_confidence": 0.8,
                "self_correction_min_overlap": 3,
                "self_correction_max_candidates": 20,
                "self_correction_cooldown_turns": 5,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.self_correction_enabled)
        self.assertAlmostEqual(
            result.memory.self_correction_min_confidence, 0.8,
        )
        self.assertEqual(result.memory.self_correction_min_overlap, 3)
        self.assertEqual(result.memory.self_correction_max_candidates, 20)
        self.assertEqual(result.memory.self_correction_cooldown_turns, 5)

    def test_clamps_out_of_range_values(self) -> None:
        path = self._write_config(
            memory_extra={
                "self_correction_min_confidence": 2.5,  # ceil 1.0
                "self_correction_min_overlap": 0,  # floor 1
                "self_correction_max_candidates": 0,  # floor 1
                "self_correction_cooldown_turns": -3,  # floor 0
            },
        )
        result = load_settings(config_path=path)
        self.assertAlmostEqual(
            result.memory.self_correction_min_confidence, 1.0,
        )
        self.assertEqual(result.memory.self_correction_min_overlap, 1)
        self.assertEqual(result.memory.self_correction_max_candidates, 1)
        self.assertEqual(result.memory.self_correction_cooldown_turns, 0)

    def test_negative_confidence_clamps_to_zero(self) -> None:
        path = self._write_config(
            memory_extra={"self_correction_min_confidence": -1.0},
        )
        result = load_settings(config_path=path)
        self.assertAlmostEqual(
            result.memory.self_correction_min_confidence, 0.0,
        )


class MoodInertiaSettingsTests(unittest.TestCase):
    """K45: agent master switch + memory knobs + avatar damping flag."""

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
        avatar_extra: dict | None = None,
    ) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(json.loads(default_path.read_text(encoding="utf-8")))
        cfg.get("agent", {}).pop("mood_inertia_enabled", None)
        for k in (
            "mood_inertia_mismatch_threshold",
            "mood_inertia_cooldown_turns",
        ):
            cfg.get("memory", {}).pop(k, None)
        cfg.get("avatar", {}).pop("mood_inertia_damping", None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        if memory_extra is not None:
            cfg["memory"] = {**cfg.get("memory", {}), **memory_extra}
        if avatar_extra is not None:
            cfg["avatar"] = {**cfg.get("avatar", {}), **avatar_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_load_when_keys_missing(self) -> None:
        path = self._write_config()
        result = load_settings(config_path=path)
        self.assertTrue(result.agent.mood_inertia_enabled)
        self.assertAlmostEqual(
            result.memory.mood_inertia_mismatch_threshold, 0.45,
        )
        self.assertEqual(result.memory.mood_inertia_cooldown_turns, 3)
        self.assertTrue(result.avatar.mood_inertia_damping)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={"mood_inertia_enabled": False},
            memory_extra={
                "mood_inertia_mismatch_threshold": 0.6,
                "mood_inertia_cooldown_turns": 5,
            },
            avatar_extra={"mood_inertia_damping": False},
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.mood_inertia_enabled)
        self.assertAlmostEqual(
            result.memory.mood_inertia_mismatch_threshold, 0.6,
        )
        self.assertEqual(result.memory.mood_inertia_cooldown_turns, 5)
        self.assertFalse(result.avatar.mood_inertia_damping)

    def test_clamps_out_of_range_values(self) -> None:
        path = self._write_config(
            memory_extra={
                "mood_inertia_mismatch_threshold": 0.0,  # floor 0.1
                "mood_inertia_cooldown_turns": -2,  # floor 0
            },
        )
        result = load_settings(config_path=path)
        self.assertAlmostEqual(
            result.memory.mood_inertia_mismatch_threshold, 0.1,
        )
        self.assertEqual(result.memory.mood_inertia_cooldown_turns, 0)


class CueRegisterRotationSettingsTests(unittest.TestCase):
    """K51: agent master switch for cue-register rotation."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.user_json = Path(self._tmp.name) / "user.json"
        patcher = mock.patch.object(
            settings_mod, "USER_CONFIG_PATH", self.user_json,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_config(self, agent_extra: dict | None = None) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(json.loads(default_path.read_text(encoding="utf-8")))
        cfg.get("agent", {}).pop("cue_register_rotation_enabled", None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_default_on_when_key_missing(self) -> None:
        path = self._write_config()
        result = load_settings(config_path=path)
        self.assertTrue(result.agent.cue_register_rotation_enabled)

    def test_override_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={"cue_register_rotation_enabled": False},
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.cue_register_rotation_enabled)


class ConsolidationSettingsTests(unittest.TestCase):
    """K35: agent master switch + caps + memory knobs round-trip + clamps."""

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
        self, agent_extra: dict | None = None, memory_extra: dict | None = None,
    ) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(json.loads(default_path.read_text(encoding="utf-8")))
        for k in (
            "memory_consolidation_enabled",
            "memory_consolidation_per_hour_cap",
            "memory_consolidation_per_day_cap",
        ):
            cfg.get("agent", {}).pop(k, None)
        for k in (
            "consolidation_interval_seconds",
            "consolidation_lookback_days",
            "consolidation_similarity_threshold",
            "consolidation_max_corpus",
            "consolidation_max_clusters_per_run",
            "consolidation_min_cluster_size",
        ):
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
        self.assertTrue(result.agent.memory_consolidation_enabled)
        self.assertEqual(result.agent.memory_consolidation_per_hour_cap, 6)
        self.assertEqual(result.agent.memory_consolidation_per_day_cap, 30)
        self.assertEqual(result.memory.consolidation_interval_seconds, 21600)
        self.assertEqual(result.memory.consolidation_lookback_days, 30)
        self.assertAlmostEqual(
            result.memory.consolidation_similarity_threshold, 0.90,
        )
        self.assertEqual(result.memory.consolidation_max_corpus, 1000)
        self.assertEqual(result.memory.consolidation_max_clusters_per_run, 20)
        self.assertEqual(result.memory.consolidation_min_cluster_size, 2)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={
                "memory_consolidation_enabled": False,
                "memory_consolidation_per_day_cap": 10,
            },
            memory_extra={
                "consolidation_interval_seconds": 3600,
                "consolidation_similarity_threshold": 0.95,
                "consolidation_max_clusters_per_run": 5,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.memory_consolidation_enabled)
        self.assertEqual(result.agent.memory_consolidation_per_day_cap, 10)
        self.assertEqual(result.memory.consolidation_interval_seconds, 3600)
        self.assertAlmostEqual(
            result.memory.consolidation_similarity_threshold, 0.95,
        )
        self.assertEqual(result.memory.consolidation_max_clusters_per_run, 5)

    def test_clamps_out_of_range_values(self) -> None:
        path = self._write_config(
            memory_extra={
                "consolidation_interval_seconds": 1,  # floor 60
                "consolidation_similarity_threshold": 9.0,  # cap 1.0
                "consolidation_min_cluster_size": 0,  # floor 2
                "consolidation_max_corpus": 1,  # floor 10
            },
        )
        result = load_settings(config_path=path)
        self.assertEqual(result.memory.consolidation_interval_seconds, 60)
        self.assertAlmostEqual(
            result.memory.consolidation_similarity_threshold, 1.0,
        )
        self.assertEqual(result.memory.consolidation_min_cluster_size, 2)
        self.assertEqual(result.memory.consolidation_max_corpus, 10)


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


class TurningOverSettingsTests(unittest.TestCase):
    """K28: 1 agent flag + 5 memory knobs round-trip with clamps."""

    _TO_AGENT_KEYS = ("turning_over_enabled",)
    _TO_MEMORY_KEYS = (
        "turning_over_min_gap_minutes",
        "turning_over_min_age_hours",
        "turning_over_max_age_hours",
        "turning_over_min_topical_similarity",
        "turning_over_recent_msgs_window",
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
            for k in self._TO_AGENT_KEYS:
                cfg.get("agent", {}).pop(k, None)
            for k in self._TO_MEMORY_KEYS:
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
        self.assertTrue(result.agent.turning_over_enabled)
        self.assertAlmostEqual(
            result.memory.turning_over_min_gap_minutes, 90.0,
        )
        self.assertAlmostEqual(
            result.memory.turning_over_min_age_hours, 24.0,
        )
        self.assertAlmostEqual(
            result.memory.turning_over_max_age_hours, 72.0,
        )
        self.assertAlmostEqual(
            result.memory.turning_over_min_topical_similarity, 0.30,
        )
        self.assertEqual(result.memory.turning_over_recent_msgs_window, 12)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={"turning_over_enabled": False},
            memory_extra={
                "turning_over_min_gap_minutes": 120.0,
                "turning_over_min_age_hours": 12.0,
                "turning_over_max_age_hours": 48.0,
                "turning_over_min_topical_similarity": 0.50,
                "turning_over_recent_msgs_window": 6,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.turning_over_enabled)
        self.assertAlmostEqual(
            result.memory.turning_over_min_gap_minutes, 120.0,
        )
        self.assertAlmostEqual(
            result.memory.turning_over_min_age_hours, 12.0,
        )
        self.assertAlmostEqual(
            result.memory.turning_over_max_age_hours, 48.0,
        )
        self.assertAlmostEqual(
            result.memory.turning_over_min_topical_similarity, 0.50,
        )
        self.assertEqual(result.memory.turning_over_recent_msgs_window, 6)

    def test_min_gap_minutes_clamps_to_floor(self) -> None:
        path = self._write_config(
            memory_extra={"turning_over_min_gap_minutes": 0.1},
        )
        result = load_settings(config_path=path)
        # Floor is 5 minutes; lower values clamp up.
        self.assertAlmostEqual(
            result.memory.turning_over_min_gap_minutes, 5.0,
        )

    def test_min_age_hours_clamps_to_floor(self) -> None:
        path = self._write_config(
            memory_extra={"turning_over_min_age_hours": 0.0},
        )
        result = load_settings(config_path=path)
        # Floor is 1 hour.
        self.assertAlmostEqual(
            result.memory.turning_over_min_age_hours, 1.0,
        )

    def test_max_age_hours_clamps_above_min_plus_one(self) -> None:
        # Hostile config: max <= min. Parser clamps max to min + 1
        # so the picker window is always non-empty.
        path = self._write_config(
            memory_extra={
                "turning_over_min_age_hours": 24.0,
                "turning_over_max_age_hours": 10.0,
            },
        )
        result = load_settings(config_path=path)
        self.assertAlmostEqual(
            result.memory.turning_over_min_age_hours, 24.0,
        )
        self.assertAlmostEqual(
            result.memory.turning_over_max_age_hours, 25.0,
        )

    def test_min_topical_similarity_clamps_unit_interval(self) -> None:
        path = self._write_config(
            memory_extra={"turning_over_min_topical_similarity": -0.4},
        )
        result = load_settings(config_path=path)
        self.assertAlmostEqual(
            result.memory.turning_over_min_topical_similarity, 0.0,
        )
        path = self._write_config(
            memory_extra={"turning_over_min_topical_similarity": 5.0},
        )
        result = load_settings(config_path=path)
        self.assertAlmostEqual(
            result.memory.turning_over_min_topical_similarity, 1.0,
        )

    def test_recent_msgs_window_floors_at_zero(self) -> None:
        path = self._write_config(
            memory_extra={"turning_over_recent_msgs_window": -3},
        )
        result = load_settings(config_path=path)
        # Floor 0 disables the thread pool; not negative.
        self.assertEqual(result.memory.turning_over_recent_msgs_window, 0)


class WillFamilySettingsTests(unittest.TestCase):
    """K52 + K53: agent knobs round-trip with clamps."""

    _KEYS = (
        "wants_ledger_enabled",
        "wants_growth_per_day",
        "wants_imperative_threshold",
        "wants_cap",
        "wants_max_age_days",
        "wants_reentry_cooldown_days",
        "wants_worker_interval_seconds",
        "initiative_turns_enabled",
        "initiative_base_period",
        "initiative_warmup_turns",
        "initiative_substantial_chars",
        "thread_ownership_enabled",
        "thread_engaged_chars",
        "thread_min_topical_similarity",
        "topic_appetite_enabled",
        "appetite_short_reply_chars",
        "appetite_short_share_threshold",
        "appetite_window",
        "appetite_min_want_pressure",
        "appetite_min_axes",
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

    def _write_config(self, agent_extra: dict | None = None) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(
            json.loads(default_path.read_text(encoding="utf-8"))
        )
        for k in self._KEYS:
            cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_load_when_keys_missing(self) -> None:
        result = load_settings(config_path=self._write_config())
        agent = result.agent
        self.assertTrue(agent.wants_ledger_enabled)
        self.assertEqual(agent.wants_growth_per_day, 0.25)
        self.assertEqual(agent.wants_imperative_threshold, 0.7)
        self.assertEqual(agent.wants_cap, 8)
        self.assertEqual(agent.wants_max_age_days, 14.0)
        self.assertEqual(agent.wants_reentry_cooldown_days, 5.0)
        self.assertEqual(agent.wants_worker_interval_seconds, 3600.0)
        self.assertTrue(agent.initiative_turns_enabled)
        self.assertEqual(agent.initiative_base_period, 8)
        self.assertEqual(agent.initiative_warmup_turns, 3)
        self.assertEqual(agent.initiative_substantial_chars, 240)
        self.assertTrue(agent.thread_ownership_enabled)
        self.assertEqual(agent.thread_engaged_chars, 80)
        self.assertEqual(agent.thread_min_topical_similarity, 0.30)
        self.assertTrue(agent.topic_appetite_enabled)
        self.assertEqual(agent.appetite_short_reply_chars, 160)
        self.assertEqual(agent.appetite_short_share_threshold, 0.6)
        self.assertEqual(agent.appetite_window, 6)
        self.assertEqual(agent.appetite_min_want_pressure, 0.35)
        self.assertEqual(agent.appetite_min_axes, 0.15)

    def test_overrides_round_trip(self) -> None:
        result = load_settings(config_path=self._write_config({
            "wants_ledger_enabled": False,
            "wants_growth_per_day": 0.5,
            "wants_imperative_threshold": 0.9,
            "wants_cap": 4,
            "initiative_turns_enabled": False,
            "initiative_base_period": 12,
            "thread_ownership_enabled": False,
            "thread_engaged_chars": 120,
            "thread_min_topical_similarity": 0.5,
            "topic_appetite_enabled": False,
            "appetite_window": 10,
            "appetite_min_want_pressure": 0.5,
        }))
        agent = result.agent
        self.assertFalse(agent.wants_ledger_enabled)
        self.assertEqual(agent.wants_growth_per_day, 0.5)
        self.assertEqual(agent.wants_imperative_threshold, 0.9)
        self.assertEqual(agent.wants_cap, 4)
        self.assertFalse(agent.initiative_turns_enabled)
        self.assertEqual(agent.initiative_base_period, 12)
        self.assertFalse(agent.thread_ownership_enabled)
        self.assertEqual(agent.thread_engaged_chars, 120)
        self.assertEqual(agent.thread_min_topical_similarity, 0.5)
        self.assertFalse(agent.topic_appetite_enabled)
        self.assertEqual(agent.appetite_window, 10)
        self.assertEqual(agent.appetite_min_want_pressure, 0.5)

    def test_clamps(self) -> None:
        result = load_settings(config_path=self._write_config({
            "wants_growth_per_day": -1.0,
            "wants_imperative_threshold": 5.0,
            "wants_cap": 0,
            "wants_max_age_days": 0.1,
            "wants_worker_interval_seconds": 1,
            "initiative_base_period": 1,
            "initiative_warmup_turns": -2,
            "initiative_substantial_chars": 0,
            "thread_engaged_chars": 0,
            "thread_min_topical_similarity": 7.0,
            "appetite_short_reply_chars": 0,
            "appetite_short_share_threshold": 3.0,
            "appetite_window": 1,
            "appetite_min_want_pressure": -1.0,
            "appetite_min_axes": -5.0,
        }))
        agent = result.agent
        self.assertEqual(agent.wants_growth_per_day, 0.0)
        self.assertEqual(agent.wants_imperative_threshold, 1.0)
        self.assertEqual(agent.wants_cap, 1)
        self.assertEqual(agent.wants_max_age_days, 1.0)
        self.assertEqual(agent.wants_worker_interval_seconds, 30.0)
        self.assertEqual(agent.initiative_base_period, 3)
        self.assertEqual(agent.initiative_warmup_turns, 0)
        self.assertEqual(agent.initiative_substantial_chars, 1)
        self.assertEqual(agent.thread_engaged_chars, 1)
        self.assertEqual(agent.thread_min_topical_similarity, 1.0)
        self.assertEqual(agent.appetite_short_reply_chars, 1)
        self.assertEqual(agent.appetite_short_share_threshold, 1.0)
        self.assertEqual(agent.appetite_window, 2)
        self.assertEqual(agent.appetite_min_want_pressure, 0.0)
        self.assertEqual(agent.appetite_min_axes, -1.0)


class EmotionEpisodeSettingsTests(unittest.TestCase):
    """K57: 4 agent knobs round-trip with clamps."""

    _KEYS = (
        "emotion_episodes_enabled",
        "emotion_episode_cap",
        "emotion_lonely_threshold_hours",
        "emotion_high_band",
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

    def _write_config(self, agent_extra: dict | None = None) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(
            json.loads(default_path.read_text(encoding="utf-8"))
        )
        for k in self._KEYS:
            cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults(self) -> None:
        result = load_settings(config_path=self._write_config())
        agent = result.agent
        self.assertTrue(agent.emotion_episodes_enabled)
        self.assertEqual(agent.emotion_episode_cap, 3)
        self.assertEqual(agent.emotion_lonely_threshold_hours, 5.0)
        self.assertEqual(agent.emotion_high_band, 0.5)

    def test_overrides_round_trip(self) -> None:
        result = load_settings(config_path=self._write_config({
            "emotion_episodes_enabled": False,
            "emotion_episode_cap": 5,
            "emotion_lonely_threshold_hours": 8.0,
            "emotion_high_band": 0.7,
        }))
        agent = result.agent
        self.assertFalse(agent.emotion_episodes_enabled)
        self.assertEqual(agent.emotion_episode_cap, 5)
        self.assertEqual(agent.emotion_lonely_threshold_hours, 8.0)
        self.assertEqual(agent.emotion_high_band, 0.7)

    def test_clamps(self) -> None:
        result = load_settings(config_path=self._write_config({
            "emotion_episode_cap": 0,
            "emotion_lonely_threshold_hours": 0.0,
            "emotion_high_band": 5.0,
        }))
        agent = result.agent
        self.assertEqual(agent.emotion_episode_cap, 1)
        self.assertEqual(agent.emotion_lonely_threshold_hours, 0.5)
        self.assertEqual(agent.emotion_high_band, 1.0)


class TeaseEconomySettingsTests(unittest.TestCase):
    """K59: 6 agent knobs round-trip with clamps."""

    _KEYS = (
        "tease_economy_enabled",
        "tease_cap",
        "tease_expiry_days",
        "tease_collect_cooldown_hours",
        "tease_min_humor",
        "tease_min_age_hours",
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

    def _write_config(self, agent_extra: dict | None = None) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(
            json.loads(default_path.read_text(encoding="utf-8"))
        )
        for k in self._KEYS:
            cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults(self) -> None:
        agent = load_settings(config_path=self._write_config()).agent
        self.assertTrue(agent.tease_economy_enabled)
        self.assertEqual(agent.tease_cap, 5)
        self.assertEqual(agent.tease_expiry_days, 14.0)
        self.assertEqual(agent.tease_collect_cooldown_hours, 12.0)
        self.assertEqual(agent.tease_min_humor, 0.2)
        self.assertEqual(agent.tease_min_age_hours, 1.0)

    def test_overrides_round_trip(self) -> None:
        agent = load_settings(config_path=self._write_config({
            "tease_economy_enabled": False,
            "tease_cap": 8,
            "tease_expiry_days": 7.0,
            "tease_collect_cooldown_hours": 1.0,
            "tease_min_humor": 0.5,
            "tease_min_age_hours": 0.0,
        })).agent
        self.assertFalse(agent.tease_economy_enabled)
        self.assertEqual(agent.tease_cap, 8)
        self.assertEqual(agent.tease_expiry_days, 7.0)
        self.assertEqual(agent.tease_collect_cooldown_hours, 1.0)
        self.assertEqual(agent.tease_min_humor, 0.5)
        self.assertEqual(agent.tease_min_age_hours, 0.0)

    def test_clamps(self) -> None:
        agent = load_settings(config_path=self._write_config({
            "tease_cap": 0,
            "tease_expiry_days": 0.0,
            "tease_collect_cooldown_hours": -5.0,
            "tease_min_humor": -3.0,
            "tease_min_age_hours": -1.0,
        })).agent
        self.assertEqual(agent.tease_cap, 1)
        self.assertEqual(agent.tease_expiry_days, 0.5)
        self.assertEqual(agent.tease_collect_cooldown_hours, 0.0)
        self.assertEqual(agent.tease_min_humor, -1.0)
        self.assertEqual(agent.tease_min_age_hours, 0.0)


class ExpressionMaskSettingsTests(unittest.TestCase):
    """K60: mode whitelist + slip-cooldown clamp."""

    _KEYS = ("expression_mask", "mask_slip_cooldown_days")

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.user_json = Path(self._tmp.name) / "user.json"
        patcher = mock.patch.object(
            settings_mod, "USER_CONFIG_PATH", self.user_json,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_config(self, agent_extra: dict | None = None) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(
            json.loads(default_path.read_text(encoding="utf-8"))
        )
        for k in self._KEYS:
            cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults(self) -> None:
        agent = load_settings(config_path=self._write_config()).agent
        self.assertEqual(agent.expression_mask, "off")
        self.assertEqual(agent.mask_slip_cooldown_days, 2.0)

    def test_modes_round_trip(self) -> None:
        for mode in ("off", "tsundere_light", "tsundere_full"):
            agent = load_settings(config_path=self._write_config({
                "expression_mask": mode,
            })).agent
            self.assertEqual(agent.expression_mask, mode)

    def test_unknown_mode_falls_back_to_off(self) -> None:
        for bad in ("tsundere", "yes", 1, None):
            agent = load_settings(config_path=self._write_config({
                "expression_mask": bad,
            })).agent
            self.assertEqual(agent.expression_mask, "off")

    def test_case_normalised(self) -> None:
        agent = load_settings(config_path=self._write_config({
            "expression_mask": " TSUNDERE_LIGHT ",
        })).agent
        self.assertEqual(agent.expression_mask, "tsundere_light")

    def test_cooldown_clamped_non_negative(self) -> None:
        agent = load_settings(config_path=self._write_config({
            "mask_slip_cooldown_days": -3.0,
        })).agent
        self.assertEqual(agent.mask_slip_cooldown_days, 0.0)


class DayColorSettingsTests(unittest.TestCase):
    """K27: 2 agent knobs round-trip with clamps."""

    _DC_AGENT_KEYS = (
        "day_color_enabled",
        "day_color_check_interval_seconds",
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
            for k in self._DC_AGENT_KEYS:
                cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_load_when_keys_missing(self) -> None:
        # Strip both keys and verify the dataclass defaults land.
        # The defaults are part of the documented contract: the
        # patterns.md / shipped.md sections all quote them.
        path = self._write_config()
        result = load_settings(config_path=path)
        self.assertTrue(result.agent.day_color_enabled)
        self.assertEqual(
            result.agent.day_color_check_interval_seconds, 3600,
        )

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={
                "day_color_enabled": False,
                "day_color_check_interval_seconds": 7200,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.day_color_enabled)
        self.assertEqual(
            result.agent.day_color_check_interval_seconds, 7200,
        )

    def test_interval_clamps_to_floor(self) -> None:
        # Floor is 60s; lower values clamp up. Guards against a
        # buggy override pinning the scheduler against the wall.
        path = self._write_config(
            agent_extra={"day_color_check_interval_seconds": 5},
        )
        result = load_settings(config_path=path)
        self.assertEqual(
            result.agent.day_color_check_interval_seconds, 60,
        )

    def test_negative_interval_clamps_to_floor(self) -> None:
        path = self._write_config(
            agent_extra={"day_color_check_interval_seconds": -100},
        )
        result = load_settings(config_path=path)
        self.assertEqual(
            result.agent.day_color_check_interval_seconds, 60,
        )

    def test_enabled_accepts_truthy_values(self) -> None:
        # bool() coercion -- a JSON-side "true" string or 1 should
        # still flip the switch on. Confirms the parser doesn't
        # require a Python-side bool literal.
        path = self._write_config(
            agent_extra={"day_color_enabled": 1},
        )
        result = load_settings(config_path=path)
        self.assertTrue(result.agent.day_color_enabled)


class VulnerabilityBudgetSettingsTests(unittest.TestCase):
    """K15: 7 agent knobs round-trip with the documented clamps."""

    _VB_AGENT_KEYS = (
        "vulnerability_budget_enabled",
        "vulnerability_budget_min_capacity",
        "vulnerability_budget_max_capacity",
        "vulnerability_budget_regen_per_hour",
        "vulnerability_budget_tier1_cost",
        "vulnerability_budget_tier2_cost",
        "vulnerability_budget_tier3_cost",
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
            for k in self._VB_AGENT_KEYS:
                cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_load_when_keys_missing(self) -> None:
        # Strip all 7 keys, verify the dataclass defaults land.
        # These defaults are part of the documented contract --
        # shipped.md and the persona file reference the values.
        path = self._write_config()
        result = load_settings(config_path=path)
        self.assertTrue(result.agent.vulnerability_budget_enabled)
        self.assertEqual(
            result.agent.vulnerability_budget_min_capacity, 1,
        )
        self.assertEqual(
            result.agent.vulnerability_budget_max_capacity, 12,
        )
        self.assertEqual(
            result.agent.vulnerability_budget_regen_per_hour, 0.5,
        )
        self.assertEqual(
            result.agent.vulnerability_budget_tier1_cost, 1,
        )
        self.assertEqual(
            result.agent.vulnerability_budget_tier2_cost, 3,
        )
        self.assertEqual(
            result.agent.vulnerability_budget_tier3_cost, 6,
        )

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={
                "vulnerability_budget_enabled": False,
                "vulnerability_budget_min_capacity": 2,
                "vulnerability_budget_max_capacity": 20,
                "vulnerability_budget_regen_per_hour": 1.0,
                "vulnerability_budget_tier1_cost": 2,
                "vulnerability_budget_tier2_cost": 4,
                "vulnerability_budget_tier3_cost": 8,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.vulnerability_budget_enabled)
        self.assertEqual(
            result.agent.vulnerability_budget_min_capacity, 2,
        )
        self.assertEqual(
            result.agent.vulnerability_budget_max_capacity, 20,
        )
        self.assertEqual(
            result.agent.vulnerability_budget_regen_per_hour, 1.0,
        )
        self.assertEqual(
            result.agent.vulnerability_budget_tier1_cost, 2,
        )
        self.assertEqual(
            result.agent.vulnerability_budget_tier2_cost, 4,
        )
        self.assertEqual(
            result.agent.vulnerability_budget_tier3_cost, 8,
        )

    def test_min_capacity_floor(self) -> None:
        # Floor is 1; lower values clamp up so the bucket math
        # always has a non-zero divisor.
        path = self._write_config(
            agent_extra={"vulnerability_budget_min_capacity": 0},
        )
        result = load_settings(config_path=path)
        self.assertEqual(
            result.agent.vulnerability_budget_min_capacity, 1,
        )

    def test_max_capacity_floor(self) -> None:
        # Floor is 1; negative / zero values clamp up.
        path = self._write_config(
            agent_extra={"vulnerability_budget_max_capacity": -5},
        )
        result = load_settings(config_path=path)
        self.assertEqual(
            result.agent.vulnerability_budget_max_capacity, 1,
        )

    def test_regen_clamps_to_floor(self) -> None:
        # Floor is 0.01 -- below that, decay would be functionally
        # disabled and the bucket would never recover. A zero / neg
        # value silently clamps up.
        path = self._write_config(
            agent_extra={"vulnerability_budget_regen_per_hour": 0.0},
        )
        result = load_settings(config_path=path)
        self.assertEqual(
            result.agent.vulnerability_budget_regen_per_hour, 0.01,
        )

    def test_tier_costs_clamp_at_zero(self) -> None:
        # Floor is 0 -- negative costs would credit the bucket,
        # which makes no semantic sense.
        path = self._write_config(
            agent_extra={
                "vulnerability_budget_tier1_cost": -1,
                "vulnerability_budget_tier2_cost": -3,
                "vulnerability_budget_tier3_cost": -6,
            },
        )
        result = load_settings(config_path=path)
        self.assertEqual(
            result.agent.vulnerability_budget_tier1_cost, 0,
        )
        self.assertEqual(
            result.agent.vulnerability_budget_tier2_cost, 0,
        )
        self.assertEqual(
            result.agent.vulnerability_budget_tier3_cost, 0,
        )

    def test_enabled_accepts_truthy_values(self) -> None:
        path = self._write_config(
            agent_extra={"vulnerability_budget_enabled": 1},
        )
        result = load_settings(config_path=path)
        self.assertTrue(result.agent.vulnerability_budget_enabled)


class ChatLlmSettingsTests(unittest.TestCase):
    """``chat_llm.workers_use_local`` + ``provider_preset`` round-trip
    through the loader. Also verifies ``provider="openai_compatible"``
    is accepted while a typo'd preset collapses to ``""``.
    """

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
        self._base_config = json.loads(
            default_path.read_text(encoding="utf-8"),
        )

    def _write_config(self, chat_llm_extra: dict | None = None) -> Path:
        cfg = copy.deepcopy(self._base_config)
        if chat_llm_extra is not None:
            cfg["chat_llm"] = {**cfg.get("chat_llm", {}), **chat_llm_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_workers_use_local_defaults_true(self) -> None:
        path = self._write_config()
        result = load_settings(config_path=path)
        self.assertTrue(result.chat_llm.workers_use_local)

    def test_workers_use_local_override_round_trips(self) -> None:
        path = self._write_config({"workers_use_local": False})
        result = load_settings(config_path=path)
        self.assertFalse(result.chat_llm.workers_use_local)

    def test_provider_preset_round_trips(self) -> None:
        for preset in ("ollama", "gemini", "openai", "groq", "openrouter"):
            path = self._write_config({"provider_preset": preset})
            result = load_settings(config_path=path)
            self.assertEqual(
                result.chat_llm.provider_preset, preset,
                f"preset {preset} did not round-trip",
            )

    def test_unknown_preset_collapses_to_empty(self) -> None:
        path = self._write_config({"provider_preset": "made-up"})
        result = load_settings(config_path=path)
        self.assertEqual(result.chat_llm.provider_preset, "")

    def test_provider_openai_compatible_accepted(self) -> None:
        path = self._write_config({"provider": "openai_compatible"})
        result = load_settings(config_path=path)
        self.assertEqual(result.chat_llm.provider, "openai_compatible")

    def test_unknown_provider_falls_back_to_ollama(self) -> None:
        path = self._write_config({"provider": "azure"})
        result = load_settings(config_path=path)
        self.assertEqual(result.chat_llm.provider, "ollama")


class TaskOrchestrationSettingsTests(unittest.TestCase):
    """Chunk 4: 9 agent knobs round-trip with the documented clamps.

    Mirrors the doc table in ``docs/configuration.md`` under
    "Brain orchestration — long-running tasks (schema v16)". Each
    field has its own min/max contract pinned here so a typo in
    ``user.json`` can never crash boot or pin a runaway value.
    """

    _TASK_KEYS = (
        "tasks_enabled",
        "tasks_per_user_cap",
        "tasks_resume_on_boot",
        "tasks_running_block_enabled",
        "brain_loop_deferred_grace_ms",
        "task_cue_max_age_seconds",
        "task_cue_max_aggregated",
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
            for k in self._TASK_KEYS:
                cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_load_when_keys_missing(self) -> None:
        path = self._write_config()
        a = load_settings(config_path=path).agent
        self.assertTrue(a.tasks_enabled)
        self.assertEqual(a.tasks_per_user_cap, 8)
        self.assertTrue(a.tasks_resume_on_boot)
        self.assertTrue(a.tasks_running_block_enabled)
        self.assertEqual(a.brain_loop_deferred_grace_ms, 100)
        self.assertEqual(a.task_cue_max_age_seconds, 1800)
        self.assertEqual(a.task_cue_max_aggregated, 5)
        # Duration-hybrid task reply defaults.
        self.assertTrue(a.task_reply_on_complete_enabled)
        self.assertEqual(a.task_inline_grace_seconds, 3.0)

    def test_reply_on_complete_overrides_and_clamps(self) -> None:
        path = self._write_config(
            agent_extra={
                "task_reply_on_complete_enabled": False,
                "task_inline_grace_seconds": 999.0,  # clamp to 30
            },
        )
        a = load_settings(config_path=path).agent
        self.assertFalse(a.task_reply_on_complete_enabled)
        self.assertEqual(a.task_inline_grace_seconds, 30.0)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={
                "tasks_enabled": False,
                "tasks_per_user_cap": 4,
                "tasks_resume_on_boot": False,
                "tasks_running_block_enabled": False,
                "brain_loop_deferred_grace_ms": 250,
                "task_cue_max_age_seconds": 3600,
                "task_cue_max_aggregated": 10,
            },
        )
        a = load_settings(config_path=path).agent
        self.assertFalse(a.tasks_enabled)
        self.assertEqual(a.tasks_per_user_cap, 4)
        self.assertFalse(a.tasks_resume_on_boot)
        self.assertFalse(a.tasks_running_block_enabled)
        self.assertEqual(a.brain_loop_deferred_grace_ms, 250)
        self.assertEqual(a.task_cue_max_age_seconds, 3600)
        self.assertEqual(a.task_cue_max_aggregated, 10)

    def test_tasks_per_user_cap_floor(self) -> None:
        path = self._write_config(agent_extra={"tasks_per_user_cap": 0})
        a = load_settings(config_path=path).agent
        # Floor is 1 -- the orchestrator needs at least one slot.
        self.assertEqual(a.tasks_per_user_cap, 1)
        # Negative clamps up too.
        path = self._write_config(agent_extra={"tasks_per_user_cap": -5})
        a = load_settings(config_path=path).agent
        self.assertEqual(a.tasks_per_user_cap, 1)

    def test_brain_loop_grace_floor_and_ceiling(self) -> None:
        path = self._write_config(
            agent_extra={"brain_loop_deferred_grace_ms": 1}
        )
        a = load_settings(config_path=path).agent
        self.assertEqual(a.brain_loop_deferred_grace_ms, 10)
        path = self._write_config(
            agent_extra={"brain_loop_deferred_grace_ms": 99999}
        )
        a = load_settings(config_path=path).agent
        self.assertEqual(a.brain_loop_deferred_grace_ms, 5000)

    def test_cue_max_age_floor_and_ceiling(self) -> None:
        path = self._write_config(
            agent_extra={"task_cue_max_age_seconds": 1}
        )
        a = load_settings(config_path=path).agent
        self.assertEqual(a.task_cue_max_age_seconds, 60)
        path = self._write_config(
            agent_extra={"task_cue_max_age_seconds": 999999}
        )
        a = load_settings(config_path=path).agent
        self.assertEqual(a.task_cue_max_age_seconds, 86400)

    def test_cue_max_aggregated_floor_and_ceiling(self) -> None:
        path = self._write_config(
            agent_extra={"task_cue_max_aggregated": 0}
        )
        a = load_settings(config_path=path).agent
        self.assertEqual(a.task_cue_max_aggregated, 1)
        path = self._write_config(
            agent_extra={"task_cue_max_aggregated": 99}
        )
        a = load_settings(config_path=path).agent
        self.assertEqual(a.task_cue_max_aggregated, 20)

    def test_bool_fields_accept_truthy_values(self) -> None:
        path = self._write_config(
            agent_extra={
                "tasks_enabled": 1,
                "tasks_resume_on_boot": 0,
                "tasks_running_block_enabled": "",
            },
        )
        a = load_settings(config_path=path).agent
        self.assertTrue(a.tasks_enabled)
        self.assertFalse(a.tasks_resume_on_boot)
        self.assertFalse(a.tasks_running_block_enabled)


class PersonaTaskBannerSettingsTests(unittest.TestCase):
    """Chunk 15: ``agent.persona_task_banner_enabled`` is the master
    switch for the persona-window mirror of the task strip. Pure
    boolean round-trip + default + truthy coercion."""

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
        strip_key: bool = True,
    ) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(
            json.loads(default_path.read_text(encoding="utf-8"))
        )
        if strip_key:
            cfg.get("agent", {}).pop("persona_task_banner_enabled", None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_default_is_enabled_when_key_missing(self) -> None:
        path = self._write_config()
        a = load_settings(config_path=path).agent
        self.assertTrue(a.persona_task_banner_enabled)

    def test_explicit_false_round_trips(self) -> None:
        path = self._write_config(
            agent_extra={"persona_task_banner_enabled": False}
        )
        a = load_settings(config_path=path).agent
        self.assertFalse(a.persona_task_banner_enabled)

    def test_truthy_coercion(self) -> None:
        # Mirrors ``test_bool_fields_accept_truthy_values`` in the
        # task-orchestration block: a typo like ``0`` or ``""`` in
        # ``user.json`` should resolve to ``False`` cleanly.
        path = self._write_config(
            agent_extra={"persona_task_banner_enabled": 0}
        )
        a = load_settings(config_path=path).agent
        self.assertFalse(a.persona_task_banner_enabled)
        path = self._write_config(
            agent_extra={"persona_task_banner_enabled": 1}
        )
        a = load_settings(config_path=path).agent
        self.assertTrue(a.persona_task_banner_enabled)


class TaskLifecycleSafetySettingsTests(unittest.TestCase):
    """Schema v17 (Brain Orchestration Phase 2): six new agent settings
    for heartbeat / stalled / cleanup / cascade. Pin defaults + clamps.
    """

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
        strip_keys: tuple[str, ...] = (),
    ) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(
            json.loads(default_path.read_text(encoding="utf-8"))
        )
        for k in strip_keys:
            cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_match_design(self) -> None:
        path = self._write_config(
            strip_keys=(
                "task_heartbeat_check_interval_seconds",
                "task_stalled_seconds",
                "task_stalled_action",
                "task_cleanup_retention_days",
                "task_cleanup_interval_seconds",
                "task_cascade_cancel_children",
            ),
        )
        a = load_settings(config_path=path).agent
        self.assertEqual(a.task_heartbeat_check_interval_seconds, 30)
        self.assertEqual(a.task_stalled_seconds, 300)
        self.assertEqual(a.task_stalled_action, "warn")
        self.assertEqual(a.task_cleanup_retention_days, 30)
        self.assertEqual(a.task_cleanup_interval_seconds, 21600)
        self.assertTrue(a.task_cascade_cancel_children)

    def test_floor_clamps(self) -> None:
        path = self._write_config(
            agent_extra={
                "task_heartbeat_check_interval_seconds": 1,
                "task_stalled_seconds": 10,
                "task_cleanup_retention_days": 0,
                "task_cleanup_interval_seconds": 1,
            },
        )
        a = load_settings(config_path=path).agent
        self.assertGreaterEqual(a.task_heartbeat_check_interval_seconds, 5)
        self.assertGreaterEqual(a.task_stalled_seconds, 60)
        self.assertGreaterEqual(a.task_cleanup_retention_days, 1)
        self.assertGreaterEqual(a.task_cleanup_interval_seconds, 600)

    def test_action_unknown_value_falls_back_to_warn(self) -> None:
        path = self._write_config(
            agent_extra={"task_stalled_action": "nuke"},
        )
        a = load_settings(config_path=path).agent
        self.assertEqual(a.task_stalled_action, "warn")

    def test_action_fail_round_trips(self) -> None:
        path = self._write_config(
            agent_extra={"task_stalled_action": "fail"},
        )
        a = load_settings(config_path=path).agent
        self.assertEqual(a.task_stalled_action, "fail")

    def test_cascade_disable_round_trips(self) -> None:
        path = self._write_config(
            agent_extra={"task_cascade_cancel_children": False},
        )
        a = load_settings(config_path=path).agent
        self.assertFalse(a.task_cascade_cancel_children)


class TaskApprovalAndFileWriteSettingsTests(unittest.TestCase):
    """``agent.task_approval_*`` + the nested ``agent.file_write`` block."""

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
        strip_keys: tuple[str, ...] = (),
    ) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(
            json.loads(default_path.read_text(encoding="utf-8"))
        )
        for k in strip_keys:
            cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults(self) -> None:
        path = self._write_config(
            strip_keys=(
                "task_approval_mode",
                "task_approval_overrides",
                "file_write",
            ),
        )
        a = load_settings(config_path=path).agent
        self.assertEqual(a.task_approval_mode, "ask")
        self.assertEqual(a.task_approval_overrides, {})
        self.assertFalse(a.file_write.enabled)
        self.assertEqual(a.file_write.max_bytes, 262144)
        self.assertIn(".md", a.file_write.allowed_extensions)

    def test_dataclass_default_matches_config(self) -> None:
        path = self._write_config()
        a = load_settings(config_path=path).agent
        self.assertEqual(a.task_approval_mode, "ask")
        self.assertFalse(a.file_write.enabled)

    def test_approval_mode_invalid_falls_back(self) -> None:
        path = self._write_config(agent_extra={"task_approval_mode": "bogus"})
        a = load_settings(config_path=path).agent
        self.assertEqual(a.task_approval_mode, "ask")

    def test_approval_mode_auto_round_trips(self) -> None:
        path = self._write_config(agent_extra={"task_approval_mode": "auto"})
        a = load_settings(config_path=path).agent
        self.assertEqual(a.task_approval_mode, "auto")

    def test_overrides_drop_invalid_modes(self) -> None:
        path = self._write_config(
            agent_extra={
                "task_approval_overrides": {
                    "file_write": "auto",
                    "shell_exec": "nonsense",
                }
            }
        )
        a = load_settings(config_path=path).agent
        self.assertEqual(a.task_approval_overrides, {"file_write": "auto"})

    def test_file_write_enabled_and_clamp(self) -> None:
        path = self._write_config(
            agent_extra={
                "file_write": {
                    "enabled": True,
                    "max_bytes": 5,  # below 1 KiB floor
                    "allowed_extensions": ["TXT", ".md"],
                }
            }
        )
        a = load_settings(config_path=path).agent
        self.assertTrue(a.file_write.enabled)
        self.assertEqual(a.file_write.max_bytes, 1024)
        self.assertEqual(a.file_write.allowed_extensions, (".txt", ".md"))


class VisionSettingsTests(unittest.TestCase):
    """The nested ``agent.vision`` block (describe_image capability)."""

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
        strip_keys: tuple[str, ...] = (),
    ) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(
            json.loads(default_path.read_text(encoding="utf-8"))
        )
        for k in strip_keys:
            cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_when_block_missing(self) -> None:
        path = self._write_config(strip_keys=("vision",))
        v = load_settings(config_path=path).agent.vision
        self.assertFalse(v.enabled)
        self.assertEqual(v.model, "")
        self.assertEqual(v.max_bytes, 8 * 1024 * 1024)
        self.assertEqual(v.timeout_seconds, 180)
        self.assertIn(".png", v.allowed_extensions)
        self.assertTrue(v.default_prompt)

    def test_default_config_block_matches(self) -> None:
        path = self._write_config()
        v = load_settings(config_path=path).agent.vision
        self.assertFalse(v.enabled)
        self.assertIn(".jpg", v.allowed_extensions)

    def test_enabled_override_and_model(self) -> None:
        path = self._write_config(
            agent_extra={
                "vision": {"enabled": True, "model": "qwen3.5:27b"}
            }
        )
        v = load_settings(config_path=path).agent.vision
        self.assertTrue(v.enabled)
        self.assertEqual(v.model, "qwen3.5:27b")

    def test_clamps_and_extension_normalisation(self) -> None:
        path = self._write_config(
            agent_extra={
                "vision": {
                    "enabled": True,
                    "max_bytes": 5,  # below 1 KiB floor
                    "timeout_seconds": 1,  # below 5s floor
                    "allowed_extensions": ["PNG", ".webp"],
                }
            }
        )
        v = load_settings(config_path=path).agent.vision
        self.assertEqual(v.max_bytes, 1024)
        self.assertEqual(v.timeout_seconds, 5)
        self.assertEqual(v.allowed_extensions, (".png", ".webp"))

    def test_max_bytes_upper_clamp(self) -> None:
        path = self._write_config(
            agent_extra={"vision": {"max_bytes": 999 * 1024 * 1024}}
        )
        v = load_settings(config_path=path).agent.vision
        self.assertEqual(v.max_bytes, 64 * 1024 * 1024)

    def test_blank_prompt_falls_back_to_default(self) -> None:
        path = self._write_config(
            agent_extra={"vision": {"default_prompt": "   "}}
        )
        v = load_settings(config_path=path).agent.vision
        self.assertTrue(v.default_prompt.strip())


class ExternalMcpSettingsTests(unittest.TestCase):
    """Phase 1: ``mcp_clients.servers`` parse + ``agent.mcp_clients_enabled``."""

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
        self, agent_extra: dict | None = None, mcp_clients: dict | None = None
    ) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(json.loads(default_path.read_text(encoding="utf-8")))
        cfg.get("agent", {}).pop("mcp_clients_enabled", None)
        cfg.pop("mcp_clients", None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        if mcp_clients is not None:
            cfg["mcp_clients"] = mcp_clients
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_when_missing(self) -> None:
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.mcp_clients_enabled)
        self.assertEqual(result.mcp_clients.servers, [])

    def test_parses_stdio_server(self) -> None:
        path = self._write_config(
            mcp_clients={
                "servers": [
                    {
                        "id": "filesystem",
                        "name": "Files",
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                        "env": {"TOKEN": "${ENV:MY_TOKEN}"},
                        "expose_tools": ["read_text_file"],
                    }
                ]
            },
        )
        result = load_settings(config_path=path)
        servers = result.mcp_clients.servers
        self.assertEqual(len(servers), 1)
        s = servers[0]
        self.assertEqual(s.id, "filesystem")
        self.assertEqual(s.transport, "stdio")
        self.assertEqual(s.command, "npx")
        self.assertEqual(
            s.args,
            ("-y", "@modelcontextprotocol/server-filesystem", "/tmp"),
        )
        self.assertEqual(s.env, {"TOKEN": "${ENV:MY_TOKEN}"})
        self.assertEqual(s.expose_tools, ("read_text_file",))

    def test_drops_stdio_without_command(self) -> None:
        path = self._write_config(
            mcp_clients={"servers": [{"id": "bad", "transport": "stdio"}]},
        )
        result = load_settings(config_path=path)
        self.assertEqual(result.mcp_clients.servers, [])

    def test_drops_sse_without_url_and_dedupes(self) -> None:
        path = self._write_config(
            mcp_clients={
                "servers": [
                    {"id": "remote", "transport": "sse"},  # no url -> dropped
                    {"id": "dup", "command": "a"},
                    {"id": "dup", "command": "b"},  # duplicate id -> skipped
                ]
            },
        )
        result = load_settings(config_path=path)
        ids = [s.id for s in result.mcp_clients.servers]
        self.assertEqual(ids, ["dup"])
        self.assertEqual(result.mcp_clients.servers[0].command, "a")

    def test_master_switch_off(self) -> None:
        path = self._write_config(agent_extra={"mcp_clients_enabled": False})
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.mcp_clients_enabled)


if __name__ == "__main__":
    unittest.main()
