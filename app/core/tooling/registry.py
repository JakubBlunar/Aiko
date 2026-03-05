from __future__ import annotations

from collections.abc import Iterable

from app.core.tooling.contracts import Tool
from app.core.tooling.types import ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        name = str(tool.spec.name or "").strip()
        if not name:
            raise ValueError("Tool name must be non-empty")
        self._tools[name] = tool

    def register_many(self, tools: Iterable[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all_specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def filtered_specs(self, *, enabled: list[str], disabled: list[str]) -> list[ToolSpec]:
        enabled_set = {item.strip() for item in enabled if item.strip()}
        disabled_set = {item.strip() for item in disabled if item.strip()}
        specs: list[ToolSpec] = []
        for spec in self.all_specs():
            if spec.name in disabled_set:
                continue
            if enabled_set and spec.name not in enabled_set:
                continue
            specs.append(spec)
        return specs
