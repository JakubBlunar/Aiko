"""Pluggable web-search backends.

Aiko's web search has three consumers: the worker-facing
:class:`~app.llm.tools.builtins.WebSearchTool` (F1 fact-checker, G3
curiosity worker, F9 knowledge worker), the background
:class:`~app.core.tasks.handlers.web_search.WebSearchHandler` (goal
workflow lane), and the D3 brain-lane
:class:`~app.llm.tools.web_search_brain.BrainWebSearchTool` (the
synchronous tool on a conversational turn). All three used to talk to
DuckDuckGo directly. This package factors the network call behind a
small :class:`SearchProvider` protocol so the backend can be swapped
(DuckDuckGo with no key, or LangSearch when an API key is configured)
without touching any consumer.

The first two share one provider built by :func:`build_search_provider`;
the brain lane gets its own from :func:`build_brain_search_provider`,
which drops the DuckDuckGo fallback and uses a shorter timeout because a
user is waiting on it.
"""
from __future__ import annotations

from app.llm.search.providers import (
    DuckDuckGoProvider,
    FallbackProvider,
    LangSearchProvider,
    SearchProvider,
    SearchResult,
    build_brain_search_provider,
    build_search_provider,
)

__all__ = [
    "SearchResult",
    "SearchProvider",
    "DuckDuckGoProvider",
    "LangSearchProvider",
    "FallbackProvider",
    "build_search_provider",
    "build_brain_search_provider",
]
