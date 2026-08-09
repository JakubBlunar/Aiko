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
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from app.core.infra import settings as settings_mod
from app.core.infra.settings import AvatarSettings, McpServerSettings, load_settings


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

    def _write_config(
        self,
        agent_extra: dict | None = None,
        memory_extra: dict | None = None,
        tools_extra: dict | None = None,
    ) -> Path:
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
        if tools_extra is not None:
            cfg["tools"] = {**cfg.get("tools", {}), **tools_extra}
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
        self.assertTrue(result.agent.topic_graph_persistent_enabled)
        self.assertEqual(
            result.agent.topic_graph_rebuild_interval_seconds, 86_400.0,
        )
        self.assertEqual(result.agent.topic_graph_refit_pending_threshold, 25)
        self.assertEqual(result.memory.curiosity_seed_interval_seconds, 3600)

    def test_topic_graph_persistence_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={
                "topic_graph_persistent_enabled": False,
                "topic_graph_rebuild_interval_seconds": 10,  # below floor
                "topic_graph_refit_pending_threshold": 0,    # below min
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.topic_graph_persistent_enabled)
        # Floors apply.
        self.assertEqual(
            result.agent.topic_graph_rebuild_interval_seconds, 60.0,
        )
        self.assertEqual(result.agent.topic_graph_refit_pending_threshold, 1)

    def test_topic_label_settings_round_trip(self) -> None:
        # Defaults.
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.topic_label_enabled)
        self.assertEqual(result.agent.topic_label_interval_seconds, 1800.0)
        self.assertEqual(result.agent.topic_label_max_per_run, 4)
        self.assertEqual(result.agent.topic_label_max_tokens, 32)
        # Overrides + floors.
        path = self._write_config(
            agent_extra={
                "topic_label_enabled": False,
                "topic_label_interval_seconds": 5,  # below 60s floor
                "topic_label_max_per_run": 0,       # below min 1
                "topic_label_max_tokens": 1,        # below min 8
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.topic_label_enabled)
        self.assertEqual(result.agent.topic_label_interval_seconds, 60.0)
        self.assertEqual(result.agent.topic_label_max_per_run, 1)
        self.assertEqual(result.agent.topic_label_max_tokens, 8)

    def test_topic_digest_settings_round_trip(self) -> None:
        # Defaults.
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.topic_digest_enabled)
        self.assertEqual(result.agent.topic_digest_interval_seconds, 3600.0)
        self.assertEqual(result.agent.topic_digest_max_per_run, 3)
        self.assertEqual(result.agent.topic_digest_max_tokens, 256)
        self.assertEqual(result.agent.topic_digest_min_cluster_size, 6)
        self.assertTrue(result.agent.topic_digest_surface_in_rag)
        self.assertEqual(result.agent.rag_digest_sibling_cap, 1)
        # Overrides + floors.
        path = self._write_config(
            agent_extra={
                "topic_digest_enabled": False,
                "topic_digest_interval_seconds": 5,    # below 60s floor
                "topic_digest_max_per_run": 0,         # below min 1
                "topic_digest_max_tokens": 1,          # below min 32
                "topic_digest_min_cluster_size": 1,    # below min 2
                "topic_digest_surface_in_rag": False,
                "rag_digest_sibling_cap": -3,          # below min 0
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.topic_digest_enabled)
        self.assertEqual(result.agent.topic_digest_interval_seconds, 60.0)
        self.assertEqual(result.agent.topic_digest_max_per_run, 1)
        self.assertEqual(result.agent.topic_digest_max_tokens, 32)
        self.assertEqual(result.agent.topic_digest_min_cluster_size, 2)
        self.assertFalse(result.agent.topic_digest_surface_in_rag)
        self.assertEqual(result.agent.rag_digest_sibling_cap, 0)

    def test_rag_cluster_diversity_settings_round_trip(self) -> None:
        # Defaults.
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.rag_cluster_diversity_enabled)
        self.assertEqual(result.agent.rag_max_per_cluster, 3)
        # Overrides + floor.
        path = self._write_config(
            agent_extra={
                "rag_cluster_diversity_enabled": False,
                "rag_max_per_cluster": 0,  # below min 1
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.rag_cluster_diversity_enabled)
        self.assertEqual(result.agent.rag_max_per_cluster, 1)

    def test_rag_topic_expansion_settings_round_trip(self) -> None:
        # Defaults.
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.rag_topic_expansion_enabled)
        self.assertEqual(result.agent.rag_expand_max, 2)
        self.assertAlmostEqual(result.agent.rag_expand_trigger_score, 0.55)
        self.assertAlmostEqual(result.agent.rag_expand_min_sim, 0.45)
        # Overrides + floor (expand_max clamps to >= 0).
        path = self._write_config(
            agent_extra={
                "rag_topic_expansion_enabled": False,
                "rag_expand_max": -5,
                "rag_expand_trigger_score": 0.7,
                "rag_expand_min_sim": 0.6,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.rag_topic_expansion_enabled)
        self.assertEqual(result.agent.rag_expand_max, 0)
        self.assertAlmostEqual(result.agent.rag_expand_trigger_score, 0.7)
        self.assertAlmostEqual(result.agent.rag_expand_min_sim, 0.6)

    def test_rag_direct_recall_settings_round_trip(self) -> None:
        # Defaults (K-time2 direct recall).
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.rag_direct_recall_enabled)
        self.assertEqual(result.agent.rag_direct_recall_max_messages, 6)
        # Overrides + floor (max clamps to >= 0).
        path = self._write_config(
            agent_extra={
                "rag_direct_recall_enabled": False,
                "rag_direct_recall_max_messages": -4,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.rag_direct_recall_enabled)
        self.assertEqual(result.agent.rag_direct_recall_max_messages, 0)

    def test_recall_topic_tool_setting_round_trip(self) -> None:
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.tools.recall_topic)
        path = self._write_config(tools_extra={"recall_topic": False})
        result = load_settings(config_path=path)
        self.assertFalse(result.tools.recall_topic)

    def test_knowledge_gap_notice_settings_round_trip(self) -> None:
        # Defaults.
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.knowledge_gap_notice_enabled)
        self.assertEqual(
            result.memory.knowledge_gap_notice_interval_seconds, 3600,
        )
        self.assertEqual(result.memory.knowledge_gap_notice_min_size, 5)
        self.assertAlmostEqual(
            result.memory.knowledge_gap_notice_max_knowledge_fraction, 0.15,
        )
        self.assertEqual(
            result.memory.knowledge_gap_notice_topic_cooldown_hours, 72,
        )
        self.assertEqual(result.memory.knowledge_gap_notice_journal_max, 6)
        # Overrides + floors.
        path = self._write_config(
            agent_extra={"knowledge_gap_notice_enabled": False},
            memory_extra={
                "knowledge_gap_notice_interval_seconds": 5,   # below 60s
                "knowledge_gap_notice_min_size": 1,           # below min 2
                "knowledge_gap_notice_max_knowledge_fraction": 2.0,  # >1
                "knowledge_gap_notice_topic_cooldown_hours": -3,     # <0
                "knowledge_gap_notice_journal_max": 0,        # below min 1
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.knowledge_gap_notice_enabled)
        self.assertEqual(
            result.memory.knowledge_gap_notice_interval_seconds, 60,
        )
        self.assertEqual(result.memory.knowledge_gap_notice_min_size, 2)
        self.assertAlmostEqual(
            result.memory.knowledge_gap_notice_max_knowledge_fraction, 1.0,
        )
        self.assertEqual(
            result.memory.knowledge_gap_notice_topic_cooldown_hours, 0,
        )
        self.assertEqual(result.memory.knowledge_gap_notice_journal_max, 1)

    def test_topic_temperature_settings_round_trip(self) -> None:
        # Defaults.
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.topic_temperature_enabled)
        self.assertTrue(result.agent.topic_mood_origin_enabled)  # H8
        self.assertAlmostEqual(result.memory.topic_temperature_min_sim, 0.45)
        self.assertAlmostEqual(result.memory.topic_temperature_threshold, 0.5)
        self.assertEqual(
            result.memory.topic_temperature_cooldown_turns, 6,
        )
        # Overrides + clamps.
        path = self._write_config(
            agent_extra={
                "topic_temperature_enabled": False,
                "topic_mood_origin_enabled": False,  # H8
            },
            memory_extra={
                "topic_temperature_min_sim": 2.0,        # clamped to 1.0
                "topic_temperature_threshold": -1.0,     # clamped to 0.0
                "topic_temperature_cooldown_turns": -5,  # floor 0
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.topic_temperature_enabled)
        self.assertFalse(result.agent.topic_mood_origin_enabled)  # H8
        self.assertAlmostEqual(result.memory.topic_temperature_min_sim, 1.0)
        self.assertAlmostEqual(result.memory.topic_temperature_threshold, 0.0)
        self.assertEqual(result.memory.topic_temperature_cooldown_turns, 0)

    def test_upcoming_horizon_settings_round_trip(self) -> None:
        # Defaults.
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.upcoming_horizon_enabled)
        self.assertEqual(result.memory.upcoming_horizon_days, 7)
        self.assertEqual(result.memory.upcoming_horizon_max_items, 3)
        self.assertEqual(result.memory.upcoming_horizon_cooldown_turns, 6)
        # Overrides + clamps.
        path = self._write_config(
            agent_extra={"upcoming_horizon_enabled": False},
            memory_extra={
                "upcoming_horizon_days": 0,            # floor 1
                "upcoming_horizon_max_items": 0,       # floor 1
                "upcoming_horizon_cooldown_turns": -5,  # floor 0
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.upcoming_horizon_enabled)
        self.assertEqual(result.memory.upcoming_horizon_days, 1)
        self.assertEqual(result.memory.upcoming_horizon_max_items, 1)
        self.assertEqual(result.memory.upcoming_horizon_cooldown_turns, 0)

    def test_cluster_scoped_memory_hygiene_round_trip(self) -> None:
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.cluster_scoped_memory_hygiene_enabled)
        path = self._write_config(
            agent_extra={"cluster_scoped_memory_hygiene_enabled": False},
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.cluster_scoped_memory_hygiene_enabled)

    def test_topic_tracking_settings_round_trip(self) -> None:
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.topic_tracking_enabled)
        self.assertAlmostEqual(
            result.memory.topic_tracking_min_sim, 0.30, places=6,
        )
        path = self._write_config(
            agent_extra={"topic_tracking_enabled": False},
            memory_extra={"topic_tracking_min_sim": 5.0},  # clamps to 1.0
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.topic_tracking_enabled)
        self.assertEqual(result.memory.topic_tracking_min_sim, 1.0)

    def test_topic_confidence_settings_round_trip(self) -> None:
        # Defaults.
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.topic_confidence_enabled)
        self.assertAlmostEqual(result.memory.topic_confidence_min_sim, 0.45)
        self.assertAlmostEqual(
            result.memory.topic_confidence_thin_threshold, 0.25,
        )
        self.assertAlmostEqual(
            result.memory.topic_confidence_familiar_threshold, 0.7,
        )
        self.assertEqual(result.memory.topic_confidence_cooldown_turns, 6)
        # Overrides + clamps.
        path = self._write_config(
            agent_extra={"topic_confidence_enabled": False},
            memory_extra={
                "topic_confidence_min_sim": -0.5,            # clamped 0.0
                "topic_confidence_thin_threshold": 2.0,      # clamped 1.0
                "topic_confidence_familiar_threshold": -1.0, # clamped 0.0
                "topic_confidence_cooldown_turns": -3,       # floor 0
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.topic_confidence_enabled)
        self.assertAlmostEqual(result.memory.topic_confidence_min_sim, 0.0)
        self.assertAlmostEqual(
            result.memory.topic_confidence_thin_threshold, 1.0,
        )
        self.assertAlmostEqual(
            result.memory.topic_confidence_familiar_threshold, 0.0,
        )
        self.assertEqual(result.memory.topic_confidence_cooldown_turns, 0)

    def test_earned_familiarity_settings_round_trip(self) -> None:
        # Defaults.
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.earned_familiarity_enabled)
        self.assertAlmostEqual(
            result.memory.earned_familiarity_min_sim, 0.45,
        )
        self.assertEqual(
            result.memory.earned_familiarity_deep_threshold, 14,
        )
        self.assertEqual(
            result.memory.earned_familiarity_cooldown_turns, 12,
        )
        # Overrides + clamps.
        path = self._write_config(
            agent_extra={"earned_familiarity_enabled": False},
            memory_extra={
                "earned_familiarity_min_sim": 2.0,         # clamped 1.0
                "earned_familiarity_deep_threshold": -3,   # floor 1
                "earned_familiarity_cooldown_turns": -5,   # floor 0
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.earned_familiarity_enabled)
        self.assertAlmostEqual(
            result.memory.earned_familiarity_min_sim, 1.0,
        )
        self.assertEqual(
            result.memory.earned_familiarity_deep_threshold, 1,
        )
        self.assertEqual(
            result.memory.earned_familiarity_cooldown_turns, 0,
        )

    def test_user_expertise_settings_round_trip(self) -> None:
        # Defaults.
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.user_expertise_enabled)
        self.assertAlmostEqual(result.memory.user_expertise_min_sim, 0.45)
        self.assertAlmostEqual(
            result.memory.user_expertise_learning_rate, 0.25,
        )
        self.assertEqual(result.memory.user_expertise_min_samples, 4)
        self.assertAlmostEqual(
            result.memory.user_expertise_novice_threshold, -0.35,
        )
        self.assertAlmostEqual(
            result.memory.user_expertise_expert_threshold, 0.35,
        )
        self.assertEqual(result.memory.user_expertise_cooldown_turns, 12)
        # Overrides + clamps.
        path = self._write_config(
            agent_extra={"user_expertise_enabled": False},
            memory_extra={
                "user_expertise_min_sim": 2.0,             # clamp 1.0
                "user_expertise_learning_rate": 5.0,       # clamp 1.0
                "user_expertise_min_samples": 0,           # floor 1
                "user_expertise_novice_threshold": 0.9,    # clamp 0.0
                "user_expertise_expert_threshold": 9.0,    # clamp 1.0
                "user_expertise_cooldown_turns": -5,       # floor 0
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.user_expertise_enabled)
        self.assertAlmostEqual(result.memory.user_expertise_min_sim, 1.0)
        self.assertAlmostEqual(
            result.memory.user_expertise_learning_rate, 1.0,
        )
        self.assertEqual(result.memory.user_expertise_min_samples, 1)
        self.assertAlmostEqual(
            result.memory.user_expertise_novice_threshold, 0.0,
        )
        self.assertAlmostEqual(
            result.memory.user_expertise_expert_threshold, 1.0,
        )
        self.assertEqual(result.memory.user_expertise_cooldown_turns, 0)

    def test_vitality_settings_round_trip(self) -> None:
        # Defaults.
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.vitality_enabled)
        self.assertEqual(result.agent.vitality_check_interval_seconds, 900)
        self.assertAlmostEqual(
            result.memory.vitality_recover_half_life_hours, 2.0,
        )
        self.assertAlmostEqual(result.memory.vitality_low_threshold, 0.30)
        self.assertAlmostEqual(result.memory.vitality_high_threshold, 0.70)
        self.assertAlmostEqual(result.memory.vitality_boost_max, 0.15)
        self.assertAlmostEqual(result.memory.vitality_proactive_factor, 0.4)
        self.assertTrue(result.agent.vitality_rhythm_enabled)
        self.assertAlmostEqual(
            result.memory.vitality_rhythm_exception_chance, 0.3,
        )
        # Overrides + clamps.
        path = self._write_config(
            agent_extra={
                "vitality_enabled": False,
                "vitality_check_interval_seconds": 5,  # floor 60
                "vitality_rhythm_enabled": False,
            },
            memory_extra={
                "vitality_low_threshold": 2.0,        # clamped 1.0
                "vitality_high_threshold": -1.0,      # clamped 0.0
                "vitality_proactive_factor": 5.0,     # clamped 1.0
                "vitality_recover_half_life_hours": 0.0,  # floor 0.01
                "vitality_rhythm_exception_chance": 5.0,  # clamped 1.0
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.vitality_enabled)
        self.assertEqual(result.agent.vitality_check_interval_seconds, 60)
        self.assertAlmostEqual(result.memory.vitality_low_threshold, 1.0)
        self.assertAlmostEqual(result.memory.vitality_high_threshold, 0.0)
        self.assertAlmostEqual(result.memory.vitality_proactive_factor, 1.0)
        self.assertAlmostEqual(
            result.memory.vitality_recover_half_life_hours, 0.01,
        )
        self.assertFalse(result.agent.vitality_rhythm_enabled)
        self.assertAlmostEqual(
            result.memory.vitality_rhythm_exception_chance, 1.0,
        )

    def test_context_budget_settings_round_trip(self) -> None:
        # Defaults.
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.memory.context_budget_enabled)
        self.assertAlmostEqual(result.memory.context_budget_fraction, 0.15)
        self.assertEqual(result.memory.context_budget_max_tokens, 4096)
        self.assertEqual(result.memory.context_budget_memory_cap, 8)
        self.assertEqual(result.memory.context_budget_core_cap, 2)
        self.assertAlmostEqual(
            result.memory.context_budget_core_min_confidence, 0.75,
        )
        # Overrides + clamps (fraction capped at 0.8, weights floored at 0).
        path = self._write_config(
            memory_extra={
                "context_budget_enabled": False,
                "context_budget_fraction": 5.0,      # clamped to 0.8
                "context_budget_concept_min_relevance": -1.0,  # clamped to 0
                "context_budget_memory_cap": 3,
                "context_budget_core_cap": 5,
                "context_budget_core_min_confidence": 2.0,  # clamped to 1
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.memory.context_budget_enabled)
        self.assertAlmostEqual(result.memory.context_budget_fraction, 0.8)
        self.assertAlmostEqual(
            result.memory.context_budget_concept_min_relevance, 0.0,
        )
        self.assertEqual(result.memory.context_budget_memory_cap, 3)
        self.assertEqual(result.memory.context_budget_core_cap, 5)
        self.assertAlmostEqual(
            result.memory.context_budget_core_min_confidence, 1.0,
        )

    def test_context_budget_core_lane_legacy_identity_keys(self) -> None:
        # Pre-L27 configs used ``context_budget_identity_*``; they still parse
        # into the renamed ``core`` lane so existing user.json keeps working.
        path = self._write_config(
            memory_extra={
                "context_budget_identity_cap": 4,
                "context_budget_identity_min_confidence": 0.6,
            },
        )
        result = load_settings(config_path=path)
        self.assertEqual(result.memory.context_budget_core_cap, 4)
        self.assertAlmostEqual(
            result.memory.context_budget_core_min_confidence, 0.6,
        )

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


class PreThoughtSettingsTests(unittest.TestCase):
    """K11: pre-thought agent + memory knobs default / round-trip / clamp."""

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
            "pre_thought_enabled",
            "pre_thought_max_active",
            "pre_thought_candidates",
            "pre_thought_max_per_run",
            "pre_thought_min_novelty",
            "pre_thought_per_hour_cap",
            "pre_thought_per_day_cap",
        ):
            cfg.get("agent", {}).pop(k, None)
        cfg.get("memory", {}).pop("pre_thought_interval_seconds", None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        if memory_extra is not None:
            cfg["memory"] = {**cfg.get("memory", {}), **memory_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults(self) -> None:
        result = load_settings(config_path=self._write_config())
        a, m = result.agent, result.memory
        self.assertTrue(a.pre_thought_enabled)
        self.assertEqual(a.pre_thought_max_active, 12)
        self.assertEqual(a.pre_thought_candidates, 4)
        self.assertEqual(a.pre_thought_max_per_run, 2)
        self.assertAlmostEqual(a.pre_thought_min_novelty, 0.85)
        self.assertEqual(a.pre_thought_per_hour_cap, 6)
        self.assertEqual(a.pre_thought_per_day_cap, 40)
        self.assertEqual(m.pre_thought_interval_seconds, 3600)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={
                "pre_thought_enabled": False,
                "pre_thought_max_active": 20,
                "pre_thought_max_per_run": 3,
            },
            memory_extra={"pre_thought_interval_seconds": 900},
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.pre_thought_enabled)
        self.assertEqual(result.agent.pre_thought_max_active, 20)
        self.assertEqual(result.agent.pre_thought_max_per_run, 3)
        self.assertEqual(result.memory.pre_thought_interval_seconds, 900)

    def test_clamps(self) -> None:
        path = self._write_config(
            agent_extra={
                "pre_thought_min_novelty": 99.0,
                "pre_thought_max_active": 0,
            },
            memory_extra={"pre_thought_interval_seconds": 1},
        )
        result = load_settings(config_path=path)
        self.assertAlmostEqual(result.agent.pre_thought_min_novelty, 1.0)
        self.assertEqual(result.agent.pre_thought_max_active, 1)
        self.assertEqual(result.memory.pre_thought_interval_seconds, 60)


class ContagionSettingsTests(unittest.TestCase):
    """K37: emotional contagion agent knobs default / round-trip / clamp."""

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
        for k in (
            "contagion_enabled",
            "contagion_strength",
            "contagion_max_per_turn",
        ):
            cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults(self) -> None:
        a = load_settings(config_path=self._write_config()).agent
        self.assertTrue(a.contagion_enabled)
        self.assertAlmostEqual(a.contagion_strength, 0.15)
        self.assertAlmostEqual(a.contagion_max_per_turn, 0.05)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(agent_extra={
            "contagion_enabled": False,
            "contagion_strength": 0.3,
            "contagion_max_per_turn": 0.1,
        })
        a = load_settings(config_path=path).agent
        self.assertFalse(a.contagion_enabled)
        self.assertAlmostEqual(a.contagion_strength, 0.3)
        self.assertAlmostEqual(a.contagion_max_per_turn, 0.1)

    def test_clamps(self) -> None:
        path = self._write_config(agent_extra={
            "contagion_strength": 99.0,
            "contagion_max_per_turn": 99.0,
        })
        a = load_settings(config_path=path).agent
        self.assertAlmostEqual(a.contagion_strength, 1.0)
        self.assertAlmostEqual(a.contagion_max_per_turn, 0.5)


class QuestionBalanceSettingsTests(unittest.TestCase):
    """K47: question/share balance agent knobs default / round-trip / clamp."""

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
        for k in (
            "question_balance_enabled",
            "question_balance_ratio_threshold",
            "question_balance_window",
            "question_balance_suppress_turns",
        ):
            cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults(self) -> None:
        a = load_settings(config_path=self._write_config()).agent
        self.assertTrue(a.question_balance_enabled)
        self.assertAlmostEqual(a.question_balance_ratio_threshold, 0.55)
        self.assertEqual(a.question_balance_window, 10)
        self.assertEqual(a.question_balance_suppress_turns, 2)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(agent_extra={
            "question_balance_enabled": False,
            "question_balance_ratio_threshold": 0.7,
            "question_balance_window": 6,
            "question_balance_suppress_turns": 3,
        })
        a = load_settings(config_path=path).agent
        self.assertFalse(a.question_balance_enabled)
        self.assertAlmostEqual(a.question_balance_ratio_threshold, 0.7)
        self.assertEqual(a.question_balance_window, 6)
        self.assertEqual(a.question_balance_suppress_turns, 3)

    def test_clamps(self) -> None:
        path = self._write_config(agent_extra={
            "question_balance_ratio_threshold": 99.0,
            "question_balance_window": 1,
            "question_balance_suppress_turns": -5,
        })
        a = load_settings(config_path=path).agent
        self.assertAlmostEqual(a.question_balance_ratio_threshold, 1.0)
        self.assertEqual(a.question_balance_window, 2)
        self.assertEqual(a.question_balance_suppress_turns, 0)


class TeaseRhythmSettingsTests(unittest.TestCase):
    """K48: tease-rhythm agent knobs default / round-trip / clamp."""

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
        for k in (
            "tease_rhythm_enabled",
            "tease_rhythm_window",
            "tease_rhythm_consecutive_cap",
            "tease_rhythm_green_light_humor",
            "tease_rhythm_cooldown_turns",
        ):
            cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults(self) -> None:
        a = load_settings(config_path=self._write_config()).agent
        self.assertTrue(a.tease_rhythm_enabled)
        self.assertEqual(a.tease_rhythm_window, 6)
        self.assertEqual(a.tease_rhythm_consecutive_cap, 3)
        self.assertAlmostEqual(a.tease_rhythm_green_light_humor, 0.2)
        self.assertEqual(a.tease_rhythm_cooldown_turns, 3)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(agent_extra={
            "tease_rhythm_enabled": False,
            "tease_rhythm_window": 8,
            "tease_rhythm_consecutive_cap": 2,
            "tease_rhythm_green_light_humor": 0.4,
            "tease_rhythm_cooldown_turns": 5,
        })
        a = load_settings(config_path=path).agent
        self.assertFalse(a.tease_rhythm_enabled)
        self.assertEqual(a.tease_rhythm_window, 8)
        self.assertEqual(a.tease_rhythm_consecutive_cap, 2)
        self.assertAlmostEqual(a.tease_rhythm_green_light_humor, 0.4)
        self.assertEqual(a.tease_rhythm_cooldown_turns, 5)

    def test_clamps(self) -> None:
        path = self._write_config(agent_extra={
            "tease_rhythm_window": 1,
            "tease_rhythm_consecutive_cap": 0,
            "tease_rhythm_green_light_humor": 99.0,
            "tease_rhythm_cooldown_turns": -3,
        })
        a = load_settings(config_path=path).agent
        self.assertEqual(a.tease_rhythm_window, 2)
        self.assertEqual(a.tease_rhythm_consecutive_cap, 1)
        self.assertAlmostEqual(a.tease_rhythm_green_light_humor, 1.0)
        self.assertEqual(a.tease_rhythm_cooldown_turns, 0)


class ThreadResummarySettingsTests(unittest.TestCase):
    """K21: thread re-summary agent + memory knobs default / round-trip / clamp."""

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
            "thread_resummary_enabled",
            "thread_resummary_min_messages",
            "thread_resummary_message_interval",
            "thread_resummary_max_age_hours",
            "thread_resummary_per_hour_cap",
            "thread_resummary_per_day_cap",
        ):
            cfg.get("agent", {}).pop(k, None)
        cfg.get("memory", {}).pop("thread_resummary_interval_seconds", None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        if memory_extra is not None:
            cfg["memory"] = {**cfg.get("memory", {}), **memory_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults(self) -> None:
        result = load_settings(config_path=self._write_config())
        a, m = result.agent, result.memory
        self.assertTrue(a.thread_resummary_enabled)
        self.assertEqual(a.thread_resummary_min_messages, 12)
        self.assertEqual(a.thread_resummary_message_interval, 50)
        self.assertAlmostEqual(a.thread_resummary_max_age_hours, 24.0)
        self.assertEqual(a.thread_resummary_per_hour_cap, 6)
        self.assertEqual(a.thread_resummary_per_day_cap, 24)
        self.assertEqual(m.thread_resummary_interval_seconds, 3600)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={
                "thread_resummary_enabled": False,
                "thread_resummary_message_interval": 30,
                "thread_resummary_max_age_hours": 12.0,
            },
            memory_extra={"thread_resummary_interval_seconds": 900},
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.thread_resummary_enabled)
        self.assertEqual(result.agent.thread_resummary_message_interval, 30)
        self.assertAlmostEqual(result.agent.thread_resummary_max_age_hours, 12.0)
        self.assertEqual(result.memory.thread_resummary_interval_seconds, 900)

    def test_clamps(self) -> None:
        path = self._write_config(
            agent_extra={
                "thread_resummary_min_messages": 0,
                "thread_resummary_message_interval": 0,
                "thread_resummary_max_age_hours": -5.0,
            },
            memory_extra={"thread_resummary_interval_seconds": 1},
        )
        result = load_settings(config_path=path)
        self.assertEqual(result.agent.thread_resummary_min_messages, 1)
        self.assertEqual(result.agent.thread_resummary_message_interval, 1)
        self.assertAlmostEqual(result.agent.thread_resummary_max_age_hours, 0.0)
        self.assertEqual(result.memory.thread_resummary_interval_seconds, 60)


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
            result.memory.forward_curiosity_interval_seconds, 900,
        )
        self.assertEqual(
            result.memory.forward_curiosity_cooldown_seconds, 3600,
        )
        self.assertAlmostEqual(
            result.memory.forward_curiosity_min_gap_hours, 4.0,
        )
        self.assertEqual(result.memory.forward_curiosity_journal_max, 8)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={"forward_curiosity_enabled": False},
            memory_extra={
                "forward_curiosity_interval_seconds": 900,
                "forward_curiosity_min_gap_hours": 6.0,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.forward_curiosity_enabled)
        self.assertEqual(
            result.memory.forward_curiosity_interval_seconds, 900,
        )
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


