"""Conversation adapters for the shared, synchronous household action service."""
from __future__ import annotations

import json
from typing import Any

from app.llm.tools.base import ToolError, ToolSchema


class AvailableHomeActionsTool:
    def __init__(self, session: Any) -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="available_home_actions",
            description=(
                "Read the supported household actions and exact live tokens, including making "
                "tea, coffee, hot chocolate, water or juice, sipping, reheating, emptying and "
                "washing reusable vessels. Synchronous, read-only. Use when choosing a home "
                "activity or checking what is possible. Each action names its required location "
                "and item. Move there and bring the vessel with take_item/put_item if needed, "
                "then refresh actions. Missing supplies/equipment never count as available."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
        )

    def run(self, arguments: dict[str, Any]) -> str:
        store = getattr(self._session, "_world_store", None)
        if store is None:
            raise ToolError("The home is unavailable")
        return json.dumps(store.available_home_actions(), ensure_ascii=False)


class PerformHomeActionTool:
    def __init__(self, session: Any) -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="perform_home_action",
            description=(
                "Execute one small simulated household action synchronously using an exact "
                "token returned by available_home_actions. Call when actually preparing or "
                "drinking a drink, reheating it, washing a vessel, or caring for supplies. "
                "Move to its required spot first. Effects and receipt are saved together. "
                "Stale actions fail: inspect available actions again. already_applied means "
                "the original action was recovered, not another completed experience. "
                "A posture/activity change alone does not prepare or consume a drink."
            ),
            parameters={"type": "object", "properties": {
                "token": {"type": "string", "minLength": 64, "maxLength": 64},
            }, "required": ["token"]},
        )

    def run(self, arguments: dict[str, Any]) -> str:
        store = getattr(self._session, "_world_store", None)
        if store is None:
            raise ToolError("The home is unavailable")
        try:
            receipt = store.perform_home_action(arguments.get("token"))
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        self._session._notify_world({"snapshot": store.snapshot()})
        return json.dumps(receipt, ensure_ascii=False)
