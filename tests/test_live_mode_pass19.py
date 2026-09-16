"""Live mode Pass 19: L0 frame fill, allowed_actions, wake names, prompt."""
from __future__ import annotations

import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from app.core.live.allowed import (
    NONVERBAL_MENU,
    SLEEP_MENU,
    SPEECH_MENU,
    allowed_actions_for,
    intent_is_allowed,
)
from app.core.live.arbiter import arbitrate_live_proposal
from app.core.live.assembler import (
    COMMITMENT_WAKE_ON,
    LiveAssembleInput,
    assemble_live_situation,
)
from app.core.live.labels import cap_live_subject
from app.core.live.policy_context import (
    LivePolicyContextRuntime,
    situation_summary,
)
from app.core.live.prompt import LivePolicyPromptAssembler
from app.core.live.urge import LiveUrge
from app.core.live.wait import WAKE_ALIASES, WAKE_ALLOWLIST, LiveWaitScheduler
from tests.test_live_mode_pass7 import _Host, _frame, _proposal, _snapshot
from tests.test_live_mode_pass11 import _coding_evidence, _frame as _c6_frame


def _now() -> datetime:
    return datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


class AllowedActionsTests(unittest.TestCase):
    def test_idle_awake_includes_speech(self) -> None:
        frame = _frame()
        allowed = allowed_actions_for(frame)
        self.assertEqual(allowed, NONVERBAL_MENU + SPEECH_MENU)
        self.assertEqual(frame.constraints.allowed_actions, allowed)
        self.assertIn("request_main_speech", allowed)

    def test_sleep_is_wait_only(self) -> None:
        frame = _frame(sleep={"status": "asleep"})
        self.assertEqual(frame.constraints.allowed_actions, SLEEP_MENU)
        self.assertNotIn("request_main_speech", frame.constraints.allowed_actions)
        self.assertNotIn("react_affectively", frame.constraints.allowed_actions)

    def test_playback_closes_speech_menu(self) -> None:
        inp = LiveAssembleInput(
            snapshot=_snapshot(),
            impulses=(),
            now=_now(),
            monotonic_ms=10_000.0,
            playback_active=True,
        )
        frame = assemble_live_situation(inp, generation=1)
        self.assertTrue(frame.interaction.playback_active)
        self.assertNotIn("request_main_speech", frame.constraints.allowed_actions)
        self.assertIn("wait", frame.constraints.allowed_actions)

    def test_speech_budget_restamp_drops_speech(self) -> None:
        ev = _coding_evidence(
            app="", source="lock", os_idle="locked", confidence=0.0,
        )
        frame = _c6_frame(user_active_app="", activity_evidence=ev)
        stamped = LivePolicyContextRuntime().apply(frame)
        self.assertEqual(stamped.constraints.speech_budget, "forbidden")
        self.assertNotIn(
            "request_main_speech", stamped.constraints.allowed_actions,
        )


class ArbiterAllowedTests(unittest.TestCase):
    def test_empty_menu_does_not_reject(self) -> None:
        result = arbitrate_live_proposal(
            _proposal(intent="request_main_speech", selected_urge_id="u1"),
            snapshot_generation=1,
            known_urge_ids=("u1",),
            allowed_actions=(),
        )
        self.assertTrue(result.accepted)

    def test_sleep_menu_rejects_speech(self) -> None:
        result = arbitrate_live_proposal(
            _proposal(intent="request_main_speech", selected_urge_id="u1"),
            snapshot_generation=1,
            known_urge_ids=("u1",),
            allowed_actions=SLEEP_MENU,
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "not_allowed")

    def test_intent_is_allowed_unknown_is_false(self) -> None:
        self.assertFalse(intent_is_allowed("explode", NONVERBAL_MENU))
        self.assertTrue(intent_is_allowed("wait", ()))


class WakeNameTests(unittest.TestCase):
    def test_aliases_map_dead_names(self) -> None:
        self.assertEqual(
            WAKE_ALIASES["user.speech_started"], "user.voice_start",
        )
        self.assertEqual(
            WAKE_ALIASES["world.activity_changed"], "activity.session_changed",
        )
        self.assertIn("user.voice_start", WAKE_ALLOWLIST)
        self.assertIn("activity.session_changed", WAKE_ALLOWLIST)
        self.assertIn("activity.idle", WAKE_ALLOWLIST)
        self.assertIn("activity.lock", WAKE_ALLOWLIST)
        self.assertNotIn("user.speech_started", WAKE_ALLOWLIST)
        self.assertNotIn("world.activity_changed", WAKE_ALLOWLIST)

    def test_schedule_rewrites_aliases(self) -> None:
        sched = LiveWaitScheduler()
        wait = sched.schedule(
            generation=1,
            urge_id="u1",
            reconsider_after_ms=5_000,
            now_mono_ms=10_000.0,
            wake_on=("user.speech_started", "world.activity_changed"),
            reason_code="test",
        )
        self.assertEqual(
            wait.wake_on,
            ("user.voice_start", "activity.session_changed"),
        )
        self.assertFalse(
            sched.should_suppress_inference(
                "user.voice_start", now_mono_ms=11_000.0, generation=1,
            )
        )

    def test_commitment_wakes_use_producer_names(self) -> None:
        self.assertIn("user.voice_start", COMMITMENT_WAKE_ON)
        self.assertIn("activity.session_changed", COMMITMENT_WAKE_ON)
        self.assertNotIn("user.speech_started", COMMITMENT_WAKE_ON)
        self.assertNotIn("world.activity_changed", COMMITMENT_WAKE_ON)
        frame = _frame()
        self.assertEqual(frame.commitment.wake_on, COMMITMENT_WAKE_ON)


