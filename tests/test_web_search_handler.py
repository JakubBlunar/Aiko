"""Tests for the WebSearchHandler task handler."""
from __future__ import annotations

import sys
import types
import unittest
from typing import Any

from app.core.tasks.handler_names import HANDLER_WEB_SEARCH
from app.core.tasks.handlers.web_search import WebSearchHandler
from app.core.tasks.task_handler import (
    TaskCompleted,
    TaskFailed,
    TaskOutcome,
)


class _Emitter:
    def __init__(self) -> None:
        self.outcomes: list[TaskOutcome] = []

    def __call__(self, outcome: TaskOutcome) -> None:
        self.outcomes.append(outcome)


def _install_fake_ddgs(results: list[dict[str, Any]] | None, *, raises: bool = False):
    """Install a fake search module under both the new (``ddgs``) and legacy
    (``duckduckgo_search``) names, returning ``results``."""

    class _DDGS:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def text(self, query: str, max_results: int = 5):
            if raises:
                raise RuntimeError("network down")
            return list((results or [])[:max_results])

    for name in ("ddgs", "duckduckgo_search"):
        mod = types.ModuleType(name)
        mod.DDGS = _DDGS  # type: ignore[attr-defined]
        sys.modules[name] = mod


class WebSearchHandlerTests(unittest.TestCase):
    def tearDown(self) -> None:
        sys.modules.pop("duckduckgo_search", None)
        sys.modules.pop("ddgs", None)

    def test_name(self) -> None:
        self.assertEqual(WebSearchHandler.name, HANDLER_WEB_SEARCH)

    def test_empty_query_fails(self) -> None:
        h = WebSearchHandler()
        emit = _Emitter()
        h.start({"query": "  "}, emit)
        self.assertEqual(len(emit.outcomes), 1)
        self.assertIsInstance(emit.outcomes[0], TaskFailed)

    def test_successful_search(self) -> None:
        _install_fake_ddgs(
            [
                {"title": "Aiko", "href": "https://x", "body": "hi"},
                {"title": "Two", "url": "https://y", "body": "yo"},
            ]
        )
        h = WebSearchHandler()
        emit = _Emitter()
        state = h.start({"query": "aiko", "max_results": 5}, emit)
        self.assertEqual(state["phase"], "done")
        self.assertEqual(len(emit.outcomes), 1)
        done = emit.outcomes[0]
        self.assertIsInstance(done, TaskCompleted)
        self.assertEqual(done.result["result_count"], 2)
        self.assertEqual(done.result["results"][0]["url"], "https://x")
        self.assertIn("summary", done.result)

    def test_max_results_clamped_to_handler_ceiling(self) -> None:
        captured: dict[str, Any] = {}

        class _DDGS:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def text(self, query: str, max_results: int = 5):
                captured["max_results"] = max_results
                return []

        for name in ("ddgs", "duckduckgo_search"):
            mod = types.ModuleType(name)
            mod.DDGS = _DDGS  # type: ignore[attr-defined]
            sys.modules[name] = mod
        h = WebSearchHandler(max_results=3)
        emit = _Emitter()
        h.start({"query": "x", "max_results": 100}, emit)
        self.assertEqual(captured["max_results"], 3)

    def test_ddgs_exception_fails_gracefully(self) -> None:
        _install_fake_ddgs(None, raises=True)
        h = WebSearchHandler()
        emit = _Emitter()
        state = h.start({"query": "x"}, emit)
        self.assertEqual(state["phase"], "rejected")
        self.assertIsInstance(emit.outcomes[0], TaskFailed)

    def test_missing_dependency_fails_gracefully(self) -> None:
        # Break BOTH import names (new + legacy) with modules that have no
        # DDGS attribute, so the provider exhausts both and raises.
        for name in ("ddgs", "duckduckgo_search"):
            sys.modules[name] = types.ModuleType(name)  # no DDGS attribute
        h = WebSearchHandler()
        emit = _Emitter()
        state = h.start({"query": "x"}, emit)
        self.assertEqual(state["phase"], "rejected")
        self.assertIsInstance(emit.outcomes[0], TaskFailed)

    def test_resume_and_on_input_terminal(self) -> None:
        h = WebSearchHandler()
        emit = _Emitter()
        h.resume({"args": {}}, emit)
        self.assertIsInstance(emit.outcomes[0], TaskFailed)
        emit2 = _Emitter()
        h.on_input({"args": {}}, "answer", emit2)
        self.assertIsInstance(emit2.outcomes[0], TaskFailed)

    def test_cancel_noop(self) -> None:
        self.assertIsNone(WebSearchHandler().cancel({}))


if __name__ == "__main__":
    unittest.main()