class DreamHotClusterSettingsTests(unittest.TestCase):
    """K65e: dream hot-cluster agent switch + recency knob default/round-trip/clamp."""

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
        for k in ("dream_hot_cluster_enabled", "dream_hot_cluster_recency_days"):
            cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_when_missing(self) -> None:
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.dream_hot_cluster_enabled)
        self.assertAlmostEqual(result.agent.dream_hot_cluster_recency_days, 3.0)

    def test_override_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={
                "dream_hot_cluster_enabled": False,
                "dream_hot_cluster_recency_days": 1.5,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.dream_hot_cluster_enabled)
        self.assertAlmostEqual(result.agent.dream_hot_cluster_recency_days, 1.5)

    def test_clamps_negative_recency(self) -> None:
        path = self._write_config(
            agent_extra={"dream_hot_cluster_recency_days": -2.0},
        )
        result = load_settings(config_path=path)
        self.assertAlmostEqual(result.agent.dream_hot_cluster_recency_days, 0.0)


class CuriosityClusterAnchorSettingsTests(unittest.TestCase):
    """K65c: curiosity-worker cluster-anchor agent knobs default/round-trip/clamp."""

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
        for k in (
            "curiosity_worker_cluster_anchor_enabled",
            "curiosity_worker_quiet_days",
            "curiosity_subject_quota",
        ):
            cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_load_when_keys_missing(self) -> None:
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.curiosity_worker_cluster_anchor_enabled)
        self.assertAlmostEqual(result.agent.curiosity_worker_quiet_days, 7.0)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={
                "curiosity_worker_cluster_anchor_enabled": False,
                "curiosity_worker_quiet_days": 14.0,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.curiosity_worker_cluster_anchor_enabled)
        self.assertAlmostEqual(result.agent.curiosity_worker_quiet_days, 14.0)

    def test_clamps_negative_quiet_days(self) -> None:
        path = self._write_config(
            agent_extra={"curiosity_worker_quiet_days": -3.0},
        )
        result = load_settings(config_path=path)
        self.assertAlmostEqual(result.agent.curiosity_worker_quiet_days, 0.0)

    def test_subject_quota_defaults_and_clamps(self) -> None:
        # K87. Shared by all three curiosity generators.
        self.assertAlmostEqual(
            load_settings(
                config_path=self._write_config()
            ).agent.curiosity_subject_quota,
            0.4,
        )
        for raw, expected in ((-1.0, 0.0), (2.5, 1.0), (0.75, 0.75)):
            path = self._write_config(
                agent_extra={"curiosity_subject_quota": raw},
            )
            self.assertAlmostEqual(
                load_settings(config_path=path).agent.curiosity_subject_quota,
                expected,
            )


