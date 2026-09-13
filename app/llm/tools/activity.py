"""C7 on-demand ``get_activity`` tool.

Forced sample on the same ingest + redact path as the push collector.
Timeout or no desktop returns the last stored session, not an error.
"""
from __future__ import annotations

from typing import Any, Callable

from app.llm.tools.base import ToolSchema


class GetActivityTool:
    """What the user is doing on the machine right now, if we know."""

    def __init__(self, pull: Callable[[], str]) -> None:
        self._pull = pull

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="get_activity",
            description=(
                "Look up what the user is doing on this machine right now "
                "-- foreground app, and a window title only when that app "
                "is allowlisted. Call this when they ask what they are "
                "looking at, which app is in front, or what is on screen. "
                "Returns a live sample when the desktop collector answers "
                "quickly, otherwise the last stored session with how stale "
                "it is. Never a screen recording."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        del arguments
        return self._pull()
