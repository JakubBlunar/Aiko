"""BrowserPerception — the server-agnostic snapshot middleware.

Sits over an MCP browser server's accessibility-snapshot tool: parse via
the configured adapter, then dedup -> heading-context -> form-group ->
rank -> diff-vs-previous -> render. Best-effort: any failure returns
``None`` so the caller falls back to the raw tool output and browsing
never breaks.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from app.core.browser.accessibility import A11yNode
from app.core.browser.adapters import get_adapter
from app.core.browser.grouping import dedup_nodes, group_forms, heading_context
from app.core.browser.page_state import PageStateMemory
from app.core.browser.ranking import RankingWeights, rank_elements
from app.core.browser.rendering import render_page


log = logging.getLogger("app.browser.perception")


@dataclass(frozen=True, slots=True)
class PerceptionResult:
    content: str
    summary: str
    element_count: int


_PAGE_KEY_ARGS = ("url", "tab", "tab_id", "tabId", "target")


class BrowserPerception:
    """Reshapes a browser snapshot result for the workflow planner."""

    def __init__(
        self,
        *,
        enabled: bool,
        server_id: str,
        snapshot_tools: tuple[str, ...],
        adapter: str,
        max_ranked_elements: int,
        weights: RankingWeights,
        state_memory_pages: int,
    ) -> None:
        self._enabled = bool(enabled)
        self._server_id = server_id
        self._snapshot_tools = frozenset(snapshot_tools)
        self._adapter_name = adapter
        self._adapter = get_adapter(adapter)
        self._max_ranked = max(1, int(max_ranked_elements))
        self._weights = weights
        self._memory = PageStateMemory(max_pages=state_memory_pages)
        self._lock = threading.Lock()
        self._last_summary: str = ""
        self._transform_count = 0

    @classmethod
    def from_settings(cls, settings: Any) -> "BrowserPerception":
        return cls(
            enabled=settings.enabled,
            server_id=settings.server_id,
            snapshot_tools=tuple(settings.snapshot_tools),
            adapter=settings.adapter,
            max_ranked_elements=settings.max_ranked_elements,
            weights=RankingWeights(
                role=settings.weight_role,
                visibility=settings.weight_visibility,
                position=settings.weight_position,
                text=settings.weight_text,
                context=settings.weight_context,
            ),
            state_memory_pages=settings.state_memory_pages,
        )

    # ── public API ───────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def server_id(self) -> str:
        return self._server_id

    @property
    def snapshot_tools(self) -> tuple[str, ...]:
        return tuple(sorted(self._snapshot_tools))

    def claims(self, server_id: str, tool_name: str) -> bool:
        """True when this result should be reshaped by the perception layer."""
        return (
            self._enabled
            and server_id == self._server_id
            and tool_name in self._snapshot_tools
        )

    def transform(
        self,
        server_id: str,
        tool_name: str,
        raw_text: str,
        tool_args: dict[str, Any] | None = None,
    ) -> PerceptionResult | None:
        """Reshape a snapshot. Returns ``None`` on any failure / non-claim."""
        if not self.claims(server_id, tool_name):
            return None
        try:
            nodes = self._adapter.parse(raw_text)
        except Exception:
            log.debug("perception: adapter raised", exc_info=True)
            return None
        if nodes is None:
            log.debug("perception: adapter could not parse, raw passthrough")
            return None
        try:
            return self._build(nodes, tool_args or {})
        except Exception:
            log.exception("perception: pipeline failed, raw passthrough")
            return None

    # ── internals ────────────────────────────────────────────────────

    def _build(
        self, nodes: list[A11yNode], tool_args: dict[str, Any]
    ) -> PerceptionResult:
        deduped = dedup_nodes(nodes)
        context_map = heading_context(deduped)
        forms = group_forms(deduped, context_map)
        ranked = rank_elements(deduped, context_map, self._weights, self._max_ranked)

        title = self._page_title(deduped, tool_args)
        page_key = self._page_key(title, tool_args)
        with self._lock:
            diff = self._memory.update_and_diff(page_key, deduped)
            self._transform_count += 1

        content, summary = render_page(
            title, ranked, forms, diff, total_nodes=len(nodes)
        )
        with self._lock:
            self._last_summary = summary
        log.info(
            "browser-perception: nodes=%d deduped=%d ranked=%d forms=%d page=%r",
            len(nodes),
            len(deduped),
            len(ranked),
            len(forms),
            page_key[:60],
        )
        return PerceptionResult(
            content=content, summary=summary, element_count=len(ranked)
        )

    @staticmethod
    def _page_title(nodes: list[A11yNode], tool_args: dict[str, Any]) -> str:
        url = str(tool_args.get("url", "") or "").strip()
        if url:
            return url
        for node in nodes:
            if node.is_heading and node.name.strip():
                return node.name.strip()
        return "active page"

    @staticmethod
    def _page_key(title: str, tool_args: dict[str, Any]) -> str:
        for key in _PAGE_KEY_ARGS:
            val = tool_args.get(key)
            if val not in (None, ""):
                return str(val)
        return title or "active"

    def debug_state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "server_id": self._server_id,
                "snapshot_tools": sorted(self._snapshot_tools),
                "adapter": self._adapter_name,
                "max_ranked_elements": self._max_ranked,
                "memory_pages": len(self._memory),
                "transform_count": self._transform_count,
                "last_summary": self._last_summary,
            }


__all__ = ["BrowserPerception", "PerceptionResult"]
