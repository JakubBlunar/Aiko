"""Unit tests for the lean tool-calling stack (Phase F)."""
from __future__ import annotations

import json
import unittest
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.llm.tools import ToolError, ToolRegistry, ToolResult, ToolSchema, build_default_registry
from app.llm.tools.builtins import GetTimeTool, RecallTool, WebSearchTool


# ── fakes ─────────────────────────────────────────────────────────────────


@dataclass
class _FakeHit:
    text: str
    source: str = "memory"
    score: float = 0.5
    metadata: dict[str, Any] | None = None
    kind: str = "fact"


class _FakeRagRetriever:
    """Stand-in for :class:`RagRetriever` that records the last query and
    returns a fixed list of hits."""

    def __init__(self, hits: list[_FakeHit] | None = None) -> None:
        self.hits = list(hits or [])
        self.last_query: str | None = None

    def retrieve(self, query: str, **_kwargs: Any) -> list[_FakeHit]:
        self.last_query = query
        return list(self.hits)


# ── get_time ──────────────────────────────────────────────────────────────


class GetTimeToolTests(unittest.TestCase):
    def test_schema_has_required_shape(self) -> None:
        schema = GetTimeTool().schema()
        self.assertIsInstance(schema, ToolSchema)
        self.assertEqual(schema.name, "get_time")
        ollama = schema.to_ollama()
        self.assertEqual(ollama["type"], "function")
        self.assertEqual(ollama["function"]["name"], "get_time")
        self.assertIn("parameters", ollama["function"])

    def test_run_no_arguments_returns_local_time(self) -> None:
        result = GetTimeTool().run({})
        payload = json.loads(result)
        # iso parses cleanly
        parsed = datetime.fromisoformat(payload["iso"])
        self.assertIsNotNone(parsed)
        self.assertIn(payload["weekday"], {
            "Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday",
        })

    def test_run_with_unknown_timezone_raises_tool_error(self) -> None:
        with self.assertRaises(ToolError):
            GetTimeTool().run({"timezone": "Mars/Olympus_Mons"})


# ── recall ────────────────────────────────────────────────────────────────


class RecallToolTests(unittest.TestCase):
    def test_run_returns_hits_as_json(self) -> None:
        rag = _FakeRagRetriever(hits=[
            _FakeHit(text="Jacob loves coffee", source="memory", score=0.91),
            _FakeHit(text="Jacob lives in Krakow", source="memory", score=0.83),
        ])
        result = RecallTool(rag).run({"query": "what does Jacob like"})
        payload = json.loads(result)
        self.assertEqual(rag.last_query, "what does Jacob like")
        self.assertEqual(len(payload["hits"]), 2)
        self.assertEqual(payload["hits"][0]["text"], "Jacob loves coffee")
        self.assertAlmostEqual(payload["hits"][0]["score"], 0.91, places=2)
        self.assertEqual(payload["hits"][0]["source"], "memory")

    def test_run_clamps_limit(self) -> None:
        hits = [_FakeHit(text=f"fact {i}") for i in range(20)]
        rag = _FakeRagRetriever(hits=hits)
        result = RecallTool(rag).run({"query": "x", "limit": 99})
        payload = json.loads(result)
        self.assertEqual(len(payload["hits"]), 12)  # max clamp

    def test_run_empty_query_raises(self) -> None:
        with self.assertRaises(ToolError):
            RecallTool(_FakeRagRetriever()).run({"query": ""})

    def test_run_no_hits_returns_note(self) -> None:
        result = RecallTool(_FakeRagRetriever([])).run({"query": "anything"})
        payload = json.loads(result)
        self.assertEqual(payload["hits"], [])
        self.assertIn("note", payload)

    def test_run_with_no_retriever_raises(self) -> None:
        with self.assertRaises(ToolError):
            RecallTool(None).run({"query": "anything"})

    def test_run_truncates_long_text(self) -> None:
        long = "x" * 1000
        rag = _FakeRagRetriever([_FakeHit(text=long)])
        result = RecallTool(rag).run({"query": "anything"})
        payload = json.loads(result)
        self.assertLessEqual(len(payload["hits"][0]["text"]), 280)


# ── web_search ────────────────────────────────────────────────────────────