class ClockAndContinuityTests(unittest.TestCase):
    def test_mixin_supplies_clocks_and_held_action(self) -> None:
        host = _Host()
        now = time.monotonic() * 1000.0
        host._live_last_aiko_spoke_ms = now - 4_000.0
        host._live_last_semantic_action_ms = now - 2_000.0
        host._live_policy_controller.last_accepted_nonverbal = {
            "intent": "attend",
            "hold_until_ms": now + 8_000.0,
        }
        host._goal_store = SimpleNamespace(list_active=lambda: [
            SimpleNamespace(metadata={"summary": "learn piano"}, content=""),
            SimpleNamespace(
                metadata={"summary": "https://evil.example/x"}, content="",
            ),
            SimpleNamespace(metadata={"summary": "see her today"}, content=""),
        ])
        host._relationship_tracker = SimpleNamespace(
            current_phase=lambda _uid: "familiar",
        )
        host._live_policy_client = SimpleNamespace(_gate=object())
        frame = host.refresh_live_situation(
            trigger_kind="heartbeat", now_mono_ms=now,
        )
        assert frame is not None
        self.assertGreaterEqual(frame.temporal.since_aiko_spoke_ms, 3_500)
        self.assertGreaterEqual(frame.temporal.since_semantic_action_ms, 1_500)
        self.assertEqual(frame.aiko.current_actions, ("attend",))
        self.assertEqual(frame.continuity.relationship_phase, "familiar")
        self.assertEqual(frame.continuity.goals, ("learn piano",))
        self.assertEqual(frame.constraints.resource_contention, "shared_worker")
        self.assertFalse(hasattr(host, "_live_mode_enabled"))

    def test_micro_stamps_spoke_clock(self) -> None:
        host = _Host()
        self.assertIsNone(host._live_last_aiko_spoke_ms)
        host._live_mark_aiko_spoke()
        self.assertIsNotNone(host._live_last_aiko_spoke_ms)

    def test_embodiment_change_stamps_semantic_clock(self) -> None:
        host = _Host()
        frame = host.refresh_live_situation(trigger_kind="heartbeat")
        assert frame is not None
        self.assertIsNone(host._live_last_semantic_action_ms)
        host._live_policy_controller.last_accepted_nonverbal = {
            "generation": int(frame.generation),
            "intent": "acknowledge_user",
            "attention_target": frame.attention.target,
            "ttl_ms": 800,
            "hold_until_ms": time.monotonic() * 1000.0 + 800,
            "budget_class": "expression",
        }
        host._refresh_live_embodiment(frame)
        self.assertIsNotNone(host._live_last_semantic_action_ms)

    def test_playback_pending_closes_speech(self) -> None:
        host = _Host()
        host.mark_playback_pending()
        frame = host.refresh_live_situation(trigger_kind="heartbeat")
        assert frame is not None
        self.assertTrue(frame.interaction.playback_active)
        self.assertNotIn("request_main_speech", frame.constraints.allowed_actions)

    def test_recent_completed_actions_newest_last(self) -> None:
        runtime = LivePolicyContextRuntime()
        frame = _frame()
        runtime.apply(frame)
        runtime.record_outcome(
            proposed_action="attend", arbiter_result="ok", executed=True,
        )
        runtime.apply(frame)
        runtime.record_outcome(
            proposed_action="wait", arbiter_result="ok", executed=True,
        )
        runtime.apply(frame)
        runtime.record_outcome(
            proposed_action="attend", arbiter_result="ok", executed=True,
        )
        self.assertEqual(runtime.recent_completed_actions(), ("wait", "attend"))


class PromptTests(unittest.TestCase):
    def test_situation_includes_mood_vitality_speech_ok(self) -> None:
        frame = _frame()
        rendered = LivePolicyPromptAssembler()._render_situation(frame)
        summary = situation_summary(frame)
        self.assertIn("mood=content", rendered)
        self.assertIn("vitality=normal", rendered)
        self.assertIn("speech_ok=yes", rendered)
        self.assertIn("mood content", summary)
        self.assertIn("vitality normal", summary)
        self.assertNotIn("learn piano", summary)
        self.assertNotIn("title=", rendered)
        self.assertNotIn("title=", summary)

    def test_cue_subject_is_privacy_capped(self) -> None:
        assembler = LivePolicyPromptAssembler()
        safe = LiveUrge(
            urge_id="u1",
            kind="ask_about_result",
            subject="film photography",
            created_at="2026-09-14T12:00:00Z",
            created_monotonic_ms=9_000.0,
            expires_after_ms=30_000,
            source="cue",
        )
        dirty = LiveUrge(
            urge_id="u2",
            kind="ask_about_result",
            subject="https://secret.example/today",
            created_at="2026-09-14T12:00:00Z",
            created_monotonic_ms=9_000.0,
            expires_after_ms=30_000,
            source="cue",
        )
        prompt = assembler.assemble(
            frame=_frame(),
            urges=(safe, dirty),
            context_window=40960,
            max_tokens=64,
            prompt_ceiling=12000,
        ).messages[0]["content"]
        self.assertIn("1. id=u1 kind=ask_about_result subject=film photography", prompt)
        self.assertNotIn("secret.example", prompt)
        self.assertNotIn("https://", prompt)
        self.assertIn("2. id=u2 kind=ask_about_result", prompt)
        self.assertNotIn("subject=https://", prompt)

    def test_cap_live_subject_drops_unsafe(self) -> None:
        self.assertEqual(cap_live_subject("film photography"), "film photography")
        self.assertEqual(cap_live_subject("see her today"), "")
        self.assertEqual(cap_live_subject("https://evil.example"), "")
        self.assertEqual(cap_live_subject("a" * 80), "")
        self.assertEqual(cap_live_subject("  two   words  "), "two words")


if __name__ == "__main__":
    unittest.main()
