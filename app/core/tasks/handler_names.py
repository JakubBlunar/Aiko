"""Canonical handler-name constants.

Every concrete :class:`TaskHandler` carries a ``name`` attribute used
as the registry key in :class:`TaskOrchestrator`. Stringly-typing the
name at each handler class invites typos that only surface at
``orchestrator.start_task`` time (the unknown-handler WARNING).

This module is the single source of truth for the canonical spellings.
Handler classes import the constant; LLM-tool wrappers and tests
import the same constant. Re-registering with a typo'd name is then
impossible: there's exactly one place to look.

Adding a new handler:

1. Land the handler class with ``name: str = HANDLER_FOO``.
2. Add ``HANDLER_FOO`` here and to :data:`KNOWN_HANDLER_NAMES`.
3. Register the handler from
   :mod:`app.core.session.task_orchestration_mixin`.

The DB column (``tasks.handler_name``) stays plain TEXT — an enum at
the DB layer makes handler-removal migrations brittle. The registry
in :class:`TaskOrchestrator` enforces "must exist" at spawn time;
this module enforces "must be canonically spelled" at compile time.
"""
from __future__ import annotations


# Vision handler — describe an image with the local worker (multimodal)
# model. Read-only (no approval), reuses the already-loaded worker
# Ollama client + model, reachable only as the ``describe_image``
# ``WorkflowSkill`` child of a goal workflow.
HANDLER_VISION_DESCRIBE = "vision_describe"

# Nested-workflow handlers.
#
# * ``web_search`` — background DuckDuckGo lookup. Lives as a task
#   handler (not a brain builtin) because the network round-trip is
#   too slow for the fast conversational lane; it's reachable only as
#   a ``WorkflowSkill`` child of a goal workflow.
# * ``goal_workflow`` — the parent multi-step orchestrator. Plans →
#   spawns children (web_search / describe_image / MCP tools / …) →
#   observes → replies with an aggregated summary.
HANDLER_WEB_SEARCH = "web_search"
HANDLER_GOAL_WORKFLOW = "goal_workflow"

# Generic external-MCP tool handler — proxies a single tool call to a
# connected external MCP server via :class:`ExternalMcpManager`. One
# handler serves every MCP tool; the specific ``server_id`` / ``tool_name``
# ride in the task args. Reachable only as a ``WorkflowSkill`` child of a
# goal workflow (MCP tools are surfaced to the background lane only).
HANDLER_MCP_TOOL = "mcp_tool"


# Every handler this build is aware of. Used by the MCP debug surface
# + tests asserting "registered handlers match the canonical list".
# Phase-2 / phase-3 handlers (browser, research, summariser, ...)
# extend this tuple; the orchestrator silently accepts unknown names
# at register time so out-of-tree custom handlers still work.
KNOWN_HANDLER_NAMES: tuple[str, ...] = (
    HANDLER_VISION_DESCRIBE,
    HANDLER_WEB_SEARCH,
    HANDLER_GOAL_WORKFLOW,
    HANDLER_MCP_TOOL,
)


__all__ = [
    "HANDLER_VISION_DESCRIBE",
    "HANDLER_WEB_SEARCH",
    "HANDLER_GOAL_WORKFLOW",
    "HANDLER_MCP_TOOL",
    "KNOWN_HANDLER_NAMES",
]
