"""Tests for the pluggable web-search providers + factory."""
from __future__ import annotations

import sys
import time
import types
import unittest
from typing import Any
from unittest import mock

from app.core.infra.settings import SearchSettings
from app.llm.search import providers as providers_mod
from app.llm.search.providers import (
    DuckDuckGoProvider,
    FallbackProvider,
    LangSearchProvider,
    SearchResult,
    build_search_provider,
    resolve_api_key,
)


def _install_fake_ddgs(results: list[dict[str, Any]] | None, *, raises: bool = False):
    # The provider prefers the new ``ddgs`` package and falls back to the
    # legacy ``duckduckgo_search`` name, so inject the fake under both to
    # exercise whichever import path the install resolves to first.
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


class _FakeResponse:
    def __init__(self, body: dict[str, Any], *, status: int = 200) -> None:
        self._body = body
        self._status = status

    def raise_for_status(self) -> None:
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")

    def json(self) -> dict[str, Any]:
        return self._body


class DuckDuckGoProviderTests(unittest.TestCase):
    def tearDown(self) -> None:
        sys.modules.pop("duckduckgo_search", None)
        sys.modules.pop("ddgs", None)

    def test_maps_fields(self) -> None:
        _install_fake_ddgs([
            {"title": "A", "href": "https://a", "body": "abody"},
            {"title": "B", "url": "https://b", "body": "bbody"},
        ])
        hits = DuckDuckGoProvider().search("q", 5)
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0].title, "A")
        self.assertEqual(hits[0].url, "https://a")
        self.assertEqual(hits[0].snippet, "abody")
        self.assertEqual(hits[1].url, "https://b")

    def test_raises_on_ddgs_error(self) -> None:
        _install_fake_ddgs(None, raises=True)
        with self.assertRaises(Exception):
            DuckDuckGoProvider().search("q", 5)


class LangSearchProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        # Snapshot the real ``requests`` module so we can restore it
        # exactly in tearDown (popping + re-importing leaves a fresh
        # object that can confuse other suites' lazy imports).
        self._real_requests = sys.modules.get("requests")
        # Reset the process-wide throttle so a previous test's request
        # timestamp can't make this test sleep (~1.1s) or couple to it.
        LangSearchProvider._last_request_monotonic = 0.0

    def tearDown(self) -> None:  # noqa: D401 - see body
        LangSearchProvider._last_request_monotonic = 0.0
        self._restore_requests()

    def _restore_requests(self) -> None:
        if self._real_requests is not None:
            sys.modules["requests"] = self._real_requests
        else:
            sys.modules.pop("requests", None)

    def _patch_requests(self, response: _FakeResponse) -> dict[str, Any]:
        captured: dict[str, Any] = {}
        mod = types.ModuleType("requests")

        def _post(url, json=None, headers=None, timeout=None):  # noqa: A002
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            captured["timeout"] = timeout
            return response

        mod.post = _post  # type: ignore[attr-defined]
        sys.modules["requests"] = mod
        return captured

    def _body(self, values: list[dict[str, Any]], *, code: int = 200) -> dict[str, Any]:
        return {
            "code": code,
            "data": {"webPages": {"value": values}},
        }

    def test_prefers_summary_over_snippet(self) -> None:
        self._patch_requests(_FakeResponse(self._body([
            {
                "name": "Title",
                "url": "https://x",
                "snippet": "short",
                "summary": "the long summary",
            },
        ])))
        hits = LangSearchProvider(api_key="k").search("q", 5)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].title, "Title")
        self.assertEqual(hits[0].url, "https://x")
        self.assertEqual(hits[0].snippet, "the long summary")

    def test_falls_back_to_snippet_when_no_summary(self) -> None:
        self._patch_requests(_FakeResponse(self._body([
            {"name": "T", "url": "https://x", "snippet": "just snippet"},
        ])))
        hits = LangSearchProvider(api_key="k", summary=False).search("q", 5)
        self.assertEqual(hits[0].snippet, "just snippet")

    def test_sends_bearer_and_params(self) -> None:
        captured = self._patch_requests(_FakeResponse(self._body([])))
        LangSearchProvider(
            api_key="secret", summary=True, freshness="oneWeek", count=7,
        ).search("hello", 3)
        self.assertEqual(captured["headers"]["Authorization"], "Bearer secret")
        # count clamps to min(provider count, max_results) = min(7, 3) = 3
        self.assertEqual(captured["json"]["count"], 3)
        self.assertEqual(captured["json"]["freshness"], "oneWeek")
        self.assertTrue(captured["json"]["summary"])
        self.assertEqual(captured["json"]["query"], "hello")

    def test_raises_on_api_error_code(self) -> None:
        self._patch_requests(_FakeResponse(self._body([], code=403)))
        with self.assertRaises(Exception):
            LangSearchProvider(api_key="k").search("q", 5)

    def test_raises_on_http_error(self) -> None:
        self._patch_requests(_FakeResponse(self._body([]), status=500))
        with self.assertRaises(Exception):
            LangSearchProvider(api_key="k").search("q", 5)

    def test_empty_key_rejected(self) -> None:
        with self.assertRaises(ValueError):
            LangSearchProvider(api_key="")

    def test_invalid_freshness_defaults(self) -> None:
        captured = self._patch_requests(_FakeResponse(self._body([])))
        LangSearchProvider(api_key="k", freshness="bogus").search("q", 5)
        self.assertEqual(captured["json"]["freshness"], "noLimit")


