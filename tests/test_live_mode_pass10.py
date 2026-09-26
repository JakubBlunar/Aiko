"""Live mode Pass 10: gated unprompted main-wake."""
from __future__ import annotations

import threading
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.core.brain.events import ProactiveEvent
from app.core.concepts.concept_diets import DietTuning
from app.core.conversation.stance import INITIATE, _OFFERS
from app.core.live.inclination import LiveInclinationRuntime
from app.core.live.main_wake import (
    TRANSCRIPT_FLOOR_TOKENS,
    admit_main_wake,
    render_live_talk_about,
)
from app.core.live.prompt import LivePolicyPromptAssembler
from app.core.session.prompt_assembler import _PROMPT_BLOCK_TIERS
from app.core.session.task_orchestration_mixin import TaskOrchestrationMixin
from tests.test_live_mode_pass1 import LiveModeMixinHost
from tests.test_live_mode_pass7 import _controller, _FakePolicyClient, _frame


def _delight_runtime() -> tuple[LiveInclinationRuntime, str]:
    runtime = LiveInclinationRuntime()
    now_ms = time.monotonic() * 1000.0
    urge = runtime.urges.propose(
        kind="share_delight",
        subject="shared_activity",
        source="test",
        source_ids=("x",),
        repetition_key="delight:x",
        now_mono_ms=now_ms,
    )
    assert urge is not None
    return runtime, urge.urge_id


class AdmitGateTests(unittest.TestCase):
    def test_admits_share_delight_on_idle_floor(self) -> None:
        runtime, urge_id = _delight_runtime()
        enqueued: list[dict] = []
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": urge_id,
            "intent": "request_main_speech",
            "arguments": {},
            "reason_code": "share_the_scene",
            "context_refs": [],
        })
        frame = _frame()
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
            inclination_provider=lambda: runtime,
            on_main_wake=lambda payload: enqueued.append(payload) or True,
        )
        result = controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={"known_urge_ids": (urge_id,)},
            user_intent=False,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=client,
        )
        assert result is not None
        self.assertTrue(result.accepted)
        self.assertTrue(controller.last_proposal["executed"])
        self.assertTrue(controller.last_proposal.get("main_wake_admitted"))
        self.assertFalse(controller.last_proposal["shadow"])
        self.assertEqual(len(enqueued), 1)
        self.assertEqual(enqueued[0]["reason_code"], "share_the_scene")
        self.assertEqual(
            runtime.budget.remaining(
                "main_wake", now_mono_ms=time.monotonic() * 1000.0,
            ),
            0,
        )
        diag = controller.diagnostics()
        self.assertEqual(diag["main_wake_admitted"], 1)
        self.assertEqual(diag["main_wake_proposed"], 1)
        self.assertEqual(controller.proactive_enqueued, 1)

    def test_user_intent_does_not_enqueue(self) -> None:
        runtime, urge_id = _delight_runtime()
        enqueued: list[dict] = []
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": urge_id,
            "intent": "request_main_speech",
            "arguments": {},
            "reason_code": "share_the_scene",
            "context_refs": [],
        })
        frame = _frame()
        controller = _controller(
            client_provider=lambda: client,
            generation_provider=lambda: int(frame.generation),
            inclination_provider=lambda: runtime,
            on_main_wake=lambda payload: enqueued.append(payload) or True,
        )
        controller._infer(
            frame,
            trigger_kind="user.message_sent",
            prompt_input={"known_urge_ids": (urge_id,)},
            user_intent=True,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=client,
        )
        self.assertEqual(enqueued, [])
        self.assertEqual(
            controller.last_proposal.get("main_wake_rejected"), "user_turn",
        )
        self.assertTrue(controller.last_proposal["shadow"])
        self.assertEqual(controller.proactive_enqueued, 0)

    def test_same_generation_retry_is_already_decided(self) -> None:
        runtime, urge_id = _delight_runtime()
        enqueued: list[dict] = []
        payload = {
            "snapshot_generation": 1,
            "selected_urge_id": urge_id,
            "intent": "request_main_speech",
            "arguments": {},
            "reason_code": "share_the_scene",
            "context_refs": [],
        }
        frame = _frame()
        controller = _controller(
            client_provider=lambda: _FakePolicyClient(payload),
            generation_provider=lambda: int(frame.generation),
            inclination_provider=lambda: runtime,
            on_main_wake=lambda item: enqueued.append(item) or True,
        )
        controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={"known_urge_ids": (urge_id,)},
            user_intent=False,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=_FakePolicyClient(payload),
        )
        self.assertEqual(len(enqueued), 1)
        # Replenish the 10-minute main_wake bucket so the retry is
        # rejected for generation lock, not budget.
        runtime.budget._tokens["main_wake"] = 1
        controller._infer(
            frame,
            trigger_kind="silence.wake",
            prompt_input={"known_urge_ids": (urge_id,)},
            user_intent=False,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=_FakePolicyClient(payload),
        )
        self.assertEqual(len(enqueued), 1)
        self.assertEqual(
            controller.last_proposal.get("main_wake_rejected"),
            "already_decided",
        )
        self.assertEqual(
            controller.diagnostics()["main_wake_reject_reasons"]["already_decided"],
            1,
        )

    def test_sleep_and_forbidden_and_floor_busy_reject(self) -> None:
        runtime, urge_id = _delight_runtime()
        frame = _frame()
        asleep = replace(
            frame,
            constraints=replace(frame.constraints, sleep_status="asleep"),
        )
        self.assertEqual(
            admit_main_wake(
                intent="request_main_speech",
                user_intent=False,
                selected_urge_id=urge_id,
                urges=runtime.urges.active(),
                frame=asleep,
                decided_generation=None,
                now_mono_ms=time.monotonic() * 1000.0,
                budget_remaining=1,
            ),
            "sleep",
        )
        forbidden = replace(
            frame,
            constraints=replace(frame.constraints, speech_budget="forbidden"),
        )
        self.assertEqual(
            admit_main_wake(
                intent="request_main_speech",
                user_intent=False,
                selected_urge_id=urge_id,
                urges=runtime.urges.active(),
                frame=forbidden,
                decided_generation=None,
                now_mono_ms=time.monotonic() * 1000.0,
                budget_remaining=1,
            ),
            "speech_forbidden",
        )
        busy = replace(
            frame,
            interaction=replace(frame.interaction, floor_owner="user"),
        )
        self.assertEqual(
            admit_main_wake(
                intent="request_main_speech",
                user_intent=False,
                selected_urge_id=urge_id,
                urges=runtime.urges.active(),
                frame=busy,
                decided_generation=None,
                now_mono_ms=time.monotonic() * 1000.0,
                budget_remaining=1,
            ),
            "floor_busy",
        )


