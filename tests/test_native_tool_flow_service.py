from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.services import native_tool_flow_service
from app.core.tooling.types import ToolError, ToolResult, ToolSpec


class NativeToolFlowServiceTests(unittest.TestCase):
    def test_build_ollama_tool_definitions_filters_prefix(self) -> None:
        specs = [
            ToolSpec(name="mcp.add", description="Add", input_schema={"properties": {"a": "int"}}),
            ToolSpec(name="history.read", description="Read", input_schema={}),
        ]
        tools = native_tool_flow_service.build_ollama_tool_definitions(
            specs,
            allowed_prefixes=("mcp.",),
        )
        self.assertEqual(len(tools), 1)
        function = tools[0]["function"]
        self.assertEqual(function["name"], "mcp.add")
        self.assertIn("parameters", function)

    def test_build_pre_execution_summary_uses_preview(self) -> None:
        calls = [
            SimpleNamespace(name="mcp.add", arguments={"a": 1, "b": 2}),
            SimpleNamespace(name="mcp.greet", arguments={"name": "John"}),
        ]
        summary = native_tool_flow_service.build_pre_execution_summary(
            calls,
            preview_tool_args=lambda args: str(args),
        )
        self.assertIn("I will run these tools now:", summary)
        self.assertIn("mcp.add", summary)
        self.assertIn("mcp.greet", summary)

    def test_tool_result_to_message_content_success_and_error(self) -> None:
        success = ToolResult(success=True, data={"text": "Hello"})
        self.assertEqual(native_tool_flow_service.tool_result_to_message_content("mcp.greet", success), "Hello")

        failure = ToolResult(
            success=False,
            error=ToolError(code="failed", message="boom"),
        )
        text = native_tool_flow_service.tool_result_to_message_content("mcp.greet", failure)
        self.assertIn("Tool 'mcp.greet' failed: boom", text)


if __name__ == "__main__":
    unittest.main()
