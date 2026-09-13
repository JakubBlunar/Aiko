"""Live mode Pass 7.5: do not fight the turn. Cadence unchanged."""
from __future__ import annotations

import json
import threading
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from app.core.conversation.conversation_situation import (
    ConversationSituationSnapshot,
    WorldSituation,
)
from app.core.infra.settings import LlmRoute
from app.core.live.assembler import LiveAssembleInput, LiveSituationAssembler
from app.core.live.controller import LivePolicyController
from app.core.live.impulse import LiveImpulse
from app.core.live.inclination import LiveInclinationRuntime
from app.core.live.prompt import LivePolicyPromptAssembler
from app.core.live.wait import LiveWaitScheduler
from app.core.session.live_mode_mixin import LiveModeMixin


def _now() -> datetime:
    return datetime(2026, 9, 11, 22, 0, tzinfo=timezone.utc)


def _snapshot(**kwargs: object) -> ConversationSituationSnapshot:
    fields = dict(
        session_id="s",
        generation=0,
        observed_at="2026-09-11T22:00:00Z",
        input_mode="typed",
        floor_transition="neither",
        dialogue_act="",
        arc="casual_check_in",
        arc_confidence=0.4,
        mood_label="content",
        vitality_band="normal",
        user_present=True,
        user_active_app="Cursor",
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


def _assemble(**kwargs: object):
    assembler = LiveSituationAssembler()
    fields: dict[str, object] = dict(
        snapshot=_snapshot(),
        impulses=(),
        now=_now(),
        monotonic_ms=10_000.0,
        trigger_kind="silence.wake",
    )
    fields.update(kwargs)
    return assembler.assemble(LiveAssembleInput(**fields))  # type: ignore[arg-type]


class _Usage:
    prompt_tokens = 40
    completion_tokens = 20


class _FakePolicyClient:
    def __init__(self, payload: dict[str, object] | None = None) -> None:
        self.payload = payload or {
            "snapshot_generation": 99,
            "selected_urge_id": None,
            "intent": "wait",
            "arguments": {"reasoning": "nothing new"},
            "reason_code": "idle",
            "context_refs": [],
        }
        self.calls: list[dict[str, object]] = []

    def chat_json(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return json.dumps(self.payload), _Usage()


def _route() -> LlmRoute:
    return LlmRoute(
        provider_id="local_ollama",
        model="qwen3.5:4b",
        context_window=40960,
        max_tokens=512,
        temperature=0.0,
    )


def _controller(**kwargs: object) -> LivePolicyController:
    fields: dict[str, object] = dict(
        route_provider=_route,
        generation_provider=lambda: 1,
    )
    fields.update(kwargs)
    return LivePolicyController(**fields)  # type: ignore[arg-type]


class FloorOverlayTests(unittest.TestCase):
    def test_wait_during_turn_schedules_without_overlay(self) -> None:
        runtime = LiveInclinationRuntime()
        client = _FakePolicyClient()
        typing = LiveImpulse(
            event_id="t1",
            kind="user.typing_started",
            source="test",
            session_key="s",
            mode_generation=1,
            sequence=1,
            occurred_at="2026-09-11T22:00:00Z",
            monotonic_ms=9_500.0,
            priority="attention",
            ttl_ms=30_000,
            coalesce_key="typing",
            privacy="local_state",
        )
        frame = _assemble(
            trigger_kind="user.message_sent",
            turn_in_progress=True,
            impulses=(typing,),
        )
        self.assertTrue(frame.interaction.turn_active)
        self.assertEqual(frame.attention.target, "user")
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
            inclination_provider=lambda: runtime,
        )
        result = controller._infer(
            frame,
            trigger_kind="user.message_sent",
            prompt_input={},
            user_intent=True,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=client,
        )
        assert result is not None
        self.assertTrue(result.accepted)
        self.assertTrue(controller.last_proposal["executed"])
        self.assertEqual(
            controller.last_proposal.get("overlay_skipped"), "turn_or_tts",
        )
        self.assertFalse(controller.last_accepted_nonverbal)
        self.assertIsNone(
            controller.accepted_nonverbal_for(int(frame.generation)),
        )
        self.assertIsNotNone(runtime.wait.current)

        host = _Host()
        host._live_policy_controller = controller
        host._refresh_live_embodiment(frame)
        plan = host._live_behavior_plan
        assert plan is not None
        self.assertEqual(plan.intent, "attend")
        self.assertNotEqual(plan.intent, "wait")

    def test_wait_during_tts_skips_overlay(self) -> None:
        runtime = LiveInclinationRuntime()
        client = _FakePolicyClient()
        frame = _assemble(tts_active=True)
        self.assertTrue(frame.interaction.tts_active)
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
            inclination_provider=lambda: runtime,
        )
        result = controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={},
            user_intent=False,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=client,
        )
        assert result is not None
        self.assertTrue(controller.last_proposal["executed"])
        self.assertEqual(
            controller.last_proposal.get("overlay_skipped"), "turn_or_tts",
        )
        self.assertFalse(controller.last_accepted_nonverbal)
        self.assertIsNotNone(runtime.wait.current)


