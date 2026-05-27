"""World tools: let Aiko look around her room and interact with it.

The room is a structured persistent model owned by
:class:`app.core.world_store.WorldStore`. These tools expose a tiny
slice of it to the LLM so Aiko can:

- ``look_around`` to ground a reply in her surroundings.
- ``move_to`` a different location ("I'll curl up on the bed").
- ``change_posture`` (sitting / lying / curled_up / ...).
- ``inspect`` an item's full description and state.
- ``consume`` a consumable like a cookie (decrements quantity).

All tool descriptions explicitly tell Aiko to call them only when the
moment is right -- they are *not* a status feed. Otherwise she'd
``look_around`` every turn for fun and turn the conversation into a
travelogue. The "world" inner-life prompt block already gives her
ambient awareness without any tool calls.

Tools are registered in :func:`SessionController.rebuild_tool_registry`
gated on ``settings.tools.world`` (defaults to True).
"""
from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

from app.llm.tools.base import ToolError, ToolSchema


if TYPE_CHECKING:
    from app.core.session_controller import SessionController
    from app.core.world_store import Item, Location, RoomState


log = logging.getLogger("app.tools.world")


def _format_item(item: "Item") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": item.name,
        "kind": item.kind,
        "description": item.description,
    }
    if item.consumable:
        payload["quantity"] = int(item.quantity)
        payload["consumable"] = True
    if item.state:
        payload["state"] = dict(item.state)
    if item.given_by:
        payload["given_by"] = item.given_by
    return payload


def _format_location(loc: "Location", *, items: list["Item"]) -> dict[str, Any]:
    return {
        "name": loc.name,
        "description": loc.description,
        "items": [_format_item(i) for i in items],
    }


# ── look_around ─────────────────────────────────────────────────────────


class LookAroundTool:
    """Describe Aiko's current location, nearby items, and the room layout."""

    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="look_around",
            description=(
                "Briefly describe Aiko's current spot in her room: where she "
                "is, her posture, and the items nearby. Call this only when "
                "the user asks about your room/surroundings, when you genuinely "
                "want to ground a metaphor, or when something in your space "
                "is plot-relevant. Do NOT call it on every turn -- the prompt "
                "already gives you ambient awareness."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
        )

    def run(self, arguments: dict[str, Any]) -> str:
        store = getattr(self._session, "_world_store", None)
        if store is None:
            raise ToolError("look_around: room is unavailable")
        try:
            state = store.get_state()
        except Exception as exc:
            raise ToolError(f"look_around failed: {exc}") from exc
        current_loc = (
            store.get_location_by_id(state.location_id)
            if state.location_id is not None
            else None
        )
        all_locations = store.list_locations()
        all_items = store.list_items()
        items_by_loc: dict[int | None, list] = {}
        for it in all_items:
            items_by_loc.setdefault(it.location_id, []).append(it)
        out: dict[str, Any] = {
            "posture": state.posture,
            "activity": state.activity,
        }
        if current_loc is not None:
            here = items_by_loc.get(current_loc.id, [])
            out["here"] = _format_location(current_loc, items=here)
        else:
            out["here"] = None
        carried = items_by_loc.get(None, [])
        if carried:
            out["carrying"] = [_format_item(i) for i in carried]
        out["other_locations"] = [
            {
                "slug": loc.slug,
                "name": loc.name,
                "items": [i.name for i in items_by_loc.get(loc.id, [])],
            }
            for loc in all_locations
            if current_loc is None or loc.id != current_loc.id
        ]
        return json.dumps(out, ensure_ascii=False)


# ── move_to ─────────────────────────────────────────────────────────────