class BeliefInterestBiasSettingsTests(unittest.TestCase):
    """K65b: belief-worker interest-bias agent switch + memory knobs."""

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
        cfg.get("agent", {}).pop("belief_interest_bias_enabled", None)
        for k in (
            "belief_worker_interest_top_n",
            "belief_worker_reconsider_max",
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
        self.assertTrue(result.agent.belief_interest_bias_enabled)
        self.assertEqual(result.memory.belief_worker_interest_top_n, 5)
        self.assertEqual(result.memory.belief_worker_reconsider_max, 3)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={"belief_interest_bias_enabled": False},
            memory_extra={
                "belief_worker_interest_top_n": 8,
                "belief_worker_reconsider_max": 2,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.belief_interest_bias_enabled)
        self.assertEqual(result.memory.belief_worker_interest_top_n, 8)
        self.assertEqual(result.memory.belief_worker_reconsider_max, 2)

    def test_clamps_out_of_range_values(self) -> None:
        path = self._write_config(
            memory_extra={
                "belief_worker_interest_top_n": -3,  # floor 0
                "belief_worker_reconsider_max": -1,  # floor 0
            },
        )
        result = load_settings(config_path=path)
        self.assertEqual(result.memory.belief_worker_interest_top_n, 0)
        self.assertEqual(result.memory.belief_worker_reconsider_max, 0)


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
            result.memory.promise_followthrough_interval_seconds, 900,
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


class PromiseWorkerSettingsTests(unittest.TestCase):
    """Phase 3c (reworked): promise-extraction-worker knobs round-trip + clamps."""

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
            "promise_worker_enabled",
            "promise_worker_per_hour_cap",
            "promise_worker_per_day_cap",
        ):
            cfg.get("agent", {}).pop(k, None)
        for k in (
            "promise_worker_interval_seconds",
            "promise_worker_lookback_turns",
            "promise_worker_max_per_run",
            "promise_worker_max_msg_chars",
            "promise_worker_max_transcript_chars",
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
        self.assertTrue(result.agent.promise_worker_enabled)
        self.assertEqual(result.agent.promise_worker_per_hour_cap, 10)
        self.assertEqual(result.agent.promise_worker_per_day_cap, 60)
        self.assertEqual(
            result.memory.promise_worker_interval_seconds, 600,
        )
        self.assertEqual(result.memory.promise_worker_lookback_turns, 12)
        self.assertEqual(result.memory.promise_worker_max_per_run, 5)
        self.assertEqual(result.memory.promise_worker_max_msg_chars, 2000)
        self.assertEqual(
            result.memory.promise_worker_max_transcript_chars, 8000,
        )

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={
                "promise_worker_enabled": False,
                "promise_worker_per_hour_cap": 3,
                "promise_worker_per_day_cap": 9,
            },
            memory_extra={
                "promise_worker_interval_seconds": 1200,
                "promise_worker_lookback_turns": 20,
                "promise_worker_max_per_run": 8,
                "promise_worker_max_msg_chars": 4000,
                "promise_worker_max_transcript_chars": 12000,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.promise_worker_enabled)
        self.assertEqual(result.agent.promise_worker_per_hour_cap, 3)
        self.assertEqual(result.agent.promise_worker_per_day_cap, 9)
        self.assertEqual(
            result.memory.promise_worker_interval_seconds, 1200,
        )
        self.assertEqual(result.memory.promise_worker_lookback_turns, 20)
        self.assertEqual(result.memory.promise_worker_max_per_run, 8)
        self.assertEqual(result.memory.promise_worker_max_msg_chars, 4000)
        self.assertEqual(
            result.memory.promise_worker_max_transcript_chars, 12000,
        )

    def test_clamps_out_of_range_values(self) -> None:
        path = self._write_config(
            memory_extra={
                "promise_worker_interval_seconds": 1,  # floor 60
                "promise_worker_lookback_turns": 0,  # floor 1
                "promise_worker_max_per_run": 0,  # floor 1
                "promise_worker_max_msg_chars": 10,  # floor 200
                "promise_worker_max_transcript_chars": 10,  # floor 500
            },
        )
        result = load_settings(config_path=path)
        self.assertEqual(
            result.memory.promise_worker_interval_seconds, 60,
        )
        self.assertEqual(result.memory.promise_worker_lookback_turns, 1)
        self.assertEqual(result.memory.promise_worker_max_per_run, 1)
        self.assertEqual(result.memory.promise_worker_max_msg_chars, 200)
        self.assertEqual(
            result.memory.promise_worker_max_transcript_chars, 500,
        )


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

    def test_implicit_need_round_trip(self) -> None:
        # K69: agent master switch + memory confidence floor (clamped).
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.implicit_need_enabled)
        self.assertAlmostEqual(
            result.memory.implicit_need_min_confidence, 2.0,
        )
        path = self._write_config(
            agent_extra={"implicit_need_enabled": False},
        )
        # Inject the memory override directly (clamp floor 0.5).
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg["memory"] = {
            **cfg.get("memory", {}),
            "implicit_need_min_confidence": 0.0,
        }
        path.write_text(json.dumps(cfg), encoding="utf-8")
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.implicit_need_enabled)
        self.assertAlmostEqual(
            result.memory.implicit_need_min_confidence, 0.5,
        )

    def test_growth_witness_round_trip(self) -> None:
        # K70: agent master switch + cadence/cooldown + memory thresholds.
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.growth_witness_enabled)
        self.assertEqual(
            result.agent.growth_witness_check_interval_seconds, 21600,
        )
        self.assertAlmostEqual(
            result.agent.growth_witness_cooldown_days, 14.0,
        )
        self.assertEqual(result.memory.growth_witness_min_samples, 10)
        self.assertAlmostEqual(
            result.memory.growth_witness_min_valence_delta, 0.25,
        )
        path = self._write_config(
            agent_extra={
                "growth_witness_enabled": False,
                # interval clamps to floor 60.
                "growth_witness_check_interval_seconds": 5,
            },
        )
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg["memory"] = {
            **cfg.get("memory", {}),
            "growth_witness_min_samples": 1,  # clamps to floor 2
            "growth_witness_journal_max": 0,  # clamps to floor 1
        }
        path.write_text(json.dumps(cfg), encoding="utf-8")
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.growth_witness_enabled)
        self.assertEqual(
            result.agent.growth_witness_check_interval_seconds, 60,
        )
        self.assertEqual(result.memory.growth_witness_min_samples, 2)
        self.assertEqual(result.memory.growth_witness_journal_max, 1)

    def test_self_callback_round_trip(self) -> None:
        # K71: agent master switch + heartbeat + memory age floor. The
        # old ``self_callback_cooldown_days`` is gone -- rarity moved to
        # ``CuePolicy.surface_cooldown_hours``, where it paces surfacing
        # rather than production.
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.self_callback_enabled)
        self.assertEqual(
            result.agent.self_callback_check_interval_seconds, 21600,
        )
        self.assertFalse(
            hasattr(result.agent, "self_callback_cooldown_days"),
        )
        self.assertTrue(result.agent.self_callback_llm_enabled)
        self.assertEqual(result.memory.self_callback_min_age_days, 14)
        path = self._write_config(
            agent_extra={
                "self_callback_enabled": False,
                "self_callback_llm_enabled": False,
                "self_callback_check_interval_seconds": 5,  # floor 60
            },
        )
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg["memory"] = {
            **cfg.get("memory", {}),
            "self_callback_min_age_days": 0,  # floor 1
            "self_callback_journal_max": 0,  # floor 1
        }
        path.write_text(json.dumps(cfg), encoding="utf-8")
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.self_callback_enabled)
        self.assertFalse(result.agent.self_callback_llm_enabled)
        self.assertEqual(
            result.agent.self_callback_check_interval_seconds, 60,
        )
        self.assertEqual(result.memory.self_callback_min_age_days, 1)
        self.assertEqual(result.memory.self_callback_journal_max, 1)

    def test_wellbeing_concern_round_trip(self) -> None:
        # K72: agent master switch + heartbeat + memory thresholds. The
        # cooldown that used to sit here is the type's
        # ``CuePolicy.surface_cooldown_hours``.
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.wellbeing_concern_enabled)
        self.assertEqual(
            result.agent.wellbeing_concern_check_interval_seconds, 21600,
        )
        self.assertFalse(
            hasattr(result.agent, "wellbeing_concern_cooldown_days"),
        )
        self.assertEqual(result.memory.wellbeing_concern_window_days, 7)
        self.assertEqual(result.memory.wellbeing_concern_late_night_min, 3)
        self.assertEqual(result.memory.wellbeing_concern_neglect_min_days, 2)
        self.assertEqual(result.memory.wellbeing_concern_rough_run, 5)
        self.assertAlmostEqual(
            result.memory.wellbeing_concern_rough_threshold, -0.25,
        )
        path = self._write_config(
            agent_extra={
                "wellbeing_concern_enabled": False,
                "wellbeing_concern_check_interval_seconds": 5,  # floor 60
            },
        )
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg["memory"] = {
            **cfg.get("memory", {}),
            "wellbeing_concern_window_days": 0,  # floor 1
            "wellbeing_concern_late_night_min": 0,  # floor 1
            "wellbeing_concern_journal_max": 0,  # floor 1
        }
        path.write_text(json.dumps(cfg), encoding="utf-8")
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.wellbeing_concern_enabled)
        self.assertEqual(
            result.agent.wellbeing_concern_check_interval_seconds, 60,
        )
        self.assertEqual(result.memory.wellbeing_concern_window_days, 1)
        self.assertEqual(result.memory.wellbeing_concern_late_night_min, 1)
        self.assertEqual(result.memory.wellbeing_concern_journal_max, 1)

    def test_inside_joke_birth_round_trip(self) -> None:
        # K80: agent master switch + cooldown + min phrase length.
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.inside_joke_birth_enabled)
        self.assertAlmostEqual(
            result.agent.inside_joke_birth_cooldown_hours, 24.0,
        )
        self.assertEqual(result.agent.inside_joke_birth_min_words, 3)
        path = self._write_config(
            agent_extra={
                "inside_joke_birth_enabled": False,
                "inside_joke_birth_cooldown_hours": -5.0,  # floor 0
                "inside_joke_birth_min_words": 1,  # floor 2
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.inside_joke_birth_enabled)
        self.assertAlmostEqual(
            result.agent.inside_joke_birth_cooldown_hours, 0.0,
        )
        self.assertEqual(result.agent.inside_joke_birth_min_words, 2)

    def test_voice_adoption_round_trip(self) -> None:
        # K26: agent master switch + cadence, memory-side slow knobs.
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.voice_adoption_enabled)
        self.assertEqual(result.agent.voice_adoption_interval_seconds, 86400)
        self.assertAlmostEqual(result.memory.voice_adoption_min_age_days, 14.0)
        self.assertAlmostEqual(
            result.memory.voice_adoption_min_days_between, 10.0,
        )
        self.assertEqual(result.memory.voice_adoption_max_adopted, 3)
        self.assertEqual(result.memory.voice_adoption_max_rendered, 2)
        path = self._write_config(
            agent_extra={
                "voice_adoption_enabled": False,
                "voice_adoption_interval_seconds": 5,  # floor 60
            },
        )
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg["memory"] = {
            **cfg.get("memory", {}),
            "voice_adoption_min_age_days": -1.0,  # floor 0
            "voice_adoption_min_days_between": -1.0,  # floor 0
            "voice_adoption_max_adopted": 0,  # floor 1
            "voice_adoption_max_rendered": 0,  # floor 1
        }
        path.write_text(json.dumps(cfg), encoding="utf-8")
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.voice_adoption_enabled)
        self.assertEqual(result.agent.voice_adoption_interval_seconds, 60)
        self.assertAlmostEqual(result.memory.voice_adoption_min_age_days, 0.0)
        self.assertAlmostEqual(
            result.memory.voice_adoption_min_days_between, 0.0,
        )
        self.assertEqual(result.memory.voice_adoption_max_adopted, 1)
        self.assertEqual(result.memory.voice_adoption_max_rendered, 1)

    def test_shared_ritual_round_trip(self) -> None:
        # K73: agent master switch + heartbeat + memory thresholds. The
        # surface cooldown that used to sit here is the type's
        # ``CuePolicy.surface_cooldown_hours``.
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.shared_ritual_enabled)
        self.assertEqual(
            result.agent.shared_ritual_check_interval_seconds, 86400,
        )
        self.assertFalse(
            hasattr(result.agent, "shared_ritual_surface_cooldown_days"),
        )
        self.assertEqual(result.memory.shared_ritual_window_days, 56)
        self.assertEqual(result.memory.shared_ritual_min_weeks, 3)
        self.assertAlmostEqual(result.memory.shared_ritual_min_share, 0.34)
        self.assertEqual(result.memory.shared_ritual_max_active, 6)
        self.assertEqual(result.memory.shared_ritual_min_messages, 30)
        path = self._write_config(
            agent_extra={
                "shared_ritual_enabled": False,
                "shared_ritual_check_interval_seconds": 5,  # floor 60
            },
        )
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg["memory"] = {
            **cfg.get("memory", {}),
            "shared_ritual_window_days": 0,  # floor 7
            "shared_ritual_min_weeks": 0,  # floor 1
            "shared_ritual_min_share": 5.0,  # clamp to 1.0
            "shared_ritual_max_active": 0,  # floor 1
            "shared_ritual_min_messages": 0,  # floor 1
        }
        path.write_text(json.dumps(cfg), encoding="utf-8")
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.shared_ritual_enabled)
        self.assertEqual(
            result.agent.shared_ritual_check_interval_seconds, 60,
        )
        self.assertEqual(result.memory.shared_ritual_window_days, 7)
        self.assertEqual(result.memory.shared_ritual_min_weeks, 1)
        self.assertAlmostEqual(result.memory.shared_ritual_min_share, 1.0)
        self.assertEqual(result.memory.shared_ritual_max_active, 1)
        self.assertEqual(result.memory.shared_ritual_min_messages, 1)

    def test_humor_style_round_trip(self) -> None:
        # K74: agent-side humor-style learner knobs + clamps.
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.humor_style_enabled)
        self.assertAlmostEqual(result.agent.humor_style_learning_rate, 0.04)
        self.assertAlmostEqual(result.agent.humor_style_floor, 0.05)
        self.assertAlmostEqual(result.agent.humor_style_hint_min_rel, 1.25)
        self.assertEqual(
            result.agent.humor_style_decay_interval_seconds, 21600,
        )
        path = self._write_config(
            agent_extra={
                "humor_style_enabled": False,
                "humor_style_floor": 0.9,  # clamp to 0.2
                "humor_style_hint_min_rel": 0.1,  # clamp to 1.0
                "humor_style_decay_interval_seconds": 5,  # floor 60
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.humor_style_enabled)
        self.assertAlmostEqual(result.agent.humor_style_floor, 0.2)
        self.assertAlmostEqual(result.agent.humor_style_hint_min_rel, 1.0)
        self.assertEqual(
            result.agent.humor_style_decay_interval_seconds, 60,
        )

    def test_flashbulb_round_trip(self) -> None:
        # K76: memory-side affective-salience knobs + clamps.
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.memory.flashbulb_enabled)
        self.assertAlmostEqual(result.memory.flashbulb_max_boost, 0.35)
        self.assertAlmostEqual(result.memory.flashbulb_arousal_weight, 0.6)
        self.assertAlmostEqual(result.memory.flashbulb_episode_weight, 0.7)
        self.assertAlmostEqual(result.memory.flashbulb_arousal_neutral, 0.4)
        path = self._write_config()
        cfg = json.loads(path.read_text(encoding="utf-8"))
        cfg["memory"] = {
            **cfg.get("memory", {}),
            "flashbulb_enabled": False,
            "flashbulb_max_boost": 5.0,  # clamp to 1.0
            "flashbulb_arousal_neutral": 2.0,  # clamp to 1.0
        }
        path.write_text(json.dumps(cfg), encoding="utf-8")
        result = load_settings(config_path=path)
        self.assertFalse(result.memory.flashbulb_enabled)
        self.assertAlmostEqual(result.memory.flashbulb_max_boost, 1.0)
        self.assertAlmostEqual(result.memory.flashbulb_arousal_neutral, 1.0)

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
        "stance_persistence_enabled",  # K46
    )
    _OI_MEMORY_KEYS = (
        "opinion_injection_min_cosine",
        "opinion_injection_min_user_words",
        "opinion_injection_cooldown_turns",
        "opinion_injection_per_session_cap",
        "opinion_injection_per_hour_cap",
        "opinion_injection_per_day_cap",
        "stance_persistence_window",  # K46
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
        # K46 stance persistence.
        self.assertTrue(result.agent.stance_persistence_enabled)
        self.assertEqual(result.memory.stance_persistence_window, 3)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={
                "opinion_injection_enabled": False,
                "opinion_injection_require_definite": True,
                "stance_persistence_enabled": False,
            },
            memory_extra={
                "opinion_injection_min_cosine": 0.70,
                "opinion_injection_min_user_words": 6,
                "opinion_injection_cooldown_turns": 8,
                "opinion_injection_per_session_cap": 1,
                "opinion_injection_per_hour_cap": 12,
                "opinion_injection_per_day_cap": 50,
                "stance_persistence_window": 5,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.opinion_injection_enabled)
        self.assertTrue(result.agent.opinion_injection_require_definite)
        self.assertFalse(result.agent.stance_persistence_enabled)
        self.assertEqual(result.memory.stance_persistence_window, 5)
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
                "stance_persistence_window": -2,
            },
        )
        result = load_settings(config_path=path)
        self.assertEqual(result.memory.opinion_injection_min_user_words, 0)
        self.assertEqual(result.memory.opinion_injection_cooldown_turns, 0)
        self.assertEqual(result.memory.opinion_injection_per_session_cap, 0)
        self.assertEqual(result.memory.opinion_injection_per_hour_cap, 0)
        self.assertEqual(result.memory.opinion_injection_per_day_cap, 0)
        self.assertEqual(result.memory.stance_persistence_window, 0)


