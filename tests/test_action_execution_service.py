from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.planning.action_planner import ActionPlanner
from app.core.services.action_execution_service import ActionExecutionService
from app.core.tooling.runtime.action_runtime import ActionExecutionResult
from app.core.tooling.types import ToolError, ToolResult


class _NoopPlanner:
    def plan_action(self, **_kwargs):
        from app.core.tooling.runtime.action_runtime import ActionPlan

        return ActionPlan(steps=[], description="planner returned no steps")


class ActionExecutionServiceTests(unittest.TestCase):
    def test_fallback_minimize_assistant_window_plan_is_executed(self) -> None:
        captured_plan: dict | None = None

        def invoke_tool(_name: str, *, args: dict | None = None, cancel_token=None):
            _ = cancel_token
            nonlocal captured_plan
            captured_plan = dict(args or {})
            return ToolResult(
                success=True,
                data={
                    "executed": True,
                    "dry_run": True,
                    "blocked": False,
                    "requires_confirmation": False,
                    "message": "Dry-run plan (1 step(s)): window_state('minimize', hwnd=1001)",
                },
            )

        service = ActionExecutionService(
            actions_settings=SimpleNamespace(
                enabled=True,
                decision_mode="explicit_only",
                max_actions_per_turn=2,
            ),
            action_planner=_NoopPlanner(),
            capture_screen_text=lambda **_kwargs: None,
            invoke_tool=invoke_tool,
            trace=lambda *_args, **_kwargs: None,
            screen_enabled=lambda: False,
            active_goal=lambda: "ui_automation",
            last_screen_elements=lambda: [],
            all_windows=lambda: [
                {"hwnd": 1001, "title": "Assistant", "is_foreground": True},
                {"hwnd": 1002, "title": "Notepad", "is_foreground": False},
            ],
        )

        result = service.maybe_execute_action(
            user_text="Minimise your assistant window please.",
            assistant_reply="I will minimize the assistant window now.",
            screen_text=None,
            allow_planning_override=True,
            action_intent="Minimize assistant window",
        )

        self.assertIsNotNone(result)
        self.assertIsInstance(result, ActionExecutionResult)
        self.assertTrue(result.executed)
        self.assertIsNotNone(captured_plan)
        plan = (captured_plan or {}).get("plan", {})
        steps = list(plan.get("steps", [])) if isinstance(plan, dict) else []
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0].get("kind"), "window_state")
        self.assertEqual(steps[0].get("text"), "minimize")
        self.assertEqual(steps[0].get("hwnd"), 1001)


if __name__ == "__main__":
    unittest.main()
