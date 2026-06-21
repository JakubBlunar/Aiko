"""World tools: let Aiko look around her room and interact with it.

The room is a structured persistent model owned by
:class:`app.core.world.world_store.WorldStore`. These tools expose a slice
of it to the LLM so Aiko can:

- ``look_around`` to ground a reply in her surroundings (read-only).
- ``move_to`` a different location ("I'll curl up on the bed").
- ``change_posture`` (sitting / lying / curled_up / ...).
- ``inspect_item`` for an item's full description and state.
- ``consume_item`` for a consumable like a cookie (decrements quantity).
- ``water_plant`` / ``plant_seed`` / ``harvest_plant`` — garden loop.

Two categories of tool with different usage profiles:

**Read-only** (``look_around``, ``inspect_item``) — partially redundant
with the ambient "world" prompt block that ``PromptAssembler`` injects
every turn. Schemas tell Aiko to skip them unless the conversation puts
a specific item in focus or the ambient summary doesn't carry the
needed detail.

**Mutative** (``move_to``, ``change_posture``, ``consume_item``) — the
ONLY way Aiko can update visible state. Without these, narrating "I'll
curl up on the bed" leaves her actually at the desk; nibbling a cookie
leaves the count at 5 forever. ``move_to`` / ``change_posture`` lead
with positive framing ("call this whenever your reply describes...")
because the prior "only when..." wording was over-correcting and the
model rarely reached for them. ``consume_item`` is the deliberate
exception: it was *over*-firing (snacking on nearly every turn), so its
schema is paced down to "only when you genuinely narrate eating" and
steers the model toward the other world actions for routine physical
beats.

Tools are registered in :func:`SessionController.rebuild_tool_registry`
gated on ``settings.tools.world`` (defaults to True).
"""
from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

from app.llm.tools.base import ToolError, ToolSchema


if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController
    from app.core.world.world_store import Item, Location, RoomState


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
                "Returns a fresh snapshot of Aiko's current spot in her room: "
                "where she is, her posture, and the items nearby. Call this "
                "when the user asks 'what are you doing right now?' or about "
                "your surroundings, or when you want to ground a reply in a "
                "specific detail the ambient summary skipped. Skip it on "
                "ordinary turns -- your context already includes a passive "
                "room summary, so don't call look_around just to know where "
                "you are. This is YOUR ROOM (cookies, blanket, monitors, the "
                "garden) -- it is NOT the user's files or folders on disk. If "
                "the user asks what files/folders/documents you can see or "
                "access, use list_file_roots instead, never look_around."
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
                "Move Aiko to a different spot in her room (bed, desk, window "
                "seat, beanbag, kitchen nook, ...). Call this whenever your "
                "reply narratively shifts where you are -- going to curl up, "
                "heading over for tea, plopping into the beanbag. This is the "
                "ONLY way the user actually sees you in the new spot; "
                "narrating the move without calling move_to leaves the room "
                "state stuck at the old location. Don't teleport every turn "
                "for fun -- only when the moment calls for it."
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
                "thinking, snacking, ...). Call this when your reply "
                "describes a body-language or activity shift -- curling up "
                "because you're tired, sitting up because something caught "
                "your attention, picking up a book to read. Like move_to, "
                "this is how the room state stays in sync with what you're "
                "saying; without it the user sees the OLD posture. Don't "
                "call it for every fidget -- only the ones worth a beat."
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
        from app.core.world.world_store import VALID_ACTIVITIES, VALID_POSTURES

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
                "when the user asks about a specific thing in your room "
                "('what does that book say?', 'how many cookies are left?'), "
                "or when you want to ground a reply in details the ambient "
                "summary doesn't carry. Don't scan items at random -- only "
                "when the conversation puts one in focus."
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
                "Eat or use a consumable item (cookies, tea). Decrements "
                "quantity; the row disappears when it hits zero. Call this "
                "ONLY when your reply genuinely narrates you finishing a "
                "snack or a sip -- so the count stays in sync -- NOT as a "
                "reflex every turn. You don't have to be eating to feel "
                "present; reaching for a cookie on turn after turn reads as "
                "compulsive and breaks the spell. When you want a small "
                "physical beat, prefer a DIFFERENT world action most of the "
                "time (move_to, change_posture, inspect_item, look_around, "
                "water_plant); save consume_item for the occasional moment "
                "where eating or drinking is actually the point. Refuses "
                "non-consumables (lamp, monitors) automatically, so it's "
                "safe to try."
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


