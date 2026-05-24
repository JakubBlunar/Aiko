"""Lean tool calling for Aiko.

Tools are tiny, single-purpose Python callables exposed to the LLM via the
Ollama tool-calling protocol. Each tool registers its JSON schema with
:class:`ToolRegistry`; the registry can then build the schema list expected
by ``chat_with_tools`` and dispatch incoming tool calls back to the
implementation.

Three tools ship today:
  - ``get_time`` -- the current local date/time.
  - ``recall``   -- semantic search over the RAG store (memories + messages
                    + documents).
  - ``web_search`` -- DuckDuckGo HTML search; returns top hits as a short
                      title/snippet/url list.

The :class:`TurnRunner` two-pass loop calls ``chat_with_tools`` first; if
the model emits tool calls, we dispatch them, append the results, and then
do the streaming chat for the final spoken reply.
"""
from __future__ import annotations

from app.llm.tools.base import (
    Tool,
    ToolError,
    ToolRegistry,
    ToolResult,
    ToolSchema,
    build_default_registry,
)

__all__ = [
    "Tool",
    "ToolError",
    "ToolRegistry",
    "ToolResult",
    "ToolSchema",
    "build_default_registry",
]
