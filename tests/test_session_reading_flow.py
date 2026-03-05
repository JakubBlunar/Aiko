from __future__ import annotations

import types
import unittest
from types import SimpleNamespace

from app.core.sessions.reading_session_adapter import ReadingSessionAdapter
from app.core.sessions.reading_session import ReadingSessionConfig, ReadingSessionManager
from app.core.session_controller import SessionController
from app.core.tooling.runtime.action_runtime import ActionExecutionResult
from app.core.tooling.types import ToolResult


class SessionReadingFlowTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
