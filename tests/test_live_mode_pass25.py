"""Live mode Pass 25: one-shot result-aware fallback (L7)."""
from __future__ import annotations

import threading
import time
import unittest

from app.core.live.epochs import classify_epoch
from app.core.live.fallback import (
    classify_execute_failure,
    legal_fallbacks,
    render_fallback_prompt,
    should_arm_fallback,
)
from app.core.live.inclination import LiveInclinationRuntime
from app.core.live.prompt import LivePolicyPromptAssembler
from tests.test_live_mode_pass7 import _FakePolicyClient, _controller, _frame
from tests.test_live_mode_pass22 import _shared_frame


class ClassifyFallbackTests(unittest.TestCase):
    def test_typed_failures_and_silent_degrade(self) -> None:
        self.assertEqual(
            classify_execute_failure("budget_exhausted", {}),
            "budget_exhausted",
        )
        self.assertEqual(
            classify_execute_failure("not_executed", {"micro_skipped": "floor_busy"}),
            "floor_preempted",
        )
        self.assertEqual(
            classify_execute_failure("capability_absent", {}),
            "capability_absent",
        )
        self.assertTrue(should_arm_fallback("budget_exhausted", intent="react_affectively"))
        self.assertTrue(should_arm_fallback("floor_preempted", intent="backchannel_user"))
        self.assertFalse(should_arm_fallback("capability_absent", intent="react_affectively"))
        self.assertFalse(should_arm_fallback("budget_exhausted", intent="wait"))
        self.assertEqual(
            legal_fallbacks("budget_exhausted"),
            ("wait", "noop", "remain_present"),
        )


class ArmFallbackTests(unittest.TestCase):
    def test_budget_exhausted_arms_once(self) -> None:
        runtime = LiveInclinationRuntime()
        now_ms = time.monotonic() * 1000.0
        while runtime.budget.consume("expression", now_mono_ms=now_ms):
            pass
        client = _FakePolicyClient({
            "snapshot_generation": 1,
            "selected_urge_id": None,
            "intent": "react_affectively",
            "arguments": {},
            "reason_code": "react",
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
        self.assertFalse(controller.last_proposal["executed"])
        self.assertTrue(controller.last_proposal.get("budget_exhausted"))
        pending = controller._pending_fallback
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(pending["failure"], "budget_exhausted")
        action_id = str(pending["action_id"])
        self.assertIn("wait", pending["legal"])
        self.assertNotIn("react_affectively", pending["legal"])
        self.assertNotIn("request_main_speech", pending["legal"])

        client.payload["intent"] = "react_affectively"
        result = controller._infer(
            frame,
            trigger_kind="idle.reconsider",
            prompt_input={},
            user_intent=False,
            started_generation=int(frame.generation),
            cancel=threading.Event(),
            client=client,
        )
        assert result is not None
        self.assertEqual(controller.last_proposal.get("fallback_coerced"), "wait")
        self.assertIsNone(controller._pending_fallback)
        self.assertFalse(
            controller._arm_fallback(
                action_id=action_id,
                failure="budget_exhausted",
                original_intent="react_affectively",
                generation=int(frame.generation),
                frame=frame,
            )
        )

    def test_user_intent_cancels_pending(self) -> None:
        controller = _controller()
        controller._pending_fallback = {
            "action_id": "abc",
            "failure": "floor_preempted",
            "original_intent": "request_main_speech",
            "legal": ["wait", "remain_present"],
            "generation": 1,
        }
        controller.consider(
            _frame(),
            trigger_kind="user.message_sent",
            prompt_input={},
            user_intent=True,
        )
        self.assertIsNone(controller._pending_fallback)

    def test_silence_does_not_arm_fallback(self) -> None:
        controller = _controller()
        controller.note_main_wake_silence(1)
        self.assertIsNone(controller._pending_fallback)

    def test_floor_reject_arms_floor_preempted(self) -> None:
        from app.core.live.actions import start_action

        controller = _controller()
        record = start_action(
            intent="request_main_speech", generation=1, reason="ok",
        )
        controller.last_action = record
        controller.note_main_wake_reject("floor_busy", 1)
        pending = controller._pending_fallback
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(pending["failure"], "floor_preempted")
        self.assertEqual(pending["action_id"], record.action_id)


class FallbackPromptTests(unittest.TestCase):
    def test_prompt_lists_legal_intents(self) -> None:
        text = render_fallback_prompt({
            "action_id": "deadbeef",
            "failure": "budget_exhausted",
            "original_intent": "react_affectively",
            "legal": ("wait", "noop", "remain_present"),
        })
        self.assertIn("FALLBACK", text)
        self.assertIn("budget_exhausted", text)
        self.assertIn("deadbeef", text)
        self.assertIn("remain_present", text)
        self.assertNotIn("title", text)

        prompt = LivePolicyPromptAssembler().assemble(
            frame=_shared_frame(),
            context_window=4096,
            max_tokens=512,
            prompt_ceiling=4000,
            fallback={
                "action_id": "deadbeef",
                "failure": "budget_exhausted",
                "original_intent": "react_affectively",
                "legal": ("wait", "noop", "remain_present"),
            },
        )
        body = prompt.messages[0]["content"]
        self.assertIn("FALLBACK", body)
        self.assertIn("Do not retry the failed action_id", body)

    def test_action_results_stay_data_only(self) -> None:
        self.assertEqual(classify_epoch("aiko.action_rejected"), "data_only")
        self.assertEqual(classify_epoch("aiko.action_completed"), "data_only")


if __name__ == "__main__":
    unittest.main()
