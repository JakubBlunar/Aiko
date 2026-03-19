from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.session_controller import GoalInference, SessionController
from app.core.tooling.runtime.action_runtime import ActionExecutionResult


class SessionControllerFlowTests(unittest.TestCase):
    def test_autonomy_plan_agentic_switch_turn_never_requests_action(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._settings = SimpleNamespace(
            autonomy=SimpleNamespace(enabled=True),
        )
        controller._autonomy_mode = "interactive"

        plan = controller._plan_turn_autonomy(user_text="Switch to agentic mode.")

        self.assertFalse(plan.should_plan_action)
        self.assertEqual(plan.action_intent, "")

    def test_autonomy_mode_interactive_requires_confirmation(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._trace = lambda *_args, **_kwargs: None
        controller._settings = SimpleNamespace(actions=SimpleNamespace(require_confirmation=False))
        controller._state = SimpleNamespace(autonomy_mode="automatic")
        controller._autonomy_mode = "automatic"

        controller.set_autonomy_mode("interactive")

        self.assertTrue(controller._settings.actions.require_confirmation)

    def test_autonomy_mode_automatic_disables_confirmation(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._trace = lambda *_args, **_kwargs: None
        controller._settings = SimpleNamespace(actions=SimpleNamespace(require_confirmation=True))
        controller._state = SimpleNamespace(autonomy_mode="interactive")
        controller._autonomy_mode = "interactive"

        controller.set_autonomy_mode("automatic")

        self.assertFalse(controller._settings.actions.require_confirmation)

    def test_trace_turn_action_policy_logs_mode_and_confirmation(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._autonomy_mode = "automatic"
        controller._settings = SimpleNamespace(actions=SimpleNamespace(require_confirmation=False))
        traces: list[tuple[str, str]] = []
        controller._trace = lambda stage, message: traces.append((str(stage), str(message)))

        controller._trace_turn_action_policy()

        self.assertEqual(len(traces), 1)
        self.assertEqual(traces[0][0], "action.confirmation")
        self.assertIn("mode=automatic", traces[0][1])
        self.assertIn("require_confirmation=false", traces[0][1])

    def test_memory_assistant_text_strips_action_metadata(self) -> None:
        source = (
            "I will send the Win+M shortcut to minimize VSCode! "
            "[Action] Executed MCP tool 'mcp.windows.Shortcut'. Pressed Win+M."
        )

        cleaned = SessionController._build_memory_assistant_text(source)

        self.assertIn("I will send the Win+M shortcut", cleaned)
        self.assertNotIn("[Action]", cleaned)
        self.assertNotIn("Executed MCP tool", cleaned)

    def test_approve_pending_action_returns_message_and_none_followup(self) -> None:
        # No action executor; approve returns stub message.
        controller = SessionController.__new__(SessionController)
        controller._trace = lambda *_args, **_kwargs: None

        message, followup = controller.approve_pending_action()

        self.assertIn("No pending action", message)
        self.assertIsNone(followup)

    def test_summarize_followup_for_tts_agentic_loop(self) -> None:
        followup = (
            "Agentic continuation completed. Progress: 2/5.\n"
            "- step 1: mcp.windows.Snapshot ok\n"
            "- step 2: mcp.windows.App ok"
        )
        spoken = SessionController.summarize_followup_for_tts(followup)

        self.assertIn("Agentic continuation update", spoken)
        self.assertIn("Progress 2 of 5", spoken)
        self.assertIn("Step 1", spoken)
        self.assertIn("Step 2", spoken)

    def test_tts_text_for_followup_off_level(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._settings = SimpleNamespace(autonomy=SimpleNamespace(agentic_narration_level="off"))

        spoken = controller.tts_text_for_followup("Agentic continuation completed. Progress: 1/3.")

        self.assertEqual(spoken, "")

    def test_tts_text_for_followup_full_level_agentic(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._settings = SimpleNamespace(autonomy=SimpleNamespace(agentic_narration_level="full"))

        spoken = controller.tts_text_for_followup("Agentic continuation completed. Progress: 1/3.")

        self.assertEqual(spoken, "Agentic continuation completed.")

    def test_tts_text_for_followup_summary_level(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._settings = SimpleNamespace(autonomy=SimpleNamespace(agentic_narration_level="summary"))

        spoken = controller.tts_text_for_followup(
            "Agentic continuation completed. Progress: 2/5.\n- step 1: mcp.windows.Snapshot ok"
        )

        self.assertIn("Progress 2 of 5", spoken)

    def test_continue_session_after_approval_returns_emergency_message_when_estop_active(self) -> None:
        from app.core.tooling.runtime.emergency_stop import EmergencyStopState

        controller = SessionController.__new__(SessionController)
        traces: list[str] = []
        controller._trace = lambda _stage, message: traces.append(str(message))
        controller._action_stop_state = EmergencyStopState()
        controller._action_stop_state.trigger()
        controller._autonomy_mode = "automatic"
        controller._state = SimpleNamespace(autonomy_mode="automatic")
        controller._sync_action_confirmation_policy = lambda: None

        reply = controller._continue_session_after_approval()

        self.assertIn("Emergency stop is active", reply)
        self.assertEqual(controller._autonomy_mode, "interactive")
        self.assertEqual(controller._state.autonomy_mode, "interactive")

    def test_continue_session_after_approval_returns_empty_when_no_emergency(self) -> None:
        from app.core.tooling.runtime.emergency_stop import EmergencyStopState

        controller = SessionController.__new__(SessionController)
        controller._action_stop_state = EmergencyStopState()

        reply = controller._continue_session_after_approval()

        self.assertEqual(reply, "")

    def test_goal_update_updates_goal_and_description(self) -> None:
        controller = SessionController.__new__(SessionController)
        traces: list[str] = []
        controller._trace = lambda _stage, message: traces.append(str(message))
        controller._settings = SimpleNamespace(
            autonomy=SimpleNamespace(
                enabled=True,
                auto_goal_switch=True,
                goal_switch_min_confidence=0.5,
            )
        )
        controller._active_goal = "general_conversation"
        controller._active_goal_description = ""
        controller._state = SimpleNamespace(session_type="chat")
        controller._infer_goal = lambda **kwargs: GoalInference(
            goal="ui_automation",
            confidence=0.9,
            reason="user asked to minimize a window",
            description="Minimize the assistant window",
            session_type="chat",
        )

        controller._update_goal_from_conversation(user_text="Minimise your assistant window please.")

        self.assertEqual(controller._active_goal, "ui_automation")
        self.assertEqual(controller._active_goal_description, "Minimize the assistant window")

    def test_active_session_type_always_chat(self) -> None:
        controller = SessionController.__new__(SessionController)
        self.assertEqual(controller.active_session_type, "chat")


if __name__ == "__main__":
    unittest.main()
