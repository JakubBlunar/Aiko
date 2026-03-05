from __future__ import annotations

from collections.abc import Callable
import json
import time
from typing import Any
from typing import Protocol

from app.core.tooling.contracts import Tool
from app.core.tooling.types import ToolContext, ToolError, ToolResult, ToolSpec


class MCPClientLike(Protocol):
    def list_tools(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        ...

    def call_tool(self, *, name: str, args: dict[str, Any], timeout_ms: int = 10000) -> dict[str, Any]:
        ...


class MCPToolWrapper:
    def __init__(
        self,
        *,
        spec: ToolSpec,
        mcp_tool_name: str,
        client: MCPClientLike,
        timeout_ms: int,
    ) -> None:
        self.spec = spec
        self._mcp_tool_name = str(mcp_tool_name)
        self._client = client
        self._timeout_ms = max(100, int(timeout_ms))

    def run(
        self,
        context: ToolContext,
        args: dict,
        cancel_token: Callable[[], bool] | None = None,
    ) -> ToolResult:
        _ = context
        started = time.perf_counter()
        if cancel_token and cancel_token():
            return ToolResult(
                success=False,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                error=ToolError(code="tool_cancelled", message="Tool call cancelled before execution."),
            )

        try:
            result = self._client.call_tool(
                name=self._mcp_tool_name,
                args=dict(args),
                timeout_ms=self._timeout_ms,
            )
        except TimeoutError as exc:
            return ToolResult(
                success=False,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                error=ToolError(code="mcp_timeout", message=str(exc)),
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                error=ToolError(code="mcp_call_failed", message=str(exc)),
            )

        is_error = bool(result.get("isError", False))
        payload = _normalize_mcp_result_payload(result)
        if is_error:
            return ToolResult(
                success=False,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                data=payload,
                error=ToolError(
                    code="mcp_tool_error",
                    message=str(payload.get("text", "MCP tool returned error.")).strip() or "MCP tool returned error.",
                    details={"result": payload},
                ),
            )

        return ToolResult(
            success=True,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            data=payload,
        )


def build_mcp_tools(
    *,
    client: MCPClientLike,
    prefix: str,
    timeout_ms: int,
    mutating_tools: set[str],
    allowed_tools: set[str],
    blocked_tools: set[str],
) -> list[Tool]:
    tools: list[Tool] = []
    prefix_norm = str(prefix or "mcp").strip().strip(".") or "mcp"

    for raw in client.list_tools(refresh=False):
        if not isinstance(raw, dict):
            continue
        source_name = str(raw.get("name", "")).strip()
        if not source_name:
            continue

        if blocked_tools and source_name in blocked_tools:
            continue
        if allowed_tools and source_name not in allowed_tools:
            continue

        mapped_name = f"{prefix_norm}.{source_name}"
        input_schema = raw.get("inputSchema", {})
        if not isinstance(input_schema, dict):
            input_schema = {}

        spec = ToolSpec(
            name=mapped_name,
            description=str(raw.get("description", "MCP tool")).strip() or "MCP tool",
            is_mutating=(source_name in mutating_tools),
            input_schema=_schema_to_executor_shape(input_schema),
            output_schema={},
        )
        tools.append(
            MCPToolWrapper(
                spec=spec,
                mcp_tool_name=source_name,
                client=client,
                timeout_ms=timeout_ms,
            )
        )
    return tools


def _schema_to_executor_shape(input_schema: dict[str, Any]) -> dict[str, Any]:
    properties = input_schema.get("properties", {}) if isinstance(input_schema, dict) else {}
    required = input_schema.get("required", []) if isinstance(input_schema, dict) else []

    mapped_props: dict[str, str] = {}
    enum_hints: dict[str, list[str]] = {}
    if isinstance(properties, dict):
        for key, value in properties.items():
            key_name = str(key).strip()
            if not key_name or not isinstance(value, dict):
                continue
            type_name = str(value.get("type", "")).strip().lower()
            if type_name == "number":
                type_name = "float"
            if type_name == "integer":
                type_name = "int"
            if type_name == "boolean":
                type_name = "bool"
            if type_name == "string":
                type_name = "str"
            if type_name == "object":
                type_name = "dict"
            if type_name == "array":
                type_name = "list"
            if type_name in {"str", "int", "float", "bool", "dict", "list"}:
                mapped_props[key_name] = type_name

            raw_enum = value.get("enum")
            if isinstance(raw_enum, list):
                enum_values = [str(item).strip() for item in raw_enum if str(item).strip()]
                if enum_values:
                    enum_hints[key_name] = enum_values

    required_list: list[str] = []
    if isinstance(required, list):
        for item in required:
            text = str(item or "").strip()
            if text:
                required_list.append(text)

    schema_shape = {
        "required": required_list,
        "properties": mapped_props,
    }
    if enum_hints:
        schema_shape["enum_hints"] = enum_hints
    return schema_shape


def _normalize_mcp_result_payload(result: dict[str, Any]) -> dict[str, Any]:
    content = result.get("content", [])
    payload: dict[str, Any] = {
        "raw": result,
        "text": "",
    }

    texts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type", "")).strip().lower()
            if item_type == "text":
                text = str(item.get("text", "")).strip()
                if text:
                    texts.append(text)
            elif item_type == "json":
                value = item.get("json", {})
                payload.setdefault("json_items", []).append(value)
                try:
                    texts.append(json.dumps(value, ensure_ascii=True))
                except Exception:
                    pass

    payload["text"] = "\n".join(texts).strip()
    if "structuredContent" in result:
        payload["structured_content"] = result.get("structuredContent")
    return payload
