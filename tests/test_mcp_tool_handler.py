"""Tests for :class:`McpToolHandler` (the generic external-MCP proxy)."""
from __future__ import annotations

import unittest

from app.core.tasks.handlers.mcp_tool import McpToolHandler, _flatten_content
from app.core.tasks.task_handler import TaskCompleted, TaskFailed


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _ImageBlock:
    # No ``.text`` attribute -> counts as non-text content.
    def __init__(self) -> None:
        self.data = "base64..."


class _FakeResult:
    def __init__(self, content, is_error: bool = False) -> None:
        self.content = content
        self.isError = is_error


class _FakeManager:
    """Captures the call + returns a canned result (or raises)."""

    def __init__(self, result=None, raises: Exception | None = None) -> None:
        self._result = result
        self._raises = raises
        self.calls: list[tuple] = []

    def call_tool(self, server_id, tool_name, tool_args, *, timeout=None):
        self.calls.append((server_id, tool_name, tool_args))
        if self._raises is not None:
            raise self._raises
        return self._result


def _run(handler, args):
    outcomes: list = []
    state = handler.start(args, outcomes.append)
    return outcomes, state


class FlattenContentTests(unittest.TestCase):
    def test_joins_text_blocks_and_counts_non_text(self) -> None:
        result = _FakeResult([_TextBlock("hello"), _ImageBlock(), _TextBlock("world")])
        text, non_text = _flatten_content(result)
        self.assertEqual(text, "hello\nworld")
        self.assertEqual(non_text, 1)

    def test_empty_content(self) -> None:
        text, non_text = _flatten_content(_FakeResult([]))
        self.assertEqual(text, "")
        self.assertEqual(non_text, 0)


class McpToolHandlerTests(unittest.TestCase):
    def test_success_emits_completed_with_content_and_summary(self) -> None:
        mgr = _FakeManager(result=_FakeResult([_TextBlock("file contents here")]))
        handler = McpToolHandler(manager=mgr)
        outcomes, state = _run(
            handler,
            {"server_id": "fs", "tool_name": "read_text_file", "tool_args": {"path": "a.txt"}},
        )
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], TaskCompleted)
        result = outcomes[0].result
        self.assertEqual(result["server_id"], "fs")
        self.assertEqual(result["tool_name"], "read_text_file")
        self.assertEqual(result["content"], "file contents here")
        self.assertIn("read_text_file", result["summary"])
        self.assertEqual(state["phase"], "done")
        # The manager was called with the forwarded args.
        self.assertEqual(mgr.calls, [("fs", "read_text_file", {"path": "a.txt"})])

    def test_is_error_emits_failed(self) -> None:
        mgr = _FakeManager(
            result=_FakeResult([_TextBlock("permission denied")], is_error=True)
        )
        handler = McpToolHandler(manager=mgr)
        outcomes, _ = _run(
            handler, {"server_id": "fs", "tool_name": "read_text_file"}
        )
        self.assertIsInstance(outcomes[0], TaskFailed)
        self.assertIn("permission denied", outcomes[0].error)

    def test_call_raises_emits_failed(self) -> None:
        mgr = _FakeManager(raises=RuntimeError("not connected"))
        handler = McpToolHandler(manager=mgr)
        outcomes, _ = _run(
            handler, {"server_id": "fs", "tool_name": "read_text_file"}
        )
        self.assertIsInstance(outcomes[0], TaskFailed)
        self.assertIn("not connected", outcomes[0].error)

    def test_missing_server_or_tool_rejected(self) -> None:
        handler = McpToolHandler(manager=_FakeManager())
        outcomes, state = _run(handler, {"tool_name": "x"})
        self.assertIsInstance(outcomes[0], TaskFailed)
        self.assertEqual(state["phase"], "rejected")

        outcomes2, _ = _run(handler, {"server_id": "fs"})
        self.assertIsInstance(outcomes2[0], TaskFailed)

    def test_non_dict_tool_args_rejected(self) -> None:
        handler = McpToolHandler(manager=_FakeManager())
        outcomes, _ = _run(
            handler,
            {"server_id": "fs", "tool_name": "x", "tool_args": "not-a-dict"},
        )
        self.assertIsInstance(outcomes[0], TaskFailed)

    def test_non_text_only_result_summary(self) -> None:
        mgr = _FakeManager(result=_FakeResult([_ImageBlock(), _ImageBlock()]))
        handler = McpToolHandler(manager=mgr)
        outcomes, _ = _run(
            handler, {"server_id": "fs", "tool_name": "screenshot"}
        )
        self.assertIsInstance(outcomes[0], TaskCompleted)
        self.assertIn("non-text", outcomes[0].result["summary"])

    def test_resume_and_on_input_are_terminal(self) -> None:
        handler = McpToolHandler(manager=_FakeManager())
        out1: list = []
        handler.resume({}, out1.append)
        self.assertIsInstance(out1[0], TaskFailed)
        out2: list = []
        handler.on_input({}, "answer", out2.append)
        self.assertIsInstance(out2[0], TaskFailed)


if __name__ == "__main__":
    unittest.main()
