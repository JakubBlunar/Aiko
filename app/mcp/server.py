"""FastMCP debug server for the lean Aiko app.

Exposes a small surface for Cursor / VSCode MCP clients to drive the running
session: send messages, inspect status, clear history, peek at the latest
metrics. Browser-snapshot and agent-tool tools from the legacy build are
gone -- v1 has no agent tools yet.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP


if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController


log = logging.getLogger("app.mcp.server")
_session_ref: "SessionController | None" = None


def create_mcp_server(session: "SessionController", port: int = 6274) -> FastMCP:
    """Build a FastMCP server wired to the live ``session``."""
    global _session_ref
    _session_ref = session
    mcp = FastMCP("assistant", host="127.0.0.1", port=port)

    # ── Tools ────────────────────────────────────────────────────────

    from app.mcp.server_tools import (
        core_tools,
        memory_worker_tools,
        self_state_tools,
        emotion_touch_tools,
        proactive_task_tools,
        resource_file_tools,
    )

    core_tools.register(mcp, session)
    memory_worker_tools.register(mcp, session)
    self_state_tools.register(mcp, session)
    emotion_touch_tools.register(mcp, session)
    proactive_task_tools.register(mcp, session)
    resource_file_tools.register(mcp, session)
    return mcp

