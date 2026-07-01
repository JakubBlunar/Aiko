"""ToolPlugins -- Aiko's plugin framework (SDK-primary).

A *plugin* is a self-contained folder that teaches Aiko a new capability with
**no core code change**. Today the shipped capability is an MCP server (+ its
planner guidance + tool-result middleware) for the background-worker lane, but
this package is deliberately **not** under ``app.mcp`` -- the loader / runtime /
SDK are a general plugin framework, so future plugin *types* (workflow tools,
lifecycle hooks, ...) get a natural home here without an MCP coupling.

Every plugin is a small Python package::

    plugins/<id>/
      plugin.json          # STUB: {id, name, plugin_api_version, enabled}
      entry.py             # def define_plugin(api): ... (SDK-primary)
      config/
        default.json       # committed defaults
        user.json          # gitignored machine-specific / secret values
      skills/SKILL.md      # optional planner guidance

The ``plugin.json`` stub is JSON-only identity + enable state so the loader can
list / enable plugins without importing code. The ``entry.py`` registers the
plugin's capabilities against the :class:`~app.plugins.sdk.PluginApi`.

Two-phase lifecycle with the trust gate between them:

* :mod:`app.plugins.loader` -- **pure JSON**: discover stubs + resolve
  plugin-local config. Never imports ``entry.py``.
* :mod:`app.plugins.runtime` -- **runs code**: only for enabled stubs,
  installs isolated deps + imports ``entry.py`` + calls ``define_plugin(api)``.

So a disabled plugin is completely inert (its code is never imported). MCP tools
reach the background planner only -- never the brain's fast ``ToolRegistry``.
"""
from __future__ import annotations

from app.plugins.loader import (
    SUPPORTED_PLUGIN_API_VERSION,
    LoadedSkill,
    PluginStub,
    default_plugin_roots,
    discover_plugins,
    load_skill_dirs,
    parse_manifest,
    parse_skill_md,
    resolve_plugin_config,
)
from app.plugins.runtime import (
    ActivatedPlugin,
    activate_all,
    activate_plugin,
    ensure_dependencies,
)
from app.plugins.sdk import (
    MiddlewareResult,
    PluginApi,
    PluginGatedError,
    ToolResultMiddleware,
)


__all__ = [
    "SUPPORTED_PLUGIN_API_VERSION",
    "LoadedSkill",
    "PluginStub",
    "default_plugin_roots",
    "discover_plugins",
    "load_skill_dirs",
    "parse_manifest",
    "parse_skill_md",
    "resolve_plugin_config",
    "ActivatedPlugin",
    "activate_all",
    "activate_plugin",
    "ensure_dependencies",
    "MiddlewareResult",
    "PluginApi",
    "PluginGatedError",
    "ToolResultMiddleware",
]
