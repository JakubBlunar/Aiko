"""Tests for the D3 brain-lane web-search tool
(:mod:`app.llm.tools.web_search_brain`).

Contracts pinned here:

1. **The outbound query is scrubbed, not trusted.** The model authors the
   query with the persona, memories and transcript in view, so anything
   personal in it must be dropped before the request leaves the process,
   and a query that can't be made safe must fail loudly rather than
   quietly search something else.
2. **The result shape stays small.** Three hits at 400 characters — the
   turn pays for this in prompt tokens.
3. **No silent fallback.** A provider failure surfaces as a
   :class:`ToolError` so the model says it couldn't reach the web instead
   of inventing the answer it was about to look up.
4. **Results are handed to the sink** so the post-turn distill can keep
   what she learned.
"""
from __future__ import annotations

import json
import unittest

from app.llm.search.providers import SearchResult
from app.llm.tools import ToolError
from app.llm.tools.web_search_brain import BrainWebSearchTool


class _FakeProvider:
    name = "fake"

    def __init__(
        self,
        results: list[SearchResult] | None = None,
        *,
        raises: bool = False,
    ) -> None:
        self._results = list(results or [])
        self._raises = raises
        self.last_query: str | None = None
        self.last_max: int | None = None
        self.calls = 0

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        self.calls += 1
        self.last_query = query
        self.last_max = max_results
        if self._raises:
            raise RuntimeError("boom")
        return list(self._results)


def _hit(title: str = "t", url: str = "https://e.com", snippet: str = "s"):
    return SearchResult(title=title, url=url, snippet=snippet)


def _build(
    results: list[SearchResult] | None = None,
    *,
    raises: bool = False,
    sink: object = None,
):
    provider = _FakeProvider(results, raises=raises)
    tool = BrainWebSearchTool(
        provider,
        user_names_provider=lambda: ["Jacob"],
        assistant_name_provider=lambda: "Aiko",
        on_results=sink,  # type: ignore[arg-type]
    )
    return tool, provider


class SchemaTests(unittest.TestCase):
    def test_registers_as_web_search(self) -> None:
        schema = _build()[0].schema()
        self.assertEqual(schema.name, "web_search")

    def test_query_description_forbids_personal_details(self) -> None:
        params = _build()[0].schema().parameters
        description = params["properties"]["query"]["description"].lower()
        self.assertIn("no personal details", description)

    def test_max_results_capped_at_five(self) -> None:
        params = _build()[0].schema().parameters
        self.assertEqual(params["properties"]["max_results"]["maximum"], 5)


class OutboundPrivacyTests(unittest.TestCase):
    """The scrubber is the enforcement half of the schema instruction."""

    def test_clean_topic_query_passes_through_untouched(self) -> None:
        tool, provider = _build([_hit()])
        tool.run({"query": "Dandadan season 2 episode count"})
        self.assertEqual(
            provider.last_query, "Dandadan season 2 episode count",
        )

    def test_first_person_tokens_are_dropped(self) -> None:
        tool, provider = _build([_hit()])
        tool.run({"query": "my favourite anime Dandadan season 2 release"})
        sent = (provider.last_query or "").lower()
        self.assertNotIn("my ", sent)
        # The searchable topic has to survive the redaction, or the
        # scrub has cost us the whole point of the lookup.
        self.assertIn("dandadan", sent)

    def test_user_name_is_dropped(self) -> None:
        tool, provider = _build([_hit()])
        tool.run({"query": "Jacob asked about Frieren season 2 release date"})
        self.assertNotIn("Jacob", provider.last_query or "")
        self.assertIn("Frieren", provider.last_query or "")

    def test_url_in_query_refuses_and_never_searches(self) -> None:
        tool, provider = _build([_hit()])
        with self.assertRaises(ToolError) as ctx:
            tool.run({"query": "is https://internal.example.com/me down"})
        self.assertIn("rewrite it", str(ctx.exception).lower())
        self.assertEqual(provider.calls, 0)

    def test_email_in_query_refuses(self) -> None:
        tool, provider = _build([_hit()])
        with self.assertRaises(ToolError):
            tool.run({"query": "who owns jacob@example.com"})
        self.assertEqual(provider.calls, 0)

    def test_query_that_collapses_to_nothing_refuses(self) -> None:
        tool, provider = _build([_hit()])
        with self.assertRaises(ToolError):
            tool.run({"query": "my Jacob"})
        self.assertEqual(provider.calls, 0)

    def test_missing_name_providers_still_scrub(self) -> None:
        # Providers are optional; the first-person rules must still apply.
        provider = _FakeProvider([_hit()])
        tool = BrainWebSearchTool(provider)
        tool.run({"query": "my favourite anime Dandadan release date"})
        self.assertNotIn("my ", (provider.last_query or "").lower())

    def test_raising_name_provider_does_not_break_the_search(self) -> None:
        def _boom() -> list[str]:
            raise RuntimeError("no session")

        provider = _FakeProvider([_hit()])
        tool = BrainWebSearchTool(provider, user_names_provider=_boom)
        tool.run({"query": "Frieren season 2 release date"})
        self.assertEqual(provider.calls, 1)