class DispatchTests(unittest.TestCase):
    def test_stale_generation_drops(self) -> None:
        host = LiveModeMixinHost()
        host._live_mode_generation = 2
        event = ProactiveEvent(
            session_key="u:s",
            source="live_main_wake",
            live_generation=1,
            urge_id="u1",
            reason_code="share_the_scene",
        )
        host._run_live_main_wake(event)
        diag = host._live_policy_controller.diagnostics()
        self.assertEqual(diag["main_wake_rejected"], 1)
        self.assertEqual(diag["main_wake_reject_reasons"].get("stale"), 1)

    def test_handler_routes_live_main_wake_not_director(self) -> None:
        class _Host(TaskOrchestrationMixin):
            def __init__(self) -> None:
                self._proactive = MagicMock()
                self._live_voice_session_active = False
                self.ran: list[object] = []

            def is_live_presence(self) -> bool:
                return True

            def _run_live_main_wake(self, event: object) -> None:
                self.ran.append(event)

        host = _Host()
        event = ProactiveEvent(
            session_key="s",
            source="live_main_wake",
            live_generation=4,
            reason_code="share_the_scene",
        )
        host._on_task_proactive_event(event)
        self.assertEqual(len(host.ran), 1)
        host._proactive.notify_silence.assert_not_called()
        host._proactive.notify_task_escalation.assert_not_called()

    def test_silence_sources_stay_starved_under_live(self) -> None:
        class _Host(TaskOrchestrationMixin):
            def __init__(self) -> None:
                self._proactive = MagicMock()
                self._live_voice_session_active = False
                self.ran: list[object] = []

            def is_live_presence(self) -> bool:
                return True

            def publish_live_impulse(self, **_kwargs: object) -> None:
                return None

            def _run_live_main_wake(self, event: object) -> None:
                self.ran.append(event)

        host = _Host()
        host._on_task_proactive_event(
            ProactiveEvent(session_key="s", source="voice_silence"),
        )
        host._on_task_proactive_event(
            ProactiveEvent(session_key="s", source="typed_silence"),
        )
        host._proactive.notify_silence.assert_not_called()
        host._proactive.notify_typed_silence.assert_not_called()
        self.assertEqual(host.ran, [])


