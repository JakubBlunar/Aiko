"""Adapter: wrap a plugin's fast-tool spec into the brain ``Tool`` protocol.

A code plugin contributes brain-lane fast tools by calling
``api.register_fast_tool(...)`` (see :mod:`app.plugins.sdk`), which collects a
plain ``_FastToolSpec`` (name / description / parameters / handler / family /
gate_patterns). This module turns one such spec into an object satisfying the
[`Tool`](app/llm/tools/base.py) protocol so it can be dropped straight into a
:class:`~app.llm.tools.base.ToolRegistry` alongside the builtins.

The spec is duck-typed (``Any``) rather than imported from ``app.plugins.sdk``
so the tool layer stays decoupled from the plugin runtime.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.llm.tools.base import ToolError, ToolSchema


log = logging.getLogger("app.tools.plugin")


class PluginFastTool:
    """Brain ``Tool`` backed by a plugin's fast-tool spec.

    ``schema()`` mirrors the spec's name / description / parameters;
    ``run(args)`` calls the plugin handler synchronously, coercing a
    non-string return to JSON/str and any exception into a clean
    :class:`ToolError` (the registry forwards the message to the model).
    """

    def __init__(self, spec: Any) -> None:
        self._spec = spec
        self.name = str(getattr(spec, "name", "") or "")

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=str(getattr(self._spec, "description", "") or ""),
            parameters=dict(getattr(self._spec, "parameters", {}) or {}),
        )

    def run(self, arguments: dict[str, Any]) -> str:
        handler = getattr(self._spec, "handler", None)
        if not callable(handler):
            raise ToolError(f"{self.name}: plugin tool has no handler")
        try:
            result = handler(arguments or {})
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface a clean message
            log.warning("plugin tool %s failed: %r", self.name, exc)
            raise ToolError(f"{self.name}: {exc}") from exc
        if isinstance(result, str):
            return result
        try:
            return json.dumps(result, ensure_ascii=False)
        except Exception:
            return str(result)


__all__ = ["PluginFastTool"]
