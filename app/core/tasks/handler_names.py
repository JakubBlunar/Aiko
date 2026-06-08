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


# Phase-1 handlers (shipped with chunks 11 + 12).
HANDLER_FILE_SEARCH = "file_search"
HANDLER_FILE_READ = "file_read"

# Nested-workflow handlers.
#
# * ``web_search`` — background DuckDuckGo lookup. Lives as a task
#   handler (not a brain builtin) because the network round-trip is
#   too slow for the fast conversational lane; it's reachable only as
#   a ``WorkflowSkill`` child of a goal workflow.
# * ``goal_workflow`` — the parent multi-step orchestrator. Plans →
#   spawns children (file_search / file_read / web_search / …) →
#   observes → replies with an aggregated summary.
HANDLER_WEB_SEARCH = "web_search"
HANDLER_GOAL_WORKFLOW = "goal_workflow"


# Every handler this build is aware of. Used by the MCP debug surface
# + tests asserting "registered handlers match the canonical list".
# Phase-2 / phase-3 handlers (browser, research, summariser, ...)
# extend this tuple; the orchestrator silently accepts unknown names
# at register time so out-of-tree custom handlers still work.
KNOWN_HANDLER_NAMES: tuple[str, ...] = (
    HANDLER_FILE_SEARCH,
    HANDLER_FILE_READ,
    HANDLER_WEB_SEARCH,
    HANDLER_GOAL_WORKFLOW,
)


__all__ = [
    "HANDLER_FILE_SEARCH",
    "HANDLER_FILE_READ",
    "HANDLER_WEB_SEARCH",
    "HANDLER_GOAL_WORKFLOW",
    "KNOWN_HANDLER_NAMES",
]
