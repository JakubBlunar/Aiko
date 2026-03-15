from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


def save_coding_tooling(
    user_path: Path,
    *,
    enabled: bool,
    allowed_roots: list[str],
) -> None:
    """Merge tools.coding into tooling.user.json and write. allowed_roots should be absolute paths."""
    current = _read_config(user_path)
    tools = dict(current.get("tools", {}) if isinstance(current.get("tools"), dict) else {})
    tools["coding"] = {
        "enabled": enabled,
        "allowed_roots": [str(p).strip() for p in allowed_roots if str(p).strip()],
    }
    current["tools"] = tools
    user_path.parent.mkdir(parents=True, exist_ok=True)
    user_path.write_text(json.dumps(current, indent=2), encoding="utf-8")


@dataclass(slots=True)
class ToolPolicyConfig:
    full_auto: bool = False
    mutating_requires_confirmation: bool = True
    max_tool_calls_per_turn: int = 64


def _agno_entries_raw(value: Any) -> list[Any]:
    """Return agno_toolkits as a list (strings or { id, params } objects)."""
    if not isinstance(value, list):
        return []
    return list(value)


def _agno_params_raw(value: Any) -> dict[str, dict[str, Any]]:
    """Return agno_toolkit_params as id -> params dict (Option B)."""
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in value.items() if isinstance(v, dict)}


@dataclass(slots=True)
class ToolingConfig:
    enabled_tools: list[str] = field(default_factory=list)
    disabled_tools: list[str] = field(default_factory=list)
    agno_toolkits: list[Any] = field(default_factory=list)
    agno_toolkit_params: dict[str, dict[str, Any]] = field(default_factory=dict)
    policies: ToolPolicyConfig = field(default_factory=ToolPolicyConfig)
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    runtime_overrides: dict[str, Any] = field(default_factory=dict)

    def tool_settings(self, namespace: str) -> dict[str, Any]:
        """Return settings for a tool namespace, e.g. 'history' or 'ocr'."""
        key = str(namespace or "").strip().lower()
        value = self.tools.get(key, {})
        return dict(value) if isinstance(value, dict) else {}


_TOOLING_DEFAULT_PATH = Path(__file__).resolve().parents[3] / "config" / "tooling.default.json"
_TOOLING_USER_PATH = Path(__file__).resolve().parents[3] / "config" / "tooling.user.json"


def _read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _read_merged(*paths: Path) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for path in paths:
        try:
            current = _read_config(path)
        except Exception:
            continue
        merged = _deep_merge(merged, current)
    return merged


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _as_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return result


def load_tooling_config(
    *,
    default_path: Path | None = None,
    user_path: Path | None = None,
    runtime_overrides: dict[str, Any] | None = None,
) -> ToolingConfig:
    if default_path is not None:
        base = _read_config(default_path)
    else:
        base = _read_merged(_TOOLING_DEFAULT_PATH)

    if user_path is not None:
        user = _read_config(user_path)
    else:
        user = _read_merged(_TOOLING_USER_PATH)
    merged = _deep_merge(base, user)

    if runtime_overrides:
        merged = _deep_merge(merged, runtime_overrides)

    policies_raw = merged.get("policies", {}) if isinstance(merged.get("policies"), dict) else {}
    policies = ToolPolicyConfig(
        full_auto=bool(policies_raw.get("full_auto", False)),
        mutating_requires_confirmation=bool(policies_raw.get("mutating_requires_confirmation", True)),
        max_tool_calls_per_turn=max(1, int(policies_raw.get("max_tool_calls_per_turn", 64))),
    )

    tools_raw = merged.get("tools", {}) if isinstance(merged.get("tools"), dict) else {}
    tools: dict[str, dict[str, Any]] = {}
    for key, value in tools_raw.items():
        if isinstance(value, dict):
            tools[str(key).strip().lower()] = dict(value)

    agno_toolkits = _agno_entries_raw(merged.get("agno_toolkits", []))
    agno_params = _agno_params_raw(merged.get("agno_toolkit_params", {}))

    return ToolingConfig(
        enabled_tools=_as_list(merged.get("enabled_tools", [])),
        disabled_tools=_as_list(merged.get("disabled_tools", [])),
        agno_toolkits=agno_toolkits,
        agno_toolkit_params=agno_params,
        policies=policies,
        tools=tools,
        runtime_overrides=(runtime_overrides or {}),
    )


def resolve_agno_toolkit_entries(config: ToolingConfig) -> list[tuple[str, dict[str, Any]]]:
    """
    Resolve agno_toolkits (Option A: list of ids or { id, params }) and agno_toolkit_params (Option B)
    into a list of (toolkit_id, params_dict). User params (Option B) override per-id params from entries.
    """
    result: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for entry in config.agno_toolkits:
        if isinstance(entry, str):
            tid = (entry or "").strip()
            if tid and tid not in seen:
                seen.add(tid)
                params = dict(config.agno_toolkit_params.get(tid, {}))
                result.append((tid, params))
        elif isinstance(entry, dict):
            tid = (entry.get("id") or entry.get("toolkit_id") or "").strip()
            if not tid:
                continue
            params = dict(entry.get("params", {})) if isinstance(entry.get("params"), dict) else {}
            if tid in config.agno_toolkit_params:
                _deep_merge(params, config.agno_toolkit_params[tid])
            if tid not in seen:
                seen.add(tid)
                result.append((tid, params))
    return result
