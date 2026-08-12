"""D3 — the synchronous ``web_search`` tool on the conversational lane.

Distinct from the worker-facing :class:`app.llm.tools.builtins.WebSearchTool`
in three ways, all of which follow from "a user is waiting through this
round-trip":

* **Tighter result shape.** Three hits at 400 characters instead of five
  at 600. Measured against LangSearch, five hits cost ~960 tokens of the
  turn's prompt and their long-text summaries were being truncated
  mid-sentence anyway; three at 400 costs ~450 and reads cleaner.
* **A privacy boundary on the outbound query.** The query is written by
  the chat model, which composes it with the persona, retrieved memories
  and the live transcript in view — so "does my girlfriend's favourite
  show have a season 3" is a query it can plausibly emit. Everything
  here goes to a third party, so the query passes through the same
  scrubber the background fact-checker uses
  (:func:`app.core.memory.fact_check_privacy.scrub_claim_for_search`)
  before it leaves the process. A query that cannot be made safe comes
  back as a :class:`ToolError` telling the model to rephrase, rather
  than being silently mangled into a different search.
* **A results sink.** The raw hits are handed to an optional callback so
  the session can distil them into a ``knowledge`` memory *after* the
  turn (D3 write-back), keeping the reply itself free of that cost.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from typing import Any

from app.llm.tools.base import ToolError, ToolSchema


log = logging.getLogger("app.tools.web_search_brain")


# Chat-lane result shape. Deliberately smaller than the worker tool's
# 5 x 600: see the module docstring for the measurement behind it.
_DEFAULT_MAX_RESULTS = 3
_MAX_MAX_RESULTS = 5
_SNIPPET_CAP = 400

# The sink receives ``(query, [{title, url, snippet}, ...])``.
ResultsSink = Callable[[str, list[dict[str, str]]], None]


class BrainWebSearchTool:
    """``web_search`` for the live turn: scrubbed query, small result set."""

    def __init__(
        self,
        provider: Any,
        *,
        user_names_provider: Callable[[], Sequence[str]] | None = None,
        assistant_name_provider: Callable[[], str] | None = None,
        on_results: ResultsSink | None = None,
    ) -> None:
        self._provider = provider
        self._user_names_provider = user_names_provider
        self._assistant_name_provider = assistant_name_provider
        self._on_results = on_results

    def set_provider(self, provider: Any) -> None:
        """Swap the backend live (used by ``reconfigure_search``)."""
        self._provider = provider

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="web_search",
            description=(
                "Look something up on the public web, synchronously, and get "
                "the results back before you reply. Call this when you are "
                "genuinely unsure about a fact that can be checked and that "
                "may have changed since your training cutoff: a new anime "
                "season or episode count, a release date, whether something "
                "was announced, current events, prices, standings. Do NOT "
                "call it for anything you already know, for anything about "
                "the user's own life (that lives in your notebook — use the "
                "recall tools), or to keep a casual conversation going. It "
                "costs the user a few seconds of waiting, so only reach for "
                "it when the answer actually matters and you'd otherwise be "
                "guessing."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "A standalone search query, written the way you "
                            "would type it into a search engine. It leaves "
                            "this machine and goes to a third-party search "
                            "API, so it must contain no personal details: no "
                            "names of anyone in this conversation, no 'my' / "
                            "'we' / 'our', nothing about the user's life, "
                            "job, health or location. Name the topic itself "
                            "instead — 'Dandadan season 2 episode count', "
                            "not 'the anime my girlfriend likes'."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": (
                            f"How many results to return (1-{_MAX_MAX_RESULTS}). "
                            f"Defaults to {_DEFAULT_MAX_RESULTS}; ask for more "
                            "only when you need to cross-check."
                        ),
                        "minimum": 1,
                        "maximum": _MAX_MAX_RESULTS,
                    },
                },
                "required": ["query"],
            },
        )

    # ── privacy boundary ─────────────────────────────────────────────

    def _safe_query(self, query: str) -> str:
        """Return a publishable form of ``query`` or raise ``ToolError``.

        The scrubber is the enforcement half of the schema's instruction:
        the description asks the model for a clean query, this makes a
        slip non-fatal. Names and first-person tokens are dropped and the
        rest of the query survives (search engines treat a placeholder
        like ``<user>`` as a literal term, so removal beats substitution);
        hard identifiers — a URL, an email, an address — refuse outright.
        """
        from app.core.memory.fact_check_privacy import scrub_claim_for_search

        user_names: list[str] | None = None
        if self._user_names_provider is not None:
            try:
                provided = self._user_names_provider()
                if provided:
                    user_names = list(provided)
            except Exception:
                user_names = None
        assistant_name: str | None = None
        if self._assistant_name_provider is not None:
            try:
                assistant_name = self._assistant_name_provider() or None
            except Exception:
                assistant_name = None

        safe = scrub_claim_for_search(
            query, user_names=user_names, assistant_name=assistant_name,
        )
        if not safe:
            raise ToolError(
                "web_search: that query can't be sent to a search engine "
                "because it carries personal details. Rewrite it as a "
                "standalone topic query — name the thing itself, with no "
                "names, no 'my'/'we', and no personal context — or answer "
                "from what you already know."
            )
        return safe

    # ── dispatch ─────────────────────────────────────────────────────

    def run(self, arguments: dict[str, Any]) -> str:
        query = (arguments.get("query") or "").strip()
        if not query:
            raise ToolError("web_search: 'query' is required")
        safe_query = self._safe_query(query)
        try:
            limit = int(arguments.get("max_results", _DEFAULT_MAX_RESULTS))
        except (TypeError, ValueError):
            limit = _DEFAULT_MAX_RESULTS
        limit = max(1, min(_MAX_MAX_RESULTS, limit))

        try:
            hits = self._provider.search(safe_query, limit)
        except Exception as exc:
            # No DuckDuckGo fallback on this lane, so a provider failure
            # is terminal for the turn. Surfaced as a ToolError so the
            # model narrates "I couldn't reach the web" instead of
            # inventing the answer it was about to look up.
            raise ToolError(f"web_search failed: {exc}") from exc

        results: list[dict[str, str]] = []
        for hit in hits:
            snippet = str(getattr(hit, "snippet", "") or "")[:_SNIPPET_CAP]
            if not snippet:
                continue
            results.append({
                "title": str(getattr(hit, "title", "") or "")[:160],
                "url": str(getattr(hit, "url", "") or "")[:200],
                "snippet": snippet,
            })
        log.info(
            "brain web_search: q=%r results=%d",
            safe_query[:120], len(results),
        )
        if not results:
            return json.dumps({"results": [], "note": "no results"})

        if self._on_results is not None:
            try:
                self._on_results(safe_query, [dict(r) for r in results])
            except Exception:
                log.debug("web_search results sink failed", exc_info=True)
        return json.dumps({"results": results}, ensure_ascii=False)


__all__ = ["BrainWebSearchTool"]