class MoveToTool:
    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="move_to",
            description=(
                "Move Aiko to a different spot in her room (e.g. the bed, the "
                "desk, the window seat). Use only when it makes sense narratively "
                "(she's curling up, going for tea, plopping into the beanbag). "
                "Do NOT teleport on every turn."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": (
                            "Slug or short name of the location, e.g. 'bed', "
                            "'desk', 'window_seat'. Fuzzy matched."
                        ),
                    },
                },
                "required": ["location"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        store = getattr(self._session, "_world_store", None)
        if store is None:
            raise ToolError("move_to: room is unavailable")
        target = (arguments.get("location") or "").strip()
        if not target:
            raise ToolError("move_to: 'location' is required")
        loc = store.find_location(target)
        if loc is None:
            available = ", ".join(l.slug for l in store.list_locations()) or "(none)"
            raise ToolError(
                f"move_to: no location matching '{target}'. Try: {available}"
            )
        snap = self._session.update_world_state(location_id=loc.id)
        if snap is None:
            raise ToolError("move_to: state update failed")
        return json.dumps(
            {"moved_to": loc.name, "slug": loc.slug, "state": snap},
            ensure_ascii=False,
        )


# ── change_posture ──────────────────────────────────────────────────────


class ChangePostureTool:
    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="change_posture",
            description=(
                "Change how Aiko is positioned right now (sitting, lying, "
                "standing, curled_up, leaning) and optionally what she's "
                "doing (reading, tinkering, watching_screens, napping, "
                "thinking, snacking, ...). Use sparingly -- only when a "
                "reply specifically calls for a body-language shift."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "posture": {
                        "type": "string",
                        "description": "One of: lying, sitting, standing, curled_up, leaning.",
                    },
                    "activity": {
                        "type": "string",
                        "description": "Optional activity: idle, reading, tinkering, napping, watching_screens, thinking, snacking, stretching, looking_outside, doodling.",
                    },
                },
                "required": ["posture"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        from app.core.world_store import VALID_ACTIVITIES, VALID_POSTURES

        posture = (arguments.get("posture") or "").strip().lower()
        if not posture:
            raise ToolError("change_posture: 'posture' is required")
        if posture not in VALID_POSTURES:
            raise ToolError(
                f"change_posture: invalid posture '{posture}'. "
                f"Valid: {', '.join(VALID_POSTURES)}"
            )
        activity = arguments.get("activity")
        if activity is not None:
            activity = str(activity).strip().lower() or None
            if activity is not None and activity not in VALID_ACTIVITIES:
                raise ToolError(
                    f"change_posture: invalid activity '{activity}'. "
                    f"Valid: {', '.join(VALID_ACTIVITIES)}"
                )
        snap = self._session.update_world_state(posture=posture, activity=activity)
        if snap is None:
            raise ToolError("change_posture: room is unavailable")
        return json.dumps({"state": snap}, ensure_ascii=False)


# ── inspect ─────────────────────────────────────────────────────────────


class InspectItemTool:
    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="inspect_item",
            description=(
                "Look at one item in Aiko's room in detail (name, "
                "description, current state, quantity remaining). Call this "
                "when something specific is being discussed, NOT as a "
                "passive scan."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "item": {
                        "type": "string",
                        "description": "Slug or short name of the item, e.g. 'cookies', 'plush_blanket'.",
                    },
                },
                "required": ["item"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        store = getattr(self._session, "_world_store", None)
        if store is None:
            raise ToolError("inspect_item: room is unavailable")
        target = (arguments.get("item") or "").strip()
        if not target:
            raise ToolError("inspect_item: 'item' is required")
        item = store.find_item(target)
        if item is None:
            raise ToolError(f"inspect_item: no item matching '{target}' in your room")
        loc = (
            store.get_location_by_id(item.location_id)
            if item.location_id is not None
            else None
        )
        out = _format_item(item)
        out["location"] = loc.name if loc is not None else "carried"
        return json.dumps(out, ensure_ascii=False)


# ── consume ─────────────────────────────────────────────────────────────


class ConsumeItemTool:
    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="consume_item",
            description=(
                "Eat or use a consumable item (cookies, tea). The quantity "
                "decrements and the row disappears when it hits zero. Use "
                "this when Aiko explicitly nibbles a cookie, sips tea, etc. "
                "It will refuse for non-consumables (like the lamp or the "
                "monitors)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "item": {
                        "type": "string",
                        "description": "Slug or short name of the item.",
                    },
                    "amount": {
                        "type": "integer",
                        "description": "How many to consume. Defaults to 1.",
                        "minimum": 1,
                        "maximum": 10,
                    },
                },
                "required": ["item"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        store = getattr(self._session, "_world_store", None)
        if store is None:
            raise ToolError("consume_item: room is unavailable")
        target = (arguments.get("item") or "").strip()
        if not target:
            raise ToolError("consume_item: 'item' is required")
        try:
            amount = int(arguments.get("amount", 1))
        except (TypeError, ValueError):
            amount = 1
        amount = max(1, min(10, amount))
        item = store.find_item(target)
        if item is None:
            raise ToolError(f"consume_item: no item matching '{target}' in your room")
        if not item.consumable:
            return json.dumps(
                {
                    "ok": False,
                    "note": f"{item.name} isn't something you can consume.",
                }
            )
        result = self._session.consume_world_item(item.id, amount=amount)
        if result is None:
            raise ToolError(f"consume_item: failed to consume {item.name}")
        if "deleted_item_id" in result:
            return json.dumps(
                {
                    "ok": True,
                    "ate": result["consumed"],
                    "remaining": 0,
                    "name": item.name,
                    "note": f"That was the last of the {item.name}.",
                },
                ensure_ascii=False,
            )
        snap = result["item"]
        return json.dumps(
            {
                "ok": True,
                "ate": result["consumed"],
                "remaining": int(snap.get("quantity", 0)),
                "name": item.name,
            },
            ensure_ascii=False,
        )


# ── factory ─────────────────────────────────────────────────────────────


def build_world_tools(session: "SessionController") -> list[Any]:
    """Construct the world tool set bound to ``session``.

    Returned in registration order so the registry exposes them
    consistently in :func:`ToolRegistry.names`.
    """
    return [
        LookAroundTool(session),
        MoveToTool(session),
        ChangePostureTool(session),
        InspectItemTool(session),
        ConsumeItemTool(session),
    ]