class LongArcCallbackSettingsTests(unittest.TestCase):
    """K63: 1 agent flag + 4 memory knobs round-trip with clamps."""

    _LAC_AGENT_KEYS = ("long_arc_callback_enabled",)
    _LAC_MEMORY_KEYS = (
        "long_arc_callback_min_age_days",
        "long_arc_callback_min_cosine",
        "long_arc_callback_per_session_cap",
        "long_arc_callback_min_user_words",
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
            for k in self._LAC_AGENT_KEYS:
                cfg.get("agent", {}).pop(k, None)
            for k in self._LAC_MEMORY_KEYS:
                cfg.get("memory", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        if memory_extra is not None:
            cfg["memory"] = {**cfg.get("memory", {}), **memory_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_load_when_keys_missing(self) -> None:
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.long_arc_callback_enabled)
        self.assertEqual(result.memory.long_arc_callback_min_age_days, 21)
        self.assertAlmostEqual(result.memory.long_arc_callback_min_cosine, 0.55)
        # The wall-clock spacing moved onto CuePolicy.
        self.assertFalse(
            hasattr(result.memory, "long_arc_callback_cooldown_hours"),
        )
        self.assertEqual(result.memory.long_arc_callback_per_session_cap, 1)
        self.assertEqual(result.memory.long_arc_callback_min_user_words, 5)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={"long_arc_callback_enabled": False},
            memory_extra={
                "long_arc_callback_min_age_days": 60,
                "long_arc_callback_min_cosine": 0.7,
                "long_arc_callback_per_session_cap": 2,
                "long_arc_callback_min_user_words": 8,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.long_arc_callback_enabled)
        self.assertEqual(result.memory.long_arc_callback_min_age_days, 60)
        self.assertAlmostEqual(result.memory.long_arc_callback_min_cosine, 0.7)
        self.assertEqual(result.memory.long_arc_callback_per_session_cap, 2)
        self.assertEqual(result.memory.long_arc_callback_min_user_words, 8)

    def test_clamps(self) -> None:
        path = self._write_config(
            memory_extra={
                "long_arc_callback_min_age_days": 0,  # floors at 1
                "long_arc_callback_min_cosine": 5.0,  # clamps to 1.0
                "long_arc_callback_per_session_cap": -1,  # floors at 0
                "long_arc_callback_min_user_words": -4,  # floors at 0
            },
        )
        result = load_settings(config_path=path)
        self.assertEqual(result.memory.long_arc_callback_min_age_days, 1)
        self.assertAlmostEqual(result.memory.long_arc_callback_min_cosine, 1.0)
        self.assertEqual(result.memory.long_arc_callback_per_session_cap, 0)
        self.assertEqual(result.memory.long_arc_callback_min_user_words, 0)


class DormantInterestSettingsTests(unittest.TestCase):
    """K67: 1 agent flag + 6 memory knobs round-trip with clamps."""

    _DI_AGENT_KEYS = ("dormant_interest_enabled",)
    _DI_MEMORY_KEYS = (
        "dormant_interest_interval_seconds",
        "dormant_interest_journal_max",
        "dormant_interest_min_size",
        "dormant_interest_max_clusters",
        "dormant_interest_dormant_days",
        "dormant_interest_topic_cooldown_hours",
        "dormant_interest_surface_cooldown_hours",
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
            for k in self._DI_AGENT_KEYS:
                cfg.get("agent", {}).pop(k, None)
            for k in self._DI_MEMORY_KEYS:
                cfg.get("memory", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        if memory_extra is not None:
            cfg["memory"] = {**cfg.get("memory", {}), **memory_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_load_when_keys_missing(self) -> None:
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.dormant_interest_enabled)
        self.assertEqual(
            result.memory.dormant_interest_interval_seconds, 21600,
        )
        self.assertEqual(result.memory.dormant_interest_journal_max, 6)
        self.assertEqual(result.memory.dormant_interest_min_size, 6)
        self.assertEqual(result.memory.dormant_interest_max_clusters, 40)
        self.assertAlmostEqual(
            result.memory.dormant_interest_dormant_days, 21.0,
        )
        self.assertEqual(
            result.memory.dormant_interest_topic_cooldown_hours, 336,
        )
        self.assertAlmostEqual(
            result.memory.dormant_interest_surface_cooldown_hours, 24.0,
        )

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={"dormant_interest_enabled": False},
            memory_extra={
                "dormant_interest_interval_seconds": 7200,
                "dormant_interest_journal_max": 10,
                "dormant_interest_min_size": 8,
                "dormant_interest_max_clusters": 20,
                "dormant_interest_dormant_days": 30.0,
                "dormant_interest_topic_cooldown_hours": 168,
                "dormant_interest_surface_cooldown_hours": 12.0,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.dormant_interest_enabled)
        self.assertEqual(
            result.memory.dormant_interest_interval_seconds, 7200,
        )
        self.assertEqual(result.memory.dormant_interest_journal_max, 10)
        self.assertEqual(result.memory.dormant_interest_min_size, 8)
        self.assertEqual(result.memory.dormant_interest_max_clusters, 20)
        self.assertAlmostEqual(
            result.memory.dormant_interest_dormant_days, 30.0,
        )
        self.assertEqual(
            result.memory.dormant_interest_topic_cooldown_hours, 168,
        )
        self.assertAlmostEqual(
            result.memory.dormant_interest_surface_cooldown_hours, 12.0,
        )

    def test_clamps(self) -> None:
        path = self._write_config(
            memory_extra={
                "dormant_interest_interval_seconds": 1,  # floors at 60
                "dormant_interest_journal_max": 0,  # floors at 1
                "dormant_interest_min_size": 1,  # floors at 2
                "dormant_interest_max_clusters": 0,  # floors at 1
                "dormant_interest_dormant_days": -5.0,  # floors at 0
                "dormant_interest_topic_cooldown_hours": -3,  # floors at 0
                "dormant_interest_surface_cooldown_hours": -2.0,  # floors at 0
            },
        )
        result = load_settings(config_path=path)
        self.assertEqual(
            result.memory.dormant_interest_interval_seconds, 60,
        )
        self.assertEqual(result.memory.dormant_interest_journal_max, 1)
        self.assertEqual(result.memory.dormant_interest_min_size, 2)
        self.assertEqual(result.memory.dormant_interest_max_clusters, 1)
        self.assertAlmostEqual(
            result.memory.dormant_interest_dormant_days, 0.0,
        )
        self.assertEqual(
            result.memory.dormant_interest_topic_cooldown_hours, 0,
        )
        self.assertAlmostEqual(
            result.memory.dormant_interest_surface_cooldown_hours, 0.0,
        )


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
    """K59: 4 agent knobs round-trip with clamps.

    Was six. How many debts to hold and how long they stay funny moved
    onto the cue policy when the ledger became pool rows, and the two
    that stayed are the two the pool has no field for: an axis floor,
    and an interval J11 moves.
    """

    _KEYS = (
        "tease_economy_enabled",
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
        self.assertEqual(agent.tease_collect_cooldown_hours, 12.0)
        self.assertEqual(agent.tease_min_humor, 0.2)
        self.assertEqual(agent.tease_min_age_hours, 1.0)

    def test_overrides_round_trip(self) -> None:
        agent = load_settings(config_path=self._write_config({
            "tease_economy_enabled": False,
            "tease_collect_cooldown_hours": 1.0,
            "tease_min_humor": 0.5,
            "tease_min_age_hours": 0.0,
        })).agent
        self.assertFalse(agent.tease_economy_enabled)
        self.assertEqual(agent.tease_collect_cooldown_hours, 1.0)
        self.assertEqual(agent.tease_min_humor, 0.5)
        self.assertEqual(agent.tease_min_age_hours, 0.0)

    def test_clamps(self) -> None:
        agent = load_settings(config_path=self._write_config({
            "tease_collect_cooldown_hours": -5.0,
            "tease_min_humor": -3.0,
            "tease_min_age_hours": -1.0,
        })).agent
        self.assertEqual(agent.tease_collect_cooldown_hours, 0.0)
        self.assertEqual(agent.tease_min_humor, -1.0)
        self.assertEqual(agent.tease_min_age_hours, 0.0)

    def test_the_pool_owns_the_shelf_now(self) -> None:
        agent = load_settings(config_path=self._write_config()).agent
        self.assertFalse(hasattr(agent, "tease_cap"))
        self.assertFalse(hasattr(agent, "tease_expiry_days"))


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


class MoodDriftSettingsTests(unittest.TestCase):
    """H3: 3 agent knobs round-trip with the documented clamps."""

    _MD_AGENT_KEYS = (
        "mood_drift_enabled",
        "mood_drift_check_interval_seconds",
        "mood_drift_cooldown_days",
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
        for k in self._MD_AGENT_KEYS:
            cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_load_when_keys_missing(self) -> None:
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.agent.mood_drift_enabled)
        self.assertEqual(
            result.agent.mood_drift_check_interval_seconds, 3600,
        )
        self.assertEqual(result.agent.mood_drift_cooldown_days, 4.0)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            agent_extra={
                "mood_drift_enabled": False,
                "mood_drift_check_interval_seconds": 7200,
                "mood_drift_cooldown_days": 2.5,
            },
        )
        result = load_settings(config_path=path)
        self.assertFalse(result.agent.mood_drift_enabled)
        self.assertEqual(
            result.agent.mood_drift_check_interval_seconds, 7200,
        )
        self.assertEqual(result.agent.mood_drift_cooldown_days, 2.5)

    def test_interval_clamps_to_floor(self) -> None:
        path = self._write_config(
            agent_extra={"mood_drift_check_interval_seconds": 5},
        )
        result = load_settings(config_path=path)
        self.assertEqual(
            result.agent.mood_drift_check_interval_seconds, 60,
        )

    def test_cooldown_clamps_nonnegative(self) -> None:
        path = self._write_config(
            agent_extra={"mood_drift_cooldown_days": -3},
        )
        result = load_settings(config_path=path)
        self.assertEqual(result.agent.mood_drift_cooldown_days, 0.0)


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


class LlmBlockSettingsTests(unittest.TestCase):
    """The ``llm`` block is the only LLM config surface.

    Pins the parts a broken first-run would trip over: the shipped
    defaults are self-consistent, an explicit route ``context_window``
    survives the loader (auto-detect would ask Ollama for the model's
    advertised 256 k maximum), and ``AppSettings.ollama`` is derived
    from the catalogue rather than parsed from a config block.
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

    def _write_config(self, llm_extra: dict | None = None) -> Path:
        cfg = copy.deepcopy(self._base_config)
        if llm_extra is not None:
            cfg["llm"] = {**cfg.get("llm", {}), **llm_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_shipped_defaults_are_self_consistent(self) -> None:
        result = load_settings(config_path=self._write_config())
        ids = {p.id for p in result.llm.providers}
        self.assertIn("local_ollama", ids)
        for role in ("main_chat", "worker_default", "workflow"):
            route = result.llm.routes[role]
            self.assertIn(
                route.provider_id, ids,
                f"route {role} points at an unknown provider",
            )
            self.assertTrue(route.model, f"route {role} has no model")
        self.assertIn(result.llm.embedding.provider_id, ids)

    def test_default_routes_pin_an_explicit_context_window(self) -> None:
        # Left at None the controller asks Ollama for the model's
        # maximum, and current Qwen tags advertise 256 k -- which spills
        # a 9B model out of VRAM on a 12 GB card.
        result = load_settings(config_path=self._write_config())
        for role in ("main_chat", "worker_default", "workflow"):
            self.assertEqual(result.llm.routes[role].context_window, 65_536)

    def test_route_context_window_override_round_trips(self) -> None:
        cfg = copy.deepcopy(self._base_config["llm"])
        cfg["routes"]["main_chat"]["context_window"] = 8192
        result = load_settings(config_path=self._write_config(cfg))
        self.assertEqual(result.llm.routes["main_chat"].context_window, 8192)

    def test_embedding_block_round_trips(self) -> None:
        cfg = copy.deepcopy(self._base_config["llm"])
        cfg["embedding"] = {
            "provider_id": "local_ollama",
            "model": "nomic-embed-text",
            "num_ctx": 512,
            "num_gpu": 0,
        }
        result = load_settings(config_path=self._write_config(cfg))
        self.assertEqual(result.llm.embedding.model, "nomic-embed-text")
        self.assertEqual(result.llm.embedding.num_ctx, 512)
        self.assertEqual(result.llm.embedding.num_gpu, 0)

    def test_ollama_transport_is_derived_from_the_local_provider(self) -> None:
        # ``AppSettings.ollama`` is no longer a parsed config block --
        # it's a transport template built from the catalogue, so callers
        # that still read it see the provider's real endpoint.
        cfg = copy.deepcopy(self._base_config["llm"])
        cfg["providers"][0]["base_url"] = "http://ollama.internal:11434"
        result = load_settings(config_path=self._write_config(cfg))
        self.assertEqual(
            result.ollama.base_url, "http://ollama.internal:11434",
        )

    def test_unknown_provider_kind_falls_back_to_ollama(self) -> None:
        cfg = copy.deepcopy(self._base_config["llm"])
        cfg["providers"][0]["kind"] = "azure"
        result = load_settings(config_path=self._write_config(cfg))
        self.assertEqual(result.llm.providers[0].kind, "ollama")


class UserConfigPathTests(unittest.TestCase):
    """``AIKO_USER_CONFIG`` relocates the writable overrides file.

    The container points it into the data volume — ``/app/config`` sits
    in the image's writable layer, so without the override every setting
    the first-run wizard collects is discarded on the next recreate.
    """

    def test_env_override_wins(self) -> None:
        with mock.patch.dict(
            settings_mod.os.environ,
            {"AIKO_USER_CONFIG": "/tmp/elsewhere/user.json"},
        ):
            resolved = settings_mod._resolve_user_config_path()
        self.assertEqual(resolved, Path("/tmp/elsewhere/user.json"))

    def test_default_is_the_repo_config_dir(self) -> None:
        env = {
            k: v
            for k, v in settings_mod.os.environ.items()
            if k != "AIKO_USER_CONFIG"
        }
        with mock.patch.dict(settings_mod.os.environ, env, clear=True):
            resolved = settings_mod._resolve_user_config_path()
        self.assertEqual(resolved.name, "user.json")
        self.assertEqual(resolved.parent.name, "config")

    def test_blank_env_falls_back_to_the_default(self) -> None:
        with mock.patch.dict(
            settings_mod.os.environ, {"AIKO_USER_CONFIG": "   "},
        ):
            resolved = settings_mod._resolve_user_config_path()
        self.assertEqual(resolved.parent.name, "config")


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


class TaskApprovalSettingsTests(unittest.TestCase):
    """``agent.task_approval_*`` generic approval policy."""

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
            ),
        )
        a = load_settings(config_path=path).agent
        self.assertEqual(a.task_approval_mode, "ask")
        self.assertEqual(a.task_approval_overrides, {})

    def test_dataclass_default_matches_config(self) -> None:
        path = self._write_config()
        a = load_settings(config_path=path).agent
        self.assertEqual(a.task_approval_mode, "ask")

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


class PluginsSettingsTests(unittest.TestCase):
    """Declarative plugin bundles: ``plugins`` block parse + defaults."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.user_json = Path(self._tmp.name) / "user.json"
        patcher = mock.patch.object(
            settings_mod, "USER_CONFIG_PATH", self.user_json,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_config(self, plugins: dict | None = None) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(json.loads(default_path.read_text(encoding="utf-8")))
        cfg.pop("plugins", None)
        if plugins is not None:
            cfg["plugins"] = plugins
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_when_missing(self) -> None:
        result = load_settings(config_path=self._write_config())
        self.assertTrue(result.plugins.enabled)
        self.assertEqual(result.plugins.paths, [])
        self.assertEqual(result.plugins.entries, {})

    def test_round_trip(self) -> None:
        path = self._write_config(
            plugins={
                "enabled": False,
                "paths": ["F:/custom/plugins", ""],
                "entries": {
                    "filesystem": {
                        "enabled": True,
                        "config": {"root": "F:/notes"},
                    },
                    "browser": {"enabled": False},
                    "": {"enabled": True},  # blank id dropped
                    "bad": "not-a-dict",  # dropped
                },
            }
        )
        p = load_settings(config_path=path).plugins
        self.assertFalse(p.enabled)
        self.assertEqual(p.paths, ["F:/custom/plugins"])
        self.assertIn("filesystem", p.entries)
        self.assertTrue(p.entries["filesystem"].enabled)
        self.assertEqual(p.entries["filesystem"].config, {"root": "F:/notes"})
        self.assertFalse(p.entries["browser"].enabled)
        self.assertNotIn("", p.entries)
        self.assertNotIn("bad", p.entries)

    def test_malformed_block_defaults(self) -> None:
        path = self._write_config(plugins=["not", "a", "dict"])
        p = load_settings(config_path=path).plugins
        self.assertTrue(p.enabled)
        self.assertEqual(p.entries, {})


class ReconnectionSettingsTests(unittest.TestCase):
    """J5: reconnection ritual agent knobs default / round-trip / clamp."""

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
        for k in ("reconnection_enabled", "reconnection_base_gap_hours"):
            cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults(self) -> None:
        a = load_settings(config_path=self._write_config()).agent
        self.assertTrue(a.reconnection_enabled)
        self.assertAlmostEqual(a.reconnection_base_gap_hours, 24.0)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(agent_extra={
            "reconnection_enabled": False,
            "reconnection_base_gap_hours": 48.0,
        })
        a = load_settings(config_path=path).agent
        self.assertFalse(a.reconnection_enabled)
        self.assertAlmostEqual(a.reconnection_base_gap_hours, 48.0)

    def test_base_gap_floor(self) -> None:
        path = self._write_config(
            agent_extra={"reconnection_base_gap_hours": 0.1}
        )
        a = load_settings(config_path=path).agent
        self.assertAlmostEqual(a.reconnection_base_gap_hours, 1.0)


class SessionClockSettingsTests(unittest.TestCase):
    """K-time4: session-clock agent knobs default / round-trip / clamp."""

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
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults(self) -> None:
        a = load_settings(config_path=self._write_config()).agent
        self.assertTrue(a.session_clock_enabled)
        self.assertAlmostEqual(a.session_clock_long_minutes, 60.0)
        self.assertAlmostEqual(a.session_clock_very_long_minutes, 150.0)
        self.assertAlmostEqual(a.session_clock_break_minutes, 30.0)
        self.assertAlmostEqual(a.session_clock_gap_min_minutes, 10.0)
        self.assertAlmostEqual(a.session_clock_gap_max_minutes, 30.0)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(agent_extra={
            "session_clock_enabled": False,
            "session_clock_long_minutes": 45.0,
            "session_clock_very_long_minutes": 120.0,
            "session_clock_gap_max_minutes": 25.0,
        })
        a = load_settings(config_path=path).agent
        self.assertFalse(a.session_clock_enabled)
        self.assertAlmostEqual(a.session_clock_long_minutes, 45.0)
        self.assertAlmostEqual(a.session_clock_very_long_minutes, 120.0)
        self.assertAlmostEqual(a.session_clock_gap_max_minutes, 25.0)

    def test_floors(self) -> None:
        path = self._write_config(agent_extra={
            "session_clock_long_minutes": 0.0,    # floor 1
            "session_clock_break_minutes": -5.0,  # floor 1
            "session_clock_gap_min_minutes": -3.0,  # floor 0
        })
        a = load_settings(config_path=path).agent
        self.assertAlmostEqual(a.session_clock_long_minutes, 1.0)
        self.assertAlmostEqual(a.session_clock_break_minutes, 1.0)
        self.assertAlmostEqual(a.session_clock_gap_min_minutes, 0.0)


class AppreciationSettingsTests(unittest.TestCase):
    """J10: appreciation-beat agent knobs default / round-trip / clamp."""

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
        for k in (
            "appreciation_beats_enabled",
            "appreciation_min_closeness",
            "appreciation_cooldown_hours",
            "appreciation_max_anchor_age_days",
        ):
            cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults(self) -> None:
        a = load_settings(config_path=self._write_config()).agent
        self.assertTrue(a.appreciation_beats_enabled)
        self.assertAlmostEqual(a.appreciation_min_closeness, 0.25)
        self.assertAlmostEqual(a.appreciation_cooldown_hours, 72.0)
        self.assertAlmostEqual(a.appreciation_max_anchor_age_days, 21.0)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(agent_extra={
            "appreciation_beats_enabled": False,
            "appreciation_min_closeness": 0.6,
            "appreciation_cooldown_hours": 12.0,
            "appreciation_max_anchor_age_days": 7.0,
        })
        a = load_settings(config_path=path).agent
        self.assertFalse(a.appreciation_beats_enabled)
        self.assertAlmostEqual(a.appreciation_min_closeness, 0.6)
        self.assertAlmostEqual(a.appreciation_cooldown_hours, 12.0)
        self.assertAlmostEqual(a.appreciation_max_anchor_age_days, 7.0)

    def test_clamps(self) -> None:
        path = self._write_config(agent_extra={
            "appreciation_min_closeness": 9.0,
            "appreciation_cooldown_hours": 0.0,
            "appreciation_max_anchor_age_days": 0.0,
        })
        a = load_settings(config_path=path).agent
        self.assertAlmostEqual(a.appreciation_min_closeness, 1.0)
        self.assertAlmostEqual(a.appreciation_cooldown_hours, 1.0)
        self.assertAlmostEqual(a.appreciation_max_anchor_age_days, 1.0)


class ReciprocalVulnerabilitySettingsTests(unittest.TestCase):
    """J9: reciprocal-vulnerability agent knobs default / round-trip / clamp."""

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
        for k in (
            "reciprocal_vulnerability_enabled",
            "reciprocal_vulnerability_cooldown_hours",
            "reciprocal_vulnerability_min_trust",
        ):
            cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults(self) -> None:
        a = load_settings(config_path=self._write_config()).agent
        self.assertTrue(a.reciprocal_vulnerability_enabled)
        self.assertAlmostEqual(a.reciprocal_vulnerability_cooldown_hours, 96.0)
        self.assertAlmostEqual(a.reciprocal_vulnerability_min_trust, 0.2)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(agent_extra={
            "reciprocal_vulnerability_enabled": False,
            "reciprocal_vulnerability_cooldown_hours": 24.0,
            "reciprocal_vulnerability_min_trust": 0.5,
        })
        a = load_settings(config_path=path).agent
        self.assertFalse(a.reciprocal_vulnerability_enabled)
        self.assertAlmostEqual(a.reciprocal_vulnerability_cooldown_hours, 24.0)
        self.assertAlmostEqual(a.reciprocal_vulnerability_min_trust, 0.5)

    def test_clamps(self) -> None:
        path = self._write_config(agent_extra={
            "reciprocal_vulnerability_cooldown_hours": 0.0,
            "reciprocal_vulnerability_min_trust": 9.0,
        })
        a = load_settings(config_path=path).agent
        self.assertAlmostEqual(a.reciprocal_vulnerability_cooldown_hours, 1.0)
        self.assertAlmostEqual(a.reciprocal_vulnerability_min_trust, 1.0)


class ConflictRepairSettingsTests(unittest.TestCase):
    """J6: conflict-repair agent knobs default / round-trip / clamp."""

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
        for k in (
            "conflict_repair_enabled",
            "conflict_repair_watch_turns",
            "conflict_repair_recovery_epsilon",
            "conflict_repair_min_recovery_rise",
            "conflict_repair_cooldown_hours",
        ):
            cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults(self) -> None:
        a = load_settings(config_path=self._write_config()).agent
        self.assertTrue(a.conflict_repair_enabled)
        self.assertEqual(a.conflict_repair_watch_turns, 5)
        self.assertAlmostEqual(a.conflict_repair_recovery_epsilon, 0.05)
        self.assertAlmostEqual(a.conflict_repair_min_recovery_rise, 0.10)
        self.assertAlmostEqual(a.conflict_repair_cooldown_hours, 12.0)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(agent_extra={
            "conflict_repair_enabled": False,
            "conflict_repair_watch_turns": 8,
            "conflict_repair_recovery_epsilon": 0.2,
            "conflict_repair_min_recovery_rise": 0.3,
            "conflict_repair_cooldown_hours": 6.0,
        })
        a = load_settings(config_path=path).agent
        self.assertFalse(a.conflict_repair_enabled)
        self.assertEqual(a.conflict_repair_watch_turns, 8)
        self.assertAlmostEqual(a.conflict_repair_recovery_epsilon, 0.2)
        self.assertAlmostEqual(a.conflict_repair_min_recovery_rise, 0.3)
        self.assertAlmostEqual(a.conflict_repair_cooldown_hours, 6.0)

    def test_watch_turns_floor(self) -> None:
        path = self._write_config(
            agent_extra={"conflict_repair_watch_turns": 0}
        )
        a = load_settings(config_path=path).agent
        self.assertEqual(a.conflict_repair_watch_turns, 1)


class SearchSettingsTests(unittest.TestCase):
    """Web-search backend (`search` block) parsing + clamps."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.user_json = Path(self._tmp.name) / "user.json"
        patcher = mock.patch.object(
            settings_mod, "USER_CONFIG_PATH", self.user_json,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_config(self, search_extra: dict | None = None) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(json.loads(default_path.read_text(encoding="utf-8")))
        if search_extra is not None:
            cfg["search"] = {**cfg.get("search", {}), **search_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_load_when_block_missing(self) -> None:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(json.loads(default_path.read_text(encoding="utf-8")))
        cfg.pop("search", None)
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        s = load_settings(config_path=path).search
        self.assertEqual(s.provider, "duckduckgo")
        self.assertEqual(s.api_key_env, "LANGSEARCH_API_KEY")
        self.assertTrue(s.fallback_to_duckduckgo)
        self.assertTrue(s.query_reformulation_enabled)
        self.assertEqual(s.langsearch_count, 10)

    def test_overrides_round_trip(self) -> None:
        path = self._write_config({
            "provider": "LangSearch",
            "langsearch_freshness": "oneWeek",
            "query_reformulation_enabled": False,
            "fallback_to_duckduckgo": False,
        })
        s = load_settings(config_path=path).search
        self.assertEqual(s.provider, "langsearch")  # normalised lower
        self.assertEqual(s.langsearch_freshness, "oneWeek")
        self.assertFalse(s.query_reformulation_enabled)
        self.assertFalse(s.fallback_to_duckduckgo)

    def test_count_and_timeout_clamped(self) -> None:
        path = self._write_config({
            "langsearch_count": 999,
            "timeout_seconds": 0.1,
        })
        s = load_settings(config_path=path).search
        self.assertEqual(s.langsearch_count, 10)
        self.assertGreaterEqual(s.timeout_seconds, 1.0)


class WeatherSettingsTests(unittest.TestCase):
    """H11 weather block + agent/tools flags parsing + clamps."""

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
        *,
        weather_extra: dict | None = None,
        agent_extra: dict | None = None,
        tools_extra: dict | None = None,
        drop_weather: bool = False,
    ) -> Path:
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        cfg = copy.deepcopy(json.loads(default_path.read_text(encoding="utf-8")))
        if drop_weather:
            cfg.pop("weather", None)
        elif weather_extra is not None:
            cfg["weather"] = {**cfg.get("weather", {}), **weather_extra}
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        if tools_extra is not None:
            cfg["tools"] = {**cfg.get("tools", {}), **tools_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults(self) -> None:
        result = load_settings(config_path=self._write_config())
        w = result.weather
        self.assertEqual(w.provider, "open_meteo")
        self.assertEqual(w.geocoder, "open_meteo")
        self.assertEqual(w.units, "metric")
        self.assertEqual(w.refresh_interval_minutes, 30)
        self.assertIsNone(w.latitude)
        self.assertIsNone(w.longitude)
        self.assertFalse(result.agent.weather_sync_enabled)
        self.assertTrue(result.tools.weather)

    def test_defaults_when_block_missing(self) -> None:
        result = load_settings(config_path=self._write_config(drop_weather=True))
        self.assertEqual(result.weather.provider, "open_meteo")
        self.assertEqual(result.weather.units, "metric")

    def test_overrides_round_trip(self) -> None:
        path = self._write_config(
            weather_extra={
                "location_name": "Tokyo",
                "latitude": 35.69,
                "longitude": 139.69,
                "units": "Imperial",
            },
            agent_extra={"weather_sync_enabled": True},
            tools_extra={"weather": False},
        )
        result = load_settings(config_path=path)
        w = result.weather
        self.assertEqual(w.location_name, "Tokyo")
        self.assertAlmostEqual(w.latitude, 35.69)
        self.assertEqual(w.units, "imperial")  # normalised lower
        self.assertTrue(result.agent.weather_sync_enabled)
        self.assertFalse(result.tools.weather)

    def test_clamps(self) -> None:
        path = self._write_config(
            weather_extra={
                "refresh_interval_minutes": 1,
                "latitude": 999.0,  # out of range -> None
                "longitude": "bogus",  # non-numeric -> None
                "units": "kelvin",  # invalid -> metric
                "timeout_seconds": 0.1,
            }
        )
        w = load_settings(config_path=path).weather
        self.assertEqual(w.refresh_interval_minutes, 15)
        self.assertIsNone(w.latitude)
        self.assertIsNone(w.longitude)
        self.assertEqual(w.units, "metric")
        self.assertGreaterEqual(w.timeout_seconds, 1.0)


class IntimacyPacingSettingsTests(unittest.TestCase):
    """J12: consent ceiling + learned-pacing knobs round-trip + clamps."""

    _IP_AGENT_KEYS = (
        "intimacy_ceiling",
        "intimacy_pacing_enabled",
        "intimacy_pacing_learning_rate",
        "intimacy_pacing_decay_half_life_days",
        "intimacy_pacing_follow_strength",
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
        for k in self._IP_AGENT_KEYS:
            cfg.get("agent", {}).pop(k, None)
        if agent_extra is not None:
            cfg["agent"] = {**cfg.get("agent", {}), **agent_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_load_when_keys_missing(self) -> None:
        a = load_settings(config_path=self._write_config()).agent
        self.assertAlmostEqual(a.intimacy_ceiling, 0.7)
        self.assertTrue(a.intimacy_pacing_enabled)
        self.assertAlmostEqual(a.intimacy_pacing_learning_rate, 0.15)
        self.assertAlmostEqual(a.intimacy_pacing_decay_half_life_days, 14.0)
        self.assertAlmostEqual(a.intimacy_pacing_follow_strength, 0.5)

    def test_overrides_round_trip(self) -> None:
        a = load_settings(config_path=self._write_config(agent_extra={
            "intimacy_ceiling": 0.2,
            "intimacy_pacing_enabled": False,
            "intimacy_pacing_learning_rate": 0.3,
            "intimacy_pacing_decay_half_life_days": 30.0,
            "intimacy_pacing_follow_strength": 0.9,
        })).agent
        self.assertAlmostEqual(a.intimacy_ceiling, 0.2)
        self.assertFalse(a.intimacy_pacing_enabled)
        self.assertAlmostEqual(a.intimacy_pacing_learning_rate, 0.3)
        self.assertAlmostEqual(a.intimacy_pacing_decay_half_life_days, 30.0)
        self.assertAlmostEqual(a.intimacy_pacing_follow_strength, 0.9)

    def test_ceiling_clamped_to_unit_interval(self) -> None:
        a = load_settings(config_path=self._write_config(agent_extra={
            "intimacy_ceiling": 5.0,
        })).agent
        self.assertAlmostEqual(a.intimacy_ceiling, 1.0)
        a = load_settings(config_path=self._write_config(agent_extra={
            "intimacy_ceiling": -2.0,
        })).agent
        self.assertAlmostEqual(a.intimacy_ceiling, 0.0)

    def test_rates_clamped(self) -> None:
        a = load_settings(config_path=self._write_config(agent_extra={
            "intimacy_pacing_learning_rate": 9.0,
            "intimacy_pacing_follow_strength": -1.0,
            "intimacy_pacing_decay_half_life_days": -5.0,
        })).agent
        self.assertAlmostEqual(a.intimacy_pacing_learning_rate, 1.0)
        self.assertAlmostEqual(a.intimacy_pacing_follow_strength, 0.0)
        self.assertAlmostEqual(a.intimacy_pacing_decay_half_life_days, 0.0)


class McpServerHostTests(unittest.TestCase):
    """``mcp_server.host`` defaults to loopback and only widens deliberately.

    The default is the security posture, not a formality: the MCP tools drive
    the live session unauthenticated. These pin it so a future refactor of the
    parse block can't quietly turn the debug surface into a listening socket.
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
        self._base_config = json.loads(default_path.read_text(encoding="utf-8"))

    def _write_config(self, mcp_extra: dict | None = None) -> Path:
        cfg = copy.deepcopy(self._base_config)
        if mcp_extra is not None:
            cfg["mcp_server"] = {**cfg.get("mcp_server", {}), **mcp_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_defaults_to_loopback(self) -> None:
        result = load_settings(config_path=self._write_config())
        self.assertEqual(result.mcp_server.host, "127.0.0.1")

    def test_dataclass_default_matches_loader_default(self) -> None:
        self.assertEqual(McpServerSettings().host, "127.0.0.1")

    def test_explicit_host_round_trips(self) -> None:
        result = load_settings(config_path=self._write_config({"host": "0.0.0.0"}))
        self.assertEqual(result.mcp_server.host, "0.0.0.0")

    def test_blank_and_missing_fall_back_to_loopback(self) -> None:
        for value in ("", "   ", None):
            with self.subTest(value=value):
                result = load_settings(
                    config_path=self._write_config({"host": value}),
                )
                self.assertEqual(result.mcp_server.host, "127.0.0.1")


class McpHostEnvOverrideTests(unittest.TestCase):
    """``AIKO_MCP_HOST`` is the container's only way to reach the MCP server.

    Inside a container the loopback default is unreachable even from the host
    running Docker, so a published port with no override forwards to nothing.
    """

    def test_override_applies(self) -> None:
        from app.web.__main__ import _apply_env_overrides

        settings = SimpleNamespace(
            web_server=SimpleNamespace(host="127.0.0.1", port=6275),
            mcp_server=SimpleNamespace(host="127.0.0.1", port=6274),
        )
        with mock.patch.dict(os.environ, {"AIKO_MCP_HOST": "0.0.0.0"}):
            _apply_env_overrides(settings)
        self.assertEqual(settings.mcp_server.host, "0.0.0.0")

    def test_absent_env_leaves_loopback(self) -> None:
        from app.web.__main__ import _apply_env_overrides

        settings = SimpleNamespace(
            web_server=SimpleNamespace(host="127.0.0.1", port=6275),
            mcp_server=SimpleNamespace(host="127.0.0.1", port=6274),
        )
        env = {k: v for k, v in os.environ.items() if k != "AIKO_MCP_HOST"}
        with mock.patch.dict(os.environ, env, clear=True):
            _apply_env_overrides(settings)
        self.assertEqual(settings.mcp_server.host, "127.0.0.1")

    def test_blank_env_is_ignored(self) -> None:
        from app.web.__main__ import _apply_env_overrides

        settings = SimpleNamespace(
            web_server=SimpleNamespace(host="127.0.0.1", port=6275),
            mcp_server=SimpleNamespace(host="127.0.0.1", port=6274),
        )
        with mock.patch.dict(os.environ, {"AIKO_MCP_HOST": "  "}):
            _apply_env_overrides(settings)
        self.assertEqual(settings.mcp_server.host, "127.0.0.1")

    def test_missing_block_does_not_crash(self) -> None:
        from app.web.__main__ import _apply_env_overrides

        settings = SimpleNamespace(web_server=None)
        with mock.patch.dict(os.environ, {"AIKO_MCP_HOST": "0.0.0.0"}):
            _apply_env_overrides(settings)  # must not raise


if __name__ == "__main__":
    unittest.main()
