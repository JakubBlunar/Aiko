"""Shared, deterministic drink capabilities for conversation and idle life.

These are simulated household actions. Availability is derived from stored
objects; prose cannot grant ingredients, equipment, or a completed experience.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any

from app.core.infra import timephrase

AMBIENT_C = 22.0
RECIPES = {
    "tea": {"name": "tea", "temperature": 85.0, "equipment": "kettle"},
    "coffee": {"name": "coffee", "temperature": 85.0, "equipment": "coffee_press"},
    "hot_chocolate": {"name": "hot chocolate", "temperature": 70.0, "equipment": "kettle"},
    "water": {"name": "water", "temperature": AMBIENT_C, "equipment": "tap"},
    "juice": {"name": "juice", "temperature": 8.0, "equipment": "fridge"},
}


def new_vessel(name: str) -> dict[str, Any]:
    """Defaults for an explicitly named vessel, including declared lid/insulation."""
    label = name.lower()
    words = set(re.findall(r"[a-z]+", label))
    return {
        "schema": 1, "capacity_ml": 250, "clean": True, "drink": None,
        "heat_safe": "glass" not in words,
        "has_lid": bool(words & {"lid", "lidded", "thermos"}) or "travel mug" in label,
        "insulated": bool(words & {"insulated", "thermos"}),
        "lid_closed": False,
    }


def parse_at(value: Any) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at
    except (ValueError, TypeError):
        return None


def vessel_state(item: Any) -> dict[str, Any] | None:
    state = item.get("state", {}) if isinstance(item, dict) else getattr(item, "state", {})
    vessel = state.get("home_vessel")
    if not isinstance(vessel, dict) or vessel.get("schema") != 1:
        return None
    drink = vessel.get("drink")
    if drink is not None:
        if not isinstance(drink, dict):
            return None
        amount = drink.get("amount_ml", 0)
        if (type(amount) not in (float, int) or not math.isfinite(amount)
                or not 0 <= amount <= 2000):
            return None
    capacity = vessel.get("capacity_ml", 250)
    if type(capacity) not in (int, float) or not math.isfinite(capacity):
        return None
    return vessel


def station_state(item: dict[str, Any]) -> dict[str, Any] | None:
    state = item["state"].get("home_drink_station")
    if not isinstance(state, dict) or state.get("schema") != 1:
        return None
    if not isinstance(state.get("equipment"), list) or not isinstance(state.get("supplies"), dict):
        return None
    if any(type(value) is not int or not 0 <= value <= 1000
           for value in state["supplies"].values()):
        return None
    return state


def temperature(vessel: dict[str, Any], now: datetime) -> float:
    drink = vessel.get("drink") or {}
    at = parse_at(drink.get("thermal_at"))
    try:
        initial = float(drink.get("thermal_c", AMBIENT_C))
    except (TypeError, ValueError):
        initial = AMBIENT_C
    if not math.isfinite(initial):
        initial = AMBIENT_C
    minutes = max(0.0, (now - at).total_seconds() / 60) if at else 0.0
    # Simple indoor simulation; insulation slows warming as well as cooling.
    tau = 24.0
    if vessel.get("insulated"):
        tau *= 2
    if vessel.get("has_lid") and vessel.get("lid_closed"):
        tau *= 2
    return AMBIENT_C + (initial - AMBIENT_C) * math.exp(-minutes / tau)


def drink_view(item: Any, *, now: datetime | None = None) -> dict[str, Any] | None:
    vessel = vessel_state(item)
    if vessel is None:
        return None
    at = now or timephrase.utcnow()
    drink = vessel.get("drink") or {}
    degrees = temperature(vessel, at)
    band = next((label for ceiling, label in (
        (10, "chilled"), (18, "cool"), (28, "room temperature"),
        (40, "lukewarm"), (60, "warm"), (float("inf"), "hot"),
    ) if degrees < ceiling), "room temperature")
    return {
        "recipe": drink.get("recipe"), "name": drink.get("name"),
        "remaining_ml": drink.get("amount_ml", 0), "temperature_band": band,
        "clean": bool(vessel.get("clean", True)),
        "has_lid": bool(vessel.get("has_lid")),
        "lid_closed": bool(vessel.get("lid_closed")),
        "as_of": at.isoformat(),
    }


@dataclass(frozen=True)
class HomeAction:
    action: str
    item_id: int
    location_id: int
    summary: str
    targets: tuple[dict[str, Any], ...]
    recipe: str = ""
    station_id: int | None = None
    parameters: dict[str, Any] = field(default_factory=dict)

    @property
    def token(self) -> str:
        payload = {
            "action": self.action, "recipe": self.recipe, "item_id": self.item_id,
            "location_id": self.location_id, "targets": self.targets,
        }
        if self.parameters:
            payload["parameters"] = self.parameters
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(raw.encode()).hexdigest()

    def to_dict(self, *, now: datetime | None = None) -> dict[str, Any]:
        payload = {"token": self.token, "action": self.action, "recipe": self.recipe,
                   "item_id": self.item_id, "location_id": self.location_id,
                   "summary": self.summary, "item": self.targets[0]["name"]}
        if self.parameters:
            payload["parameters"] = self.parameters
        view = drink_view(self.targets[0], now=now)
        if view is not None:
            payload["home_drink"] = view
        from app.core.world.home_routines import bathroom_view

        bathroom = bathroom_view(self.targets[0], now=now)
        if bathroom is not None:
            payload["home_bathroom"] = bathroom
        if self.action in {"new_sketch", "revise_sketch"}:
            from app.core.world.home_sketches import sketch_context

            payload["sketch_context"] = sketch_context(
                self.targets[0], self.parameters.get("page_id"),
            )
        return payload


def _version(item: dict[str, Any]) -> dict[str, Any]:
    return deepcopy({key: item.get(key) for key in (
        "id", "state", "quantity", "location_id", "updated_at", "name", "kind",
    )})


def drink_actions(
    items: list[dict[str, Any]], locations: list[dict[str, Any]],
    room: dict[str, Any], now: datetime,
) -> list[HomeAction]:
    """Eligible actions in this scene; execution still requires the named spot."""
    local = {loc["id"] for loc in locations if loc["scene_id"] == room.get("scene_id")}
    visible = [item for item in items if item["location_id"] in local
               or item["location_id"] is None]
    stations = [item for item in visible if station_state(item) is not None
                and item["location_id"] in local and item.get("quantity", 0) > 0]
    actions = []
    for item in visible:
        vessel = vessel_state(item)
        if vessel is None or item.get("quantity", 0) <= 0:
            continue
        loc = item["location_id"] or room.get("location_id")
        if loc not in local:
            continue
        drink = vessel.get("drink") or {}
        amount = drink.get("amount_ml", 0)

        def offer(action, summary, *, station=None, recipe="", location=loc, target=item):
            targets = [_version(target)]
            if station is not None:
                targets.append(_version(station))
            actions.append(HomeAction(
                action, target["id"], location, summary, tuple(targets), recipe,
                station["id"] if station else None,
            ))

        if amount > 0:
            name = str(drink.get("name") or "drink")
            if not vessel.get("has_lid") or not vessel.get("lid_closed"):
                offer("sip_drink", f"sipped {name} from {item['name']}")
            for station in stations:
                if item["location_id"] not in (None, station["location_id"]):
                    continue
                offer("empty_vessel", f"poured away the remaining {name}",
                      station=station, location=station["location_id"])
                equipment = station["state"]["home_drink_station"].get("equipment", [])
                if (drink.get("recipe") in {"tea", "coffee", "hot_chocolate"}
                        and "kettle" in equipment and temperature(vessel, now) < 45):
                    offer("reheat_drink", f"warmed the remaining {name}", station=station,
                          location=station["location_id"])
        elif vessel.get("clean", True):
            for station in stations:
                if item["location_id"] not in (None, station["location_id"]):
                    continue
                state = station["state"]["home_drink_station"]
                for recipe, facts in RECIPES.items():
                    if facts["equipment"] not in state.get("equipment", []):
                        continue
                    if recipe != "water" and state.get("supplies", {}).get(recipe, 0) < 1:
                        continue
                    if facts["temperature"] > 45 and not vessel.get("heat_safe", False):
                        continue
                    offer("prepare_drink", f"prepared {facts['name']} in {item['name']}",
                          station=station, recipe=recipe, location=station["location_id"])
        for station in stations:
            if (amount <= 0 and not vessel.get("clean", True)
                    and item["location_id"] in (None, station["location_id"])):
                offer("wash_vessel", f"washed {item['name']}", station=station,
                      location=station["location_id"])
        if amount > 0 and vessel.get("has_lid"):
            verb = "opened" if vessel.get("lid_closed") else "closed"
            offer("toggle_lid", f"{verb} the lid of {item['name']}")
    for station in stations:
        state = station["state"]["home_drink_station"]
        last = parse_at(state.get("last_restock_at"))
        if (any(state.get("supplies", {}).get(recipe, 0) <= 1
                for recipe in RECIPES if recipe != "water")
                and (last is None or (now - last).total_seconds() >= 86400)):
            actions.append(HomeAction(
                "restock_drinks", station["id"], station["location_id"],
                "replenished the household drink supplies", (_version(station),),
                station_id=station["id"],
            ))
    return actions


def apply_drink_action(
    action: HomeAction, items: dict[int, dict[str, Any]], now: datetime,
) -> list[dict[str, Any]]:
    """Pure changes for an already validated action; caller commits all or none."""
    item = deepcopy(items[action.item_id])
    station = deepcopy(items[action.station_id]) if action.station_id is not None else None
    vessel = vessel_state(item)
    if action.action == "restock_drinks":
        state = item["state"]["home_drink_station"]
        state["supplies"] = {key: 8 for key in RECIPES if key != "water"}
        state["last_restock_at"] = now.isoformat()
        return [item]
    if vessel is None:
        raise ValueError("This item is not a reusable drink vessel")
    drink = vessel.get("drink") or {}
    if action.action == "prepare_drink":
        facts = RECIPES[action.recipe]
        vessel["drink"] = {
            "recipe": action.recipe, "name": facts["name"],
            "amount_ml": min(250, max(50, int(vessel.get("capacity_ml", 250)))),
            "thermal_c": facts["temperature"], "thermal_at": now.isoformat(),
            "prepared_at": now.isoformat(),
        }
        vessel["clean"] = False
        if station is not None and action.recipe != "water":
            station["state"]["home_drink_station"]["supplies"][action.recipe] -= 1
    elif action.action == "sip_drink":
        drink["amount_ml"] = max(0, drink["amount_ml"] - 75)
        vessel["drink"] = drink if drink["amount_ml"] else None
    elif action.action == "empty_vessel":
        vessel["drink"] = None
    elif action.action == "wash_vessel":
        vessel.update(clean=True, drink=None)
    elif action.action == "reheat_drink":
        drink.update(thermal_c=65.0, thermal_at=now.isoformat())
    elif action.action == "toggle_lid":
        drink.update(thermal_c=temperature(vessel, now), thermal_at=now.isoformat())
        vessel["lid_closed"] = not vessel.get("lid_closed", False)
    else:
        raise ValueError("Unsupported drink action")
    return [item] + ([station] if station is not None else [])
