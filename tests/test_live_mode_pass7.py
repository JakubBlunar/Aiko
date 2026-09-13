"""Live mode Pass 7: nonverbal execution. Speech stays shadow; chat still replies."""
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
from app.core.live.arbiter import arbitrate_live_proposal
from app.core.live.assembler import LiveAssembleInput, LiveSituationAssembler
from app.core.live.capabilities import SemanticCapabilities
from app.core.live.controller import LivePolicyController
from app.core.live.inclination import LiveInclinationRuntime
from app.core.live.proposal import LivePolicyProposal
from app.core.live.resolver import LiveBehaviorResolver
from app.core.session.live_mode_mixin import LiveModeMixin


def _now() -> datetime:
    return datetime(2026, 9, 11, 18, 30, tzinfo=timezone.utc)


def _snapshot(**kwargs: object) -> ConversationSituationSnapshot:
    fields = dict(
        session_id="s",
        generation=0,
        observed_at="2026-09-11T18:30:00Z",
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


def _frame(**kwargs: object):
    assembler = LiveSituationAssembler()
    inp = LiveAssembleInput(
        snapshot=_snapshot(**kwargs),
        impulses=(),
        now=_now(),
        monotonic_ms=10_000.0,
        trigger_kind="silence.wake",
    )
    return assembler.assemble(inp)


def _proposal(**kwargs: object) -> LivePolicyProposal:
    fields: dict[str, object] = dict(
        snapshot_generation=1,
        selected_urge_id="",
        intent="wait",
        arguments={},
        reason_code="idle",
        context_refs=(),
    )
    fields.update(kwargs)
    return LivePolicyProposal(**fields)  # type: ignore[arg-type]


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


class ArbiterPass7Tests(unittest.TestCase):
    def test_nonverbal_without_urge_is_accepted(self) -> None:
        result = arbitrate_live_proposal(
            _proposal(intent="acknowledge_user"), snapshot_generation=1,
        )
        self.assertTrue(result.accepted)
        self.assertEqual(result.reason, "ok")

    def test_speech_without_urge_is_rejected(self) -> None:
        result = arbitrate_live_proposal(
            _proposal(intent="request_main_speech"), snapshot_generation=1,
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "missing_urge")

    def test_unknown_urge_still_fails(self) -> None:
        result = arbitrate_live_proposal(
            _proposal(intent="attend", selected_urge_id="missing"),
            snapshot_generation=1,
            known_urge_ids=("other",),
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "unknown_urge")

    def test_unknown_context_refs_are_stripped_not_rejected(self) -> None:
        result = arbitrate_live_proposal(
            _proposal(
                intent="wait",
                context_refs=("situation:shared_anime", "concept:184"),
            ),
            snapshot_generation=1,
            known_context_refs=("1", "concept:184", "urge:u1"),
        )
        self.assertTrue(result.accepted)
        assert result.proposal is not None
        self.assertEqual(result.proposal.context_refs, ("concept:184",))


class StampAndExecuteTests(unittest.TestCase):
    def test_stamps_generation_from_the_frame(self) -> None:
        client = _FakePolicyClient()
        frame = _frame()
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
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
        self.assertTrue(result.accepted)
        payload = controller.last_proposal["proposal"]
        self.assertEqual(payload["snapshot_generation"], int(frame.generation))
        self.assertNotEqual(payload["snapshot_generation"], 99)

    def test_wait_executes_and_schedules(self) -> None:
        runtime = LiveInclinationRuntime()
        client = _FakePolicyClient()
        frame = _frame()
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
        self.assertTrue(result.accepted)
        self.assertTrue(controller.last_proposal["executed"])
        self.assertIsNotNone(runtime.wait.current)
        held = controller.accepted_nonverbal_for(int(frame.generation))
        assert held is not None
        self.assertEqual(held["intent"], "wait")

    def test_late_generation_does_not_execute(self) -> None:
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": None,
            "intent": "attend",
            "arguments": {},
            "reason_code": "look",
            "context_refs": [],
        })
        gen = {"value": 1}
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(gen["value"]),
        )
        frame = _frame()
        gen["value"] = 2
        result = controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={},
            user_intent=False,
            started_generation=1,
            cancel=threading.Event(),
            client=client,
        )
        assert result is not None
        self.assertEqual(result.reason, "cancelled")
        self.assertFalse(controller.last_accepted_nonverbal)

    def test_speech_stays_shadow(self) -> None:
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": "u1",
            "intent": "request_main_speech",
            "arguments": {},
            "reason_code": "want_to_talk",
            "context_refs": [],
        })
        frame = _frame()
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
        )
        result = controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={"known_urge_ids": ("u1",)},
            user_intent=False,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=client,
        )
        assert result is not None
        self.assertTrue(result.talk_about)
        self.assertFalse(controller.last_proposal["executed"])
        self.assertTrue(controller.last_proposal["shadow"])


