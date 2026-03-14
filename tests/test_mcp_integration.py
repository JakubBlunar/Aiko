from __future__ import annotations

import unittest

from app.core.session_controller import SessionController
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


class _RestartClient:
    def __init__(self) -> None:
        self.start_calls = 0
        self.connected = False

    def get_runtime_status(self) -> dict:
        return {
            "connected": self.connected,
            "command": "node",
            "args": ["server.js"],
            "framing_mode": "newline-json",
            "server_name": "fake",
            "server_version": "1.0",
            "protocol_version": "2024-11-05",
            "capability_keys": [],
            "tool_count": 0,
            "tool_names": [],
        }

    def start(self) -> bool:
        self.start_calls += 1
        self.connected = True
        return True


class MCPIntegrationTests(unittest.TestCase):
    def test_parse_mcp_servers_payload(self) -> None:
        payload = {
            "mcpServers": {
                "my-server-name": {
                    "command": "node",
                    "args": ["/path/to/server.js"],
                    "env": {"KEY": "value"},
                }
            }
        }

        parsed = SessionController._parse_mcp_servers_payload(payload)

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["name"], "my-server-name")
        self.assertEqual(parsed[0]["command"], "node")
        self.assertEqual(parsed[0]["args"], ["/path/to/server.js"])
        self.assertEqual(parsed[0]["env"], {"KEY": "value"})

    def test_parse_mcp_servers_payload_skips_invalid_entries(self) -> None:
        payload = {
            "mcpServers": {
                "missing-command": {"args": ["x"]},
                "ok": {"command": "python", "args": ["-m", "srv"]},
            }
        }

        parsed = SessionController._parse_mcp_servers_payload(payload)

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["name"], "ok")

    def test_parse_mcp_servers_payload_http_transport(self) -> None:
        payload = {
            "mcpServers": {
                "remote": {
                    "transport": "http",
                    "url": "https://example.com/mcp",
                    "headers": {"Authorization": "Bearer x"},
                }
            }
        }

        parsed = SessionController._parse_mcp_servers_payload(payload)

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["name"], "remote")
        self.assertEqual(parsed[0]["transport"], "http")
        self.assertEqual(parsed[0]["url"], "https://example.com/mcp")

    def test_parse_mcp_servers_payload_with_server_specific_framing_mode(self) -> None:
        payload = {
            "mcpServers": {
                "windows": {
                    "transport": "stdio",
                    "command": "uvx",
                    "args": ["windows-mcp"],
                    "framing_mode": "newline-json",
                }
            }
        }

        parsed = SessionController._parse_mcp_servers_payload(payload)

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["name"], "windows")
        self.assertEqual(parsed[0]["framing_mode"], "newline-json")

    def test_normalize_mcp_framing_mode_fallback(self) -> None:
        self.assertEqual(
            SessionController._normalize_mcp_framing_mode("invalid-value", fallback="newline-json"),
            "newline-json",
        )
        self.assertEqual(
            SessionController._normalize_mcp_framing_mode("content-length", fallback="newline-json"),
            "content-length",
        )

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

    @unittest.skip("Agno-only: get_mcp_runtime_status is stubbed; no MCP restart logic")
    def test_controller_mcp_status_attempts_restart_when_disconnected(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._trace = lambda *_args, **_kwargs: None
        controller._tooling_config = type(
            "Cfg",
            (),
            {
                "tool_settings": staticmethod(
                    lambda _ns: {
                        "enabled": True,
                        "auto_restart": True,
                        "restart_backoff_seconds": 1.0,
                        "max_restart_attempts": 3,
                    }
                )
            },
        )()
        fake = _RestartClient()
        controller._mcp_servers = [
            {
                "id": "mcp-stdio-1",
                "name": "fake-server",
                "command": "node",
                "args": ["server.js"],
                "framing_mode": "newline-json",
                "prefix": "mcp.windows",
                "source": "config/mcp.servers.json",
                "client": fake,
                "error": "",
                "restart_attempts": 0,
                "last_restart_ts": 0.0,
            }
        ]

        status = controller.get_mcp_runtime_status()

        self.assertEqual(fake.start_calls, 1)
        self.assertEqual(status.get("connected_count"), 1)


if __name__ == "__main__":
    unittest.main()
