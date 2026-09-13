"""Live mode Pass 5: hysteresis, semantic resolver, idle-life plan. No speech."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from app.core.conversation.conversation_situation import (
    ConversationSituationSnapshot,
    ConversationSituationState,
    WorldSituation,
)
from app.core.live.assembler import LiveAssembleInput, LiveSituationAssembler
from app.core.live.capabilities import semantic_capabilities_from_profile
from app.core.live.impulse import LiveImpulse
from app.core.live.resolver import (
    FORBIDDEN_RIG_MARKERS,
    LiveBehaviorResolver,
    plan_contains_rig_identifiers,
)
from app.core.session.live_mode_mixin import LiveModeMixin
from app.core.session.session_controller import SessionController


def _now() -> datetime:
    return datetime(2026, 9, 11, 15, 0, tzinfo=timezone.utc)


def _impulse(kind: str, *, monotonic_ms: float = 9_500.0) -> LiveImpulse:
    return LiveImpulse(
        event_id="e1",
        kind=kind,
        source="test",
        session_key="s",
        mode_generation=1,
        sequence=1,
        occurred_at="2026-09-11T15:00:00Z",
        monotonic_ms=monotonic_ms,
        priority="attention",
        ttl_ms=30_000,
        coalesce_key=kind,
        privacy="local_state",
        payload={"active": True} if "typing" in kind else {},
    )


def _snapshot(**kwargs: object) -> ConversationSituationSnapshot:
    fields = dict(
        session_id="s",
        generation=0,
        observed_at="2026-09-11T15:00:00Z",
        input_mode="typed",
        floor_transition="neither",
        dialogue_act="",
        arc="casual_check_in",
        arc_confidence=0.4,
        mood_label="content",
        vitality_band="normal",
        user_present=True,
        user_active_app="",
        world=WorldSituation(location_slug="beanbag", activity="idle"),
        inferred=None,
        inferred_stale=True,
        world_compatible=False,
        conflict_reason="",
        shared_commitment_active=False,
        since_user_activity_ms=0,
        sleep={"status": "awake"},
    )
    fields.update(kwargs)
    return ConversationSituationSnapshot(**fields)  # type: ignore[arg-type]


def _inferred() -> ConversationSituationState:
    return ConversationSituationState(
        session_id="s",
        generation=1,
        status="active",
        summary="Watching together",
        shared=True,
        shared_activity="watching a film",
        place_ref="beanbag",
        aiko_activity="watching with the user",
        evidence_message_ids=(3,),
        source_message_id=3,
        miss_count=0,
        updated_at="2026-09-11T15:00:00Z",
    )


def _inp(snapshot: ConversationSituationSnapshot, **kwargs: object) -> LiveAssembleInput:
    fields = dict(
        snapshot=snapshot,
        impulses=(),
        now=_now(),
        monotonic_ms=10_000.0,
        trigger_kind="heartbeat",
    )
    fields.update(kwargs)
    return LiveAssembleInput(**fields)  # type: ignore[arg-type]


class HysteresisTests(unittest.TestCase):
    def test_shared_holds_against_weaker_world_after_dwell(self) -> None:
        assembler = LiveSituationAssembler()
        shared = _snapshot(
            inferred=_inferred(),
            inferred_stale=False,
            world_compatible=True,
            shared_commitment_active=True,
            world=WorldSituation(location_slug="beanbag", activity="reading"),
        )
        first = assembler.assemble(_inp(shared, monotonic_ms=10_000.0))
        self.assertEqual(first.attention.target, "shared_activity")
        self.assertEqual(first.commitment.intention, "remain_present")
        self.assertEqual(first.attention.enter_threshold, 0.5)
        self.assertEqual(first.attention.exit_threshold, 0.35)
        later = assembler.assemble(_inp(shared, monotonic_ms=40_000.0))
        self.assertEqual(later.attention.target, "shared_activity")
        self.assertEqual(later.attention.challenger_target, "")

    def test_typing_breaks_shared_immediately(self) -> None:
        assembler = LiveSituationAssembler()
        shared = _snapshot(
            inferred=_inferred(),
            inferred_stale=False,
            world_compatible=True,
            shared_commitment_active=True,
        )
        assembler.assemble(_inp(shared, monotonic_ms=10_000.0))
        typed = assembler.assemble(
            _inp(
                shared,
                impulses=(_impulse("user.typing_started", monotonic_ms=10_100.0),),
                monotonic_ms=10_200.0,
            ),
        )
        self.assertEqual(typed.attention.target, "user")
        self.assertEqual(typed.commitment.intention, "attend")

    def test_shared_ending_releases_hold(self) -> None:
        assembler = LiveSituationAssembler()
        shared = _snapshot(
            inferred=_inferred(),
            inferred_stale=False,
            world_compatible=True,
            shared_commitment_active=True,
        )
        assembler.assemble(_inp(shared, monotonic_ms=10_000.0))
        idle = assembler.assemble(
            _inp(_snapshot(), monotonic_ms=10_400.0),
        )
        self.assertEqual(idle.attention.target, "none")
        self.assertEqual(idle.commitment.intention, "wait")


class ResolverTests(unittest.TestCase):
    def test_plan_has_no_rig_identifiers(self) -> None:
        assembler = LiveSituationAssembler()
        frame = assembler.assemble(
            _inp(
                _snapshot(
                    world=WorldSituation(
                        location_slug="beanbag",
                        activity="reading",
                        posture="curled_up",
                    ),
                ),
            ),
        )
        caps = semantic_capabilities_from_profile({
            "capabilities": {
                "has_body_angle_y": True,
                "has_breath": True,
            },
            "expressions": [{"name": "neutral"}],
            "reaction_mapping": {"content": "softSmile"},
            "idle_motion_group": "Idle",
        })
        plan = LiveBehaviorResolver().resolve(frame, caps)
        self.assertEqual(plan.body_class, "settle")
        self.assertEqual(plan.gaze_class, "rest")
        self.assertEqual(plan.breath_class, "slow")
        self.assertFalse(plan_contains_rig_identifiers(plan))
        blob = str(plan.to_payload())
        for marker in FORBIDDEN_RIG_MARKERS:
            self.assertNotIn(marker, blob)

    def test_sleep_degrades_to_sleep_channel(self) -> None:
        assembler = LiveSituationAssembler()
        frame = assembler.assemble(
            _inp(_snapshot(sleep={"status": "asleep"})),
        )
        plan = LiveBehaviorResolver().resolve(
            frame,
            semantic_capabilities_from_profile({
                "capabilities": {"has_body_angle_y": True, "has_breath": True},
            }),
        )
        self.assertTrue(plan.degrade_to_sleep)
        self.assertEqual(plan.cancel_reason, "sleep_channel")
        self.assertEqual(plan.body_class, "none")
        self.assertEqual(plan.breath_class, "none")

    def test_missing_capabilities_degrade(self) -> None:
        assembler = LiveSituationAssembler()
        frame = assembler.assemble(
            _inp(
                _snapshot(
                    world=WorldSituation(activity="reading", posture="sitting"),
                ),
            ),
        )
        plan = LiveBehaviorResolver().resolve(frame)
        self.assertEqual(plan.body_class, "none")
        self.assertEqual(plan.breath_class, "none")
        self.assertEqual(plan.motion_class, "none")
        self.assertFalse(plan_contains_rig_identifiers(plan))


class MixinEmbodimentTests(unittest.TestCase):
    def test_refresh_stamps_plan_without_live_mode_enabled(self) -> None:
        host = _Host()
        frame = host.refresh_live_situation(trigger_kind="heartbeat")
        assert frame is not None
        self.assertEqual(frame.commitment.switch_margin, 0.2)
        diag = host.live_situation_diagnostics()
        self.assertIn("behavior_plan", diag)
        self.assertIsNotNone(diag["behavior_plan"])
        self.assertNotIn("Param", str(diag["behavior_plan"]))
        self.assertFalse(hasattr(host, "_live_mode_enabled"))

    def test_leaving_live_clears_embodiment(self) -> None:
        host = _Host()
        host.refresh_live_situation(trigger_kind="heartbeat")
        self.assertIsNotNone(host.live_embodiment_payload())
        host.set_behavior_posture("turn_based")
        self.assertIsNone(host.live_embodiment_payload())

    def test_still_no_live_mode_enabled_flag(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._live_voice_session_active = True
        controller._turn_in_progress = False
        controller._memory_settings = SimpleNamespace(
            idle_worker_quiet_threshold_seconds=30.0,
        )
        controller._last_user_activity_at = 0.0
        self.assertFalse(controller._is_user_idle())
        self.assertFalse(hasattr(controller, "_live_mode_enabled"))


class _Host(LiveModeMixin):
    def __init__(self) -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                behavior_posture="live_presence",
                live_quiet=False,
                live_impulse_bus_enabled=True,
            ),
        )
        self._live_voice_session_active = False
        self._turn_in_progress = False
        self.session_key = "u:s"
        self._user_id = "default"
        self._chat_db = None
        self._cue_store = SimpleNamespace(pending=lambda limit=8: [])
        self._prepared_nudge_store = SimpleNamespace(get_fresh=lambda _uid: None)
        self._snapshot = _snapshot()
        self._init_live_mode()

    def conversation_situation_snapshot(self, **_kwargs):
        return self._snapshot

    def avatar_payload(self):
        return {
            "capabilities": {"has_body_angle_y": True, "has_breath": True},
            "expressions": [{"name": "neutral"}],
            "reaction_mapping": {"content": "softSmile"},
            "idle_motion_group": "Idle",
        }


if __name__ == "__main__":
    unittest.main()
