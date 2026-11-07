"""Filesystem ToolPlugin -- wraps the official filesystem MCP server.

Registers ``@modelcontextprotocol/server-filesystem`` (via ``npx``) restricted
to the configured absolute ``roots``. Settings > Tools > Plugins manages these
host folders and runtime activation. Legacy single-root configuration remains
supported; an explicit empty roots list never falls back to it.
"""
from __future__ import annotations


def define_plugin(api) -> None:
    api.require_binary("npx")
    roots = api.config.get("roots")
    if roots is None:
        roots = [api.require_config("root")]
    if not roots:
        raise ValueError("Add at least one allowed folder")
    api.register_mcp_server(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", *map(str, roots)],
        timeout_seconds=30,
    )
    api.register_skills("skills")
