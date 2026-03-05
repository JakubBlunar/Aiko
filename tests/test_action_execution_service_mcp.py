from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.services.action_execution_service import ActionExecutionService
from app.core.tooling.runtime.action_runtime import ActionExecutionResult, ActionPlan, PlannedAction
from app.core.tooling.types import ToolError, ToolResult


class _PlannerMcpStep:
    def __init__(self, *, tool_name: str, tool_args: dict[str, object]) -> None:
        self._tool_name = tool_name
        self._tool_args = dict(tool_args)

    def plan_action(self, **_kwargs) -> ActionPlan:
        return ActionPlan(
            steps=[
                PlannedAction(
                    kind="mcp_tool",
                    text=self._tool_name,
                    meta=dict(self._tool_args),
                    confidence=0.9,
                    reason="Use MCP-native app action",
                )
            ],
            description="Use MCP app action",
            needs_screen=False,
        )


class _PlannerModelIntent:
    def __init__(self, *, detects_intent: bool) -> None:
        self._detects_intent = detects_intent
        self.detect_calls = 0
        self.plan_calls = 0

    def has_action_intent_with_model(self, _user_text: str) -> bool:
        self.detect_calls += 1
        return self._detects_intent

    def plan_action(self, **_kwargs) -> ActionPlan:
        self.plan_calls += 1
        return ActionPlan(steps=[], description="No-op", needs_screen=False)


class _PlannerRepairingMcp:
    def __init__(self) -> None:
        self.calls = 0

    def plan_action(self, **kwargs) -> ActionPlan:
        self.calls += 1
        feedback = str(kwargs.get("tool_error_feedback") or "").strip()
        if feedback:
            return ActionPlan(
                steps=[
                    PlannedAction(
                        kind="mcp_tool",
                        text="mcp.windows.App",
                        meta={"mode": "switch"},
                        confidence=0.85,
                        reason="Repair invalid enum value with allowed mode.",
                    )
                ],
                description="Repair MCP step",
                needs_screen=False,
            )
        return ActionPlan(
            steps=[
                PlannedAction(
                    kind="mcp_tool",
                    text="mcp.windows.App",
                    meta={"mode": "minimize"},
                    confidence=0.85,
                    reason="Initial attempt (invalid enum for this tool).",
                )
            ],
            description="Initial MCP step",
            needs_screen=False,
        )


class _PlannerRepairingMcpAfterTwoFailures:
    def __init__(self) -> None:
        self.calls = 0

    def plan_action(self, **kwargs) -> ActionPlan:
        self.calls += 1
        feedback = str(kwargs.get("tool_error_feedback") or "").strip()
        if not feedback:
            mode = "invalid_one"
        elif self.calls < 3:
            mode = "invalid_two"
        else:
            mode = "switch"
        return ActionPlan(
            steps=[
                PlannedAction(
                    kind="mcp_tool",
                    text="mcp.windows.App",
                    meta={"mode": mode},
                    confidence=0.85,
                    reason="Progressive repair attempts.",
                )
            ],
            description="Repair MCP step",
            needs_screen=False,
        )