class OverlayTtlTests(unittest.TestCase):
    def test_expired_overlay_falls_back_on_same_generation(self) -> None:
        host = _Host()
        frame = host.refresh_live_situation(trigger_kind="heartbeat")
        assert frame is not None
        host._live_policy_controller.last_accepted_nonverbal = {
            "generation": int(frame.generation),
            "intent": "acknowledge_user",
            "attention_target": "user",
            "ttl_ms": 800,
            "hold_until_ms": time.monotonic() * 1000.0 - 50.0,
            "budget_class": "expression",
        }
        self.assertIsNone(
            host._live_policy_controller.accepted_nonverbal_for(
                int(frame.generation),
            ),
        )
        host._refresh_live_embodiment(frame)
        plan = host._live_behavior_plan
        assert plan is not None
        self.assertNotEqual(plan.intent, "acknowledge_user")

    def test_live_overlay_still_matches_unexpired_ttl(self) -> None:
        host = _Host()
        frame = host.refresh_live_situation(trigger_kind="heartbeat")
        assert frame is not None
        host._live_policy_controller.last_accepted_nonverbal = {
            "generation": int(frame.generation),
            "intent": "acknowledge_user",
            "attention_target": frame.attention.target,
            "ttl_ms": 800,
            "hold_until_ms": time.monotonic() * 1000.0 + 800,
            "budget_class": "expression",
        }
        host._refresh_live_embodiment(frame)
        plan = host._live_behavior_plan
        assert plan is not None
        self.assertEqual(plan.intent, "acknowledge_user")


class WaitSilenceTests(unittest.TestCase):
    def test_silence_wake_generation_bump_does_not_cancel_wait(self) -> None:
        sched = LiveWaitScheduler()
        sched.schedule(
            generation=1,
            urge_id="",
            reconsider_after_ms=30_000,
            now_mono_ms=10_000.0,
            wake_on=("user.message_sent",),
            reason_code="hold",
        )
        self.assertTrue(
            sched.should_suppress_inference(
                "silence.wake", now_mono_ms=11_000.0, generation=2,
            )
        )
        self.assertIsNotNone(sched.current)

    def test_silence_wake_in_wake_on_still_wakes(self) -> None:
        sched = LiveWaitScheduler()
        sched.schedule(
            generation=1,
            urge_id="",
            reconsider_after_ms=30_000,
            now_mono_ms=10_000.0,
            wake_on=("silence.wake",),
            reason_code="hold",
        )
        self.assertFalse(
            sched.should_suppress_inference(
                "silence.wake", now_mono_ms=11_000.0, generation=2,
            )
        )
        self.assertIsNone(sched.current)

    def test_user_message_still_cancels_after_generation_bump(self) -> None:
        sched = LiveWaitScheduler()
        sched.schedule(
            generation=1,
            urge_id="",
            reconsider_after_ms=30_000,
            now_mono_ms=10_000.0,
            wake_on=("user.message_sent",),
            reason_code="hold",
        )
        self.assertFalse(
            sched.should_suppress_inference(
                "user.message_sent", now_mono_ms=11_000.0, generation=2,
            )
        )
        self.assertIsNone(sched.current)

    def test_inclination_apply_keeps_wait_on_silence_wake(self) -> None:
        runtime = LiveInclinationRuntime()
        assembler = LiveSituationAssembler()
        first = assembler.assemble(
            LiveAssembleInput(
                snapshot=_snapshot(),
                impulses=(),
                now=_now(),
                monotonic_ms=10_000.0,
                trigger_kind="silence.wake",
            ),
        )
        now_ms = time.monotonic() * 1000.0
        runtime.wait.schedule(
            generation=int(first.generation),
            urge_id="",
            reconsider_after_ms=30_000,
            now_mono_ms=now_ms,
            wake_on=("user.message_sent",),
            reason_code="hold",
        )
        second = assembler.assemble(
            LiveAssembleInput(
                snapshot=_snapshot(),
                impulses=(),
                now=_now(),
                monotonic_ms=now_ms + 1_000.0,
                trigger_kind="silence.wake",
            ),
        )
        self.assertGreater(second.generation, first.generation)
        runtime.apply(
            second,
            trigger_kind="silence.wake",
            now_mono_ms=now_ms + 1_000.0,
        )
        self.assertIsNotNone(runtime.wait.current)
        client = _FakePolicyClient()
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(second.generation),
            inclination_provider=lambda: runtime,
        )
        controller.consider(second, trigger_kind="silence.wake")
        self.assertEqual(client.calls, [])


class PromptNudgeTests(unittest.TestCase):
    def test_instructions_prefer_attend_on_user_turn(self) -> None:
        prompt = LivePolicyPromptAssembler().assemble(
            frame=_assemble(),
            context_window=40960,
            max_tokens=512,
            prompt_ceiling=12000,
        )
        system = prompt.messages[0]["content"]
        self.assertIn("wait and noop are for idle", system)
        self.assertIn("acknowledge_user", system)


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
