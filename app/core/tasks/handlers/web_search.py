"""Web-search task handler — the background DuckDuckGo lookup.

This is the nested-workflow home for web search. The original
``WebSearchTool`` (``app/llm/tools/builtins.py``) was a brain builtin:
the chat LLM could call it mid-turn. That was the wrong lane — a DDG
HTML round-trip routinely takes seconds, which stalls the fast
conversational reply Aiko is supposed to give. Web search now lives
*only* as a :class:`WorkflowSkill` child of a goal workflow, where the
latency is expected and the result is folded into an aggregated reply.

Why a task handler rather than calling ``DDGS`` inline from the skill
spawn function?

* Uniformity — every workflow child is a real task row with its own
  lifecycle, heartbeat, cancel path, and event log. The planner /
  handler loop treats ``web_search`` exactly like ``file_search``.
* Cancellation — a long DDG call can be abandoned when the parent
  workflow is cancelled (the cascade-cancel path fires
  ``handler.cancel``).
* Observability — the search shows up in the TasksTab tree under its
  parent, same as file children.

Threading model matches :class:`FileSearchHandler`: the handler runs
synchronously on a worker thread; the single network call is bounded
by the ``duckduckgo_search`` client's own timeouts. ``cancel`` is the
no-op cleanup path (the request, if in flight, is left to time out —
the orchestrator has already marked the row cancelled and any late
emit is suppressed).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.core.tasks.handler_names import HANDLER_WEB_SEARCH
from app.core.tasks.task_handler import (
    TaskCompleted,
    TaskEmitFn,
    TaskFailed,
    TaskState,
)


log = logging.getLogger("app.tasks.web_search")


DEFAULT_MAX_RESULTS = 5
_MAX_RESULTS_CEIL = 10
_TITLE_CAP = 160
_SNIPPET_CAP = 280


@dataclass(frozen=True, slots=True)
class _SearchArgs:
    """Validated form of the ``args`` dict passed to ``start``."""

    query: str
    max_results: int


def _parse_args(args: dict[str, Any]) -> _SearchArgs | str:
    """Validate ``args``; return a parsed object or a short error string."""
    query = (args or {}).get("query", "") or ""
    if not isinstance(query, str):
        return "query must be a string"
    query = query.strip()
    if not query:
        return "query is empty"
    try:
        raw = int((args or {}).get("max_results", DEFAULT_MAX_RESULTS))
    except (TypeError, ValueError):
        raw = DEFAULT_MAX_RESULTS
    max_results = max(1, min(_MAX_RESULTS_CEIL, raw))
    return _SearchArgs(query=query, max_results=max_results)


def _summary_text(query: str, results: list[dict[str, Any]]) -> str:
    """One-line cue summary lifted by the orchestrator's ``_summary_text``."""
    if not results:
        return f"web search '{query[:60]}': no results"
    top = results[0].get("title") or results[0].get("url") or ""
    return (
        f"web search '{query[:48]}': {len(results)} results "
        f"(top: {str(top)[:80]})"
    )


class WebSearchHandler:
    """Read-only DuckDuckGo HTML search as a background task.

    Construction is cheap and dependency-soft: the ``duckduckgo_search``
    import is deferred to ``start`` so a build without the optional dep
    still imports this module (the workflow skill registry skips the
    ``web_search`` skill when the dep is missing). ``max_results`` is a
    per-handler ceiling fed from settings; the per-call arg clamps under
    it.
    """

    name: str = HANDLER_WEB_SEARCH

    def __init__(self, *, max_results: int = DEFAULT_MAX_RESULTS) -> None:
        self._max_results = max(1, min(_MAX_RESULTS_CEIL, int(max_results)))

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self, args: dict[str, Any], emit: TaskEmitFn) -> TaskState:
        parsed = _parse_args(args)
        if isinstance(parsed, str):
            emit(TaskFailed(error=parsed))
            return {"args": args, "phase": "rejected"}
        # Clamp the per-call cap under the handler ceiling.
        max_results = max(1, min(self._max_results, parsed.max_results))
        try:
            from duckduckgo_search import DDGS  # type: ignore
        except Exception:
            emit(
                TaskFailed(
                    error=(
                        "web search is unavailable (duckduckgo-search not "
                        "installed)"
                    )
                )
            )
            log.warning("web_search: duckduckgo-search import failed")
            return {"args": args, "phase": "rejected"}
        try:
            with DDGS() as ddgs:
                raw = list(ddgs.text(parsed.query, max_results=max_results))
        except Exception as exc:
            emit(TaskFailed(error=f"web search failed: {exc}"[:200]))
            log.info("web_search: query=%r failed: %s", parsed.query, exc)
            return {"args": args, "phase": "rejected"}
        results: list[dict[str, Any]] = []
        for r in raw:
            results.append(
                {
                    "title": str(r.get("title", "") or "")[:_TITLE_CAP],
                    "url": str(r.get("href") or r.get("url", "") or ""),
                    "snippet": str(r.get("body", "") or "")[:_SNIPPET_CAP],
                }
            )
        result = {
            "query": parsed.query,
            "result_count": len(results),
            "results": results,
            "summary": _summary_text(parsed.query, results),
        }
        log.info(
            "web_search: completed query=%r results=%d",
            parsed.query,
            len(results),
        )
        emit(TaskCompleted(result=result))
        return {"args": args, "phase": "done", "result_count": len(results)}

    def resume(self, state: TaskState, emit: TaskEmitFn) -> TaskState:
        # A web search left running at restart is demoted to
        # ``interrupted`` by boot recovery; emit a graceful failure so
        # the row reaches a terminal state rather than hanging.
        emit(
            TaskFailed(
                error="web_search does not support resume; restart the search"
            )
        )
        return state

    def on_input(
        self, state: TaskState, answer: str, emit: TaskEmitFn
    ) -> TaskState:
        # Web search never asks for input — defensive terminal.
        emit(TaskFailed(error="web_search does not accept input"))
        return state

    def cancel(self, state: TaskState) -> None:
        # The single DDG call has already returned (or is left to time
        # out) by the time the orchestrator marks the row cancelled.
        return None


__all__ = ["WebSearchHandler", "DEFAULT_MAX_RESULTS"]