class WaitBudgetTests(unittest.TestCase):
    def test_wait_gates_inference(self) -> None:
        runtime = LiveInclinationRuntime()
        frame = _frame()
        runtime.wait.schedule(
            generation=int(frame.generation),
            urge_id="",
            reconsider_after_ms=30_000,
            now_mono_ms=time.monotonic() * 1000.0,
            wake_on=("user.message_sent",),
            reason_code="hold",
        )
        client = _FakePolicyClient()
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
            inclination_provider=lambda: runtime,
        )
        controller.consider(frame, trigger_kind="silence.wake")
        self.assertEqual(client.calls, [])
        self.assertFalse(controller._inflight)

    def test_budget_exhaustion_does_not_execute(self) -> None:
        runtime = LiveInclinationRuntime()
        now_ms = time.monotonic() * 1000.0
        budget = runtime.budget
        while budget.consume("expression", now_mono_ms=now_ms):
            pass
        self.assertFalse(budget.consume("expression", now_mono_ms=now_ms))
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": None,
            "intent": "acknowledge_user",
            "arguments": {},
            "reason_code": "hi",
            "context_refs": [],
        })
        frame = _frame()
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
        self.assertTrue(result.accepted)
        self.assertFalse(controller.last_proposal["executed"])
        self.assertTrue(controller.last_proposal.get("budget_exhausted"))
        self.assertIsNotNone(runtime.wait.current)

    def test_repeat_is_suppressed_inside_ttl(self) -> None:
        runtime = LiveInclinationRuntime()
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": None,
            "intent": "attend",
            "arguments": {},
            "reason_code": "look",
            "context_refs": [],
            "reconsider_after_ms": 20_000,
        })
        frame = _frame()
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
            inclination_provider=lambda: runtime,
        )
        first = controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={},
            user_intent=False,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=client,
        )
        assert first is not None
        self.assertTrue(controller.last_proposal["executed"])
        second = controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={},
            user_intent=False,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=client,
        )
        assert second is not None
        self.assertFalse(controller.last_proposal["executed"])
        self.assertTrue(controller.last_proposal.get("repeat_suppressed"))


class OverlayTests(unittest.TestCase):
    def test_policy_intent_overlays_idle_frame(self) -> None:
        frame = _frame()
        caps = SemanticCapabilities(
            can_orient=True, can_express=True, can_breathe=True,
        )
        baseline = LiveBehaviorResolver().resolve(frame, caps)
        overlay = LiveBehaviorResolver().resolve(
            frame, caps, policy_intent="acknowledge_user", ttl_ms=800,
        )
        self.assertEqual(overlay.intent, "acknowledge_user")
        self.assertEqual(overlay.attention_target, frame.attention.target)
        self.assertEqual(overlay.gaze_class, "user_eye_contact")
        self.assertEqual(overlay.body_class, "lean_in")
        self.assertEqual(overlay.ttl_ms, 800)
        self.assertNotEqual(overlay.intent, baseline.intent)

    def test_embodiment_follows_accepted_nonverbal(self) -> None:
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
        payload = host.live_embodiment_payload()
        assert payload is not None
        self.assertEqual(payload["plan"]["intent"], "acknowledge_user")
        self.assertFalse(hasattr(host, "_live_mode_enabled"))

    def test_stale_overlay_falls_back_to_pass5(self) -> None:
        host = _Host()
        frame = host.refresh_live_situation(trigger_kind="heartbeat")
        assert frame is not None
        host._live_policy_controller.last_accepted_nonverbal = {
            "generation": int(frame.generation) - 1,
            "intent": "acknowledge_user",
            "attention_target": "user",
            "ttl_ms": 800,
            "hold_until_ms": time.monotonic() * 1000.0 + 800,
            "budget_class": "expression",
        }
        host._refresh_live_embodiment(frame)
        plan = host._live_behavior_plan
        assert plan is not None
        self.assertNotEqual(plan.intent, "acknowledge_user")


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
