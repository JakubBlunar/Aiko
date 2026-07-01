"""Filesystem ToolPlugin -- wraps the official filesystem MCP server.

Registers ``@modelcontextprotocol/server-filesystem`` (via ``npx``) sandboxed
to a single absolute ``root``. The root is machine-specific, so it lives in
the gitignored ``config/user.json`` (see ``config/user.example.json``), never
in the committed manifest.

Enable it by setting ``enabled: true`` in ``plugin.json`` (or
``plugins.entries.filesystem.enabled`` in ``config/user.json``) and providing a
``root`` in this plugin's ``config/user.json``.
"""
from __future__ import annotations


def define_plugin(api) -> None:
    api.require_binary("npx")
    root = api.require_config("root")
    api.register_mcp_server(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", str(root)],
        timeout_seconds=30,
    )
    api.register_skills("skills")
