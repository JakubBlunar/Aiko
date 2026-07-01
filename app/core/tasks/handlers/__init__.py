"""Built-in task handlers for the brain-orchestration task layer.

Each handler is a stateless implementation of one long-running
workflow that satisfies the :class:`TaskHandler` protocol from
:mod:`app.core.tasks.task_handler`.

Current handlers:

* :class:`VisionDescribeHandler` — describe an image with the local
  multimodal worker model. Read-only; reachable only as the
  ``describe_image`` workflow skill.
* :class:`WebSearchHandler` — background web search. Reachable only as
  the ``web_search`` workflow skill.
* :class:`McpToolHandler` — generic proxy for any external MCP tool
  (see :mod:`app.core.tasks.handlers.mcp_tool`).

File read / search / write are no longer built in — they come from a
filesystem MCP server (the ``filesystem`` plugin) surfaced to the
background workflow lane.

Handlers are registered with the orchestrator via
:meth:`TaskOrchestrator.register_handler`. See
:mod:`app.core.session.task_orchestration_mixin` for the boot-time
wiring.
"""
from __future__ import annotations

from app.core.tasks.handlers.vision_describe import VisionDescribeHandler
from app.core.tasks.handlers.web_search import WebSearchHandler


__all__ = [
    "VisionDescribeHandler",
    "WebSearchHandler",
]
