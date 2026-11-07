"""Live MCP contracts and shared approval policy at the operation boundary."""

from __future__ import annotations

import hashlib
import json

from jsonschema import Draft202012Validator

from app.core.tasks.capabilities import TaskCapability
from app.core.tasks.workflow.operation_contract import mcp_effect


def prepare(manager, args: dict) -> tuple[dict | None, str | None]:
    descriptor = next(
        (
            item
            for item in manager.list_available_tools()
            if item.server_id == args.get("server_id") and item.name == args.get("tool_name")
        ),
        None,
    )
    if descriptor is None:
        return None, "The operation is no longer exposed by its capability."
    if args.get("_config_generation", "") != descriptor.generation:
        return None, "The plugin configuration changed; this operation needs a fresh plan."
    errors = list(
        Draft202012Validator(descriptor.input_schema).iter_errors(args.get("tool_args") or {})
    )
    if errors:
        return None, "Invalid operation arguments: " + errors[0].message[:300]
    effect = mcp_effect(descriptor)
    signature = hashlib.sha256(
        json.dumps(
            [descriptor.server_id, descriptor.name, descriptor.input_schema,
             effect, descriptor.generation],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    capability_id = f"mcp:{descriptor.server_id}:{descriptor.name}"
    return {
        "effect": effect,
        "generation": descriptor.generation,
        "signature": signature,
        "capability_id": capability_id,
        "approval_id": (f"{capability_id}@{descriptor.generation}"
                        if descriptor.generation else capability_id),
    }, None


def capability(args: dict) -> TaskCapability:
    return TaskCapability(
        id=f"mcp:{args['server_id']}:{args['tool_name']}",
        label=f"use {args['server_id']}/{args['tool_name']}",
        destructive=True,
    )


def action_preview(args: dict) -> str:
    def clean(value):
        if isinstance(value, dict):
            return {
                key: "[redacted]"
                if any(
                    word in key.lower()
                    for word in (
                        "password",
                        "token",
                        "secret",
                        "authorization",
                        "credential",
                        "api_key",
                    )
                )
                else clean(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [clean(item) for item in value[:20]]
        return value

    return json.dumps(clean(args.get("tool_args") or {}), ensure_ascii=False)[:2500]
