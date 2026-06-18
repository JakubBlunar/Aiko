"""External MCP-server client runtime.

This is the *client* half of MCP for the app: where ``app/mcp/server.py``
EXPOSES an MCP server for debugging, this package CONNECTS OUT to external
MCP servers (the official filesystem server, a browser-automation server,
etc.) and consumes their tools.

The discovered tools are surfaced only to the background-worker (workflow
planner) lane via ``app/core/tasks/workflow/mcp_skills.py`` -- never to the
brain's fast :class:`ToolRegistry`.
"""
from __future__ import annotations

from app.mcp.client.manager import (
    ExternalMcpManager,
    McpToolDescriptor,
    McpToolError,
)

__all__ = ["ExternalMcpManager", "McpToolDescriptor", "McpToolError"]
