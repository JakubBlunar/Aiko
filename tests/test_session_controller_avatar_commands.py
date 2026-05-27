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
from app.core.session_controller import (
    SessionController,
    _BackchannelMotionGate,
)


def _make_avatar(
    *,
    has_pajamas: bool = True,
    has_pajamas_hooded: bool = False,
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
    if has_pajamas_hooded:
        capabilities["has_pajamas_hooded"] = True
        outfits["pajamas_hooded"] = OutfitBinding(
            params=[
                OutfitParam(param_id="ParamP", on_value=30.0),
                OutfitParam(param_id="ParamH", on_value=30.0),
            ],
            label_en="Pajamas (hooded)",
        )
    if has_day:
        capabilities["has_day_clothes"] = True
        outfits["day_clothes"] = OutfitBinding(
            params=[],
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
        "expressiveness": 1.0,
    }
    controller._avatar_settings_listeners = []
    controller._avatar_overlay_listeners = []
    controller._avatar_motion_listeners = []
    controller._llm_outfit_override = ""
    controller._llm_outfit_override_period = ""
    controller._period_override = "morning"
    # ``_emit_backchannel_motion`` needs these or it'll raise on attr
    # access; tests that don't exercise that path pay no other cost.
    controller._backchannel_motion_gate = _BackchannelMotionGate(
        min_repeat_seconds=1.5,
    )
    controller._backchannel_thinking_index = 0
    # Desktop / Tauri shell knobs. Tests that don't touch
    # ``update_desktop_settings`` ignore these; the ones that do can
    # mutate the runtime cache directly.
    controller._desktop_settings_runtime = {
        "persona_window": {
            "width": 320,
            "height": 480,
            "always_on_top": True,
        }
    }
    controller._desktop_settings_listeners = []
    # ``update_avatar_settings`` mirrors the patched value back onto the
    # AppSettings dataclass so a re-read via ``self._settings`` would
    # see the new value too. Provide just enough of that surface.
    controller._settings = MagicMock()
    controller._settings.avatar = settings_mod.AvatarSettings(auto_outfit=auto_outfit)
    controller._settings.desktop = settings_mod.DesktopSettings()

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


class PajamasHoodedVariantTests(unittest.TestCase):
    """Coverage for the second pajama variant on rigs that ship both:
    a bare-pajamas binding (Param16 only) and a hooded variant
    (Param16 + Param17). Verifies the resolve precedence, the LLM
    ``[[outfit:pajamas_hooded]]`` dispatch, and the user-forced
    ``auto_outfit="pajamas_hooded"`` path."""

    def _full_rig(self, **kwargs: Any) -> AvatarProfile:
        return _make_avatar(
            has_pajamas=True,
            has_pajamas_hooded=True,
            has_day=True,
            **kwargs,
        )

    def test_llm_emit_dispatches_pajamas_hooded(self) -> None:
        controller = _make_controller(self._full_rig())
        controller._period_override = "morning"
        captured: list[dict[str, Any]] = []
        controller._avatar_settings_listeners.append(
            lambda snap: captured.append(dict(snap))
        )
        controller._emit_avatar_outfit("pajamas_hooded")
        self.assertEqual(controller._llm_outfit_override, "pajamas_hooded")
        self.assertEqual(controller.resolve_auto_outfit(), "pajamas_hooded")
        self.assertEqual(len(captured), 1)

    def test_llm_emit_skipped_when_only_hooded_capability_missing(self) -> None:
        # Rig has bare pajamas but not hooded — directive must drop.
        controller = _make_controller(_make_avatar(has_pajamas_hooded=False))
        controller._emit_avatar_outfit("pajamas_hooded")
        self.assertEqual(controller._llm_outfit_override, "")

    def test_user_forced_pajamas_hooded_resolves_to_hooded(self) -> None:
        controller = _make_controller(
            self._full_rig(), auto_outfit="pajamas_hooded",
        )
        # User-forced mode wins regardless of circadian period.
        controller._period_override = "morning"
        self.assertEqual(controller.resolve_auto_outfit(), "pajamas_hooded")
        controller._period_override = "late_night"
        self.assertEqual(controller.resolve_auto_outfit(), "pajamas_hooded")

    def test_user_forced_pajamas_hooded_falls_back_when_unsupported(self) -> None:
        # Rig only ships bare pajamas. Forced "pajamas_hooded" should
        # gracefully degrade to the bare variant rather than silently
        # showing day clothes.
        controller = _make_controller(
            _make_avatar(has_pajamas_hooded=False),
            auto_outfit="pajamas_hooded",
        )
        self.assertEqual(controller.resolve_auto_outfit(), "pajamas")

    def test_circadian_auto_prefers_bare_pajamas_when_both_present(self) -> None:
        # At night with both variants supported, auto mode picks the
        # bare variant — the hooded one is opt-in via UI / LLM.
        controller = _make_controller(self._full_rig())
        controller._period_override = "late_night"
        self.assertEqual(controller.resolve_auto_outfit(), "pajamas")

    def test_circadian_auto_falls_back_to_hooded_when_bare_missing(self) -> None:
        # Only the hooded variant available — auto mode at night must
        # still hit pajamas (the hooded one) rather than day clothes.
        controller = _make_controller(
            _make_avatar(has_pajamas=False, has_pajamas_hooded=True),
        )
        controller._period_override = "late_night"
        self.assertEqual(controller.resolve_auto_outfit(), "pajamas_hooded")

    def test_user_forced_pajamas_falls_back_to_hooded_when_bare_missing(
        self,
    ) -> None:
        # ``auto_outfit="pajamas"`` should respect the user's intent
        # ("they want pajamas") even when the rig only ships hooded.
        controller = _make_controller(
            _make_avatar(has_pajamas=False, has_pajamas_hooded=True),
            auto_outfit="pajamas",
        )
        self.assertEqual(controller.resolve_auto_outfit(), "pajamas_hooded")

    def test_unknown_outfit_name_still_rejected(self) -> None:
        # Regression: the new variant must not have widened the
        # accept-list to anything containing "pajamas".
        controller = _make_controller(self._full_rig())
        controller._emit_avatar_outfit("pajamas_silly")
        self.assertEqual(controller._llm_outfit_override, "")

    def test_user_mode_pajamas_hooded_persists_via_update(self) -> None:
        # ``update_avatar_settings`` must accept the new mode so the
        # SettingsDrawer can persist it just like "day" or "pajamas".
        controller = _make_controller(self._full_rig())
        controller._patched: list[dict[str, Any]] = []  # type: ignore[attr-defined]

        def _capture(patch: dict[str, Any]) -> None:
            controller._patched.append(patch)  # type: ignore[attr-defined]

        with mock.patch(
            "app.core.session.avatar_mixin.persist_user_overrides",
            side_effect=_capture,
        ):
            snap = controller.update_avatar_settings(
                auto_outfit="pajamas_hooded",
            )
        self.assertEqual(snap["auto_outfit"], "pajamas_hooded")
        self.assertEqual(
            controller._patched,  # type: ignore[attr-defined]
            [{"avatar": {"auto_outfit": "pajamas_hooded"}}],
        )


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

    def test_motion_name_matching_known_overlay_routes_to_overlay(self) -> None:
        """``[[motion:tail_wag]]`` from a confused LLM should still wag the
        tail: the safety net in ``_emit_avatar_motion`` re-routes the misroute
        to ``_emit_avatar_overlay`` when the avatar advertises the
        corresponding ``has_<name>`` capability.
        """
        avatar = _make_avatar(motions={
            "Talk": [MotionRef(name="nod", file="motions/nod.motion3.json")],
        })
        avatar.capabilities["has_tail_wag"] = True
        controller = _make_controller(avatar)
        overlay_captured: list[dict[str, Any]] = []
        motion_captured: list[dict[str, Any]] = []
        controller._avatar_overlay_listeners.append(
            lambda payload: overlay_captured.append(dict(payload))
        )
        controller._avatar_motion_listeners.append(
            lambda payload: motion_captured.append(dict(payload))
        )
        controller._emit_avatar_motion("tail_wag")
        self.assertEqual(motion_captured, [])
        self.assertEqual(len(overlay_captured), 1)
        self.assertEqual(overlay_captured[0]["name"], "tail_wag")

    def test_motion_name_misroute_is_case_insensitive(self) -> None:
        avatar = _make_avatar(motions={})
        avatar.capabilities["has_ear_wiggle"] = True
        controller = _make_controller(avatar)
        overlay_captured: list[dict[str, Any]] = []
        controller._avatar_overlay_listeners.append(
            lambda payload: overlay_captured.append(dict(payload))
        )
        controller._emit_avatar_motion("Ear_Wiggle")
        self.assertEqual(len(overlay_captured), 1)
        self.assertEqual(overlay_captured[0]["name"], "ear_wiggle")

    def test_motion_unknown_and_not_overlay_still_silently_dropped(self) -> None:
        """If the name matches neither a motion stem nor an overlay capability
        we keep the existing silent-drop behaviour — no listeners get poked.
        """
        avatar = _make_avatar(motions={
            "Talk": [MotionRef(name="nod", file="motions/nod.motion3.json")],
        })
        avatar.capabilities["has_tail_wag"] = True
        controller = _make_controller(avatar)
        overlay_captured: list[dict[str, Any]] = []
        motion_captured: list[dict[str, Any]] = []
        controller._avatar_overlay_listeners.append(
            lambda payload: overlay_captured.append(dict(payload))
        )
        controller._avatar_motion_listeners.append(
            lambda payload: motion_captured.append(dict(payload))
        )
        controller._emit_avatar_motion("salsa")
        self.assertEqual(motion_captured, [])
        self.assertEqual(overlay_captured, [])

    def test_known_motion_takes_precedence_over_overlay_capability(self) -> None:
        """A name that IS a valid motion stem must still hit the motion
        listener even if there happens to also be a ``has_<name>`` capability
        — the safety net is a fallthrough, never an override.
        """
        avatar = _make_avatar(motions={
            "Talk": [MotionRef(name="nod", file="motions/nod.motion3.json")],
        })
        # Hypothetical scenario: future avatar advertises ``has_nod`` overlay
        # alongside a real motion file. Motion file wins.
        avatar.capabilities["has_nod"] = True
        controller = _make_controller(avatar)
        overlay_captured: list[dict[str, Any]] = []
        motion_captured: list[dict[str, Any]] = []
        controller._avatar_overlay_listeners.append(
            lambda payload: overlay_captured.append(dict(payload))
        )
        controller._avatar_motion_listeners.append(
            lambda payload: motion_captured.append(dict(payload))
        )
        controller._emit_avatar_motion("nod")
        self.assertEqual(overlay_captured, [])
        self.assertEqual(len(motion_captured), 1)
        self.assertEqual(motion_captured[0]["name"], "nod")


class BackchannelMotionDispatchTests(unittest.TestCase):
    """Phase B2: backchannel hint -> low-priority motion broadcast.

    These exercise ``_emit_backchannel_motion`` directly: the public
    fan-out site (``add_backchannel_listener``) wires it through the
    same listener list during ``__init__``, so unit-testing the
    callback covers the contract end-to-end without spinning up a
    real STT loop.
    """

    @staticmethod
    def _make_avatar_with_backchannel_motions() -> AvatarProfile:
        return _make_avatar(motions={
            "Tap": [
                MotionRef(name="nod", file="motions/nod.motion3.json"),
                MotionRef(name="shake", file="motions/shake.motion3.json"),
            ],
            "Backchannel": [
                MotionRef(name="tilt_left", file="motions/tilt_left.motion3.json"),
                MotionRef(name="tilt_right", file="motions/tilt_right.motion3.json"),
                MotionRef(name="microshake", file="motions/microshake.motion3.json"),
            ],
        })

    def test_agreement_hint_emits_nod_at_idle_priority(self) -> None:
        controller = _make_controller(self._make_avatar_with_backchannel_motions())
        captured: list[dict[str, Any]] = []
        controller._avatar_motion_listeners.append(
            lambda payload: captured.append(dict(payload))
        )
        controller._emit_backchannel_motion("agreement", "yes!")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["name"], "nod")
        self.assertEqual(captured[0]["group"], "Tap")
        self.assertEqual(captured[0]["priority"], "idle")

    def test_disagreement_hint_emits_shake(self) -> None:
        controller = _make_controller(self._make_avatar_with_backchannel_motions())
        captured: list[dict[str, Any]] = []
        controller._avatar_motion_listeners.append(
            lambda payload: captured.append(dict(payload))
        )
        controller._emit_backchannel_motion("disagreement", "no")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["name"], "shake")

    def test_confused_hint_emits_microshake(self) -> None:
        controller = _make_controller(self._make_avatar_with_backchannel_motions())
        captured: list[dict[str, Any]] = []
        controller._avatar_motion_listeners.append(
            lambda payload: captured.append(dict(payload))
        )
        controller._emit_backchannel_motion("confused", "huh?")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["name"], "microshake")
        self.assertEqual(captured[0]["group"], "Backchannel")

    def test_thinking_hint_alternates_tilt_left_then_right(self) -> None:
        controller = _make_controller(self._make_avatar_with_backchannel_motions())
        captured: list[dict[str, Any]] = []
        controller._avatar_motion_listeners.append(
            lambda payload: captured.append(dict(payload))
        )
        # Force the gate to allow back-to-back fires for the alternation
        # check.
        controller._backchannel_motion_gate = _BackchannelMotionGate(
            min_repeat_seconds=0.0,
        )
        controller._emit_backchannel_motion("thinking", "uhhh")
        controller._emit_backchannel_motion("thinking", "let me see")
        controller._emit_backchannel_motion("thinking", "well")
        self.assertEqual([c["name"] for c in captured],
                         ["tilt_left", "tilt_right", "tilt_left"])

    def test_rate_limit_drops_repeat_within_window(self) -> None:
        controller = _make_controller(self._make_avatar_with_backchannel_motions())
        captured: list[dict[str, Any]] = []
        controller._avatar_motion_listeners.append(
            lambda payload: captured.append(dict(payload))
        )
        controller._emit_backchannel_motion("agreement", "uh huh")
        controller._emit_backchannel_motion("agreement", "yeah")
        # Default 1.5s gate -> the second call (within the same
        # synchronous tick) is dropped on the floor.
        self.assertEqual(len(captured), 1)

    def test_rate_limit_releases_after_min_repeat(self) -> None:
        controller = _make_controller(self._make_avatar_with_backchannel_motions())
        # Tighter gate so the test doesn't sit on a real sleep.
        controller._backchannel_motion_gate = _BackchannelMotionGate(
            min_repeat_seconds=0.1,
        )
        captured: list[dict[str, Any]] = []
        controller._avatar_motion_listeners.append(
            lambda payload: captured.append(dict(payload))
        )
        # Drive monotonic time via a stub so the test stays deterministic
        # and never blocks on a real wait.
        ticks = iter([1.0, 1.05, 1.5])
        with mock.patch(
            "app.core.session.avatar_mixin.time.monotonic",
            side_effect=lambda: next(ticks),
        ):
            controller._emit_backchannel_motion("agreement", "uh")
            controller._emit_backchannel_motion("agreement", "uh")  # within window
            controller._emit_backchannel_motion("agreement", "uh")  # past window
        self.assertEqual(len(captured), 2)

    def test_unmapped_hint_is_silently_dropped(self) -> None:
        controller = _make_controller(self._make_avatar_with_backchannel_motions())
        captured: list[dict[str, Any]] = []
        controller._avatar_motion_listeners.append(
            lambda payload: captured.append(dict(payload))
        )
        controller._emit_backchannel_motion("surprise", "wow!")
        controller._emit_backchannel_motion("amusement", "haha")
        controller._emit_backchannel_motion("concern", "oh no")
        self.assertEqual(captured, [])

    def test_motion_missing_on_rig_drops_silently(self) -> None:
        """A minimal rig without the ``Backchannel`` group / motion
        files shouldn't crash — just skip the broadcast."""
        avatar = _make_avatar(motions={
            "Tap": [MotionRef(name="nod", file="motions/nod.motion3.json")],
        })
        controller = _make_controller(avatar)
        captured: list[dict[str, Any]] = []
        controller._avatar_motion_listeners.append(
            lambda payload: captured.append(dict(payload))
        )
        controller._emit_backchannel_motion("thinking", "uhhh")  # tilt_left missing
        controller._emit_backchannel_motion("confused", "huh")    # microshake missing
        self.assertEqual(captured, [])

    def test_no_avatar_loaded_is_a_noop(self) -> None:
        controller = _make_controller(self._make_avatar_with_backchannel_motions())
        controller._avatar = None
        captured: list[dict[str, Any]] = []
        controller._avatar_motion_listeners.append(
            lambda payload: captured.append(dict(payload))
        )
        controller._emit_backchannel_motion("agreement", "yes")
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

    def test_expressiveness_round_trip_and_clamp(self) -> None:
        """The body-language slider value persists and is clamped on update.

        Mirrors the ``avatar.expressiveness`` plumbing introduced for the
        continuous-expressiveness pass: ``update_avatar_settings`` must
        clamp into ``[0.0, 1.5]`` and write the value back to
        ``user.json`` so the next launch starts where the user left off.
        """
        controller = _make_controller(_make_avatar())
        captured: list[dict[str, Any]] = []
        controller._avatar_settings_listeners.append(
            lambda snap: captured.append(dict(snap))
        )
        snap = controller.update_avatar_settings(expressiveness=0.4)
        self.assertEqual(snap["expressiveness"], 0.4)
        self.assertEqual(controller._settings.avatar.expressiveness, 0.4)
        body = json.loads(self.user_json.read_text(encoding="utf-8"))
        self.assertEqual(body, {"avatar": {"expressiveness": 0.4}})
        self.assertEqual(len(captured), 1)
        # Out-of-range values clamp to the [0, 1.5] band rather than
        # raising — matches the loader's tolerant clamp policy.
        snap = controller.update_avatar_settings(expressiveness=5.0)
        self.assertEqual(snap["expressiveness"], 1.5)
        snap = controller.update_avatar_settings(expressiveness=-2.0)
        self.assertEqual(snap["expressiveness"], 0.0)

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
            "app.core.session.avatar_mixin.persist_user_overrides",
            side_effect=OSError("read-only fs"),
        ):
            controller.update_avatar_settings(scale_multiplier=1.4)
        self.assertEqual(
            controller._avatar_settings_runtime["scale_multiplier"], 1.4,
        )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["scale_multiplier"], 1.4)


