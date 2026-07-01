"""Plugin activation runtime -- imports entry.py and runs define_plugin (impure).

Where :mod:`app.plugins.loader` is pure JSON (discovery + config), this
module is where third-party **code runs**: for each enabled stub it ensures
Python dependencies are installed into an isolated per-plugin dir, imports the
plugin's ``entry.py`` by file path, constructs a :class:`PluginApi`, and calls
``define_plugin(api)``. It then reads back what the plugin registered and turns
the plain server spec into an ``ExternalMcpServer`` (this app-side coupling
lives here, NOT in the SDK, so the SDK stays extractable).

Contract with the loader: ``activate_all`` only activates stubs with
``enabled=True`` and a supported api version. A disabled / unsupported stub is
returned as an inert record whose ``entry.py`` was never imported.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.plugins.loader import (
    PluginStub,
    load_skill_dirs,
)
from app.plugins.sdk import PluginApi, PluginGatedError


log = logging.getLogger("app.plugins")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DEPS_ROOT = _REPO_ROOT / "data" / "plugins-deps"
_ENTRY_FILE = "entry.py"
_ENTRY_FUNC = "define_plugin"
_INSTALL_MARKER = ".installed.json"
_PIP_TIMEOUT_SECONDS = 600


@dataclass(slots=True)
class ActivatedPlugin:
    """The result of trying to activate one plugin (code may have run)."""

    id: str
    name: str
    root: str
    status: str  # active | gated_out | invalid | disabled | unsupported
    enabled: bool
    server: Any | None = None  # ExternalMcpServer | None
    group_guidance: dict[str, str] = field(default_factory=dict)
    middlewares: list[Any] = field(default_factory=list)
    fast_tools: list[Any] = field(default_factory=list)  # sdk._FastToolSpec
    skill_count: int = 0
    deps_status: str = "none"  # none | cached | installed | failed
    reason: str = ""
    warnings: list[str] = field(default_factory=list)


# ── dependency install (isolated per-plugin dir) ──────────────────────


def _deps_hash(deps: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(deps)).encode("utf-8")).hexdigest()


def ensure_dependencies(
    stub: PluginStub, *, deps_root: Path | None = None
) -> tuple[str, str]:
    """Install a plugin's Python deps into ``deps_root/<id>`` + add to sys.path.

    Returns ``(status, reason)`` where status is ``none`` (no deps),
    ``cached`` (marker matched, nothing to do), ``installed`` (pip ran), or
    ``failed`` (reason set). Idempotent via a ``.installed.json`` marker
    (hash of the sorted dep list); re-installs only when the list changes.
    """
    deps = list(stub.python_dependencies or [])
    if not deps:
        return "none", ""
    root = (deps_root or _DEFAULT_DEPS_ROOT) / stub.id
    marker = root / _INSTALL_MARKER
    want_hash = _deps_hash(deps)

    if marker.is_file():
        try:
            saved = json.loads(marker.read_text(encoding="utf-8"))
            if saved.get("hash") == want_hash:
                _add_sys_path(root)
                return "cached", ""
        except Exception:
            log.debug("plugin %s deps marker unreadable", stub.id, exc_info=True)

    try:
        root.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--target",
            str(root),
            *deps,
        ]
        log.info("plugin %s installing deps: %s", stub.id, deps)
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_PIP_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            reason = (proc.stderr or proc.stdout or "pip failed").strip()[:300]
            log.warning("plugin %s dep install failed: %s", stub.id, reason)
            return "failed", f"dependency install failed: {reason}"
        marker.write_text(
            json.dumps({"hash": want_hash, "deps": deps}), encoding="utf-8"
        )
        _add_sys_path(root)
        return "installed", ""
    except subprocess.TimeoutExpired:
        return "failed", "dependency install timed out"
    except Exception as exc:  # noqa: BLE001
        return "failed", f"dependency install error: {exc}"[:300]


def _add_sys_path(path: Path) -> None:
    p = str(path)
    if p not in sys.path:
        sys.path.insert(0, p)


# ── entrypoint activation ─────────────────────────────────────────────


def _import_entry(stub: PluginStub) -> Any:
    """Import a plugin's ``entry.py`` under an isolated module name.

    The plugin root is put on ``sys.path`` first so ``entry.py`` can import
    the plugin's own local packages (e.g. ``import aiko_browser``) that ship
    alongside it, keeping plugin code decoupled from app core.
    """
    entry_path = Path(stub.root) / _ENTRY_FILE
    if not entry_path.is_file():
        raise FileNotFoundError(f"no {_ENTRY_FILE}")
    _add_sys_path(Path(stub.root))
    module_name = f"aiko_plugin_{stub.id}_entry"
    spec = importlib.util.spec_from_file_location(module_name, entry_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {entry_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _build_server(spec: dict[str, Any], warnings: list[str]) -> Any | None:
    """Turn the SDK's plain server spec into an ``ExternalMcpServer``."""
    from app.core.infra.settings import _parse_external_mcp_server

    payload = dict(spec)
    payload["enabled"] = True
    server = _parse_external_mcp_server(payload)
    if server is None:
        warnings.append("mcp server spec invalid (stdio needs command / sse needs url)")
    return server