class _FakeDDGS:
    """Context-manager stub for :class:`duckduckgo_search.DDGS`."""

    last_kwargs: dict[str, Any] = {}
    fixed_results: list[dict[str, Any]] = []

    def __enter__(self) -> "_FakeDDGS":
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def text(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        _FakeDDGS.last_kwargs = {"query": query, **kwargs}
        return list(self.fixed_results)


class WebSearchToolTests(unittest.TestCase):
    def _build(self, results: list[dict[str, Any]]) -> WebSearchTool:
        # Bypass the real import; inject a fake DDGS class on the instance.
        tool = WebSearchTool.__new__(WebSearchTool)
        _FakeDDGS.fixed_results = list(results)
        tool._ddgs_cls = _FakeDDGS  # type: ignore[attr-defined]
        return tool

    def test_run_returns_results(self) -> None:
        tool = self._build([
            {
                "title": "Cursor 1.0 announcement",
                "href": "https://cursor.com/post",
                "body": "We're shipping Cursor 1.0 today.",
            },
            {
                "title": "Hacker News thread",
                "url": "https://news.ycombinator.com/item?id=1",
                "body": "Discussion of the launch.",
            },
        ])
        result = tool.run({"query": "cursor 1.0"})
        payload = json.loads(result)
        self.assertEqual(_FakeDDGS.last_kwargs["query"], "cursor 1.0")
        self.assertEqual(_FakeDDGS.last_kwargs["max_results"], 5)
        self.assertEqual(len(payload["results"]), 2)
        self.assertEqual(payload["results"][0]["title"], "Cursor 1.0 announcement")
        self.assertEqual(payload["results"][1]["url"], "https://news.ycombinator.com/item?id=1")

    def test_run_clamps_max_results(self) -> None:
        tool = self._build([])
        tool.run({"query": "x", "max_results": 99})
        self.assertEqual(_FakeDDGS.last_kwargs["max_results"], 8)

    def test_run_no_results_returns_note(self) -> None:
        tool = self._build([])
        payload = json.loads(tool.run({"query": "anything"}))
        self.assertEqual(payload["results"], [])
        self.assertIn("note", payload)

    def test_run_empty_query_raises(self) -> None:
        tool = self._build([])
        with self.assertRaises(ToolError):
            tool.run({"query": ""})


# ── ToolRegistry ──────────────────────────────────────────────────────────


class ToolRegistryTests(unittest.TestCase):
    def test_register_and_dispatch(self) -> None:
        registry = ToolRegistry()
        registry.register(GetTimeTool())
        self.assertEqual(registry.names(), ["get_time"])
        self.assertEqual(len(registry), 1)
        result = registry.dispatch("get_time", {})
        self.assertIsInstance(result, ToolResult)
        self.assertTrue(result.ok)
        # content is a JSON string the LLM can read.
        self.assertIn("iso", json.loads(result.content))

    def test_dispatch_unknown_tool_returns_error_result(self) -> None:
        registry = ToolRegistry()
        result = registry.dispatch("never_registered", {})
        self.assertFalse(result.ok)
        self.assertIn("unknown tool", result.content)

    def test_dispatch_tool_error_surfaces_message(self) -> None:
        rag = _FakeRagRetriever()
        registry = ToolRegistry()
        registry.register(RecallTool(rag))
        result = registry.dispatch("recall", {"query": ""})
        self.assertFalse(result.ok)
        self.assertIn("query", result.content)

    def test_dispatch_unexpected_exception_caught(self) -> None:
        class BoomTool:
            def schema(self) -> ToolSchema:
                return ToolSchema(name="boom", description="", parameters={
                    "type": "object", "properties": {}, "required": [],
                })

            def run(self, arguments: dict[str, Any]) -> str:
                raise RuntimeError("kaboom")

        registry = ToolRegistry()
        registry.register(BoomTool())
        result = registry.dispatch("boom", {})
        self.assertFalse(result.ok)
        self.assertIn("kaboom", result.content)

    def test_to_ollama_tools_shape(self) -> None:
        registry = ToolRegistry()
        registry.register(GetTimeTool())
        registry.register(RecallTool(_FakeRagRetriever()))
        ollama = registry.to_ollama_tools()
        self.assertEqual({t["function"]["name"] for t in ollama}, {"get_time", "recall"})

    def test_build_default_registry_skips_recall_without_rag(self) -> None:
        # web_search may be skipped if duckduckgo-search is missing -- that's
        # fine. The point is recall is not registered.
        registry = build_default_registry(rag_retriever=None, web_search_enabled=False)
        self.assertNotIn("recall", registry.names())
        self.assertIn("get_time", registry.names())

    def test_build_default_registry_with_rag(self) -> None:
        registry = build_default_registry(
            rag_retriever=_FakeRagRetriever(),
            web_search_enabled=False,
        )
        self.assertIn("recall", registry.names())

    def test_describe_returns_name_description_pairs(self) -> None:
        # Backs the MCP ``list_agent_tools`` debug surface. Each entry
        # must carry ``name`` + ``description`` so a connected client
        # (Cursor, VSCode) can show the live catalogue without
        # reaching into private state.
        registry = ToolRegistry()
        registry.register(GetTimeTool())
        registry.register(RecallTool(_FakeRagRetriever()))
        described = registry.describe()
        self.assertEqual(len(described), 2)
        names = [d["name"] for d in described]
        self.assertEqual(names, sorted(names), "describe() must be sorted")
        self.assertIn("get_time", names)
        self.assertIn("recall", names)
        for entry in described:
            self.assertIn("description", entry)
            self.assertGreater(
                len(entry["description"]), 0,
                f"{entry['name']!r} has empty description",
            )

    def test_describe_empty_registry(self) -> None:
        # Brand-new registry has no tools; describe() must not crash.
        self.assertEqual(ToolRegistry().describe(), [])


# ── TurnRunner two-pass ───────────────────────────────────────────────────


class TurnRunnerTwoPassTests(unittest.TestCase):
    """Verify the pre-stream tool dispatch path mutates ``messages`` before
    handing them off to ``chat_stream``.

    We don't run the full ``TurnRunner.run()`` (it touches the DB / prompt
    assembler / Ollama). We exercise the helper directly.
    """

    def setUp(self) -> None:
        from app.core.session.turn_runner import TurnRunner

        # Build a TurnRunner without real dependencies; we won't call run().
        self._TurnRunner = TurnRunner

    def _make_runner(
        self,
        ollama: Any,
        registry: ToolRegistry,
    ) -> Any:
        runner = self._TurnRunner.__new__(self._TurnRunner)
        # Minimal attrs the helper reads.
        runner._ollama = ollama
        runner._tool_registry = registry
        runner._model = "test-model"
        runner._temperature = 0.7
        runner._context_window = 4096
        runner._max_tokens = 512
        runner._on_tool_call = None
        runner._on_tool_result = None
        import threading
        runner._stop = threading.Event()
        return runner

    def test_no_tool_calls_leaves_messages_alone(self) -> None:
        from app.llm.ollama_client import OllamaChatResponse

        class FakeOllama:
            def __init__(self) -> None:
                self.calls = 0

            def chat_with_tools(self, messages, **_kwargs):
                self.calls += 1
                return OllamaChatResponse(content="", tool_calls=[])

        registry = ToolRegistry()
        registry.register(GetTimeTool())
        ollama = FakeOllama()
        runner = self._make_runner(ollama, registry)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "be Aiko"},
            {"role": "user", "content": "hello"},
        ]
        runner._maybe_run_tool_pass(messages, stop_requested=None)
        self.assertEqual(ollama.calls, 1)
        # Messages list is unchanged.
        self.assertEqual(len(messages), 2)

    def test_tool_calls_append_assistant_and_tool_messages(self) -> None:
        from app.llm.ollama_client import OllamaChatResponse, OllamaToolCall

        # First round: model emits a tool call. Second round: nothing.
        responses = [
            OllamaChatResponse(
                content="",
                tool_calls=[OllamaToolCall(name="get_time", arguments={}, call_id="c1")],
            ),
            OllamaChatResponse(content="", tool_calls=[]),
        ]

        class FakeOllama:
            def __init__(self) -> None:
                self.received_tool_specs: list[Any] | None = None
                self.calls = 0

            def chat_with_tools(self, messages, *, tools=None, **_kwargs):
                self.received_tool_specs = tools
                resp = responses[self.calls]
                self.calls += 1
                return resp

        registry = ToolRegistry()
        registry.register(GetTimeTool())
        ollama = FakeOllama()
        runner = self._make_runner(ollama, registry)

        messages: list[dict[str, Any]] = [{"role": "user", "content": "what time is it"}]
        runner._maybe_run_tool_pass(messages, stop_requested=None)

        # Original user msg + assistant tool_calls msg + tool result msg.
        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[1]["role"], "assistant")
        self.assertIn("tool_calls", messages[1])
        self.assertEqual(messages[1]["tool_calls"][0]["function"]["name"], "get_time")
        self.assertEqual(messages[2]["role"], "tool")
        self.assertEqual(messages[2]["name"], "get_time")
        # Tool content is the JSON the tool returned.
        payload = json.loads(messages[2]["content"])
        self.assertIn("iso", payload)
        # Ollama got the schema list.
        self.assertIsNotNone(ollama.received_tool_specs)
        self.assertEqual(
            ollama.received_tool_specs[0]["function"]["name"], "get_time",
        )

    def test_tool_call_listeners_invoked(self) -> None:
        from app.llm.ollama_client import OllamaChatResponse, OllamaToolCall

        class FakeOllama:
            def __init__(self) -> None:
                self.calls = 0

            def chat_with_tools(self, messages, **_kwargs):
                if self.calls == 0:
                    resp = OllamaChatResponse(
                        content="",
                        tool_calls=[OllamaToolCall(name="get_time", arguments={}, call_id="c1")],
                    )
                else:
                    resp = OllamaChatResponse(content="", tool_calls=[])
                self.calls += 1
                return resp

        registry = ToolRegistry()
        registry.register(GetTimeTool())
        runner = self._make_runner(FakeOllama(), registry)

        call_events: list[tuple[str, dict[str, Any]]] = []
        result_events: list[tuple[str, str, bool]] = []
        runner._on_tool_call = lambda name, args: call_events.append((name, args))
        runner._on_tool_result = lambda name, content, ok: result_events.append((name, content, ok))

        runner._maybe_run_tool_pass([{"role": "user", "content": "x"}], stop_requested=None)
        self.assertEqual(call_events, [("get_time", {})])
        self.assertEqual(len(result_events), 1)
        self.assertEqual(result_events[0][0], "get_time")
        self.assertTrue(result_events[0][2])

    def test_ollama_failure_swallowed(self) -> None:
        class FakeOllama:
            def chat_with_tools(self, *_args, **_kwargs):
                raise RuntimeError("ollama down")

        registry = ToolRegistry()
        registry.register(GetTimeTool())
        runner = self._make_runner(FakeOllama(), registry)

        messages = [{"role": "user", "content": "hi"}]
        # No exception escapes; the streaming pass would still run.
        runner._maybe_run_tool_pass(messages, stop_requested=None)
        self.assertEqual(len(messages), 1)

    def test_returns_aggregated_usage(self) -> None:
        """The tool pre-pass must return cumulative ``OllamaUsage`` so the
        caller can merge it into the streaming-pass usage for accurate totals.
        """
        from app.llm.ollama_client import OllamaChatResponse, OllamaToolCall, OllamaUsage

        class FakeOllama:
            def __init__(self) -> None:
                self.calls = 0
                self.last_usage = OllamaUsage()

            def chat_with_tools(self, messages, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    self.last_usage = OllamaUsage(
                        prompt_tokens=80,
                        completion_tokens=12,
                        total_duration_ms=400.0,
                        eval_duration_ms=300.0,
                    )
                    return OllamaChatResponse(
                        content="",
                        tool_calls=[OllamaToolCall(name="get_time", arguments={}, call_id="c1")],
                    )
                self.last_usage = OllamaUsage(
                    prompt_tokens=10,
                    completion_tokens=4,
                    total_duration_ms=80.0,
                    eval_duration_ms=50.0,
                )
                return OllamaChatResponse(content="", tool_calls=[])

        registry = ToolRegistry()
        registry.register(GetTimeTool())
        runner = self._make_runner(FakeOllama(), registry)

        usage = runner._maybe_run_tool_pass(
            [{"role": "user", "content": "hi"}], stop_requested=None,
        )
        self.assertEqual(usage.prompt_tokens, 90)
        self.assertEqual(usage.completion_tokens, 16)
        self.assertEqual(usage.total_duration_ms, 480.0)
        self.assertEqual(usage.eval_duration_ms, 350.0)

    def test_stop_requested_short_circuits(self) -> None:
        from app.llm.ollama_client import OllamaChatResponse, OllamaToolCall

        class FakeOllama:
            def __init__(self) -> None:
                self.calls = 0

            def chat_with_tools(self, messages, **_kwargs):
                self.calls += 1
                return OllamaChatResponse(
                    content="",
                    tool_calls=[OllamaToolCall(name="get_time", arguments={}, call_id="c1")],
                )

        registry = ToolRegistry()
        registry.register(GetTimeTool())
        ollama = FakeOllama()
        runner = self._make_runner(ollama, registry)

        runner._maybe_run_tool_pass(
            [{"role": "user", "content": "x"}],
            stop_requested=lambda: True,
        )
        # The very first iteration sees stop=True and returns immediately.
        self.assertEqual(ollama.calls, 0)


if __name__ == "__main__":
    unittest.main()
