from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class ToolPolicyConfig:
    full_auto: bool = False
    read_only_auto: bool = True
    mutating_requires_confirmation: bool = True
    max_tool_calls_per_turn: int = 64
    default_timeout_ms: int = 10000


@dataclass(slots=True)
class ToolingConfig:
    enabled_tools: list[str] = field(default_factory=list)
    disabled_tools: list[str] = field(default_factory=list)
    policies: ToolPolicyConfig = field(default_factory=ToolPolicyConfig)
    tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    runtime_overrides: dict[str, Any] = field(default_factory=dict)

    def tool_settings(self, namespace: str) -> dict[str, Any]:
        """Return settings for a tool namespace, for example 'persona' or 'ocr'."""
        key = str(namespace or "").strip().lower()
        value = self.tools.get(key, {})
        return dict(value) if isinstance(value, dict) else {}


_TOOLING_DEFAULT_PATH = Path(__file__).resolve().parents[3] / "config" / "tooling.default.yaml"
_TOOLING_USER_PATH = Path(__file__).resolve().parents[3] / "config" / "tooling.user.yaml"


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


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
    base = _read_yaml(default_path or _TOOLING_DEFAULT_PATH)
    user = _read_yaml(user_path or _TOOLING_USER_PATH)
    merged = _deep_merge(base, user)

    if runtime_overrides:
        merged = _deep_merge(merged, runtime_overrides)

    policies_raw = merged.get("policies", {}) if isinstance(merged.get("policies"), dict) else {}
    policies = ToolPolicyConfig(
        full_auto=bool(policies_raw.get("full_auto", False)),
        read_only_auto=bool(policies_raw.get("read_only_auto", True)),
        mutating_requires_confirmation=bool(policies_raw.get("mutating_requires_confirmation", True)),
        max_tool_calls_per_turn=max(1, int(policies_raw.get("max_tool_calls_per_turn", 64))),
        default_timeout_ms=max(100, int(policies_raw.get("default_timeout_ms", 10000))),
    )

    tools_raw = merged.get("tools", {}) if isinstance(merged.get("tools"), dict) else {}
    tools: dict[str, dict[str, Any]] = {}
    for key, value in tools_raw.items():
        if isinstance(value, dict):
            tools[str(key).strip().lower()] = dict(value)

    return ToolingConfig(
        enabled_tools=_as_list(merged.get("enabled_tools", [])),
        disabled_tools=_as_list(merged.get("disabled_tools", [])),
        policies=policies,
        tools=tools,
        runtime_overrides=(runtime_overrides or {}),
    )