def _compose_guidance(api: PluginApi, warnings: list[str]) -> str:
    """Join registered SKILL.md bodies + inline skills into one block."""
    blocks: list[str] = []
    root = api.plugin_root
    if api.skill_dirs:
        for skill in load_skill_dirs(root, api.skill_dirs, warnings):
            if skill.body.strip():
                blocks.append(skill.body.strip())
            elif skill.description.strip():
                blocks.append(skill.description.strip())
    for inline in api.inline_skills:
        if inline.body:
            blocks.append(inline.body)
        elif inline.description:
            blocks.append(inline.description)
    return "\n\n".join(blocks)


def activate_plugin(
    stub: PluginStub, *, deps_root: Path | None = None
) -> ActivatedPlugin:
    """Activate one plugin: ensure deps, import entry.py, run define_plugin."""
    warnings = list(stub.warnings)
    base = dict(id=stub.id, name=stub.name, root=stub.root, enabled=stub.enabled)

    if not stub.enabled:
        return ActivatedPlugin(**base, status="disabled", warnings=warnings)
    if stub.unsupported:
        return ActivatedPlugin(
            **base,
            status="unsupported",
            reason=f"plugin_api_version {stub.plugin_api_version} unsupported",
            warnings=warnings,
        )

    deps_status, deps_reason = ensure_dependencies(stub, deps_root=deps_root)
    if deps_status == "failed":
        return ActivatedPlugin(
            **base, status="invalid", deps_status=deps_status,
            reason=deps_reason, warnings=warnings,
        )

    try:
        module = _import_entry(stub)
    except Exception as exc:  # noqa: BLE001
        log.warning("plugin %s entry import failed: %r", stub.id, exc)
        return ActivatedPlugin(
            **base, status="invalid", deps_status=deps_status,
            reason=f"entry import failed: {exc}"[:300], warnings=warnings,
        )

    define = getattr(module, _ENTRY_FUNC, None)
    if not callable(define):
        return ActivatedPlugin(
            **base, status="invalid", deps_status=deps_status,
            reason=f"{_ENTRY_FILE} has no {_ENTRY_FUNC}(api)", warnings=warnings,
        )

    api = PluginApi(
        plugin_id=stub.id,
        plugin_root=Path(stub.root),
        config=stub.config,
        logger=logging.getLogger(f"app.plugins.{stub.id}"),
    )
    try:
        define(api)
    except PluginGatedError as exc:
        log.info("plugin %s gated out: %s", stub.id, exc.reason)
        return ActivatedPlugin(
            **base, status="gated_out", deps_status=deps_status,
            reason=exc.reason, warnings=warnings,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("plugin %s define_plugin raised: %r", stub.id, exc)
        return ActivatedPlugin(
            **base, status="invalid", deps_status=deps_status,
            reason=f"define_plugin raised: {exc}"[:300], warnings=warnings,
        )

    server = None
    if api.server_spec is not None:
        server = _build_server(api.server_spec, warnings)

    group_guidance: dict[str, str] = {}
    guidance = _compose_guidance(api, warnings)
    if guidance.strip():
        group_guidance[f"mcp:{stub.id}"] = guidance

    middlewares = api.middlewares
    fast_tools = api.fast_tools
    skill_count = len(api.skill_dirs) + len(api.inline_skills)

    if (
        server is None
        and not group_guidance
        and not middlewares
        and not fast_tools
    ):
        return ActivatedPlugin(
            **base, status="invalid", deps_status=deps_status,
            reason="plugin registered no capabilities", warnings=warnings,
        )

    log.info(
        "plugin %s active: server=%s middlewares=%d fast_tools=%d guidance=%s",
        stub.id,
        server.id if server is not None else None,
        len(middlewares),
        len(fast_tools),
        bool(group_guidance),
    )
    return ActivatedPlugin(
        **base,
        status="active",
        server=server,
        group_guidance=group_guidance,
        middlewares=middlewares,
        fast_tools=fast_tools,
        skill_count=skill_count,
        deps_status=deps_status,
        warnings=warnings,
    )


def activate_all(
    stubs: list[PluginStub], *, deps_root: Path | None = None
) -> list[ActivatedPlugin]:
    """Activate every stub best-effort (one bad plugin never aborts the rest)."""
    out: list[ActivatedPlugin] = []
    for stub in stubs:
        try:
            out.append(activate_plugin(stub, deps_root=deps_root))
        except Exception as exc:  # noqa: BLE001
            log.warning("plugin %s activation crashed: %r", stub.id, exc)
            out.append(
                ActivatedPlugin(
                    id=stub.id,
                    name=stub.name,
                    root=stub.root,
                    enabled=stub.enabled,
                    status="invalid",
                    reason=f"activation crashed: {exc}"[:300],
                )
            )
    return out


__all__ = [
    "ActivatedPlugin",
    "ensure_dependencies",
    "activate_plugin",
    "activate_all",
]
