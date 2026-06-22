"""Web-search provider implementations + factory.

No dependency on ``app.core`` or the session layer, so both the worker
``WebSearchTool`` and the background ``WebSearchHandler`` can import this
without a cycle. A provider takes a query and a result cap and returns a
list of :class:`SearchResult`; the callers re-shape that into their own
JSON / task-result dicts.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.infra.settings import SearchSettings


log = logging.getLogger("app.llm.search")


# Generous per-result snippet cap applied at the provider boundary. The
# callers re-cap to their own (smaller) limits; this only stops a
# pathological multi-kilobyte LangSearch summary from being carried
# around in full before the caller trims it.
_SNIPPET_CAP = 1500
_TITLE_CAP = 160

_LANGSEARCH_URL = "https://api.langsearch.com/v1/web-search"
_LANGSEARCH_FRESHNESS = frozenset(
    {"oneDay", "oneWeek", "oneMonth", "oneYear", "noLimit"}
)


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One normalized web-search hit."""

    title: str
    url: str
    snippet: str


@runtime_checkable
class SearchProvider(Protocol):
    """A web-search backend."""

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        """Return up to ``max_results`` hits for ``query``.

        Implementations may raise on transport / auth / quota errors;
        :class:`FallbackProvider` is responsible for turning a primary
        failure into a fallback lookup.
        """
        ...


def _clip(text: Any, cap: int) -> str:
    return str(text or "")[:cap]


# ── DuckDuckGo ──────────────────────────────────────────────────────────


class DuckDuckGoProvider:
    """DuckDuckGo HTML search (the keyless default).

    The ``duckduckgo_search`` import is deferred to ``search`` so a build
    without the optional dependency can still import this module; the
    error surfaces only when a search is actually attempted.
    """

    name = "duckduckgo"

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        # The package was renamed ``duckduckgo_search`` -> ``ddgs``. Prefer
        # the new name and fall back to the legacy one so an older install
        # keeps working without a hard pin.
        try:
            from ddgs import DDGS  # type: ignore
        except Exception:
            try:
                from duckduckgo_search import DDGS  # type: ignore
            except Exception as exc:  # pragma: no cover - missing optional dep
                raise RuntimeError(
                    "the 'ddgs' package must be installed to use web search"
                ) from exc
        limit = max(1, int(max_results))
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=limit))
        out: list[SearchResult] = []
        for r in raw:
            out.append(
                SearchResult(
                    title=_clip(r.get("title", ""), _TITLE_CAP),
                    url=str(r.get("href") or r.get("url", "") or ""),
                    snippet=_clip(r.get("body", ""), _SNIPPET_CAP),
                )
            )
        return out


# ── LangSearch ──────────────────────────────────────────────────────────


