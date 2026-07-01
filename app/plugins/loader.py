"""Plugin bundle loader -- discover stubs + resolve config (pure, no code run).

A plugin bundle is a directory holding a minimal ``plugin.json`` **stub**, a
Python ``entry.py`` entrypoint, plugin-local ``config/`` JSON, and optional
``SKILL.md`` guidance::

    plugins/filesystem/
      plugin.json          # {id, name, plugin_api_version, enabled}
      entry.py             # def define_plugin(api): ...
      config/
        default.json       # committed defaults
        user.json          # gitignored machine-specific / secret values
      skills/SKILL.md

This module is **pure and code-free**: it reads JSON only (never imports
``entry.py``, never touches ``sys.path``, never runs ``pip``). It turns bundle
folders into :class:`PluginStub` records -- identity, resolved enable state,
merged config, and declared Python dependencies -- so the caller can decide
*whether* to activate a plugin (import + run its code) from JSON alone. The
disabled-plugin-is-inert guarantee is structural: activation
( :mod:`app.plugins.runtime` ) only ever receives stubs that already
resolved to ``enabled=True`` here.

``SKILL.md`` parsing helpers live here (pure) and are reused by the runtime
when composing a plugin's planner guidance.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


log = logging.getLogger("app.plugins")


# Highest ``plugin_api_version`` this loader understands. A bundle declaring
# a higher version is kept visible but never activated (we can't safely guess
# a newer contract shape); a lower / missing version is accepted as v1.
SUPPORTED_PLUGIN_API_VERSION = 1

_MANIFEST_NAME = "plugin.json"
_SKILL_FILE = "SKILL.md"
_CONFIG_DIR = "config"
_CONFIG_DEFAULT = "default.json"
_CONFIG_USER = "user.json"
_REQUIREMENTS_FILE = "requirements.txt"

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Tolerant-manifest helpers: strip ``//`` / ``/* */`` comments and trailing
# commas so a hand-written json5-ish manifest / config doesn't hard-fail.
_LINE_COMMENT_RE = re.compile(r"^\s*//.*$", re.MULTILINE)
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


# ── data records ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LoadedSkill:
    """One parsed ``SKILL.md`` (frontmatter + body)."""

    name: str
    description: str
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)
    source_path: str = ""


@dataclass(frozen=True, slots=True)
class PluginStub:
    """A discovered plugin's JSON-only identity + resolved config.

    Everything here is derived from ``plugin.json`` + plugin-local
    ``config/`` + the central per-plugin override -- no code was run. The
    runtime decides activation from ``enabled`` / ``unsupported``.
    """

    id: str
    name: str
    root: str
    enabled: bool
    plugin_api_version: int
    config: dict[str, Any] = field(default_factory=dict)
    python_dependencies: list[str] = field(default_factory=list)
    unsupported: bool = False
    warnings: list[str] = field(default_factory=list)


# ── JSON parsing ──────────────────────────────────────────────────────


def _loads_lenient(text: str) -> Any:
    """``json.loads`` with a fallback that tolerates comments / trailing commas."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        cleaned = _BLOCK_COMMENT_RE.sub("", text)
        cleaned = _LINE_COMMENT_RE.sub("", cleaned)
        cleaned = _TRAILING_COMMA_RE.sub(r"\1", cleaned)
        return json.loads(cleaned)


def parse_manifest(text: str) -> dict[str, Any]:
    """Parse a ``plugin.json`` stub body into a dict (tolerant of json5-isms)."""
    parsed = _loads_lenient(text)
    if not isinstance(parsed, dict):
        raise ValueError("plugin.json is not a JSON object")
    return parsed