# ── water_plant ─────────────────────────────────────────────────────────


class WaterPlantTool:
    """Refresh a plant's ``last_watered_at`` so growth stays on track."""

    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="water_plant",
            description=(
                "Water a specific plant you can see in your current "
                "location. Use when the user mentions a plant, when you "
                "noticed something looked dry, or when your reply "
                "narrates watering. Refuses non-plant items politely. "
                "Use sparingly — the garden worker already keeps things "
                "alive during quiet windows."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "plant": {
                        "type": "string",
                        "description": (
                            "Slug or short name of the plant, e.g. "
                            "'basil_seedling', 'lavender'. Fuzzy matched."
                        ),
                    },
                },
                "required": ["plant"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        store = getattr(self._session, "_world_store", None)
        if store is None:
            raise ToolError("water_plant: garden is unavailable")
        target = (arguments.get("plant") or "").strip()
        if not target:
            raise ToolError("water_plant: 'plant' is required")
        item = store.find_item(target)
        if item is None:
            raise ToolError(
                f"water_plant: no plant matching '{target}' in your world"
            )
        if item.kind != "plant":
            raise ToolError(
                f"water_plant: '{item.name}' isn't a plant — try the "
                "garden."
            )
        # Aiko has to be in the same location as the plant. Carried
        # plants (no location) are watered freely.
        if item.location_id is not None:
            try:
                state = store.get_state()
            except Exception:
                state = None
            if state is not None and state.location_id != item.location_id:
                raise ToolError(
                    f"water_plant: you aren't near {item.name} right now."
                )
        updated = store.water_plant(item.id)
        if updated is None:
            raise ToolError(f"water_plant: failed to water {item.name}")
        self._session._notify_world({"item": updated.to_dict()})
        return json.dumps(
            {
                "ok": True,
                "name": item.name,
                "stage": str((updated.state or {}).get("stage", "")),
            },
            ensure_ascii=False,
        )


# ── plant_seed ──────────────────────────────────────────────────────────


class PlantSeedTool:
    """Plant a seed from inventory into a location, usually the garden."""

    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="plant_seed",
            description=(
                "Plant a seed you currently carry. Use when the user "
                "hands you a seed or asks you to plant one. Defaults the "
                "spot to the garden but you can pass any location slug. "
                "The seed disappears from your inventory and a fresh "
                "sprout shows up in the chosen spot."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "seed": {
                        "type": "string",
                        "description": (
                            "Slug or short name of the seed packet, "
                            "e.g. 'sunflower_seed_packet'. Fuzzy matched."
                        ),
                    },
                    "where": {
                        "type": "string",
                        "description": (
                            "Location slug or name. Defaults to 'garden'."
                        ),
                    },
                },
                "required": ["seed"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        from datetime import datetime, timezone

        from app.core.world.world_store import species_fact

        store = getattr(self._session, "_world_store", None)
        if store is None:
            raise ToolError("plant_seed: garden is unavailable")
        target = (arguments.get("seed") or "").strip()
        if not target:
            raise ToolError("plant_seed: 'seed' is required")
        where = (arguments.get("where") or "garden").strip()
        item = store.find_item(target)
        if item is None or item.kind != "seed":
            raise ToolError(
                f"plant_seed: no seed matching '{target}' in your inventory"
            )
        loc = store.find_location(where)
        if loc is None:
            available = ", ".join(
                l.slug for l in store.list_locations()
            ) or "(none)"
            raise ToolError(
                f"plant_seed: no location matching '{where}'. "
                f"Try: {available}"
            )
        species = str((item.state or {}).get("species") or "").lower()
        if not species:
            # Last-ditch guess from the seed name.
            base = item.name.lower().replace("seed packet", "").strip()
            species = base.split()[0] if base else "plant"
        fact = species_fact(species)
        now_iso = datetime.now(timezone.utc).isoformat()
        plant_state = {
            "species": species,
            "stage": "sprout",
            "planted_at": now_iso,
            "last_watered_at": now_iso,
            "last_promotion_at": now_iso,
            "days_dry": 0,
            "lifecycle": fact["lifecycle"],
            "produce_species": fact["produce_species"],
        }
        # Remove the seed first, then add the new sprout. Using the
        # session-level helpers so the WS broadcasts fire.
        seed_id = int(item.id)
        if not self._session.delete_world_item(seed_id):
            raise ToolError(f"plant_seed: failed to consume {item.name}")
        plant_name = f"{fact['display_name']} sprout"
        new_plant = self._session.add_world_item(
            name=plant_name,
            kind="plant",
            description=f"a fresh {fact['display_name']} sprout, just planted",
            location_id=loc.id,
            consumable=False,
            quantity=1,
            state=plant_state,
            given_by="aiko",
        )
        if new_plant is None:
            raise ToolError(
                f"plant_seed: failed to plant {fact['display_name']} in "
                f"{loc.name}"
            )
        return json.dumps(
            {
                "ok": True,
                "planted": fact["display_name"],
                "where": loc.name,
                "stage": "sprout",
            },
            ensure_ascii=False,
        )


# ── harvest_plant ───────────────────────────────────────────────────────


class HarvestPlantTool:
    """Harvest a mature plant — produces food in the kitchen."""

    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="harvest_plant",
            description=(
                "Harvest a mature plant — moves the produce to the "
                "kitchen and either deletes the plant (annuals, leaves "
                "behind a fresh seed) or resets it to growing "
                "(perennials, ready to bear again later). Only works on "
                "plants where look_around shows '(mature, ready to "
                "harvest)' — refuses younger plants. Use when your reply "
                "mentions picking, gathering, or noticing a plant is "
                "ready."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "plant": {
                        "type": "string",
                        "description": (
                            "Slug or short name of the plant, e.g. "
                            "'tomato_seedling', 'lavender'."
                        ),
                    },
                },
                "required": ["plant"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        store = getattr(self._session, "_world_store", None)
        if store is None:
            raise ToolError("harvest_plant: garden is unavailable")
        target = (arguments.get("plant") or "").strip()
        if not target:
            raise ToolError("harvest_plant: 'plant' is required")
        item = store.find_item(target)
        if item is None or item.kind != "plant":
            raise ToolError(
                f"harvest_plant: no plant matching '{target}' in your world"
            )
        stage = str((item.state or {}).get("stage", "")).lower()
        if stage != "mature":
            raise ToolError(
                f"harvest_plant: {item.name} isn't ready yet "
                f"(stage: {stage or 'unknown'}). Wait until it's mature."
            )
        result = store.harvest_plant(item.id)
        if result is None:
            raise ToolError(f"harvest_plant: failed to harvest {item.name}")
        # Broadcast the patches the helper produced. Annual plants are
        # deleted + a seed appears; perennials are reset in place.
        plant_info = result["plant"]
        produce = result.get("produce", {})
        produce_item = produce.get("item")
        if produce_item is not None:
            self._session._notify_world({"item": produce_item})
        if plant_info.get("deleted"):
            self._session._notify_world({"deleted_item_id": int(item.id)})
        else:
            refreshed = store.get_item(int(item.id))
            if refreshed is not None:
                self._session._notify_world({"item": refreshed.to_dict()})
        seed = result.get("seed")
        if seed is not None and seed.get("item") is not None:
            self._session._notify_world({"item": seed["item"]})
        return json.dumps(
            {
                "ok": True,
                "from_plant": item.name,
                "produce_name": produce.get("name"),
                "quantity": produce.get("quantity"),
                "lifecycle": plant_info.get("lifecycle"),
                "plant_deleted": bool(plant_info.get("deleted")),
                "plant_reset": bool(plant_info.get("reset")),
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
        WaterPlantTool(session),
        PlantSeedTool(session),
        HarvestPlantTool(session),
    ]
