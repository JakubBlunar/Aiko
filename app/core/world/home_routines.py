"""Voluntary bathroom routines with persisted, inspectable bath stages."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
import math
from typing import Any

from app.core.infra import timephrase
from app.core.world.home_drinks import HomeAction, _version, parse_at

ROUTINE_ACTIONS = frozenset({
    "brush_teeth", "wash_face", "brush_hair", "start_bath", "finish_bath", "drain_bath",
    "restock_bathroom", "take_shower",
})


def bathroom_state(item: Any) -> dict[str, Any] | None:
    state = item.get("state", {}) if isinstance(item, dict) else getattr(item, "state", {})
    bathroom = state.get("home_bathroom")
    if not isinstance(bathroom, dict) or bathroom.get("schema") != 1:
        return None
    supplies = bathroom.get("supplies")
    if (not isinstance(supplies, dict) or not isinstance(bathroom.get("equipment"), list)
            or not isinstance(bathroom.get("bath"), dict)):
        return None
    if any(type(value) is not int or not 0 <= value <= 1000 for value in supplies.values()):
        return None
    if any(not isinstance(value, str) for value in bathroom["equipment"]):
        return None
    return bathroom


def bathroom_view(item: Any, *, now: datetime | None = None) -> dict[str, Any] | None:
    state = bathroom_state(item)
    if state is None:
        return None
    at = now or timephrase.utcnow()
    bath = state["bath"]
    filled = parse_at(bath.get("started_at"))
    ready = parse_at(bath.get("ready_at"))
    minutes = max(0, (at - filled).total_seconds() / 60) if filled else 0
    water = 22 + 18 * math.exp(-minutes / 60)
    wet = parse_at(state.get("towel_wet_at"))
    return {
        "bath_phase": bath.get("phase", "empty"),
        "bath_ready_to_finish": bool(ready is not None and at >= ready),
        "bath_ready_at": bath.get("ready_at"),
        "water_temperature_band": "warm" if water >= 30 else "cool",
        "towel": "dry" if wet is None or (at - wet).total_seconds() >= 14400 else "drying",
        "last_brushed_teeth_at": state.get("last_brushed_teeth_at"),
        "as_of": at.isoformat(),
    }


def routine_actions(items, locations, room, now: datetime) -> list[HomeAction]:
    local = {loc["id"] for loc in locations if loc["scene_id"] == room.get("scene_id")}
    actions = []
    for item in items:
        state = bathroom_state(item)
        if state is None or item["location_id"] not in local or item["quantity"] <= 0:
            continue

        def offer(name, summary, target=item):
            actions.append(HomeAction(name, target["id"], target["location_id"], summary,
                                      (_version(target),)))

        equipment, supplies = state["equipment"], state["supplies"]
        last_brushed = parse_at(state.get("last_brushed_teeth_at"))
        if ("toothbrush" in equipment and "sink" in equipment
                and supplies.get("toothpaste", 0) > 0
                and (last_brushed is None or (now - last_brushed).total_seconds() >= 28800)):
            offer("brush_teeth", "brushed my teeth at the bathroom sink")
        washed = parse_at(state.get("last_washed_face_at"))
        if ("sink" in equipment and "towel" in equipment and supplies.get("soap", 0) > 0
                and (washed is None or (now - washed).total_seconds() >= 14400)):
            offer("wash_face", "washed my face and used the bathroom towel")
        hair = parse_at(state.get("last_brushed_hair_at"))
        if ({"hairbrush", "mirror"}.issubset(equipment)
                and (hair is None or (now - hair).total_seconds() >= 14400)):
            offer("brush_hair", "brushed my hair by the bathroom mirror")
        showered = parse_at(state.get("last_showered_at"))
        if ({"shower", "towel"}.issubset(equipment) and supplies.get("soap", 0) > 0
                and state["bath"].get("phase") == "empty"
                and (showered is None or (now - showered).total_seconds() >= 28800)):
            offer("take_shower", "took a short shower and hung the towel to dry")
        bath = state["bath"]
        if bath.get("phase") == "empty":
            last = parse_at(state.get("last_bath_at"))
            if ("tub" in equipment and "towel" in equipment and supplies.get("soap", 0) > 0
                    and (last is None or (now - last).total_seconds() >= 43200)):
                offer("start_bath", "ran a warm bath and settled in for a soak")
        elif bath.get("phase") == "soaking":
            ready = parse_at(bath.get("ready_at"))
            if ready is not None and now >= ready:
                offer("finish_bath", "ended my bath, drained the tub and hung the towel to dry")
            offer("drain_bath", "ended the bath early and drained the tub")
        stocked = parse_at(state.get("last_restock_at"))
        if (any(supplies.get(key, 0) <= 1 for key in ("toothpaste", "soap"))
                and (stocked is None or (now - stocked).total_seconds() >= 86400)):
            offer("restock_bathroom", "replenished the household bathroom supplies")
    return actions


def apply_routine_action(action: HomeAction, items, now: datetime) -> list[dict[str, Any]]:
    item = deepcopy(items[action.item_id])
    state = item["state"]["home_bathroom"]
    stamp = now.isoformat()
    if action.action == "brush_teeth":
        state["supplies"]["toothpaste"] -= 1
        state["last_brushed_teeth_at"] = stamp
    elif action.action == "wash_face":
        state["supplies"]["soap"] -= 1
        state.update(last_washed_face_at=stamp, towel_wet_at=stamp)
    elif action.action == "brush_hair":
        state["last_brushed_hair_at"] = stamp
    elif action.action == "start_bath":
        state["supplies"]["soap"] -= 1
        state["bath"] = {"phase": "soaking", "started_at": stamp,
                         "ready_at": (now + timedelta(minutes=20)).isoformat()}
        state["last_bath_at"] = stamp
    elif action.action == "take_shower":
        state["supplies"]["soap"] -= 1
        state.update(last_showered_at=stamp, towel_wet_at=stamp)
    elif action.action in {"finish_bath", "drain_bath"}:
        state["bath"] = {"phase": "empty"}
        state["towel_wet_at"] = stamp
    elif action.action == "restock_bathroom":
        state["supplies"] = {"toothpaste": 20, "soap": 20}
        state["last_restock_at"] = stamp
    else:
        raise ValueError("Unsupported bathroom action")
    return [item]
