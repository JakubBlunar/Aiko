from __future__ import annotations

from collections.abc import Callable
import time
from typing import Any

from app.core.tooling.config_loader import ToolingConfig
from app.core.tooling.registry import ToolRegistry
from app.core.tooling.types import ConfirmationRequest, ToolContext, ToolError, ToolResult


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, config: ToolingConfig) -> None:
        self._registry = registry
        self._config = config
        self._calls_in_turn = 0

    def reset_turn_budget(self) -> None:
        self._calls_in_turn = 0

    def list_available_tools(self) -> list[str]:
        return [spec.name for spec in self._registry.filtered_specs(
            enabled=self._config.enabled_tools,
            disabled=self._config.disabled_tools,
        )]

    def invoke(
        self,
        name: str,
        *,
        args: dict[str, Any] | None = None,
        context: ToolContext | None = None,
        cancel_token: Callable[[], bool] | None = None,
    ) -> ToolResult:
        if self._calls_in_turn >= self._config.policies.max_tool_calls_per_turn:
            return ToolResult(
                success=False,
                error=ToolError(
                    code="tool_budget_exceeded",
                    message="Maximum tool calls per turn exceeded.",
                ),
            )

        disabled_set = {item.strip() for item in self._config.disabled_tools if item.strip()}
        enabled_set = {item.strip() for item in self._config.enabled_tools if item.strip()}
        if name in disabled_set:
            return ToolResult(
                success=False,
                error=ToolError(code="tool_disabled", message=f"Tool '{name}' is disabled."),
            )
        if enabled_set and name not in enabled_set:
            return ToolResult(
                success=False,
                error=ToolError(code="tool_not_enabled", message=f"Tool '{name}' is not enabled."),
            )

        tool = self._registry.get(name)
        if tool is None:
            return ToolResult(
                success=False,
                error=ToolError(code="tool_not_found", message=f"Unknown tool '{name}'."),
            )

        validated_args = dict(args or {})
        schema_error = self._validate_args(validated_args, tool.spec.input_schema)
        if schema_error is not None:
            return ToolResult(success=False, error=schema_error)

        if tool.spec.is_mutating and not self._config.policies.full_auto and self._config.policies.mutating_requires_confirmation:
            return ToolResult(
                success=False,
                requires_confirmation=True,
                confirmation=ConfirmationRequest(
                    tool_name=tool.spec.name,
                    summary=f"Tool '{tool.spec.name}' needs confirmation before execution.",
                    args=dict(args or {}),
                ),
                error=ToolError(
                    code="confirmation_required",
                    message="Mutating tool requires confirmation.",
                ),
            )

        started = time.perf_counter()
        try:
            self._calls_in_turn += 1
            result = tool.run(context or ToolContext(), validated_args, cancel_token=cancel_token)
            result.duration_ms = (time.perf_counter() - started) * 1000.0
            return result
        except Exception as exc:
            return ToolResult(
                success=False,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                error=ToolError(code="tool_exception", message=str(exc)),
            )

    @staticmethod
    def _validate_args(args: dict[str, Any], schema: dict[str, Any]) -> ToolError | None:
        if not isinstance(schema, dict):
            return None

        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                key_name = str(key).strip()
                if not key_name:
                    continue
                if key_name not in args:
                    return ToolError(
                        code="missing_required_arg",
                        message=f"Missing required argument: '{key_name}'.",
                    )

        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return None

        type_map: dict[str, type] = {
            "str": str,
            "int": int,
            "float": float,
            "bool": bool,
            "dict": dict,
            "list": list,
        }

        for key, expected in properties.items():
            key_name = str(key)
            if key_name not in args:
                continue
            expected_name = str(expected).strip().lower()
            if expected_name not in type_map:
                continue
            if expected_name == "float" and isinstance(args[key_name], int) and not isinstance(args[key_name], bool):
                continue
            if not isinstance(args[key_name], type_map[expected_name]):
                return ToolError(
                    code="invalid_arg_type",
                    message=(
                        f"Invalid argument type for '{key_name}': expected {expected_name}, "
                        f"got {type(args[key_name]).__name__}."
                    ),
                )

        return None
