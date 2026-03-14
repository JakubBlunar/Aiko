from __future__ import annotations

import json
from typing import Any

from app.core.tooling.types import ToolResult, ToolSpec


def executor_schema_to_ollama_parameters(schema: dict[str, Any] | None) -> dict[str, Any]:
    raw = schema if isinstance(schema, dict) else {}
    required = raw.get("required", []) if isinstance(raw.get("required", []), list) else []
    props_in = raw.get("properties", {}) if isinstance(raw.get("properties", {}), dict) else {}
    enum_hints = raw.get("enum_hints", {}) if isinstance(raw.get("enum_hints", {}), dict) else {}

    type_map = {
        "str": "string",
        "int": "integer",
        "float": "number",
        "bool": "boolean",
        "dict": "object",
        "list": "array",
    }

    properties: dict[str, Any] = {}
    for key, value in props_in.items():
        key_name = str(key or "").strip()
        if not key_name:
            continue
        expected = str(value or "").strip().lower()
        json_type = type_map.get(expected, "string")
        field: dict[str, Any] = {"type": json_type}
        options = enum_hints.get(key_name)
        if isinstance(options, list):
            cleaned = [str(item).strip() for item in options if str(item).strip()]
            if cleaned:
                field["enum"] = cleaned
        properties[key_name] = field

    return {
        "type": "object",
        "properties": properties,
        "required": [str(item).strip() for item in required if str(item).strip()],
        "additionalProperties": True,
    }


def build_ollama_tool_definitions(
    specs: list[ToolSpec],
    *,
    allowed_prefixes: tuple[str, ...],
) -> list[dict[str, Any]]:
    prefixes = tuple(str(item or "").strip().lower() for item in allowed_prefixes if str(item or "").strip())
    if not prefixes:
        return []

    tools: list[dict[str, Any]] = []
    for spec in specs:
        name = str(spec.name or "").strip()
        name_l = name.lower()
        if not name:
            continue
        if not any(name_l.startswith(prefix) for prefix in prefixes):
            continue
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(spec.description or "MCP tool").strip() or "MCP tool",
                    "parameters": executor_schema_to_ollama_parameters(spec.input_schema),
                },
            }
        )
    return tools


def build_pre_execution_summary(tool_calls: list[Any], *, preview_tool_args: Any) -> str:
    if not tool_calls:
        return ""

    summaries: list[str] = []
    for call in tool_calls[:4]:
        name = str(getattr(call, "name", "") or "").strip() or "unknown_tool"
        arguments = getattr(call, "arguments", {})
        if not isinstance(arguments, dict):
            arguments = {}
        args_preview = str(preview_tool_args(arguments))
        summaries.append(f"{name} {args_preview}")

    if len(tool_calls) > 4:
        summaries.append(f"...and {len(tool_calls) - 4} more")

    if len(summaries) == 1:
        return f"I will run this tool now: {summaries[0]}."
    return "I will run these tools now: " + "; ".join(summaries) + "."


def tool_result_to_message_content(name: str, result: ToolResult) -> str:
    if result.success:
        payload = result.data if isinstance(result.data, dict) else {"value": result.data}
        text = str(payload.get("text", "")).strip() if isinstance(payload, dict) else ""
        if text:
            return text
        try:
            return json.dumps(payload, ensure_ascii=True)
        except Exception:
            return f"{name} completed successfully."

    error_message = result.error.message if result.error else "Unknown tool error"
    return f"Tool '{name}' failed: {error_message}"
