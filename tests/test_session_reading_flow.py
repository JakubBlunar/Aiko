from __future__ import annotations

import types
import unittest
from types import SimpleNamespace

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
        controller._reading_session_memory_enabled = True
        controller._reading_active = True
        controller._reading_max_scroll_steps = 5
        controller._reading_scroll_steps = 0
        controller._reading_chunk_hashes = set()
        controller._foreground_window_title = "Visual Studio Code"
        controller._settings = SimpleNamespace(
            actions=SimpleNamespace(enabled=True, require_confirmation=True)
        )
        controller._state = SimpleNamespace(screen_enabled=True)

        traces: list[str] = []
        controller._trace = lambda _stage, message: traces.append(str(message))
        controller._is_trusted_reading_window = lambda: True

        # Keep hash-set unchanged so duplicate streak triggers stop after 2 loops.
        controller._capture_screen_text = lambda decision_source: "example article chunk"
        controller._update_reading_session = lambda **_kwargs: None
        controller._build_reading_evidence_block = lambda: 'Reading evidence:\n- "Example quote"'

        def invoke_tool(_name: str, *, args: dict | None = None, cancel_token=None):
            _ = args
            _ = cancel_token
            return ToolResult(success=True, data={"executed": True})

        controller._invoke_tool = invoke_tool

        followup = controller._continue_reading_after_approval()

        self.assertIn("continued reading automatically for 2 scroll step(s)", followup)
        self.assertIn("Reading evidence", followup)
        self.assertTrue(controller._settings.actions.require_confirmation)
        self.assertIn("step=1", "\n".join(traces))
        self.assertIn("step=2", "\n".join(traces))

    def test_stop_reading_session_clears_state(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._reading_active = True
        controller._reading_window_title = "Visual Studio Code"
        controller._reading_chunks = ["chunk one", "chunk two"]
        controller._reading_chunk_hashes = {"a", "b"}
        controller._reading_scroll_steps = 3
        controller._reading_last_summary = "Summary"
        controller._trace = lambda *_args, **_kwargs: None

        was_active = controller.stop_reading_session()

        self.assertTrue(was_active)
        self.assertFalse(controller._reading_active)
        self.assertEqual(controller._reading_window_title, "")
        self.assertEqual(controller._reading_chunks, [])
        self.assertEqual(controller._reading_chunk_hashes, set())
        self.assertEqual(controller._reading_scroll_steps, 0)
        self.assertEqual(controller._reading_last_summary, "")


if __name__ == "__main__":
    unittest.main()
