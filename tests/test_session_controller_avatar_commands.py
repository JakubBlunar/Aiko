"""Tests for the LLM-driven avatar command plumbing in
:class:`app.core.session_controller.SessionController`.

Covers ``_emit_avatar_outfit`` (sticky outfit override with priority
rules), ``_emit_avatar_motion`` (motion-file dispatch), and the
``update_avatar_settings`` persistence path that round-trips the
slider/outfit knobs through ``config/user.json``.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock
from unittest.mock import MagicMock

from app.core import settings as settings_mod
from app.core.avatar_profile import (
    AvatarProfile,
    MotionRef,
    OutfitBinding,
    OutfitParam,
)
from app.core.session_controller import SessionController


def _make_avatar(
    *,
    has_pajamas: bool = True,
    has_day: bool = True,
    motions: dict[str, list[MotionRef]] | None = None,
) -> AvatarProfile:
    """Build a synthetic avatar profile with just enough wired up to
    exercise the outfit / motion paths."""
    capabilities: dict[str, bool] = {}
    outfits: dict[str, OutfitBinding] = {}
    if has_pajamas:
        capabilities["has_pajamas"] = True
        outfits["pajamas"] = OutfitBinding(
            params=[OutfitParam(param_id="ParamP", on_value=30.0)],
            label_en="Pajamas",
        )
    if has_day:
        capabilities["has_day_clothes"] = True
        outfits["day_clothes"] = OutfitBinding(
            params=[OutfitParam(param_id="ParamD", on_value=30.0)],
            label_en="Day clothes",
        )
    return AvatarProfile(
        display_name="Test",
        entry_filename="Test.model3.json",
        cubism_version=4,
        capabilities=capabilities,
        outfits=outfits,
        motions=motions or {},
    )


def _make_controller(
    avatar: AvatarProfile,
    *,
    auto_outfit: str = "auto",
) -> SessionController:
    """Bypass __init__ and wire only the slice the avatar emit
    methods actually touch (avatar profile, runtime settings, listener
    lists, current circadian period override)."""
    controller = SessionController.__new__(SessionController)
    controller._avatar = avatar
    controller._avatar_settings_runtime = {
        "scale_multiplier": 1.0,
        "auto_outfit": auto_outfit,
    }
    controller._avatar_settings_listeners = []
    controller._avatar_overlay_listeners = []
    controller._avatar_motion_listeners = []
    controller._llm_outfit_override = ""
    controller._llm_outfit_override_period = ""
    controller._period_override = "morning"
    # ``update_avatar_settings`` mirrors the patched value back onto the
    # AppSettings dataclass so a re-read via ``self._settings`` would
    # see the new value too. Provide just enough of that surface.
    controller._settings = MagicMock()
    controller._settings.avatar = settings_mod.AvatarSettings(auto_outfit=auto_outfit)

    def _period_stub(self: SessionController) -> str:
        return self._period_override

    SessionController.current_circadian_period = _period_stub  # type: ignore[assignment]
    return controller


class OutfitOverrideTests(unittest.TestCase):
    def test_pajamas_override_sticks_to_resolved_outfit(self) -> None:
        controller = _make_controller(_make_avatar())
        controller._period_override = "morning"
        captured: list[dict[str, Any]] = []
        controller._avatar_settings_listeners.append(
            lambda snap: captured.append(dict(snap))
        )
        # Auto in the morning would normally resolve to "day".
        self.assertEqual(controller.resolve_auto_outfit(), "day")
        controller._emit_avatar_outfit("pajamas")
        # Override now wins for the rest of the period.
        self.assertEqual(controller.resolve_auto_outfit(), "pajamas")
        self.assertEqual(len(captured), 1, "settings listener should fire once")

    def test_day_override_during_late_night(self) -> None:
        # Auto at late_night would resolve to pajamas; LLM "day"
        # override flips it back to day clothes.
        controller = _make_controller(_make_avatar())
        controller._period_override = "late_night"
        self.assertEqual(controller.resolve_auto_outfit(), "pajamas")
        controller._emit_avatar_outfit("day")
        self.assertEqual(controller.resolve_auto_outfit(), "day")

    def test_override_expires_on_circadian_flip(self) -> None:
        controller = _make_controller(_make_avatar())
        controller._period_override = "evening"
        controller._emit_avatar_outfit("pajamas")
        self.assertEqual(controller.resolve_auto_outfit(), "pajamas")
        # Period rolls over to morning -> override auto-expires and
        # resolution falls back to the circadian default.
        controller._period_override = "morning"
        self.assertEqual(controller.resolve_auto_outfit(), "day")
        # Internal state cleared as a side-effect of the resolution.
        self.assertEqual(controller._llm_outfit_override, "")

    def test_user_forced_outfit_blocks_llm_override(self) -> None:
        controller = _make_controller(_make_avatar(), auto_outfit="day")
        controller._period_override = "late_night"
        # User forced "day" — even at late_night the LLM override
        # is dropped on the floor without firing the listener.
        captured: list[dict[str, Any]] = []
        controller._avatar_settings_listeners.append(
            lambda snap: captured.append(dict(snap))
        )
        controller._emit_avatar_outfit("pajamas")
        self.assertEqual(controller._llm_outfit_override, "")
        self.assertEqual(controller.resolve_auto_outfit(), "day")
        self.assertEqual(captured, [])

    def test_user_switching_to_forced_clears_existing_override(self) -> None:
        controller = _make_controller(_make_avatar())
        controller._period_override = "morning"
        controller._emit_avatar_outfit("pajamas")
        self.assertEqual(controller.resolve_auto_outfit(), "pajamas")
        # User flips to forced "day" via the panel.
        controller._avatar_settings_runtime["auto_outfit"] = "day"
        self.assertEqual(controller.resolve_auto_outfit(), "day")
        # Override cleared so a later switch back to "auto" doesn't
        # resurrect the stale pajamas directive.
        self.assertEqual(controller._llm_outfit_override, "")

    def test_unknown_outfit_name_is_ignored(self) -> None:
        controller = _make_controller(_make_avatar())
        controller._emit_avatar_outfit("hoodie")  # not "pajamas" or "day"
        self.assertEqual(controller._llm_outfit_override, "")

    def test_override_skipped_when_capability_missing(self) -> None:
        # No pajamas capability — the override is silently dropped.
        controller = _make_controller(_make_avatar(has_pajamas=False))
        controller._emit_avatar_outfit("pajamas")
        self.assertEqual(controller._llm_outfit_override, "")

    def test_no_listener_fire_when_resolved_outfit_unchanged(self) -> None:
        # Late-night already resolves to pajamas; an LLM "[[outfit:pajamas]]"
        # there shouldn't spam the settings listener.
        controller = _make_controller(_make_avatar())
        controller._period_override = "late_night"
        captured: list[dict[str, Any]] = []
        controller._avatar_settings_listeners.append(
            lambda snap: captured.append(dict(snap))
        )
        controller._emit_avatar_outfit("pajamas")
        self.assertEqual(captured, [])


class MotionDispatchTests(unittest.TestCase):
    def test_known_motion_broadcasts_group_and_index(self) -> None:
        avatar = _make_avatar(motions={
            "Idle": [
                MotionRef(name="dh", file="motions/dh.motion3.json"),
                MotionRef(name="wave", file="motions/wave.motion3.json"),
            ],
            "Talk": [
                MotionRef(name="nod", file="motions/nod.motion3.json"),
            ],
        })
        controller = _make_controller(avatar)
        captured: list[dict[str, Any]] = []
        controller._avatar_motion_listeners.append(
            lambda payload: captured.append(dict(payload))
        )
        controller._emit_avatar_motion("wave")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["name"], "wave")
        self.assertEqual(captured[0]["group"], "Idle")
        self.assertEqual(captured[0]["index"], 1)

    def test_motion_lookup_is_case_insensitive(self) -> None:
        avatar = _make_avatar(motions={
            "Talk": [MotionRef(name="Bow", file="motions/bow.motion3.json")],
        })
        controller = _make_controller(avatar)
        captured: list[dict[str, Any]] = []
        controller._avatar_motion_listeners.append(
            lambda payload: captured.append(dict(payload))
        )
        controller._emit_avatar_motion("bow")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["index"], 0)

    def test_unknown_motion_is_silently_dropped(self) -> None:
        avatar = _make_avatar(motions={
            "Talk": [MotionRef(name="nod", file="motions/nod.motion3.json")],
        })
        controller = _make_controller(avatar)
        captured: list[dict[str, Any]] = []
        controller._avatar_motion_listeners.append(
            lambda payload: captured.append(dict(payload))
        )
        controller._emit_avatar_motion("waltz")
        self.assertEqual(captured, [])

    def test_emit_with_no_avatar_loaded_is_a_noop(self) -> None:
        controller = _make_controller(_make_avatar())
        controller._avatar = None
        captured: list[dict[str, Any]] = []
        controller._avatar_motion_listeners.append(
            lambda payload: captured.append(dict(payload))
        )
        controller._emit_avatar_motion("wave")
        self.assertEqual(captured, [])


class UpdateAvatarSettingsPersistenceTests(unittest.TestCase):
    """``update_avatar_settings`` must mirror the change to ``user.json``
    so closing the browser tab (or the whole app) does not reset the
    slider back to the default scale."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.user_json = Path(self._tmp.name) / "user.json"
        # Redirect the module-level USER_CONFIG_PATH so the helper
        # inside ``update_avatar_settings`` writes here, not into the
        # repo's real ``config/user.json``.
        patcher = mock.patch.object(settings_mod, "USER_CONFIG_PATH", self.user_json)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_scale_change_writes_to_user_json(self) -> None:
        controller = _make_controller(_make_avatar())
        controller.update_avatar_settings(scale_multiplier=1.75)
        self.assertTrue(self.user_json.exists())
        body = json.loads(self.user_json.read_text(encoding="utf-8"))
        self.assertEqual(body, {"avatar": {"scale_multiplier": 1.75}})

    def test_outfit_change_writes_to_user_json(self) -> None:
        controller = _make_controller(_make_avatar())
        controller.update_avatar_settings(auto_outfit="pajamas")
        body = json.loads(self.user_json.read_text(encoding="utf-8"))
        self.assertEqual(body, {"avatar": {"auto_outfit": "pajamas"}})

    def test_combined_patch_writes_both_keys(self) -> None:
        controller = _make_controller(_make_avatar())
        controller.update_avatar_settings(scale_multiplier=2.0, auto_outfit="day")
        body = json.loads(self.user_json.read_text(encoding="utf-8"))
        self.assertEqual(
            body,
            {"avatar": {"scale_multiplier": 2.0, "auto_outfit": "day"}},
        )

    def test_noop_call_does_not_write_file(self) -> None:
        controller = _make_controller(_make_avatar())
        # Same value as the runtime default — no actual change, so the
        # helper should not be invoked at all.
        controller.update_avatar_settings(scale_multiplier=1.0)
        self.assertFalse(self.user_json.exists())

    def test_existing_unrelated_keys_are_preserved(self) -> None:
        # Pretend the user already has tts/audio overrides — the
        # avatar persist path must not nuke them.
        self.user_json.write_text(
            json.dumps({
                "tts": {"voice": "aiko1.safetensors"},
                "audio": {"output_device": 3},
            }),
            encoding="utf-8",
        )
        controller = _make_controller(_make_avatar())
        controller.update_avatar_settings(scale_multiplier=2.5)
        body = json.loads(self.user_json.read_text(encoding="utf-8"))
        self.assertEqual(body["tts"], {"voice": "aiko1.safetensors"})
        self.assertEqual(body["audio"], {"output_device": 3})
        self.assertEqual(body["avatar"], {"scale_multiplier": 2.5})

    def test_persisted_value_round_trips_through_runtime_state(self) -> None:
        # The dataclass on the controller's _settings is also mirrored
        # so a subsequent ``load_settings`` (with USER_CONFIG_PATH still
        # pointing here) would observe the new value.
        controller = _make_controller(_make_avatar())
        controller.update_avatar_settings(scale_multiplier=3.0)
        self.assertEqual(controller._settings.avatar.scale_multiplier, 3.0)
        self.assertEqual(
            controller._avatar_settings_runtime["scale_multiplier"], 3.0,
        )

    def test_write_failure_does_not_break_in_memory_update(self) -> None:
        # Simulate ``user.json.tmp.write_text`` blowing up (e.g. read-only
        # volume). The runtime knob should still flip and the listener
        # still fire — the persistence layer is best-effort.
        controller = _make_controller(_make_avatar())
        captured: list[dict[str, Any]] = []
        controller._avatar_settings_listeners.append(
            lambda snap: captured.append(dict(snap)),
        )
        with mock.patch(
            "app.core.session_controller.persist_user_overrides",
            side_effect=OSError("read-only fs"),
        ):
            controller.update_avatar_settings(scale_multiplier=1.4)
        self.assertEqual(
            controller._avatar_settings_runtime["scale_multiplier"], 1.4,
        )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["scale_multiplier"], 1.4)


if __name__ == "__main__":
    unittest.main()
