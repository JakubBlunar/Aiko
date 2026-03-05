from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    is_mutating: bool = False
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolError:
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ConfirmationRequest:
    tool_name: str
    summary: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ToolResult:
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: ToolError | None = None
    duration_ms: float = 0.0
    requires_confirmation: bool = False
    confirmation: ConfirmationRequest | None = None


@dataclass(slots=True)
class ToolContext:
    metadata: dict[str, Any] = field(default_factory=dict)
