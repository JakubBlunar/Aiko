from __future__ import annotations

import unittest

from app.core.sessions.agentic_session import AgenticSessionConfig, AgenticSessionManager
from app.core.sessions.agentic_session_adapter import AgenticSessionAdapter
from app.core.sessions.session_types import SessionRuntimeContext


class AgenticSessionFlowTests(unittest.TestCase):
    def test_agentic_intent_activates_manager(self) -> None:
        manager = AgenticSessionManager(AgenticSessionConfig(enabled=True, max_auto_steps=3))
        traces: list[str] = []

        manager.update(
            user_text="Please go fully automatic and proceed.",
            screen_text="",
            trace=lambda _stage, msg: traces.append(msg),
        )

        self.assertTrue(manager.active)
        self.assertTrue(any("objective=" in msg for msg in traces))

    def test_adapter_continue_after_approval_fallback_advances_step(self) -> None:
        manager = AgenticSessionManager(AgenticSessionConfig(enabled=True, max_auto_steps=2))
        manager.activate(objective="Automate this task", trace=lambda *_args, **_kwargs: None)
        adapter = AgenticSessionAdapter(manager)

        runtime = SessionRuntimeContext(
            actions_enabled=True,
            screen_enabled=True,
            foreground_window_title="Visual Studio Code",
            get_require_confirmation=lambda: True,
            set_require_confirmation=lambda _value: None,
            invoke_tool=lambda *_args, **_kwargs: None,
            capture_screen_text=lambda **_kwargs: None,
            trace=lambda *_args, **_kwargs: None,
        )

        result = adapter.continue_after_approval(runtime)

        self.assertIn("fallback step", result)
        self.assertEqual(manager.auto_steps, 1)

    def test_adapter_multi_step_planner_chain(self) -> None:
        manager = AgenticSessionManager(AgenticSessionConfig(enabled=True, max_auto_steps=3))
        manager.activate(objective="Open and inspect app state", trace=lambda *_args, **_kwargs: None)
        adapter = AgenticSessionAdapter(manager)

        planned = [
            {
                "done": False,
                "progress_note": "Capture a fresh snapshot.",
                "next_tool": "mcp.windows.Snapshot",
                "next_args": {"use_vision": False},
            },
            {
                "done": False,
                "progress_note": "Open settings app.",
                "next_tool": "mcp.windows.App",
                "next_args": {"name": "settings"},
            },
            {
                "done": True,
                "progress_note": "Goal appears complete.",
                "next_tool": "",
                "next_args": {},
            },
        ]
        plan_calls = {"index": 0}
        invoked_tools: list[str] = []
        stage_log: list[str] = []
        narration_log: list[str] = []
        confirmation_state = {"value": True}

        def planner(_objective: str, _screen_text: str | None, _recent: list[dict], _remaining: int) -> dict:
            idx = plan_calls["index"]
            plan_calls["index"] += 1
            return dict(planned[min(idx, len(planned) - 1)])

        def invoke_tool(name: str, *, args: dict | None = None, cancel_token=None):
            _ = (args, cancel_token)
            invoked_tools.append(name)
            return type(
                "Result",
                (),
                {
                    "success": True,
                    "requires_confirmation": False,
                    "data": {"ok": True},
                    "error": None,
                },
            )()

        runtime = SessionRuntimeContext(
            actions_enabled=True,
            screen_enabled=True,
            foreground_window_title="Visual Studio Code",
            get_require_confirmation=lambda: confirmation_state["value"],
            set_require_confirmation=lambda value: confirmation_state.__setitem__("value", bool(value)),
            invoke_tool=invoke_tool,
            capture_screen_text=lambda **_kwargs: "screen content",
            trace=lambda stage, _msg: stage_log.append(str(stage)),
            active_goal="ui_automation",
            plan_agentic_step=planner,
            narrate=lambda text: narration_log.append(str(text)),
        )

        result = adapter.continue_after_approval(runtime)

        self.assertIn("Agentic continuation completed", result)
        self.assertEqual(invoked_tools, ["mcp.windows.Snapshot", "mcp.windows.App"])
        self.assertEqual(manager.auto_steps, 2)
        self.assertTrue(confirmation_state["value"])
        self.assertIn("agentic.loop.plan", stage_log)
        self.assertIn("agentic.loop.invoke", stage_log)
        self.assertIn("agentic.loop.result", stage_log)
        self.assertIn("agentic.loop.done", stage_log)
        self.assertTrue(any("Agentic loop started" in line for line in narration_log))
        self.assertTrue(any("Invoking tool mcp.windows.Snapshot" in line for line in narration_log))
        self.assertTrue(any("Result for mcp.windows.App: success" in line for line in narration_log))


if __name__ == "__main__":
    unittest.main()
