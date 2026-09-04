"""Brain-lane take_item / put_item for pocketable carrying.

The store enforces portable kinds and the carry cap; these tools are the
chat surface so Aiko can pick up a book or cookie mid-turn and put it
back. See :mod:`app.core.world.carry`.
"""
from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

from app.core.world.carry import CARRY_CAP, is_portable
from app.llm.tools.base import ToolError, ToolSchema
from app.llm.tools.world import _not_found


if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController


class TakeItemTool:
    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="take_item",
            description=(
                "Pick up one pocketable thing so you are holding it: a "
                "cookie, a book, a plush, a keepsake, or a seed packet. "
                "Not the monitors, not a plant, not furniture, not the "
                "lamp. You can hold at most two of these at once (seed "
                "packets don't count). Put it down with put_item when "
                "you're done — pockets aren't for living in."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "item": {
                        "type": "string",
                        "description": "Name or slug of the thing to pick up.",
                    },
                },
                "required": ["item"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        store = getattr(self._session, "_world_store", None)
        if store is None:
            raise ToolError("take_item: room is unavailable")
        target = (arguments.get("item") or "").strip()
        if not target:
            raise ToolError("take_item: 'item' is required")
        item = store.find_item(target)
        if item is None:
            raise _not_found("take_item", target, store)
        if not is_portable(item.kind):
            raise ToolError(
                f"take_item: {item.name} isn't something you can pocket "
                f"(it's a {item.kind}). Leave it where it lives."
            )
        taken = store.take_into_hands(item.id)
        if taken is None:
            raise ToolError(f"take_item: couldn't pick up {item.name}")
        notify = getattr(self._session, "_notify_world", None)
        if callable(notify):
            notify({"item": taken.to_dict()})
        held = store.carried_items(include_seeds=False)
        return json.dumps(
            {
                "ok": True,
                "holding": taken.name,
                "kind": taken.kind,
                "slots_used": len(held),
                "cap": CARRY_CAP,
            },
            ensure_ascii=False,
        )


class PutItemTool:
    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="put_item",
            description=(
                "Put down something you are holding. Omit 'location' to "
                "set it at your current spot, or name a spot (desk, bed, "
                "bookshelf) to place it there. If you don't say, it goes "
                "to wherever you're standing, or back home if you aren't "
                "at a spot."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "item": {
                        "type": "string",
                        "description": "Name or slug of the thing to put down.",
                    },
                    "location": {
                        "type": "string",
                        "description": (
                            "Optional spot slug/name. Empty = current "
                            "spot, else home."
                        ),
                    },
                },
                "required": ["item"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        store = getattr(self._session, "_world_store", None)
        if store is None:
            raise ToolError("put_item: room is unavailable")
        target = (arguments.get("item") or "").strip()
        if not target:
            raise ToolError("put_item: 'item' is required")
        item = store.find_item(target)
        if item is None:
            raise _not_found("put_item", target, store)
        dest_id = None
        loc_q = (arguments.get("location") or "").strip()
        if loc_q:
            loc = store.find_location(loc_q)
            if loc is None:
                raise ToolError(
                    f"put_item: no spot matching '{loc_q}'"
                )
            dest_id = loc.id
        placed = store.put_down(item.id, location_id=dest_id)
        if placed is None:
            raise ToolError(f"put_item: couldn't put down {item.name}")
        notify = getattr(self._session, "_notify_world", None)
        if callable(notify):
            notify({"item": placed.to_dict()})
        loc = (
            store.get_location_by_id(placed.location_id)
            if placed.location_id is not None
            else None
        )
        return json.dumps(
            {
                "ok": True,
                "item": placed.name,
                "where": loc.name if loc is not None else "carried",
            },
            ensure_ascii=False,
        )


def carry_tools(session: "SessionController") -> list[Any]:
    return [TakeItemTool(session), PutItemTool(session)]


__all__ = ["PutItemTool", "TakeItemTool", "carry_tools"]
