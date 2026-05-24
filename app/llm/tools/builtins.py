"""Built-in tools shipped with Aiko."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from app.llm.tools.base import Tool, ToolError, ToolSchema


log = logging.getLogger("app.tools.builtins")


# ── get_time ────────────────────────────────────────────────────────────────


class GetTimeTool:
    """Return the current date / time. Call when the user asks "what time
    is it" / "what day is it" / scheduling questions."""

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="get_time",
            description=(
                "Return the current local date and time. Call this whenever "
                "the user asks about the current time, today's date, the "
                "day of the week, or anything similarly time-sensitive."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": (
                            "Optional IANA tz name, e.g. 'Europe/Warsaw'. "
                            "Leave empty to use the system local zone."
                        ),
                    },
                },
                "required": [],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        tz_name = (arguments.get("timezone") or "").strip()
        try:
            if tz_name:
                from zoneinfo import ZoneInfo

                now = datetime.now(ZoneInfo(tz_name))
            else:
                now = datetime.now().astimezone()
        except Exception as exc:
            raise ToolError(f"unknown timezone {tz_name!r}: {exc}") from exc
        return json.dumps(
            {
                "iso": now.isoformat(),
                "weekday": now.strftime("%A"),
                "human": now.strftime("%A, %B %d %Y at %H:%M %Z").strip(),
            },
            ensure_ascii=False,
        )


# ── recall ──────────────────────────────────────────────────────────────────


class RecallTool:
    """Search Aiko's long-term retrieval substrate (memories + chat history
    + uploaded documents) for snippets relevant to a query.
    """

    def __init__(self, rag_retriever: Any) -> None:
        self._rag = rag_retriever

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="recall",
            description=(
                "Search Aiko's long-term notebook for things she should "
                "remember. Use this when the user asks 'do you remember...', "
                "'what did I say about...', or you need to ground a reply "
                "in something specific Jacob shared earlier."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The semantic query to search for.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of snippets to return (1-12). Defaults to 6.",
                        "minimum": 1,
                        "maximum": 12,
                    },
                },
                "required": ["query"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        query = (arguments.get("query") or "").strip()
        if not query:
            raise ToolError("recall: 'query' is required and must be non-empty")
        try:
            limit = int(arguments.get("limit", 6))
        except (TypeError, ValueError):
            limit = 6
        limit = max(1, min(12, limit))
        if self._rag is None:
            raise ToolError("recall: retrieval store is not available")
        try:
            hits = self._rag.retrieve(query)
        except Exception as exc:
            raise ToolError(f"recall failed: {exc}") from exc
        if not hits:
            return json.dumps({"hits": [], "note": "no relevant memories"})
        out = []
        for hit in hits[:limit]:
            out.append({
                "source": hit.source,
                "score": round(float(hit.score), 3),
                "text": (hit.text or "")[:280],
            })
        return json.dumps({"hits": out}, ensure_ascii=False)


# ── web_search ──────────────────────────────────────────────────────────────


class WebSearchTool:
    """DuckDuckGo HTML search. Returns top results so the LLM can ground a
    reply in current info (news, prices, recent releases, etc.).
    """

    def __init__(self) -> None:
        # Import lazily so the module load doesn't fail when ddg isn't
        # installed -- the registry will skip registration in that case.
        try:
            from duckduckgo_search import DDGS  # type: ignore
        except Exception as exc:  # pragma: no cover -- missing optional dep
            raise RuntimeError(
                "duckduckgo-search must be installed to use the web_search tool"
            ) from exc
        self._ddgs_cls = DDGS

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="web_search",
            description=(
                "Search the public web (DuckDuckGo). Use this for current "
                "events, news, prices, recently released software, sports "
                "scores, and other facts that change after your training "
                "cutoff. Don't use it for things you can answer from "
                "general knowledge or from Aiko's notebook."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max number of results to return (1-8). Defaults to 5.",
                        "minimum": 1,
                        "maximum": 8,
                    },
                },
                "required": ["query"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        query = (arguments.get("query") or "").strip()
        if not query:
            raise ToolError("web_search: 'query' is required")
        try:
            limit = int(arguments.get("max_results", 5))
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(8, limit))
        try:
            with self._ddgs_cls() as ddgs:
                raw = list(ddgs.text(query, max_results=limit))
        except Exception as exc:
            raise ToolError(f"web_search failed: {exc}") from exc
        if not raw:
            return json.dumps({"results": [], "note": "no results"})
        results = []
        for r in raw:
            results.append({
                "title": str(r.get("title", "") or "")[:160],
                "url": str(r.get("href") or r.get("url", "") or ""),
                "snippet": str(r.get("body", "") or "")[:280],
            })
        return json.dumps({"results": results}, ensure_ascii=False)