class TalkAboutTests(unittest.TestCase):
    def test_block_contains_reason_and_situation(self) -> None:
        text = render_live_talk_about({
            "intent": "request_main_speech",
            "reason_code": "share_the_scene",
            "urge_kind": "share_delight",
            "urge_id": "u1",
            "situation_summary": "inferred watching_anime; sharing together",
            "concept_ids": (12, 18),
        })
        self.assertIn("share_the_scene", text)
        self.assertIn("inferred watching_anime", text)
        self.assertIn("Continue this scene", text)
        self.assertNotIn("User:", text)

    def test_talk_about_offers_initiate_and_is_registered(self) -> None:
        self.assertIn("live_talk_about_block", _OFFERS[INITIATE])
        self.assertIn(
            "live_talk_about_block",
            _PROMPT_BLOCK_TIERS["T6_detectors"],
        )
        self.assertEqual(_PROMPT_BLOCK_TIERS["T6_detectors"][-1], "stance_block")

    def test_mixin_clears_payload_after_render(self) -> None:
        host = LiveModeMixinHost()
        host._live_talk_about_payload = {
            "intent": "request_main_speech",
            "reason_code": "share_the_scene",
            "situation_summary": "app Cursor",
        }
        text = host._render_live_talk_about_block()
        self.assertIn("share_the_scene", text)
        self.assertIsNone(host._live_talk_about_payload)
        self.assertEqual(host._render_live_talk_about_block(), "")


class TranscriptFloorTests(unittest.TestCase):
    def test_history_survives_fat_concepts(self) -> None:
        frame = _frame()
        concepts = [
            SimpleNamespace(lane="rail", kind="value", label=f"value-{i} " * 40)
            for i in range(24)
        ]
        rows = [
            SimpleNamespace(
                role="user" if i % 2 == 0 else "assistant",
                content=f"turn {i} about the same film",
                created_at="2026-09-11T18:20:00Z",
            )
            for i in range(8)
        ]
        prompt = LivePolicyPromptAssembler().assemble(
            frame=frame,
            concepts=concepts,
            transcript_rows=rows,
            context_window=40960,
            max_tokens=64,
            prompt_ceiling=12000,
            diet_tuning=DietTuning(context_window=40960),
        )
        self.assertGreater(prompt.region_tokens["transcript"], 0)
        self.assertGreaterEqual(
            prompt.region_tokens["transcript"],
            min(40, TRANSCRIPT_FLOOR_TOKENS),
        )
        self.assertIn("LAST CONVERSATION:", prompt.messages[0]["content"])
        self.assertIn("continue the", prompt.messages[0]["content"])
        self.assertIn("interrupt it", prompt.messages[0]["content"])


class MixinEnqueueTests(unittest.TestCase):
    def test_new_user_input_invalidates_queued_speech(self) -> None:
        host = LiveModeMixinHost()
        host._enqueue_live_main_wake({"generation": 500, "urge_id": "u1"})
        event = host.enqueued[0]
        host.publish_live_user_meaning("A different question", mode="typed")
        self.assertEqual(host._live_main_wake_dispatch_block(event), "new_user_intent")

    def test_enqueue_builds_live_main_wake_event(self) -> None:
        host = LiveModeMixinHost()
        host._live_mode_generation = 7
        ok = host._enqueue_live_main_wake({
            "generation": 500,
            "urge_id": "u1",
            "reason_code": "share_the_scene",
            "situation_summary": "inferred watching_anime",
            "concept_ids": (3,),
        })
        self.assertTrue(ok)
        self.assertEqual(len(host.enqueued), 1)
        event = host.enqueued[0]
        self.assertIsInstance(event, ProactiveEvent)
        self.assertEqual(event.source, "live_main_wake")
        self.assertEqual(event.live_generation, 7)
        self.assertEqual(event.reason_code, "share_the_scene")
        self.assertEqual(event.concept_ids, (3,))


class ProposalShapeTests(unittest.TestCase):
    def test_missing_urge_does_not_enqueue(self) -> None:
        spoken: list[str] = []
        enqueued: list[dict] = []
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
            on_micro_utterance=lambda text, *_a: spoken.append(text) or True,
            on_main_wake=lambda payload: enqueued.append(payload) or True,
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
        self.assertEqual(enqueued, [])
        self.assertEqual(spoken, [])
        self.assertEqual(
            controller.last_proposal.get("main_wake_rejected"), "missing_urge",
        )


if __name__ == "__main__":
    unittest.main()