class LangSearchThrottleTests(unittest.TestCase):
    """The process-wide 1/sec spacing gate on LangSearch requests."""

    def setUp(self) -> None:
        self._real_requests = sys.modules.get("requests")
        LangSearchProvider._last_request_monotonic = 0.0

    def tearDown(self) -> None:
        LangSearchProvider._last_request_monotonic = 0.0
        if self._real_requests is not None:
            sys.modules["requests"] = self._real_requests
        else:
            sys.modules.pop("requests", None)

    def _patch_requests(self) -> None:
        mod = types.ModuleType("requests")

        def _post(url, json=None, headers=None, timeout=None):  # noqa: A002
            return _FakeResponse({"code": 200, "data": {"webPages": {"value": []}}})

        mod.post = _post  # type: ignore[attr-defined]
        sys.modules["requests"] = mod

    def test_consecutive_requests_are_spaced(self) -> None:
        self._patch_requests()
        clock = {"t": 10_000.0}
        sleeps: list[float] = []

        def fake_monotonic() -> float:
            return clock["t"]

        def fake_sleep(secs: float) -> None:
            sleeps.append(secs)
            clock["t"] += secs

        prov = LangSearchProvider(api_key="k", min_interval_seconds=1.1)
        with mock.patch.object(providers_mod.time, "monotonic", fake_monotonic), \
                mock.patch.object(providers_mod.time, "sleep", fake_sleep):
            prov.search("a", 3)  # first request: no wait
            prov.search("b", 3)  # immediate second: must wait ~1.1s
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 1.1, places=3)

    def test_interval_zero_never_sleeps(self) -> None:
        self._patch_requests()
        sleeps: list[float] = []
        prov = LangSearchProvider(api_key="k", min_interval_seconds=0.0)
        with mock.patch.object(
            providers_mod.time, "sleep", lambda s: sleeps.append(s)
        ):
            prov.search("a", 3)
            prov.search("b", 3)
        self.assertEqual(sleeps, [])


class _StubProvider:
    def __init__(self, *, results=None, raises=False, name="stub"):
        self.name = name
        self._results = results or []
        self._raises = raises
        self.calls = 0

    def search(self, query: str, max_results: int):
        self.calls += 1
        if self._raises:
            raise RuntimeError("primary down")
        return list(self._results)


class FallbackProviderTests(unittest.TestCase):
    def test_uses_primary_when_ok(self) -> None:
        primary = _StubProvider(results=[SearchResult("t", "u", "s")], name="p")
        fallback = _StubProvider(results=[], name="f")
        fp = FallbackProvider(primary, fallback)
        hits = fp.search("q", 5)
        self.assertEqual(len(hits), 1)
        self.assertEqual(fallback.calls, 0)

    def test_falls_back_on_error(self) -> None:
        primary = _StubProvider(raises=True, name="p")
        fallback = _StubProvider(results=[SearchResult("t", "u", "s")], name="f")
        fp = FallbackProvider(primary, fallback)
        hits = fp.search("q", 5)
        self.assertEqual(len(hits), 1)
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(fp.name, "p->f")


class ResolveApiKeyTests(unittest.TestCase):
    def test_explicit_wins(self) -> None:
        self.assertEqual(resolve_api_key("explicit", "SOME_ENV"), "explicit")

    def test_env_fallback(self) -> None:
        import os

        os.environ["LS_TEST_KEY"] = "from_env"
        try:
            self.assertEqual(resolve_api_key("", "LS_TEST_KEY"), "from_env")
        finally:
            os.environ.pop("LS_TEST_KEY", None)

    def test_empty_when_nothing(self) -> None:
        self.assertEqual(resolve_api_key("", ""), "")


class BuildSearchProviderTests(unittest.TestCase):
    def test_default_is_duckduckgo(self) -> None:
        prov = build_search_provider(SearchSettings())
        self.assertIsInstance(prov, DuckDuckGoProvider)

    def test_none_settings_is_duckduckgo(self) -> None:
        self.assertIsInstance(build_search_provider(None), DuckDuckGoProvider)

    def test_langsearch_with_key_wraps_fallback(self) -> None:
        prov = build_search_provider(
            SearchSettings(provider="langsearch", api_key="k")
        )
        self.assertIsInstance(prov, FallbackProvider)

    def test_langsearch_no_fallback_is_bare(self) -> None:
        prov = build_search_provider(
            SearchSettings(
                provider="langsearch", api_key="k", fallback_to_duckduckgo=False,
            )
        )
        self.assertIsInstance(prov, LangSearchProvider)

    def test_langsearch_without_key_falls_to_ddg(self) -> None:
        prov = build_search_provider(SearchSettings(provider="langsearch"))
        self.assertIsInstance(prov, DuckDuckGoProvider)


if __name__ == "__main__":
    unittest.main()
