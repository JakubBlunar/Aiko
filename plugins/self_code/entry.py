"""Opt-in, source-scoped editor tools in the existing background MCP lane."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys


def define_plugin(api) -> None:
    api.require_config("root")
    api.require_config("include_paths")
    from aiko_code.policy import SourceScope

    SourceScope(api.config)
    # The plugin runtime installs parsers in its isolated dependency target.
    # Stdio children do not inherit the parent's sys.path; pass only those
    # trusted dependency roots, never the inspected checkout's import paths.
    dependencies = set()
    for name in ("tree_sitter", "tree_sitter_javascript", "tree_sitter_typescript"):
        spec = importlib.util.find_spec(name)
        if spec is None or spec.origin is None:
            raise RuntimeError(f"Source parser dependency is unavailable: {name}")
        dependencies.add(str(Path(spec.origin).parent.parent))
    fields = {
        "root",
        "include_paths",
        "exclude_patterns",
        "max_file_bytes",
        "max_scan_bytes",
        "max_scan_files",
        "max_walk_entries",
        "max_query_seconds",
    }
    configuration = {key: value for key, value in api.config.items() if key in fields}
    api.register_mcp_server(
        command=sys.executable,
        args=["-I", str(api.plugin_root / "server.py"), "--config", json.dumps(configuration)],
        env={"AIKO_CODE_DEPENDENCIES": json.dumps(sorted(dependencies))},
        timeout_seconds=15,
    )
    api.register_skills("skills")
