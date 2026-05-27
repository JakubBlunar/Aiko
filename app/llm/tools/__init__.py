"""Lean tool calling for Aiko.

Tools are tiny, single-purpose Python callables exposed to the LLM via the
Ollama tool-calling protocol. Each tool registers its JSON schema with
:class:`ToolRegistry`; the registry can then build the schema list expected
by ``chat_with_tools`` and dispatch incoming tool calls back to the
implementation.

Two tool categories ship today, registered conditionally in
:meth:`SessionController.rebuild_tool_registry` based on
``config.tools.*`` flags:

**Fact tools** (``app/llm/tools/builtins.py``):
  - ``get_time`` -- the current local date/time/day of week.
  - ``recall``   -- semantic search over the RAG store (memories + messages
                    + documents).
  - ``web_search`` -- DuckDuckGo HTML search; returns top hits as a short
                      title/snippet/url list.

**World / room tools** (``app/llm/tools/world.py``), built by
:func:`app.llm.tools.world.build_world_tools` against the live
:class:`WorldStore`:
  - ``look_around`` -- snapshot of current spot + nearby items (read-only).
  - ``move_to``     -- relocate Aiko to a different room location.
  - ``change_posture`` -- update posture + activity.
  - ``inspect_item`` -- detailed read of one item.
  - ``consume_item`` -- decrement a consumable (cookies / tea / ...).

The :class:`TurnRunner` two-pass loop calls ``chat_with_tools`` first; if
the model emits tool calls, we dispatch them, append the results, and then
do the streaming chat for the final spoken reply.

:func:`build_default_registry` only assembles the fact tools (it's the
factory used by tests and lightweight harnesses). The full 8-tool
catalogue is built by :meth:`SessionController.rebuild_tool_registry`
because the world tools need a live ``SessionController`` to wire
``WorldStore`` access.
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