class ActionExecutionServiceMcpTests(unittest.TestCase):
    def test_executes_mcp_tool_step_directly(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def invoke_tool(name: str, *, args: dict | None = None, cancel_token=None):
            _ = cancel_token
            calls.append((name, dict(args or {})))
            return ToolResult(success=True, data={"text": "ok"})

        service = ActionExecutionService(
            actions_settings=SimpleNamespace(
                enabled=True,
                decision_mode="explicit_only",
                max_actions_per_turn=3,
            ),
            action_planner=_PlannerMcpStep(
                tool_name="mcp.windows.App",
                tool_args={"action": "minimize", "target": "Visual Studio Code"},
            ),
            execute_action_plan=lambda _plan: None,
            capture_screen_text=lambda **_kwargs: None,
            invoke_tool=invoke_tool,
            trace=lambda *_args, **_kwargs: None,
            screen_enabled=lambda: False,
            active_goal=lambda: "ui_automation",
            list_available_tools=lambda: ["mcp.windows.App"],
            list_available_tool_schemas=lambda: {
                "mcp.windows.App": {
                    "required": ["action", "target"],
                    "properties": {"action": "str", "target": "str"},
                }
            },
            last_screen_elements=lambda: [],
            all_windows=lambda: [],
        )

        result = service.maybe_execute_action(
            user_text="Minimise VSCode please",
            assistant_reply="I will minimize VSCode.",
            screen_text=None,
            allow_planning_override=True,
            action_intent="minimize vscode",
        )

        self.assertIsNotNone(result)
        self.assertTrue(result.executed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "mcp.windows.App")
        self.assertEqual(calls[0][1].get("action"), "minimize")

    def test_reports_mcp_tool_failure(self) -> None:
        def invoke_tool(name: str, *, args: dict | None = None, cancel_token=None):
            _ = name
            _ = args
            _ = cancel_token
            return ToolResult(
                success=False,
                error=ToolError(code="tool_exception", message="server offline"),
            )

        service = ActionExecutionService(
            actions_settings=SimpleNamespace(
                enabled=True,
                decision_mode="explicit_only",
                max_actions_per_turn=3,
            ),
            action_planner=_PlannerMcpStep(
                tool_name="mcp.windows.App",
                tool_args={"action": "minimize", "target": "Visual Studio Code"},
            ),
            execute_action_plan=lambda _plan: None,
            capture_screen_text=lambda **_kwargs: None,
            invoke_tool=invoke_tool,
            trace=lambda *_args, **_kwargs: None,
            screen_enabled=lambda: False,
            active_goal=lambda: "ui_automation",
            list_available_tools=lambda: ["mcp.windows.App"],
            list_available_tool_schemas=lambda: {
                "mcp.windows.App": {
                    "required": ["action", "target"],
                    "properties": {"action": "str", "target": "str"},
                }
            },
            last_screen_elements=lambda: [],
            all_windows=lambda: [],
        )

        result = service.maybe_execute_action(
            user_text="Minimise VSCode please",
            assistant_reply="I will minimize VSCode.",
            screen_text=None,
            allow_planning_override=True,
            action_intent="minimize vscode",
        )

        self.assertIsNotNone(result)
        self.assertFalse(result.executed)
        self.assertTrue(result.blocked)
        self.assertIn("server offline", result.message)

    def test_uses_model_intent_detector_for_explicit_only_gate(self) -> None:
        planner = _PlannerModelIntent(detects_intent=True)

        service = ActionExecutionService(
            actions_settings=SimpleNamespace(
                enabled=True,
                decision_mode="explicit_only",
                max_actions_per_turn=3,
            ),
            action_planner=planner,
            execute_action_plan=lambda _plan: ActionExecutionResult(
                executed=False,
                dry_run=False,
                blocked=False,
                requires_confirmation=False,
                message="No executable steps.",
            ),
            capture_screen_text=lambda **_kwargs: None,
            invoke_tool=lambda *_args, **_kwargs: ToolResult(success=True, data={}),
            trace=lambda *_args, **_kwargs: None,
            screen_enabled=lambda: False,
            active_goal=lambda: "ui_automation",
            list_available_tools=lambda: [],
            list_available_tool_schemas=lambda: {},
            last_screen_elements=lambda: [],
            all_windows=lambda: [],
        )

        result = service.maybe_execute_action(
            user_text="Would you make Notepad active for me?",
            assistant_reply="I can do that.",
            screen_text=None,
            allow_planning_override=False,
            action_intent="",
        )

        self.assertIsNotNone(result)
        self.assertEqual(planner.detect_calls, 1)
        self.assertEqual(planner.plan_calls, 1)

    def test_blocks_notification_tool_without_required_message_arg(self) -> None:
        called = {"value": False}

        def invoke_tool(name: str, *, args: dict | None = None, cancel_token=None):
            _ = name
            _ = args
            _ = cancel_token
            called["value"] = True
            return ToolResult(success=True, data={"text": "unexpected"})

        service = ActionExecutionService(
            actions_settings=SimpleNamespace(
                enabled=True,
                decision_mode="explicit_only",
                max_actions_per_turn=3,
            ),
            action_planner=_PlannerMcpStep(
                tool_name="mcp.windows.Notification",
                tool_args={"title": "ApproveAction", "action": "click"},
            ),
            execute_action_plan=lambda _plan: ActionExecutionResult(
                executed=False,
                dry_run=False,
                blocked=False,
                requires_confirmation=False,
                message="No executable steps.",
            ),
            capture_screen_text=lambda **_kwargs: None,
            invoke_tool=invoke_tool,
            trace=lambda *_args, **_kwargs: None,
            screen_enabled=lambda: False,
            active_goal=lambda: "ui_automation",
            list_available_tools=lambda: ["mcp.windows.Notification"],
            list_available_tool_schemas=lambda: {
                "mcp.windows.Notification": {
                    "required": ["title", "message"],
                    "properties": {"title": "str", "message": "str"},
                }
            },
            last_screen_elements=lambda: [],
            all_windows=lambda: [],
        )

        result = service.maybe_execute_action(
            user_text="Approve it.",
            assistant_reply="I will approve it.",
            screen_text=None,
            allow_planning_override=True,
            action_intent="approve notification",
        )

        self.assertIsNotNone(result)
        self.assertFalse(result.executed)
        self.assertTrue(result.blocked)
        self.assertIn("Missing required argument(s)", result.message)
        self.assertIn("'message'", result.message)
        self.assertFalse(called["value"])

    def test_blocks_notification_tool_without_required_title_arg(self) -> None:
        called = {"value": False}

        def invoke_tool(name: str, *, args: dict | None = None, cancel_token=None):
            _ = name
            _ = args
            _ = cancel_token
            called["value"] = True
            return ToolResult(success=True, data={"text": "unexpected"})

        service = ActionExecutionService(
            actions_settings=SimpleNamespace(
                enabled=True,
                decision_mode="explicit_only",
                max_actions_per_turn=3,
            ),
            action_planner=_PlannerMcpStep(
                tool_name="mcp.windows.Notification",
                tool_args={"message": "Nya~ Hello!"},
            ),
            execute_action_plan=lambda _plan: ActionExecutionResult(
                executed=False,
                dry_run=False,
                blocked=False,
                requires_confirmation=False,
                message="No executable steps.",
            ),
            capture_screen_text=lambda **_kwargs: None,
            invoke_tool=invoke_tool,
            trace=lambda *_args, **_kwargs: None,
            screen_enabled=lambda: False,
            active_goal=lambda: "ui_automation",
            list_available_tools=lambda: ["mcp.windows.Notification"],
            list_available_tool_schemas=lambda: {
                "mcp.windows.Notification": {
                    "required": ["title", "message"],
                    "properties": {"title": "str", "message": "str"},
                }
            },
            last_screen_elements=lambda: [],
            all_windows=lambda: [],
        )

        result = service.maybe_execute_action(
            user_text="Notify me.",
            assistant_reply="I will notify you.",
            screen_text=None,
            allow_planning_override=True,
            action_intent="show notification",
        )

        self.assertIsNotNone(result)
        self.assertFalse(result.executed)
        self.assertTrue(result.blocked)
        self.assertIn("Missing required argument(s)", result.message)
        self.assertIn("'title'", result.message)
        self.assertFalse(called["value"])

    def test_repairs_invalid_mcp_enum_and_retries_once(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def invoke_tool(name: str, *, args: dict | None = None, cancel_token=None):
            _ = cancel_token
            calls.append((name, dict(args or {})))
            return ToolResult(success=True, data={"text": "switched"})

        planner = _PlannerRepairingMcp()
        service = ActionExecutionService(
            actions_settings=SimpleNamespace(
                enabled=True,
                decision_mode="explicit_only",
                max_actions_per_turn=3,
            ),
            action_planner=planner,
            execute_action_plan=lambda _plan: ActionExecutionResult(
                executed=False,
                dry_run=False,
                blocked=False,
                requires_confirmation=False,
                message="No executable steps.",
            ),
            capture_screen_text=lambda **_kwargs: None,
            invoke_tool=invoke_tool,
            trace=lambda *_args, **_kwargs: None,
            screen_enabled=lambda: False,
            active_goal=lambda: "ui_automation",
            list_available_tools=lambda: ["mcp.windows.App"],
            list_available_tool_schemas=lambda: {
                "mcp.windows.App": {
                    "required": ["mode"],
                    "properties": {"mode": "str"},
                    "enum_hints": {"mode": ["launch", "resize", "switch"]},
                }
            },
            last_screen_elements=lambda: [],
            all_windows=lambda: [],
        )

        result = service.maybe_execute_action(
            user_text="Minimize VSCode.",
            assistant_reply="I will minimize it.",
            screen_text=None,
            allow_planning_override=True,
            action_intent="minimize vscode",
        )

        self.assertIsNotNone(result)
        self.assertTrue(result.executed)
        self.assertGreaterEqual(planner.calls, 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "mcp.windows.App")
        self.assertEqual(calls[0][1].get("mode"), "switch")

    def test_repairs_invalid_mcp_enum_with_multiple_retries(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def invoke_tool(name: str, *, args: dict | None = None, cancel_token=None):
            _ = cancel_token
            calls.append((name, dict(args or {})))
            return ToolResult(success=True, data={"text": "switched"})

        planner = _PlannerRepairingMcpAfterTwoFailures()
        service = ActionExecutionService(
            actions_settings=SimpleNamespace(
                enabled=True,
                decision_mode="explicit_only",
                max_actions_per_turn=3,
                mcp_repair_attempts=3,
            ),
            action_planner=planner,
            execute_action_plan=lambda _plan: ActionExecutionResult(
                executed=False,
                dry_run=False,
                blocked=False,
                requires_confirmation=False,
                message="No executable steps.",
            ),
            capture_screen_text=lambda **_kwargs: None,
            invoke_tool=invoke_tool,
            trace=lambda *_args, **_kwargs: None,
            screen_enabled=lambda: False,
            active_goal=lambda: "ui_automation",
            list_available_tools=lambda: ["mcp.windows.App"],
            list_available_tool_schemas=lambda: {
                "mcp.windows.App": {
                    "required": ["mode"],
                    "properties": {"mode": "str"},
                    "enum_hints": {"mode": ["launch", "resize", "switch"]},
                }
            },
            last_screen_elements=lambda: [],
            all_windows=lambda: [],
        )

        result = service.maybe_execute_action(
            user_text="Switch VSCode.",
            assistant_reply="I will switch it.",
            screen_text=None,
            allow_planning_override=True,
            action_intent="switch vscode",
        )

        self.assertIsNotNone(result)
        self.assertTrue(result.executed)
        self.assertGreaterEqual(planner.calls, 3)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1].get("mode"), "switch")


if __name__ == "__main__":
    unittest.main()
