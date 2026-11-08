"""Browser ToolPlugin -- wraps a browser MCP server + perception middleware.

Two capabilities in one bundle:

1. The MCP server (``real-browser-mcp`` via ``npx``), with the noisy debug
   tools disabled.
2. A tool-result middleware (``BrowserPerception`` from the plugin-local
   ``aiko_browser`` package) that reshapes the raw accessibility snapshot
   returned by ``browser_snapshot`` into a compact, ranked block before the
   planner sees it (parse -> dedup -> group -> rank -> diff -> render). This
   is real code, decoupled from app core, configured from this plugin's
   ``config/``.

Tune the perception layer in ``config/default.json`` (adapter, snapshot_tools,
ranking weights); machine-specific overrides go in the gitignored
``config/user.json``.
"""
from __future__ import annotations


def define_plugin(api) -> None:
    api.require_binary("npx")

    cfg = api.config
    disabled = list(cfg.get("disabled_tools") or [])
    api.register_mcp_server(
        command="npx",
        args=["-y", "real-browser-mcp"],
        disabled_tools=disabled,
        timeout_seconds=60,
    )
    api.register_skills("skills")
    api.register_procedure(
        "navigate_and_observe", "Navigate to a requested URL and inspect its resulting page.",
        input_schema={"type": "object", "properties": {"url": {"type": "string"}},
                      "required": ["url"], "additionalProperties": False},
        steps=[{"tool": "browser_navigate", "arguments": {"url": {"$input": "url"}}},
               {"tool": "browser_snapshot", "arguments": {}}],
    )
    api.register_playbook(
        "dynamic_controls", "Recover menus, portals and stale browser targets.",
        "After opening a menu or dialog, obtain a fresh snapshot before selecting an option. "
        "Use visible text or a fresh observed ref. If a target vanished, observe again rather "
        "than guessing a replacement. Prefer a narrowed snapshot for long pages. Verify the "
        "resulting visible state after acting; acknowledgement alone is not completion.",
    )

    # The plugin runtime put this plugin's root on sys.path, so the local
    # ``aiko_browser`` package imports cleanly and stays out of app core.
    from aiko_browser import BrowserPerception

    api.register_tool_result_middleware(
        BrowserPerception.from_config(cfg, server_id=api.plugin_id)
    )
