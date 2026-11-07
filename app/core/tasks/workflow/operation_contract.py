"""Shared operation schema and admission contracts for the workflow harness."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator


def mcp_effect(descriptor: Any) -> str:
    if descriptor.server_id == "filesystem":
        return (
            "read"
            if descriptor.name
            in {
                "read_file",
                "read_text_file",
                "read_media_file",
                "read_multiple_files",
                "list_directory",
                "list_directory_with_sizes",
                "directory_tree",
                "search_files",
                "get_file_info",
                "list_allowed_directories",
            }
            else "write"
        )
    return getattr(descriptor, "effect", "unknown")


def input_schema(skill: Any) -> dict[str, Any]:
    declared = getattr(skill, "input_schema", None)
    if declared is not None:
        return deepcopy(declared)
    properties, required = {}, []
    for name, raw in (getattr(skill, "arg_schema", None) or {}).items():
        spec = dict(raw) if isinstance(raw, dict) else {}
        if spec.pop("required", False):
            required.append(name)
        properties[name] = spec
    # Existing native v1 skills with no schema remain explicitly open.
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": not bool(properties),
    }


def validate_arguments(skill: Any, args: Any) -> list[str]:
    if not isinstance(args, dict):
        return ["arguments must be an object"]
    try:
        validator = Draft202012Validator(input_schema(skill))
        errors = sorted(validator.iter_errors(args), key=lambda e: str(list(e.path)))
        return [
            f"{'/'.join(map(str, error.path)) or 'args'}: {error.message}"[:300]
            for error in errors[:5]
        ]
    except Exception:
        return ["operation has an invalid input schema"]
