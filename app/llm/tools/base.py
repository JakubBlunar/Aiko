"""Tool-registry base classes used by :mod:`app.llm.tools`."""
from __future__ import annotations

import json
import hashlib
import logging
from dataclasses import dataclass
from copy import deepcopy
from typing import Any, Protocol

from jsonschema.validators import validator_for

from app.llm.tool_arguments import parse_tool_arguments

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

    def __init__(self, *, workflow_scope: str = "") -> None:
        self._tools: dict[str, Tool] = {}
        self._validators: dict[str, Any] = {}
        self._schemas: dict[str, ToolSchema] = {}
        self._workflow: dict[str, str] = {}
        self._workflow_scope = workflow_scope

    def register(
        self, tool: Tool, *, workflow_read_only: bool = False, workflow_generation: str = ""
    ) -> None:
        schema = tool.schema()
        parameters = dict(schema.parameters)
        # Tool parameter objects name their accepted arguments. Explicit
        # extensible schemas (notably plugin payloads) retain that choice.
        if "properties" in parameters:
            parameters.setdefault("additionalProperties", False)
        schema = ToolSchema(schema.name, schema.description, parameters)
        validator = validator_for(parameters)
        validator.check_schema(parameters)
        self._validators[schema.name] = validator(parameters)
        self._schemas[schema.name] = schema
        self._tools[schema.name] = tool
        self._workflow.pop(schema.name, None)
        if workflow_read_only:
            self._workflow[schema.name] = hashlib.sha256(json.dumps(
                [self._workflow_scope, workflow_generation, schema.to_ollama()], sort_keys=True
            ).encode()).hexdigest()

    def workflow_tools(self) -> list[dict[str, Any]]:
        """Explicitly shared read operations; reuse the registered, validated contracts."""
        return [
            {"name": name, "description": self._schemas[name].description,
             "input_schema": deepcopy(self._schemas[name].parameters), "generation": generation}
            for name, generation in sorted(self._workflow.items())
        ]

    def workflow_revision(self, name: str) -> str | None:
        return self._workflow.get(name)

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
            schema = self._schemas[name]
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
        return [self._schemas[n].to_ollama() for n in names]

    def dispatch(
        self, name: str, arguments: dict[str, Any], *, call_id: str = "",
        workflow_generation: str | None = None,
    ) -> ToolResult:
        if workflow_generation is not None and self._workflow.get(name) != workflow_generation:
            return ToolResult(name, call_id, "Tool settings changed; discover it again.", ok=False)
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(
                name=name,
                call_id=call_id,
                content=f"error: unknown tool {name!r}",
                ok=False,
            )
        arguments, error = parse_tool_arguments(arguments)
        if error is None:
            invalid = next(self._validators[name].iter_errors(arguments), None)
            if invalid is not None:
                # Do not echo argument values (which can contain private text)
                # into diagnostics. The path and schema keyword are sufficient.
                path = ".".join(str(p) for p in invalid.absolute_path) or "arguments"
                error = f"Invalid {path}: violates {invalid.validator}."
        if error is not None:
            return ToolResult(name=name, call_id=call_id, content=error, ok=False)
        try:
            run = getattr(tool, "run_for_workflow", tool.run) if workflow_generation else tool.run
            content = run(arguments or {})
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
