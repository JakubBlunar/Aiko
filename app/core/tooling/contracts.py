from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from app.core.tooling.types import ToolContext, ToolResult, ToolSpec


class Tool(Protocol):
    spec: ToolSpec

    def run(
        self,
        context: ToolContext,
        args: dict,
        cancel_token: Callable[[], bool] | None = None,
    ) -> ToolResult:
        ...
