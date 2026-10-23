"""Preserve malformed arguments as errors rather than executable defaults."""

import json
import math
from typing import Any


def parse_tool_arguments(raw: object) -> tuple[dict[str, Any], str | None]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError, RecursionError):
            return {}, "Arguments must be a valid JSON object."
    if not isinstance(raw, dict):
        return {}, "Arguments must be a JSON object."
    try:
        valid = _json_value(raw)
    except RecursionError:
        valid = False
    if not valid:
        return {}, "Arguments must contain finite JSON values and string keys."
    return dict(raw), None


def _json_value(value: object) -> bool:
    if isinstance(value, dict):
        return all(isinstance(k, str) and _json_value(v) for k, v in value.items())
    if isinstance(value, list):
        return all(_json_value(v) for v in value)
    return value is None or isinstance(value, (str, bool, int)) or (
        isinstance(value, float) and math.isfinite(value)
    )
