"""Built-in tools shipped with Aiko."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from app.llm.tools.base import Tool, ToolError, ToolSchema
from app.core.infra import timephrase


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
                now = timephrase.now()
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
                "in something specific the user shared earlier."
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


# ── recall_topic ─────────────────────────────────────────────────────────────


class RecallTopicTool:
    """Browse everything Aiko knows about one coherent topic / theme.

    Where :class:`RecallTool` does a global semantic search for the few
    closest snippets, this tool (F10d cluster-scoped recall) matches the
    query to a whole **topic cluster** of the memory graph and returns that
    cluster's members. It answers "what do I actually know about X?" by
    enumerating one theme rather than the single best line — use it when the
    user asks Aiko to gather / summarise / list what she remembers about a
    subject, not for a one-off "did I mention Y?" lookup.
    """

    def __init__(self, rag_retriever: Any) -> None:
        self._rag = rag_retriever

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="recall_topic",
            description=(
                "Pull up everything Aiko remembers about one topic or theme "
                "(a whole cluster of related notes), not just the single "
                "closest line. Use this when the user asks her to round up / "
                "summarise / go over what she knows about a subject (e.g. "
                "'what do you remember about my job?', 'tell me everything "
                "you know about my trip')."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "The topic / theme to gather notes about.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of notes to return (1-15). Defaults to 8.",
                        "minimum": 1,
                        "maximum": 15,
                    },
                },
                "required": ["topic"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        topic = (arguments.get("topic") or "").strip()
        if not topic:
            raise ToolError("recall_topic: 'topic' is required and must be non-empty")
        try:
            limit = int(arguments.get("limit", 8))
        except (TypeError, ValueError):
            limit = 8
        limit = max(1, min(15, limit))
        if self._rag is None or not hasattr(self._rag, "recall_topic"):
            raise ToolError("recall_topic: retrieval store is not available")
        try:
            label, hits = self._rag.recall_topic(topic, limit=limit)
        except Exception as exc:
            raise ToolError(f"recall_topic failed: {exc}") from exc
        if not hits:
            return json.dumps(
                {"topic_label": label, "hits": [], "note": "no matching topic cluster"},
                ensure_ascii=False,
            )
        out = []
        for hit in hits:
            out.append({
                "score": round(float(hit.score), 3),
                "text": (hit.text or "")[:280],
            })
        return json.dumps(
            {"topic_label": label, "hits": out}, ensure_ascii=False
        )


# ── recall_concept ───────────────────────────────────────────────────────────


class RecallConceptTool:
    """Look up one higher-order thing Aiko has come to understand -- about
    the user *or* about herself -- bundled with the evidence behind it.

    Where :class:`RecallTopicTool` gathers the raw notes in a topic
    cluster, this L5 tool pulls up a *concept* -- an abstraction Aiko has
    synthesised across many clusters/memories (e.g. "enjoys understanding
    systems", or her own "I tend to deflect with teasing when I feel
    exposed") -- together with its confidence, the memories that support
    it, and the topic areas it draws on, all in one call. It searches
    across subjects, so it answers both "what have you figured out about
    me?" and self-directed "what are you like? / what do you value? / have
    you changed?" questions; the returned bundle says whose concept it is.
    Not for a raw fact lookup. Set ``all_evidence`` when the full
    supporting set is wanted rather than a few representative notes.
    """

    def __init__(self, rag_retriever: Any) -> None:
        self._rag = rag_retriever

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="recall_concept",
            description=(
                "Explain WHY Aiko thinks/feels/believes something -- pull up "
                "one higher-order concept she has formed (a trait, value, or "
                "pattern) together with its reasoning ('rationale'), the "
                "memories that support it, the topic areas it spans, AND the "
                "related concepts it connects to (tensions it sits in, "
                "concepts it generalises), all in one call. Reach for this "
                "whenever: the user asks *why* she thinks/feels/believes "
                "something about them or herself; asks her to explain or "
                "justify an impression or characterization; asks 'what am I "
                "like / what do you value / have we changed?'; or she is "
                "about to assert a characterization and wants the grounding "
                "first. It is the tool for 'explain your thinking', not just "
                "fact lookup -- prefer it over recall / recall_topic when the "
                "question is about a settled understanding or self-image "
                "rather than a one-off fact. Works for concepts about the "
                "USER and about HERSELF; the result says whose it is. Set "
                "'all_evidence' true to get the full supporting set instead "
                "of a few representative notes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "What to look up a concept about -- about the "
                            "user (e.g. 'how I approach problems', 'my "
                            "values') or about Aiko herself (e.g. 'what I'm "
                            "like', 'what I value', 'how I've changed')."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Max supporting notes to return (1-15). "
                            "Defaults to 8. Ignored when all_evidence is set."
                        ),
                        "minimum": 1,
                        "maximum": 15,
                    },
                    "all_evidence": {
                        "type": "boolean",
                        "description": (
                            "Return every supporting memory for the concept "
                            "instead of a capped sample. Defaults to false."
                        ),
                    },
                },
                "required": ["query"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        query = (arguments.get("query") or "").strip()
        if not query:
            raise ToolError(
                "recall_concept: 'query' is required and must be non-empty"
            )
        try:
            limit = int(arguments.get("limit", 8))
        except (TypeError, ValueError):
            limit = 8
        limit = max(1, min(15, limit))
        all_evidence = bool(arguments.get("all_evidence", False))
        if self._rag is None or not hasattr(self._rag, "recall_concept"):
            raise ToolError("recall_concept: retrieval store is not available")
        try:
            bundle = self._rag.recall_concept(
                query, limit=limit, all_evidence=all_evidence
            )
        except Exception as exc:
            raise ToolError(f"recall_concept failed: {exc}") from exc
        if not bundle:
            return json.dumps(
                {"concept": None, "note": "no matching concept"},
                ensure_ascii=False,
            )
        return json.dumps(bundle, ensure_ascii=False)


# ── web_search ──────────────────────────────────────────────────────────────


_WEB_SEARCH_SNIPPET_CAP = 600


class WebSearchTool:
    """Web search wrapper used by the background workers.

    Delegates the actual lookup to a pluggable
    :class:`app.llm.search.providers.SearchProvider` (DuckDuckGo by
    default, LangSearch when configured), then re-shapes the hits into
    the ``{"results": [{title, url, snippet}]}`` JSON the F1 / G3 / F9
    workers parse. The snippet cap is generous (600 chars) so a
    LangSearch long-text summary survives to the worker, which applies
    its own tighter cap.
    """

    def __init__(self, provider: "Any | None" = None) -> None:
        # Provider is injected by SessionController; default to the
        # keyless DuckDuckGo backend so a bare ``WebSearchTool()`` (and
        # any test) keeps working. Construction never raises — a missing
        # ``duckduckgo-search`` dependency surfaces at search time.
        if provider is None:
            from app.llm.search.providers import DuckDuckGoProvider

            provider = DuckDuckGoProvider()
        self._provider = provider

    def set_provider(self, provider: "Any") -> None:
        """Swap the backend live (used by ``reconfigure_search``)."""
        self._provider = provider

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
            hits = self._provider.search(query, limit)
        except Exception as exc:
            raise ToolError(f"web_search failed: {exc}") from exc
        if not hits:
            return json.dumps({"results": [], "note": "no results"})
        results = []
        for r in hits:
            results.append({
                "title": str(getattr(r, "title", "") or "")[:160],
                "url": str(getattr(r, "url", "") or ""),
                "snippet": str(getattr(r, "snippet", "") or "")[
                    :_WEB_SEARCH_SNIPPET_CAP
                ],
            })
        return json.dumps({"results": results}, ensure_ascii=False)
