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


class _PerceptionResult:
    def __init__(self, content: str, summary: str, element_count: int) -> None:
        self.content = content
        self.summary = summary
        self.element_count = element_count


class _StubPerception:
    """Claims one (server, tool) and optionally returns a result/None."""

    def __init__(self, *, result, server="browser", tool="browser_snapshot") -> None:
        self._result = result
        self._server = server
        self._tool = tool
        self.transform_calls: list[tuple] = []

    def claims(self, server_id: str, tool_name: str) -> bool:
        return server_id == self._server and tool_name == self._tool

    def transform(self, server_id, tool_name, text, tool_args):
        self.transform_calls.append((server_id, tool_name, text, tool_args))
        return self._result


class PerceptionHookTests(unittest.TestCase):
    def test_claimed_snapshot_uses_perceived_render(self) -> None:
        mgr = _FakeManager(result=_FakeResult([_TextBlock("- button \"X\" [ref=e1]")]))
        perception = _StubPerception(
            result=_PerceptionResult("RANKED RENDER", "page: 1 interactive", 1)
        )
        handler = McpToolHandler(manager=mgr, perception=perception)
        outcomes, state = _run(
            handler,
            {"server_id": "browser", "tool_name": "browser_snapshot", "tool_args": {}},
        )
        self.assertIsInstance(outcomes[0], TaskCompleted)
        self.assertEqual(outcomes[0].result["content"], "RANKED RENDER")
        self.assertEqual(outcomes[0].result["summary"], "page: 1 interactive")
        self.assertEqual(state["phase"], "done")
        self.assertEqual(len(perception.transform_calls), 1)

    def test_perception_none_falls_back_to_raw(self) -> None:
        mgr = _FakeManager(result=_FakeResult([_TextBlock("raw tree text")]))
        perception = _StubPerception(result=None)
        handler = McpToolHandler(manager=mgr, perception=perception)
        outcomes, _ = _run(
            handler,
            {"server_id": "browser", "tool_name": "browser_snapshot", "tool_args": {}},
        )
        self.assertIsInstance(outcomes[0], TaskCompleted)
        self.assertEqual(outcomes[0].result["content"], "raw tree text")

    def test_non_claimed_tool_passes_through(self) -> None:
        mgr = _FakeManager(result=_FakeResult([_TextBlock("click ok")]))
        perception = _StubPerception(
            result=_PerceptionResult("SHOULD NOT BE USED", "x", 9)
        )
        handler = McpToolHandler(manager=mgr, perception=perception)
        outcomes, _ = _run(
            handler,
            {"server_id": "browser", "tool_name": "browser_click", "tool_args": {}},
        )
        self.assertEqual(outcomes[0].result["content"], "click ok")
        self.assertEqual(perception.transform_calls, [])

    def test_no_perception_is_unchanged(self) -> None:
        mgr = _FakeManager(result=_FakeResult([_TextBlock("plain")]))
        handler = McpToolHandler(manager=mgr, perception=None)
        outcomes, _ = _run(
            handler,
            {"server_id": "browser", "tool_name": "browser_snapshot"},
        )
        self.assertEqual(outcomes[0].result["content"], "plain")


class MiddlewareChainTests(unittest.TestCase):
    def test_first_claiming_non_none_wins(self) -> None:
        mgr = _FakeManager(result=_FakeResult([_TextBlock("raw")]))
        # mw1 claims but returns None (passthrough), mw2 claims + reshapes.
        mw1 = _StubPerception(result=None, server="browser", tool="browser_snapshot")
        mw2 = _StubPerception(
            result=_PerceptionResult("SHAPED", "sum", 3),
            server="browser", tool="browser_snapshot",
        )
        handler = McpToolHandler(manager=mgr, middlewares=[mw1, mw2])
        outcomes, _ = _run(
            handler,
            {"server_id": "browser", "tool_name": "browser_snapshot", "tool_args": {}},
        )
        self.assertEqual(outcomes[0].result["content"], "SHAPED")
        self.assertEqual(len(mw1.transform_calls), 1)
        self.assertEqual(len(mw2.transform_calls), 1)

    def test_earlier_result_short_circuits_later(self) -> None:
        mgr = _FakeManager(result=_FakeResult([_TextBlock("raw")]))
        mw1 = _StubPerception(result=_PerceptionResult("FIRST", "s", 1))
        mw2 = _StubPerception(result=_PerceptionResult("SECOND", "s", 1))
        handler = McpToolHandler(manager=mgr, middlewares=[mw1, mw2])
        outcomes, _ = _run(
            handler,
            {"server_id": "browser", "tool_name": "browser_snapshot", "tool_args": {}},
        )
        self.assertEqual(outcomes[0].result["content"], "FIRST")
        self.assertEqual(mw2.transform_calls, [])

    def test_none_of_them_claim_passthrough(self) -> None:
        mgr = _FakeManager(result=_FakeResult([_TextBlock("raw text")]))
        mw = _StubPerception(result=_PerceptionResult("X", "s", 1), tool="other")
        handler = McpToolHandler(manager=mgr, middlewares=[mw])
        outcomes, _ = _run(
            handler,
            {"server_id": "browser", "tool_name": "browser_snapshot", "tool_args": {}},
        )
        self.assertEqual(outcomes[0].result["content"], "raw text")

    def test_middleware_exception_falls_through(self) -> None:
        mgr = _FakeManager(result=_FakeResult([_TextBlock("raw text")]))

        class _Boom:
            def claims(self, s, t):
                return True

            def transform(self, s, t, txt, a=None):
                raise RuntimeError("kaboom")

        handler = McpToolHandler(manager=mgr, middlewares=[_Boom()])
        outcomes, _ = _run(
            handler,
            {"server_id": "browser", "tool_name": "browser_snapshot", "tool_args": {}},
        )
        self.assertEqual(outcomes[0].result["content"], "raw text")

    def test_perception_alias_prepended(self) -> None:
        # perception= (back-compat) runs before middlewares=.
        mgr = _FakeManager(result=_FakeResult([_TextBlock("raw")]))
        legacy = _StubPerception(result=_PerceptionResult("LEGACY", "s", 1))
        extra = _StubPerception(result=_PerceptionResult("EXTRA", "s", 1))
        handler = McpToolHandler(manager=mgr, perception=legacy, middlewares=[extra])
        outcomes, _ = _run(
            handler,
            {"server_id": "browser", "tool_name": "browser_snapshot", "tool_args": {}},
        )
        self.assertEqual(outcomes[0].result["content"], "LEGACY")


if __name__ == "__main__":
    unittest.main()