class LangSearchProvider:
    """LangSearch hybrid web search (``POST /v1/web-search``).

    Maps each ``data.webPages.value[]`` row onto :class:`SearchResult`,
    preferring the long-text ``summary`` (when ``summary=true``) over the
    short ``snippet`` so the downstream distillation has richer context.
    Raises on transport error, non-2xx HTTP, or a non-200 ``code`` in the
    response envelope so :class:`FallbackProvider` can take over.
    """

    name = "langsearch"

    # Process-wide spacing gate. LangSearch caps at ~1 request/second, and
    # several independent callers (F1 / G3 / F9 workers + the brain's
    # web_search tool) can each build their own provider instance, so the
    # throttle state lives on the class — shared across every instance —
    # rather than per-instance. ``_last_request_monotonic`` is the start
    # time of the most recently *issued* request.
    _rate_lock = threading.Lock()
    _last_request_monotonic: float = 0.0

    def __init__(
        self,
        *,
        api_key: str,
        summary: bool = True,
        freshness: str = "noLimit",
        count: int = 10,
        timeout_seconds: float = 12.0,
        min_interval_seconds: float = 1.1,
    ) -> None:
        if not api_key:
            raise ValueError("LangSearchProvider requires a non-empty api_key")
        self._api_key = api_key
        self._summary = bool(summary)
        self._freshness = (
            freshness if freshness in _LANGSEARCH_FRESHNESS else "noLimit"
        )
        self._count = max(1, min(10, int(count)))
        self._timeout = max(1.0, float(timeout_seconds))
        self._min_interval = max(0.0, float(min_interval_seconds))

    def _throttle(self) -> None:
        """Block until at least ``_min_interval`` has passed since the last
        LangSearch request, then reserve this request's slot.

        Held inside the class lock so concurrent callers queue up and the
        issued requests stay spaced ~1/sec apart regardless of how many
        topics arrive at once.
        """
        if self._min_interval <= 0.0:
            return
        cls = LangSearchProvider
        with cls._rate_lock:
            now = time.monotonic()
            wait = self._min_interval - (now - cls._last_request_monotonic)
            if wait > 0:
                log.debug("langsearch throttle: sleeping %.2fs", wait)
                time.sleep(wait)
                now = time.monotonic()
            cls._last_request_monotonic = now

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        import requests

        count = max(1, min(self._count, int(max_results)))
        payload = {
            "query": query,
            "freshness": self._freshness,
            "summary": self._summary,
            "count": count,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        self._throttle()
        resp = requests.post(
            _LANGSEARCH_URL,
            json=payload,
            headers=headers,
            timeout=self._timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        if not isinstance(body, dict):
            raise ValueError("langsearch: unexpected response body")
        code = body.get("code")
        if code is not None and int(code) != 200:
            raise ValueError(
                f"langsearch: api error code={code} msg={body.get('msg')!r}"
            )
        return self._parse(body)

    @staticmethod
    def _parse(body: dict[str, Any]) -> list[SearchResult]:
        data = body.get("data")
        if not isinstance(data, dict):
            return []
        web_pages = data.get("webPages")
        if not isinstance(web_pages, dict):
            return []
        values = web_pages.get("value")
        if not isinstance(values, list):
            return []
        out: list[SearchResult] = []
        for item in values:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("displayUrl") or "")
            # Prefer the long-text summary; fall back to the short snippet.
            snippet_src = item.get("summary") or item.get("snippet") or ""
            out.append(
                SearchResult(
                    title=_clip(item.get("name", ""), _TITLE_CAP),
                    url=url,
                    snippet=_clip(snippet_src, _SNIPPET_CAP),
                )
            )
        return out


# ── fallback wrapper ────────────────────────────────────────────────────


class FallbackProvider:
    """Try ``primary`` first; on any error delegate to ``fallback``.

    Used to keep web search working when LangSearch errors out or its
    daily quota is exhausted — the keyless DuckDuckGo path takes over so
    the worker still gets results.
    """

    def __init__(self, primary: SearchProvider, fallback: SearchProvider) -> None:
        self._primary = primary
        self._fallback = fallback

    @property
    def name(self) -> str:
        prim = getattr(self._primary, "name", type(self._primary).__name__)
        fb = getattr(self._fallback, "name", type(self._fallback).__name__)
        return f"{prim}->{fb}"

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        try:
            return self._primary.search(query, max_results)
        except Exception as exc:
            log.warning(
                "search primary=%s failed (%s); falling back to %s",
                getattr(self._primary, "name", "?"),
                exc,
                getattr(self._fallback, "name", "?"),
            )
            return self._fallback.search(query, max_results)


# ── factory ─────────────────────────────────────────────────────────────


def resolve_api_key(api_key: str, api_key_env: str) -> str:
    """Resolve a credential: explicit value wins, else the named env var."""
    explicit = (api_key or "").strip()
    if explicit:
        return explicit
    env_name = (api_key_env or "").strip()
    if env_name:
        return (os.environ.get(env_name, "") or "").strip()
    return ""


def build_search_provider(settings: "SearchSettings | None") -> SearchProvider:
    """Pick a provider from settings.

    Returns DuckDuckGo when ``provider != "langsearch"`` or no API key is
    resolvable. Otherwise returns a LangSearch provider, wrapped in
    :class:`FallbackProvider` (LangSearch -> DuckDuckGo) when
    ``fallback_to_duckduckgo`` is on.
    """
    ddg = DuckDuckGoProvider()
    if settings is None:
        return ddg
    provider = (getattr(settings, "provider", "duckduckgo") or "duckduckgo").strip().lower()
    if provider != "langsearch":
        return ddg
    key = resolve_api_key(
        getattr(settings, "api_key", "") or "",
        getattr(settings, "api_key_env", "") or "",
    )
    if not key:
        log.warning(
            "search provider=langsearch but no API key resolved; "
            "using duckduckgo"
        )
        return ddg
    langsearch = LangSearchProvider(
        api_key=key,
        summary=bool(getattr(settings, "langsearch_summary", True)),
        freshness=str(getattr(settings, "langsearch_freshness", "noLimit")),
        count=int(getattr(settings, "langsearch_count", 10)),
        timeout_seconds=float(getattr(settings, "timeout_seconds", 12.0)),
        min_interval_seconds=float(
            getattr(settings, "langsearch_min_interval_seconds", 1.1)
        ),
    )
    if bool(getattr(settings, "fallback_to_duckduckgo", True)):
        return FallbackProvider(langsearch, ddg)
    return langsearch


__all__ = [
    "SearchResult",
    "SearchProvider",
    "DuckDuckGoProvider",
    "LangSearchProvider",
    "FallbackProvider",
    "build_search_provider",
    "resolve_api_key",
]