def _read_json_object(path: Path) -> dict[str, Any]:
    """Best-effort read of a JSON object file; ``{}`` on any problem."""
    try:
        if not path.is_file():
            return {}
        parsed = _loads_lenient(path.read_text(encoding="utf-8", errors="replace"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        log.debug("plugin config unreadable: %s", path, exc_info=True)
        return {}


# ── SKILL.md parsing (reused by the runtime) ──────────────────────────


def parse_skill_md(text: str, *, source_path: str = "") -> LoadedSkill:
    """Parse an AgentSkills ``SKILL.md`` (single-line YAML frontmatter + body)."""
    raw = text.replace("\r\n", "\n").replace("\r", "\n")
    meta: dict[str, Any] = {}
    body = raw
    if raw.lstrip().startswith("---"):
        stripped = raw.lstrip("\n")
        lines = stripped.split("\n")
        if lines and lines[0].strip() == "---":
            end = None
            for idx in range(1, len(lines)):
                if lines[idx].strip() == "---":
                    end = idx
                    break
            if end is not None:
                for line in lines[1:end]:
                    if not line.strip() or ":" not in line:
                        continue
                    key, _, value = line.partition(":")
                    meta[key.strip()] = value.strip()
                body = "\n".join(lines[end + 1 :])
    name = str(meta.pop("name", "") or "").strip()
    description = str(meta.pop("description", "") or "").strip()
    return LoadedSkill(
        name=name,
        description=description,
        body=body.strip(),
        metadata=meta,
        source_path=source_path,
    )


def load_skill_dirs(
    root: Path, dirs: list[str], warnings: list[str]
) -> list[LoadedSkill]:
    """Load every ``SKILL.md`` under the named sub-dirs (or the dir itself)."""
    out: list[LoadedSkill] = []
    for rel in dirs:
        sub = (root / rel).resolve()
        try:
            sub.relative_to(root.resolve())
        except ValueError:
            warnings.append(f"skill dir escapes plugin root: {rel!r}")
            continue
        if not sub.exists():
            warnings.append(f"skill dir missing: {rel!r}")
            continue
        candidates: list[Path] = []
        direct = sub / _SKILL_FILE
        if direct.is_file():
            candidates.append(direct)
        candidates.extend(sorted(sub.glob(f"*/{_SKILL_FILE}")))
        if sub.is_file() and sub.name == _SKILL_FILE:
            candidates = [sub]
        for path in candidates:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                warnings.append(f"skill unreadable: {path.name}")
                continue
            out.append(parse_skill_md(text, source_path=str(path)))
    return out


# ── config resolution ─────────────────────────────────────────────────


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` onto ``base`` (overlay wins)."""
    out = dict(base)
    for key, value in overlay.items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(value, dict)
        ):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def resolve_plugin_config(
    root: Path, entry_config: dict[str, Any] | None
) -> dict[str, Any]:
    """Merge plugin-local + central config in precedence order.

    ``config/default.json`` (committed) < ``config/user.json`` (gitignored) <
    the central ``plugins.entries.<id>.config`` override (wins last).
    """
    default = _read_json_object(root / _CONFIG_DIR / _CONFIG_DEFAULT)
    user = _read_json_object(root / _CONFIG_DIR / _CONFIG_USER)
    merged = _deep_merge(default, user)
    if entry_config:
        merged = _deep_merge(merged, dict(entry_config))
    return merged


def _manifest_dependencies(manifest: dict[str, Any], root: Path) -> list[str]:
    """Python deps from the manifest key + an optional ``requirements.txt``."""
    deps: list[str] = []
    raw = manifest.get("python_dependencies")
    if isinstance(raw, (list, tuple)):
        deps.extend(str(d).strip() for d in raw if str(d).strip())
    req = root / _REQUIREMENTS_FILE
    if req.is_file():
        try:
            for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    deps.append(line)
        except Exception:
            log.debug("plugin requirements.txt unreadable: %s", req, exc_info=True)
    # De-dup preserving order.
    return list(dict.fromkeys(deps))


# ── discovery ─────────────────────────────────────────────────────────


def default_plugin_roots() -> list[str]:
    """Bundled (repo) + user (``data/``) plugin roots, in precedence order."""
    return [
        str(_REPO_ROOT / "plugins"),
        str(_REPO_ROOT / "data" / "plugins"),
    ]


def _load_one(
    manifest_path: Path,
    *,
    entry_overrides: dict[str, dict[str, Any]],
) -> PluginStub | None:
    """Parse a single plugin folder into a stub (no code run)."""
    root = manifest_path.parent
    warnings: list[str] = []
    try:
        manifest = parse_manifest(
            manifest_path.read_text(encoding="utf-8", errors="replace")
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("plugin manifest parse failed: %s (%s)", manifest_path, exc)
        return None

    plugin_id = str(manifest.get("id", "") or "").strip()
    if not plugin_id:
        log.warning("plugin has no id, skipped: %s", manifest_path)
        return None
    name = str(manifest.get("name", "") or "").strip() or plugin_id

    try:
        api_version = int(manifest.get("plugin_api_version", 1) or 1)
    except (TypeError, ValueError):
        api_version = 1

    override = entry_overrides.get(plugin_id, {}) or {}
    override_enabled = override.get("enabled")
    manifest_enabled = bool(manifest.get("enabled", True))
    enabled = (
        bool(override_enabled) if override_enabled is not None else manifest_enabled
    )
    config = resolve_plugin_config(root, override.get("config") or {})
    python_dependencies = _manifest_dependencies(manifest, root)

    unsupported = api_version > SUPPORTED_PLUGIN_API_VERSION
    if unsupported:
        warnings.append(
            f"plugin_api_version {api_version} > supported "
            f"{SUPPORTED_PLUGIN_API_VERSION}"
        )
        log.warning(
            "plugin %s needs api v%d (we support v%d) -- will not activate",
            plugin_id,
            api_version,
            SUPPORTED_PLUGIN_API_VERSION,
        )

    return PluginStub(
        id=plugin_id,
        name=name,
        root=str(root),
        enabled=enabled,
        plugin_api_version=api_version,
        config=config,
        python_dependencies=python_dependencies,
        unsupported=unsupported,
        warnings=warnings,
    )


def discover_plugins(
    roots: list[str] | None = None,
    *,
    entries: dict[str, dict[str, Any]] | None = None,
) -> list[PluginStub]:
    """Scan ``roots`` for plugin folders and return parsed stubs (no code run).

    ``entries`` is the per-plugin ``PluginsSettings.entries`` map
    (``{id: {enabled, config}}``) used for enable overrides + config merge.
    First-seen id wins across roots (bundled shadows a same-id user plugin),
    so pass roots in precedence order.
    """
    roots = roots if roots is not None else default_plugin_roots()
    entries = entries or {}

    out: list[PluginStub] = []
    seen: set[str] = set()
    for root_str in roots:
        try:
            root = Path(root_str)
        except Exception:
            continue
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            manifest_path = child / _MANIFEST_NAME
            if not manifest_path.is_file():
                continue
            stub = _load_one(manifest_path, entry_overrides=entries)
            if stub is None:
                continue
            if stub.id in seen:
                log.debug(
                    "plugin %s already loaded from an earlier root -- skipping %s",
                    stub.id,
                    child,
                )
                continue
            seen.add(stub.id)
            out.append(stub)
    return out


__all__ = [
    "SUPPORTED_PLUGIN_API_VERSION",
    "LoadedSkill",
    "PluginStub",
    "parse_manifest",
    "parse_skill_md",
    "load_skill_dirs",
    "resolve_plugin_config",
    "discover_plugins",
    "default_plugin_roots",
]
