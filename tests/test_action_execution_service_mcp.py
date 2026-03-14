from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.services.action_execution_service import ActionExecutionService
from app.core.tooling.runtime.action_runtime import ActionExecutionResult
from app.core.tooling.types import ToolError, ToolResult


class ActionExecutionServiceMcpTests(unittest.TestCase):
    def _service(
        self,
        *,
        invoke_tool,
        list_available_tools,
        list_available_tool_schemas,
        mcp_repair_attempts: int | None = None,
    ) -> ActionExecutionService:
        actions_settings = SimpleNamespace(
            enabled=True,
            decision_mode="explicit_only",
            max_actions_per_turn=3,
        )
        if mcp_repair_attempts is not None:
            actions_settings.mcp_repair_attempts = mcp_repair_attempts

        return ActionExecutionService(
            actions_settings=actions_settings,
            execute_action_plan=lambda _plan: ActionExecutionResult(
                executed=False,
                dry_run=False,
                blocked=False,
                requires_confirmation=False,
                message="No executable GUI action steps.",
            ),
            capture_screen_text=lambda **_kwargs: None,
            invoke_tool=invoke_tool,
            trace=lambda *_args, **_kwargs: None,
            screen_enabled=lambda: False,
            active_goal=lambda: "ui_automation",
            list_available_tools=list_available_tools,
            list_available_tool_schemas=list_available_tool_schemas,
            last_screen_elements=lambda: [],
            all_windows=lambda: [],
        )

    def test_executes_mcp_tool_step_directly(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def invoke_tool(name: str, *, args: dict | None = None, cancel_token=None):
            _ = cancel_token
            calls.append((name, dict(args or {})))
            return ToolResult(success=True, data={"text": "ok"})

        service = self._service(
            invoke_tool=invoke_tool,
            list_available_tools=lambda: ["mcp.windows.App"],
            list_available_tool_schemas=lambda: {
                "mcp.windows.App": {
                    "required": ["action", "target"],
                    "properties": {"action": "str", "target": "str"},
                }
            },
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
        self.assertIn("vscode", str(calls[0][1].get("target", "")).lower())

    def test_reports_mcp_tool_failure(self) -> None:
        def invoke_tool(name: str, *, args: dict | None = None, cancel_token=None):
            _ = name
            _ = args
            _ = cancel_token
            return ToolResult(
                success=False,
                error=ToolError(code="tool_exception", message="server offline"),
            )

        service = self._service(
            invoke_tool=invoke_tool,
            list_available_tools=lambda: ["mcp.windows.App"],
            list_available_tool_schemas=lambda: {
                "mcp.windows.App": {
                    "required": ["action", "target"],
                    "properties": {"action": "str", "target": "str"},
                }
            },
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

    def test_blocks_when_required_schema_fields_are_unfilled(self) -> None:
        called = {"value": False}

        def invoke_tool(name: str, *, args: dict | None = None, cancel_token=None):
            _ = name
            _ = args
            _ = cancel_token
            called["value"] = True
            return ToolResult(success=True, data={"text": "unexpected"})

        service = self._service(
            invoke_tool=invoke_tool,
            list_available_tools=lambda: ["mcp.windows.Notification"],
            list_available_tool_schemas=lambda: {
                "mcp.windows.Notification": {
                    "required": ["title", "message", "channel"],
                    "properties": {"title": "str", "message": "str", "channel": "str"},
                }
            },
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
        self.assertIn("'channel'", result.message)
        self.assertFalse(called["value"])

    def test_repairs_invalid_mcp_enum_and_retries(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def invoke_tool(name: str, *, args: dict | None = None, cancel_token=None):
            _ = cancel_token
            calls.append((name, dict(args or {})))
            return ToolResult(success=True, data={"text": "switched"})

        service = self._service(
            invoke_tool=invoke_tool,
            list_available_tools=lambda: ["mcp.windows.App"],
            list_available_tool_schemas=lambda: {
                "mcp.windows.App": {
                    "required": ["mode"],
                    "properties": {"mode": "str"},
                    "enum_hints": {"mode": ["launch", "resize", "switch"]},
                }
            },
            mcp_repair_attempts=3,
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
        self.assertEqual(len(calls), 1)
        self.assertIn(calls[0][1].get("mode"), {"launch", "resize", "switch"})


if __name__ == "__main__":
    unittest.main()
