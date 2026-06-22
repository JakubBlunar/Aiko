"""Pluggable web-search backends.

Aiko's web search has two consumers: the worker-facing
:class:`~app.llm.tools.builtins.WebSearchTool` (F1 fact-checker, G3
curiosity worker, F9 knowledge worker) and the background
:class:`~app.core.tasks.handlers.web_search.WebSearchHandler` (goal
workflow lane). Both used to talk to DuckDuckGo directly. This package
factors the network call behind a small :class:`SearchProvider`
protocol so the backend can be swapped (DuckDuckGo with no key, or
LangSearch when an API key is configured) without touching either
consumer.
"""
from __future__ import annotations

from app.llm.search.providers import (
    DuckDuckGoProvider,
    FallbackProvider,
    LangSearchProvider,
    SearchProvider,
    SearchResult,
    build_search_provider,
)

__all__ = [
    "SearchResult",
    "SearchProvider",
    "DuckDuckGoProvider",
    "LangSearchProvider",
    "FallbackProvider",
    "build_search_provider",
]