class ResultShapeTests(unittest.TestCase):
    def test_defaults_to_three_results(self) -> None:
        tool, provider = _build([_hit()])
        tool.run({"query": "Frieren season 2"})
        self.assertEqual(provider.last_max, 3)

    def test_clamps_max_results_to_five(self) -> None:
        tool, provider = _build([_hit()])
        tool.run({"query": "Frieren season 2", "max_results": 99})
        self.assertEqual(provider.last_max, 5)

    def test_non_numeric_max_results_falls_back_to_default(self) -> None:
        tool, provider = _build([_hit()])
        tool.run({"query": "Frieren season 2", "max_results": "lots"})
        self.assertEqual(provider.last_max, 3)

    def test_snippets_capped_at_400_chars(self) -> None:
        tool, _ = _build([_hit(snippet="x" * 5000)])
        payload = json.loads(tool.run({"query": "Frieren season 2"}))
        self.assertEqual(len(payload["results"][0]["snippet"]), 400)

    def test_hits_without_a_snippet_are_dropped(self) -> None:
        tool, _ = _build([_hit(snippet=""), _hit(snippet="real")])
        payload = json.loads(tool.run({"query": "Frieren season 2"}))
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["snippet"], "real")

    def test_no_results_returns_a_note(self) -> None:
        tool, _ = _build([])
        payload = json.loads(tool.run({"query": "Frieren season 2"}))
        self.assertEqual(payload["results"], [])
        self.assertIn("note", payload)

    def test_empty_query_raises(self) -> None:
        tool, _ = _build([_hit()])
        with self.assertRaises(ToolError):
            tool.run({"query": "   "})


class FailureTests(unittest.TestCase):
    def test_provider_error_becomes_tool_error(self) -> None:
        tool, _ = _build([_hit()], raises=True)
        with self.assertRaises(ToolError) as ctx:
            tool.run({"query": "Frieren season 2"})
        self.assertIn("web_search failed", str(ctx.exception))

    def test_set_provider_swaps_the_backend(self) -> None:
        tool, first = _build([_hit()])
        second = _FakeProvider([_hit()])
        tool.set_provider(second)
        tool.run({"query": "Frieren season 2"})
        self.assertEqual(first.calls, 0)
        self.assertEqual(second.calls, 1)


class ResultsSinkTests(unittest.TestCase):
    def test_sink_receives_the_scrubbed_query_and_hits(self) -> None:
        seen: list[tuple[str, list[dict[str, str]]]] = []
        tool, _ = _build(
            [_hit(title="Wiki", url="https://w.org", snippet="12 episodes")],
            sink=lambda q, r: seen.append((q, r)),
        )
        tool.run({"query": "my show Dandadan season 2 episode count"})
        self.assertEqual(len(seen), 1)
        query, results = seen[0]
        # What the sink stores must be what actually went out, not the
        # model's original personal phrasing.
        self.assertNotIn("my ", query.lower())
        self.assertEqual(results[0]["snippet"], "12 episodes")

    def test_sink_not_called_when_there_are_no_hits(self) -> None:
        seen: list[tuple[str, list[dict[str, str]]]] = []
        tool, _ = _build([], sink=lambda q, r: seen.append((q, r)))
        tool.run({"query": "Frieren season 2"})
        self.assertEqual(seen, [])

    def test_failing_sink_does_not_break_the_turn(self) -> None:
        def _boom(_q: str, _r: list[dict[str, str]]) -> None:
            raise RuntimeError("sink down")

        tool, _ = _build([_hit()], sink=_boom)
        payload = json.loads(tool.run({"query": "Frieren season 2"}))
        self.assertEqual(len(payload["results"]), 1)


if __name__ == "__main__":
    unittest.main()
