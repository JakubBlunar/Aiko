"""Tool-registry base classes used by :mod:`app.llm.tools`."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Protocol


log = logging.getLogger("app.tools")


class ToolError(Exception):
    """Raised by a tool implementation to surface a clean error to the LLM.

    The string is forwarded back to the model verbatim as the tool result.
    """


@dataclass(slots=True)
class ToolSchema:
    """Ollama-shaped tool descriptor.

    Mirrors the JSON OpenAI tools format that Ollama accepts. ``parameters``
    is a JSON-Schema object describing the call arguments.
    """

    name: str
    description: str
    parameters: dict[str, Any]

    def to_ollama(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(slots=True)
class ToolResult:
    """Result of dispatching a single tool call.

    ``content`` is the (already-formatted) string the LLM will receive as
    the ``tool`` message body. Long results should be summarized by the tool
    itself, not the runner.
    """

    name: str
    call_id: str
    content: str
    ok: bool = True


class Tool(Protocol):
    """A callable tool implementation.

    Two methods on the class:
      - ``schema()`` -- a :class:`ToolSchema` instance.
      - ``run(arguments)`` -- the implementation. Returns a ``str`` (the
        content sent back to the LLM) or raises :class:`ToolError`.
    """

    def schema(self) -> ToolSchema: ...

    def run(self, arguments: dict[str, Any]) -> str: ...


class ToolRegistry:
    """Holds the set of available tools and dispatches calls.

    Thread-safe enough for our usage (tools don't share mutable state across
    calls; the registry is read-only after build).
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        schema = tool.schema()
        self._tools[schema.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def describe(self) -> list[dict[str, str]]:
        """Return ``[{name, description}, ...]`` for every registered tool.

        Used by the MCP debug surface (``list_agent_tools``) and any
        introspection consumer that wants a lightweight summary
        without the full Ollama schema. Sorted by name for
        deterministic output.
        """
        out: list[dict[str, str]] = []
        for name in sorted(self._tools.keys()):
            schema = self._tools[name].schema()
            out.append({
                "name": schema.name,
                "description": schema.description,
            })
        return out

    def to_ollama_tools(
        self, allow: "set[str] | None" = None
    ) -> list[dict[str, Any]]:
        """Ollama-shaped schemas for the registered tools.

        When ``allow`` is given, only tools whose name is in the set are
        emitted (brain-lane progressive disclosure — see
        :func:`app.core.session.tool_pass_gate.select_active_tool_names`).
        ``allow=None`` (the default) emits every tool, unchanged.
        """
        names = sorted(self._tools.keys())
        if allow is not None:
            names = [n for n in names if n in allow]
        return [self._tools[n].schema().to_ollama() for n in names]

    def dispatch(self, name: str, arguments: dict[str, Any], *, call_id: str = "") -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                name=name,
                call_id=call_id,
                content=f"error: unknown tool {name!r}",
                ok=False,
            )
        try:
            content = tool.run(arguments or {})
        except ToolError as exc:
            return ToolResult(name=name, call_id=call_id, content=str(exc), ok=False)
        except Exception as exc:
            log.exception("tool %s crashed", name)
            return ToolResult(
                name=name,
                call_id=call_id,
                content=f"error: tool {name!r} failed: {exc}",
                ok=False,
            )
        if not isinstance(content, str):
            try:
                content = json.dumps(content, ensure_ascii=False)
            except Exception:
                content = str(content)
        return ToolResult(name=name, call_id=call_id, content=content)

    def __len__(self) -> int:
        return len(self._tools)


# ── factory ────────────────────────────────────────────────────────────────


def build_default_registry(
    *,
    rag_retriever: Any | None = None,
    web_search_enabled: bool = True,
) -> ToolRegistry:
    """Create a registry pre-populated with the default Aiko toolset.

    ``rag_retriever`` is required for the ``recall`` tool. ``web_search`` is
    skipped when disabled or when :mod:`duckduckgo_search` isn't installed.
    """
    from app.llm.tools.builtins import (
        GetTimeTool,
        RecallTool,
        RecallTopicTool,
        WebSearchTool,
    )

    registry = ToolRegistry()
    registry.register(GetTimeTool())
    if rag_retriever is not None:
        registry.register(RecallTool(rag_retriever))
        registry.register(RecallTopicTool(rag_retriever))
    if web_search_enabled:
        try:
            registry.register(WebSearchTool())
        except Exception:
            log.warning("web_search tool failed to register", exc_info=True)
    return registry
