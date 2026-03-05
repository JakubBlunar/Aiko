from __future__ import annotations

import types
import unittest
from types import SimpleNamespace

from app.core.sessions.reading_session_adapter import ReadingSessionAdapter
from app.core.sessions.reading_session import ReadingSessionConfig, ReadingSessionManager
from app.core.session_controller import SessionController
from app.core.planning.autonomy_planner import GoalInference
from app.core.tooling.runtime.action_runtime import ActionExecutionResult
from app.core.tooling.types import ToolResult


class SessionReadingFlowTests(unittest.TestCase):
    def test_action_gate_blocks_non_action_text_in_chat_session(self) -> None:
        self.assertFalse(
            SessionController._should_allow_action_execution(
                session_type="chat",
                user_text="But you can summarize what you remember until now.",
            )
        )

    def test_action_gate_allows_explicit_action_text_in_chat_session(self) -> None:
        self.assertTrue(
            SessionController._should_allow_action_execution(
                session_type="chat",
                user_text="Click the Save button and type hello in the input field.",
            )
        )

    def test_action_gate_allows_british_minimise_spelling(self) -> None:
        self.assertTrue(
            SessionController._should_allow_action_execution(
                session_type="chat",
                user_text="Minimise your assistant window please.",
            )
        )

    def test_action_gate_allows_polite_window_switch_request(self) -> None:
        self.assertTrue(
            SessionController._should_allow_action_execution(
                session_type="chat",
                user_text="Could you switch to the VSCode window please?",
            )
        )

    def test_action_gate_allows_make_window_active_request(self) -> None:
        self.assertTrue(
            SessionController._should_allow_action_execution(
                session_type="chat",
                user_text="Please make the Notepad window active.",
            )
        )

    def test_action_gate_allows_non_chat_sessions(self) -> None:
        self.assertTrue(
            SessionController._should_allow_action_execution(
                session_type="reading",
                user_text="continue reading",
            )
        )

    def test_action_gate_blocks_coding_help_goal_in_chat(self) -> None:
        self.assertFalse(
            SessionController._should_allow_action_execution(
                session_type="chat",
                user_text="Click the minimize button.",
                active_goal="coding_help",
            )
        )

    def test_action_gate_blocks_agentic_switch_turn(self) -> None:
        self.assertFalse(
            SessionController._should_allow_action_execution(
                session_type="chat",
                user_text="Please enter agentic mode now.",
                active_goal="general_conversation",
            )
        )

    def test_action_gate_blocks_agentic_switch_turn_even_in_agentic_session(self) -> None:
        self.assertFalse(
            SessionController._should_allow_action_execution(
                session_type="agentic",
                user_text="Switch to agentic mode again.",
                active_goal="ui_automation",
            )
        )

    def test_autonomy_plan_agentic_switch_turn_never_requests_action(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._settings = SimpleNamespace(
            autonomy=SimpleNamespace(enabled=True),
        )
        controller._autonomy_mode = "interactive"

        plan = controller._plan_turn_autonomy(user_text="Switch to agentic mode.")

        self.assertFalse(plan.should_plan_action)
        self.assertEqual(plan.action_intent, "")

    def test_orchestration_plan_agentic_switch_turn_never_requests_action(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._turn_orchestrator = SimpleNamespace(plan_turn=lambda **_kwargs: None)

        plan = controller._plan_turn_orchestration(
            user_text="Switch to agentic mode.",
            autonomy_plan=SimpleNamespace(
                strategy="irrelevant",
                should_use_screen=False,
                should_plan_action=False,
                ask_followup=True,
                confidence=1.0,
                action_intent="",
            ),
            screen_intent=False,
            reading_intent=False,
            continue_reading=False,
        )

        self.assertFalse(plan.should_plan_action)
        self.assertEqual(plan.action_intent, "")
        self.assertEqual(plan.requested_operations, tuple())

    def test_autonomy_mode_interactive_requires_confirmation(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._trace = lambda *_args, **_kwargs: None
        controller._settings = SimpleNamespace(actions=SimpleNamespace(require_confirmation=False))
        controller._state = SimpleNamespace(autonomy_mode="automatic")
        controller._active_session_type = "agentic"
        controller._set_active_session = lambda _session_type: None
        controller._autonomy_mode = "automatic"

        controller.set_autonomy_mode("interactive")

        self.assertTrue(controller._settings.actions.require_confirmation)

    def test_autonomy_mode_automatic_disables_confirmation(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._trace = lambda *_args, **_kwargs: None
        controller._settings = SimpleNamespace(actions=SimpleNamespace(require_confirmation=True))
        controller._state = SimpleNamespace(autonomy_mode="interactive")
        controller._active_session_type = "chat"
        controller._set_active_session = lambda _session_type: None
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

    def test_tts_action_failure_note_for_blocked_failed_action(self) -> None:
        note = SessionController._build_tts_action_failure_note(
            ActionExecutionResult(
                executed=False,
                dry_run=False,
                blocked=True,
                requires_confirmation=False,
                message="MCP tool failed after retries.",
            )
        )

        self.assertIn("I could not complete that action.", note)
        self.assertIn("failed after retries", note)

    def test_approve_pending_action_combines_followups(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._settings = SimpleNamespace(actions=SimpleNamespace(require_confirmation=True))
        controller._trace = lambda *_args, **_kwargs: None
        controller._action_executor = SimpleNamespace(
            approve_pending_action=lambda: ActionExecutionResult(
                executed=True,
                dry_run=False,
                blocked=False,
                requires_confirmation=False,
                message="Step 1 (click): Executed click at (100, 100).",
            )
        )
        controller._build_post_action_followup = lambda *_args, **_kwargs: "Done."
        controller._continue_reading_after_approval = lambda: "I continued reading automatically for 2 scroll step(s)."

        message, followup = controller.approve_pending_action()

        self.assertIn("Executed click", message)
        self.assertIsNotNone(followup)
        self.assertIn("Done.", str(followup))
        self.assertIn("continued reading automatically", str(followup))

    def test_continue_reading_after_approval_restores_confirmation(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._reading_session = ReadingSessionManager(
            ReadingSessionConfig(
                memory_enabled=True,
                max_scroll_steps=5,
                max_quotes=3,
                max_quote_chars=300,
                trusted_window_titles=[],
            )
        )
        controller._reading_session._active = True
        controller._active_session = ReadingSessionAdapter(controller._reading_session)
        controller._session_handlers = {
            "chat": SimpleNamespace(stop=lambda *_args, **_kwargs: False, get_status=lambda: {}),
            "reading": controller._active_session,
        }
        controller._foreground_window_title = "Visual Studio Code"
        controller._settings = SimpleNamespace(
            actions=SimpleNamespace(enabled=True, require_confirmation=True)
        )
        controller._state = SimpleNamespace(screen_enabled=True)

        traces: list[str] = []
        controller._trace = lambda _stage, message: traces.append(str(message))
        # Keep hash-set unchanged so duplicate streak triggers stop after 2 loops.
        controller._capture_screen_text = lambda decision_source: "example article chunk"
        controller._active_session.build_evidence_block = lambda _trace: 'Reading evidence:\n- "Example quote"'
        controller._action_executor = SimpleNamespace(
            execute_plan=lambda _plan: ActionExecutionResult(
                executed=True,
                dry_run=False,
                blocked=False,
                requires_confirmation=False,
                message="Step 1 (scroll): Executed scroll down.",
            )
        )

        def invoke_tool(_name: str, *, args: dict | None = None, cancel_token=None):
            _ = args
            _ = cancel_token
            return ToolResult(success=True, data={"executed": True})

        controller._invoke_tool = invoke_tool

        followup = controller._continue_reading_after_approval()

        self.assertIn("continued reading automatically", followup)
        self.assertIn("Reading evidence", followup)
        self.assertTrue(controller._settings.actions.require_confirmation)
        self.assertIn("step=1", "\n".join(traces))
        self.assertIn("step=2", "\n".join(traces))

    def test_reading_handler_stop_clears_state(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._reading_session = ReadingSessionManager(
            ReadingSessionConfig(
                memory_enabled=True,
                max_scroll_steps=5,
                max_quotes=3,
                max_quote_chars=300,
                trusted_window_titles=[],
            )
        )
        controller._reading_session._active = True
        controller._reading_session._window_title = "Visual Studio Code"
        controller._reading_session._chunks = ["chunk one", "chunk two"]
        controller._reading_session._chunk_hashes = {"a", "b"}
        controller._reading_session._scroll_steps = 3
        controller._reading_session._last_summary = "Summary"
        controller._session_handlers = {
            "reading": ReadingSessionAdapter(controller._reading_session),
        }
        controller._trace = lambda *_args, **_kwargs: None

        was_active = controller._session_handlers["reading"].stop(controller._trace)

        self.assertTrue(was_active)
        self.assertFalse(controller._reading_session._active)
        self.assertEqual(controller._reading_session._window_title, "")
        self.assertEqual(controller._reading_session._chunks, [])
        self.assertEqual(controller._reading_session._chunk_hashes, set())
        self.assertEqual(controller._reading_session._scroll_steps, 0)
        self.assertEqual(controller._reading_session._last_summary, "")

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

    def test_continue_after_approval_stops_agentic_when_estop_active(self) -> None:
        controller = SessionController.__new__(SessionController)
        traces: list[str] = []

        controller._trace = lambda _stage, message: traces.append(str(message))
        controller._action_executor = SimpleNamespace(emergency_stopped=True)
        controller._autonomy_mode = "automatic"
        controller._state = SimpleNamespace(autonomy_mode="automatic", session_type="agentic")
        controller._active_session_type = "agentic"
        chat_handler = SimpleNamespace(get_status=lambda: {})
        agentic_handler = SimpleNamespace(stop=lambda _trace: True)
        controller._session_handlers = {
            "chat": chat_handler,
            "agentic": agentic_handler,
        }
        controller._active_session = agentic_handler

        reply = controller._continue_session_after_approval()

        self.assertIn("Emergency stop is active", reply)
        self.assertEqual(controller._active_session_type, "chat")
        self.assertEqual(controller._autonomy_mode, "interactive")
        self.assertEqual(controller._state.autonomy_mode, "interactive")
        self.assertTrue(any("agentic session halted" in line for line in traces))

    def test_goal_update_does_not_auto_switch_into_agentic_without_explicit_intent(self) -> None:
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
        controller._active_session_type = "chat"
        controller._session_router = SimpleNamespace(resolve=lambda **_kwargs: ("agentic", "model"))
        controller._set_active_session = lambda session_type: setattr(controller, "_active_session_type", session_type)
        controller._infer_goal = lambda **_kwargs: GoalInference(
            goal="ui_automation",
            confidence=0.9,
            reason="user asked to minimize a window",
            description="Minimize the assistant window",
            session_type="agentic",
        )

        controller._update_goal_from_conversation(user_text="Minimise your assistant window please.")

        self.assertEqual(controller._active_session_type, "chat")
        self.assertTrue(any("guarded_no_explicit_agentic" in line for line in traces))


if __name__ == "__main__":
    unittest.main()