class DesktopSettingsPersistenceTests(unittest.TestCase):
    """``update_desktop_settings`` mirrors ``update_avatar_settings``:
    clamps, persists to ``user.json``, and notifies the listener bus."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.user_json = Path(self._tmp.name) / "user.json"
        patcher = mock.patch.object(settings_mod, "USER_CONFIG_PATH", self.user_json)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_width_change_writes_and_clamps(self) -> None:
        controller = _make_controller(_make_avatar())
        captured: list[dict[str, Any]] = []
        controller._desktop_settings_listeners.append(
            lambda snap: captured.append(dict(snap))
        )
        snap = controller.update_desktop_settings(persona_window_width=400)
        self.assertEqual(snap["persona_window"]["width"], 400)
        body = json.loads(self.user_json.read_text(encoding="utf-8"))
        self.assertEqual(body, {"desktop": {"persona_window": {"width": 400}}})
        self.assertEqual(len(captured), 1)
        # Clamp upper bound.
        snap = controller.update_desktop_settings(persona_window_width=99_999)
        self.assertEqual(
            snap["persona_window"]["width"],
            settings_mod.PERSONA_WINDOW_MAX_WIDTH,
        )
        # Clamp lower bound.
        snap = controller.update_desktop_settings(persona_window_width=10)
        self.assertEqual(
            snap["persona_window"]["width"],
            settings_mod.PERSONA_WINDOW_MIN_WIDTH,
        )

    def test_height_and_always_on_top_round_trip(self) -> None:
        controller = _make_controller(_make_avatar())
        snap = controller.update_desktop_settings(
            persona_window_height=600,
            persona_window_always_on_top=False,
        )
        self.assertEqual(snap["persona_window"]["height"], 600)
        self.assertFalse(snap["persona_window"]["always_on_top"])
        body = json.loads(self.user_json.read_text(encoding="utf-8"))
        self.assertEqual(
            body,
            {
                "desktop": {
                    "persona_window": {
                        "height": 600,
                        "always_on_top": False,
                    }
                }
            },
        )

    def test_noop_call_does_not_write(self) -> None:
        controller = _make_controller(_make_avatar())
        snap = controller.update_desktop_settings(persona_window_width=320)
        # 320 is the runtime default — no actual change.
        self.assertEqual(snap["persona_window"]["width"], 320)
        self.assertFalse(self.user_json.exists())

    def test_existing_unrelated_keys_preserved(self) -> None:
        self.user_json.write_text(
            json.dumps({"avatar": {"scale_multiplier": 1.5}}),
            encoding="utf-8",
        )
        controller = _make_controller(_make_avatar())
        controller.update_desktop_settings(persona_window_width=400)
        body = json.loads(self.user_json.read_text(encoding="utf-8"))
        self.assertEqual(body["avatar"], {"scale_multiplier": 1.5})
        self.assertEqual(
            body["desktop"], {"persona_window": {"width": 400}},
        )

    def test_listener_fires_only_on_change(self) -> None:
        controller = _make_controller(_make_avatar())
        captured: list[dict[str, Any]] = []
        controller._desktop_settings_listeners.append(
            lambda snap: captured.append(dict(snap))
        )
        # Default value, no change -> no listener call.
        controller.update_desktop_settings(persona_window_width=320)
        self.assertEqual(captured, [])
        # Real change -> listener fires once.
        controller.update_desktop_settings(persona_window_width=480)
        self.assertEqual(len(captured), 1)


if __name__ == "__main__":
    unittest.main()
