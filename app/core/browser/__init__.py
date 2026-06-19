"""Browser perception layer.

A server-agnostic middleware that turns a raw accessibility-tree snapshot
(from *any* MCP browser server) into a compact, deduped, form-grouped,
heading-contextualized, heuristically ranked, change-diffed page model
for the background workflow planner.

The only format-specific piece is the per-server **adapter** in
:mod:`app.core.browser.adapters` (raw tool text -> normalized
:class:`~app.core.browser.accessibility.A11yNode` list). Everything
downstream (grouping / ranking / page-state memory / rendering) consumes
the normalized schema and never changes when the MCP server is swapped.
"""
from __future__ import annotations

from app.core.browser.accessibility import A11yNode
from app.core.browser.perception import BrowserPerception, PerceptionResult

__all__ = ["A11yNode", "BrowserPerception", "PerceptionResult"]
