"""Tests for :class:`ExternalMcpManager`.

The real ``mcp`` SDK launches child processes; these tests patch the two
import points the manager uses inside ``_connect_once``
(``mcp.client.stdio.stdio_client`` and ``mcp.ClientSession``) with async
fakes, so the full start → list_tools → call_tool → stop path runs in-loop
without any subprocess.
"""
from __future__ import annotations

import contextlib
import logging
import time
import unittest
from unittest import mock

import mcp
import mcp.client.stdio as mcp_stdio

from app.core.infra.settings import ExternalMcpServer
from app.mcp.client.manager import (
    STATUS_CONNECTED,
    STATUS_DISABLED,
    ExternalMcpManager,
    McpToolError,
    _StderrPump,
    resolve_env,
)


# ── fakes ────────────────────────────────────────────────────────────


class _FakeTool:
    def __init__(self, name, description, input_schema) -> None:
        self.name = name
        self.description = description
        self.inputSchema = input_schema


class _FakeListResult:
    def __init__(self, tools) -> None:
        self.tools = tools


class _FakeTextBlock:
    def __init__(self, text) -> None:
        self.text = text


class _FakeCallResult:
    def __init__(self, content, is_error=False) -> None:
        self.content = content
        self.isError = is_error


class _FakeSession:
    def __init__(self, read, write) -> None:
        self.read = read
        self.write = write
        self.calls: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def initialize(self):
        return None

    async def list_tools(self):
        return _FakeListResult(
            [
                _FakeTool(
                    "read_text_file",
                    "Read a file.",
                    {"type": "object", "properties": {"path": {"type": "string"}}},
                ),
                _FakeTool("write_file", "Write a file.", {}),
            ]
        )

    async def call_tool(self, name, arguments=None, read_timeout_seconds=None):
        self.calls.append((name, arguments))
        return _FakeCallResult([_FakeTextBlock(f"called {name}")])


@contextlib.asynccontextmanager
async def _fake_stdio_client(server, errlog=None):
    if errlog is not None:
        errlog.write("fake server booting\n")
    yield ("READ", "WRITE")


def _patch_mcp():
    """Patch the two SDK symbols the manager imports at connect time."""
    return (
        mock.patch.object(mcp_stdio, "stdio_client", _fake_stdio_client),
        mock.patch.object(mcp, "ClientSession", _FakeSession),
    )


def _stdio_server(server_id="filesystem", expose=()):
    return ExternalMcpServer(
        id=server_id,
        name=server_id,
        transport="stdio",
        command="fake",
        args=("--root", "/tmp"),
        expose_tools=tuple(expose),
    )


# ── pure-unit tests (no loop) ──────────────────────────────────────────


class ResolveEnvTests(unittest.TestCase):
    def test_literal_and_indirection(self) -> None:
        with mock.patch.dict("os.environ", {"MY_TOK": "secret"}, clear=False):
            out = resolve_env(
                {"A": "literal", "B": "${ENV:MY_TOK}", "C": "${ENV:MISSING}"}
            )
        self.assertEqual(out, {"A": "literal", "B": "secret", "C": ""})


class StderrPumpTests(unittest.TestCase):
    def test_forwards_child_stderr_lines_to_logger(self) -> None:
        logger = logging.getLogger("test.mcp.stderrpump")
        with self.assertLogs(logger, level="DEBUG") as cap:
            pump = _StderrPump(logger)
            # The SDK needs a real fileno on the write end.
            self.assertIsInstance(pump.writer.fileno(), int)
            pump.writer.write("server booting\n")
            pump.writer.write("listening on stdio\n")
            pump.writer.flush()
            time.sleep(0.1)
            pump.close()  # joins the reader thread
        joined = "\n".join(cap.output)
        self.assertIn("server booting", joined)
        self.assertIn("listening on stdio", joined)


# ── integration tests (with the fake loop) ─────────────────────────────


class ManagerLifecycleTests(unittest.TestCase):
    def _wait_connected(self, manager, server_id="filesystem", timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for s in manager.server_status():
                if s["id"] == server_id and s["status"] == STATUS_CONNECTED:
                    return True
            time.sleep(0.02)
        return False

    def test_connect_list_call_stop(self) -> None:
        p1, p2 = _patch_mcp()
        with p1, p2:
            mgr = ExternalMcpManager([_stdio_server()])
            fired = {"count": 0}
            mgr.set_tools_changed_callback(
                lambda: fired.__setitem__("count", fired["count"] + 1)
            )
            mgr.start()
            try:
                self.assertTrue(self._wait_connected(mgr))
                tools = mgr.list_available_tools()
                names = sorted(t.name for t in tools)
                self.assertEqual(names, ["read_text_file", "write_file"])
                self.assertEqual(
                    tools[0].qualified_name.split("__")[0], "filesystem"
                )
                self.assertGreaterEqual(fired["count"], 1)
                # Thread-safe call from the test (main) thread.
                result = mgr.call_tool(
                    "filesystem", "read_text_file", {"path": "x"}
                )
                self.assertFalse(result.isError)
                self.assertEqual(result.content[0].text, "called read_text_file")
            finally:
                mgr.stop()

    def test_expose_tools_allow_list(self) -> None:
        p1, p2 = _patch_mcp()
        with p1, p2:
            mgr = ExternalMcpManager([_stdio_server(expose=["read_text_file"])])
            mgr.start()
            try:
                self.assertTrue(self._wait_connected(mgr))
                names = [t.name for t in mgr.list_available_tools()]
                self.assertEqual(names, ["read_text_file"])
            finally:
                mgr.stop()

    def test_disabled_server_not_connected(self) -> None:
        server = _stdio_server()
        object.__setattr__(server, "enabled", False)
        mgr = ExternalMcpManager([server])
        mgr.start()
        try:
            statuses = {s["id"]: s["status"] for s in mgr.server_status()}
            self.assertEqual(statuses["filesystem"], STATUS_DISABLED)
            self.assertEqual(mgr.list_available_tools(), [])
        finally:
            mgr.stop()

    def test_call_tool_unknown_server_raises(self) -> None:
        mgr = ExternalMcpManager([])
        mgr.start()
        try:
            with self.assertRaises(McpToolError):
                mgr.call_tool("nope", "tool", {})
        finally:
            mgr.stop()

    def test_call_tool_not_connected_raises(self) -> None:
        # No patching -> the real stdio_client tries to launch "fake" and
        # fails, so the server never reaches connected; call_tool raises.
        mgr = ExternalMcpManager([_stdio_server()])
        mgr.start()
        try:
            with self.assertRaises(McpToolError):
                mgr.call_tool("filesystem", "read_text_file", {})
        finally:
            mgr.stop()


if __name__ == "__main__":
    unittest.main()
