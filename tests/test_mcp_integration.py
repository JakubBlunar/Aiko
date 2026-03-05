from __future__ import annotations

import unittest

from app.core.tooling.mcp_tools import build_mcp_tools
from app.core.tooling.types import ToolContext


class _FakeMcpClient:
    def __init__(self, tools: list[dict], call_result: dict | None = None, raise_on_call: Exception | None = None) -> None:
        self._tools = tools
        self._call_result = call_result or {"content": [{"type": "text", "text": "ok"}], "isError": False}
        self._raise_on_call = raise_on_call

    def list_tools(self, *, refresh: bool = False) -> list[dict]:
        _ = refresh
        return [dict(item) for item in self._tools]

    def call_tool(self, *, name: str, args: dict, timeout_ms: int = 10000) -> dict:
        _ = (name, args, timeout_ms)
        if self._raise_on_call is not None:
            raise self._raise_on_call
        return dict(self._call_result)


class MCPIntegrationTests(unittest.TestCase):
    def test_build_tools_maps_schema_and_prefix(self) -> None:
        client = _FakeMcpClient(
            tools=[
                {
                    "name": "Snapshot",
                    "description": "Capture state",
                    "inputSchema": {
                        "required": ["use_vision"],
                        "properties": {
                            "use_vision": {"type": "boolean"},
                            "limit": {"type": "integer"},
                        },
                    },
                }
            ]
        )
        tools = build_mcp_tools(
            client=client,
            prefix="mcp.windows",
            timeout_ms=12000,
            mutating_tools=set(),
            allowed_tools=set(),
            blocked_tools=set(),
        )

        self.assertEqual(len(tools), 1)
        spec = tools[0].spec
        self.assertEqual(spec.name, "mcp.windows.Snapshot")
        self.assertEqual(spec.input_schema["required"], ["use_vision"])
        self.assertEqual(spec.input_schema["properties"]["use_vision"], "bool")
        self.assertEqual(spec.input_schema["properties"]["limit"], "int")

    def test_wrapper_success_returns_text_payload(self) -> None:
        client = _FakeMcpClient(
            tools=[{"name": "Snapshot", "description": "Capture", "inputSchema": {}}],
            call_result={
                "isError": False,
                "content": [{"type": "text", "text": "snapshot ready"}],
            },
        )
        tool = build_mcp_tools(
            client=client,
            prefix="mcp.windows",
            timeout_ms=12000,
            mutating_tools=set(),
            allowed_tools=set(),
            blocked_tools=set(),
        )[0]

        result = tool.run(ToolContext(), {"any": "value"})
        self.assertTrue(result.success)
        self.assertIn("snapshot ready", result.data.get("text", ""))

    def test_wrapper_timeout_is_reported(self) -> None:
        client = _FakeMcpClient(
            tools=[{"name": "Snapshot", "description": "Capture", "inputSchema": {}}],
            raise_on_call=TimeoutError("timed out"),
        )
        tool = build_mcp_tools(
            client=client,
            prefix="mcp.windows",
            timeout_ms=12000,
            mutating_tools=set(),
            allowed_tools=set(),
            blocked_tools=set(),
        )[0]

        result = tool.run(ToolContext(), {})
        self.assertFalse(result.success)
        self.assertEqual(result.error.code, "mcp_timeout")

    def test_allow_block_and_mutating_filters(self) -> None:
        client = _FakeMcpClient(
            tools=[
                {"name": "Snapshot", "description": "Capture", "inputSchema": {}},
                {"name": "Shell", "description": "Shell", "inputSchema": {}},
            ]
        )
        tools = build_mcp_tools(
            client=client,
            prefix="mcp.windows",
            timeout_ms=12000,
            mutating_tools={"Shell"},
            allowed_tools={"Snapshot", "Shell"},
            blocked_tools={"Shell"},
        )

        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].spec.name, "mcp.windows.Snapshot")
        self.assertFalse(tools[0].spec.is_mutating)


if __name__ == "__main__":
    unittest.main()
