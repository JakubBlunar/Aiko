from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


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
    toolkits: list[Any] = field(default_factory=list)
    toolkit_params: dict[str, dict[str, Any]] = field(default_factory=dict)
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
    tools_dict: dict[str, dict[str, Any]] = {}
    for key, value in tools_raw.items():
        if isinstance(value, dict) and str(key).strip().lower() != "coding":
            tools_dict[str(key).strip().lower()] = dict(value)

    toolkits = _agno_entries_raw(merged.get("toolkits", merged.get("agno_toolkits", [])))
    toolkit_params = _agno_params_raw(merged.get("toolkit_params", merged.get("agno_toolkit_params", {})))

    return ToolingConfig(
        enabled_tools=_as_list(merged.get("enabled_tools", [])),
        disabled_tools=_as_list(merged.get("disabled_tools", [])),
        toolkits=toolkits,
        toolkit_params=toolkit_params,
        agno_toolkits=toolkits,
        agno_toolkit_params=toolkit_params,
        policies=policies,
        tools=tools_dict,
        runtime_overrides=(runtime_overrides or {}),
    )


def resolve_toolkit_entries(config: ToolingConfig) -> list[tuple[str, dict[str, Any]]]:
    """
    Resolve toolkits (list of ids or { id, params }) and toolkit_params into
    a list of (toolkit_id, params_dict). Backward compatible with agno_toolkits.
    """
    entries = config.toolkits if config.toolkits else config.agno_toolkits
    params_map = config.toolkit_params if config.toolkit_params else config.agno_toolkit_params
    result: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for entry in entries:
        if isinstance(entry, str):
            tid = (entry or "").strip()
            if tid and tid not in seen:
                seen.add(tid)
                result.append((tid, dict(params_map.get(tid, {}))))
        elif isinstance(entry, dict):
            tid = (entry.get("id") or entry.get("toolkit_id") or "").strip()
            if not tid:
                continue
            params = dict(entry.get("params", {})) if isinstance(entry.get("params"), dict) else {}
            if tid in params_map:
                _deep_merge(params, params_map[tid])
            if tid not in seen:
                seen.add(tid)
                result.append((tid, params))
    return result


def resolve_agno_toolkit_entries(config: ToolingConfig) -> list[tuple[str, dict[str, Any]]]:
    """Backward-compatible alias for resolve_toolkit_entries."""
    return resolve_toolkit_entries(config)
