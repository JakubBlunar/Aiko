"""Virtual computer shopping, bounded room space, storage and recoverable disposal."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from app.core.world.home_drinks import HomeAction, _version, new_vessel, parse_at

PRODUCTS = {
    "lidded_mug": ("ceramic mug with lid", "vessel", "kitchenette"),
    "thermos": ("insulated thermos", "vessel", "kitchenette"),
    "sketchbook": ("sketchbook", "sketchbook", "bookshelf"),
    "pencils": ("pencil and eraser set", "art_supply", "desk"),
    "cushion": ("small soft cushion", "toy", "beanbag"),
    "frame": ("small picture frame", "decor", "bookshelf"),
    "plant_pot": ("little ceramic plant pot", "decor", "window_seat"),
    "bookmarks": ("paper bookmark set", "other", "bookshelf"),
}
LIFECYCLE_ACTIONS = frozenset({
    "place_home_order", "wish_for_item", "cancel_home_order", "unpack_parcel",
    "store_item", "retrieve_item", "discard_item", "restore_item", "keep_item",
})


def active_item(item: Any) -> bool:
    state = item.get("state", {}) if isinstance(item, dict) else getattr(item, "state", {})
    life = state.get("home_lifecycle", {})
    return not isinstance(life, dict) or life.get("status") not in {"stored", "discarded"}


def protected_item(item: dict[str, Any]) -> bool:
    state = item["state"]
    return bool(item.get("given_by") == "user" or item["kind"] == "keepsake"
                or state.get("home_favorite") or state.get("home_essential")
                or state.get("creation_schema") or state.get("home_sketchbook"))


def occupies_space(item: dict[str, Any]) -> bool:
    return item["kind"] in {
        "book", "toy", "keepsake", "decor", "vessel", "art_supply", "sketchbook", "other",
    } or bool(item["state"].get("home_origin"))


def organizer(items):
    return next((item for item in items if active_item(item)
                 and isinstance(item["state"].get("home_household"), dict)
                 and item["state"]["home_household"].get("schema") == 1), None)


def pending_orders(state):
    return [row for row in state.get("orders", [])
            if row.get("status") not in {"cancelled", "unpacked"}]


def room_space(items, state, destination: int) -> bool:
    active = [item for item in items if active_item(item) and occupies_space(item)
              and (item["location_id"] in state["location_ids"] or item["location_id"] is None)]
    reserved = pending_orders(state)
    return (len(active) + len(reserved) < state["capacity"]
            and sum(item["location_id"] == destination for item in active)
            + sum(row["destination_id"] == destination for row in reserved)
            < state["surface_capacity"].get(str(destination), 4))


def lifecycle_actions(items, locations, room, now: datetime) -> list[HomeAction]:
    ledger = organizer(items)
    if ledger is None:
        return []
    state = ledger["state"]["home_household"]
    local = {loc["id"] for loc in locations if loc["scene_id"] == room.get("scene_id")}
    if ledger["location_id"] not in local:
        return []
    state = {**state, "location_ids": list(local)}
    desk = next((loc["id"] for loc in locations if loc["id"] in local and loc["slug"] == "desk"),
                ledger["location_id"])
    computer = any(active_item(item) and item["location_id"] == desk and (
        item["slug"] == "dual_monitors" or "computer" in item["name"].lower()
    ) for item in items)
    actions = []

    def offer(name, target, summary, parameters=None, location=None):
        targets = [_version(target)]
        if target["id"] != ledger["id"]:
            targets.append(_version(ledger))
        actions.append(HomeAction(
            name, target["id"], location or target["location_id"] or room["location_id"],
            summary, tuple(targets), station_id=ledger["id"], parameters=parameters or {},
        ))

    last = parse_at(state.get("last_ordered_at"))
    pending = pending_orders(state)
    can_order = computer and (last is None or (now - last).total_seconds() >= 86400)
    for product, (name, _kind, _home) in PRODUCTS.items():
        if product not in state.get("wishlist", []):
            offer("wish_for_item", ledger, f"put {name} on my virtual shop wishlist",
                  {"product": product}, desk)
        already = any(item["state"].get("home_origin", {}).get("product") == product
                      and active_item(item) for item in items)
        if (can_order and not already and len(pending) < 2 and room_space(items, state, desk)
                and not any(row["product"] == product for row in pending)):
            offer("place_home_order", ledger, f"ordered {name} from the virtual home shop",
                  {"product": product, "destination_id": desk}, desk)
    for order in pending:
        due = parse_at(order.get("arrives_at"))
        name = PRODUCTS[order["product"]][0]
        if order["status"] == "ordered" and now < parse_at(order["dispatches_at"]):
            offer("cancel_home_order", ledger, f"cancelled my order for {name}",
                  {"order_id": order["id"]}, desk)
        if due is not None and now >= due:
            # The order already owns its reserved slot; remove only that reservation.
            without = {**state, "orders": [row for row in state["orders"]
                                          if row["id"] != order["id"]]}
            if room_space(items, without, order["destination_id"]):
                offer("unpack_parcel", ledger, f"unpacked my delivered {name}",
                      {"order_id": order["id"]}, desk)
    stored = sum(item["state"].get("home_lifecycle", {}).get("status") == "stored"
                 for item in items)
    discarded = sum(item["state"].get("home_lifecycle", {}).get("status") == "discarded"
                    for item in items)
    for item in items:
        if item["location_id"] not in local and item["location_id"] is not None:
            continue
        if item.get("home_location_id") not in local:
            continue
        if not occupies_space(item) or item["state"].get("home_essential"):
            continue
        life = item["state"].get("home_lifecycle", {})
        status = life.get("status")
        if status in {"stored", "discarded"}:
            dest = life.get("home_location_id")
            if dest not in local:
                dest = ledger["location_id"]
            if room_space(items, state, dest):
                verb = "retrieve_item" if status == "stored" else "restore_item"
                offer(verb, item, f"brought {item['name']} back into the room",
                      {"destination_id": dest}, ledger["location_id"])
            continue
        if stored < 8:
            offer("store_item", item, f"put {item['name']} away in the storage box")
        if not protected_item(item) and discarded < 16:
            offer("discard_item", item, f"put {item['name']} in the recoverable bin")
        if not item["state"].get("home_favorite"):
            offer("keep_item", item, f"marked {item['name']} as a favorite to keep")
    return actions


def apply_lifecycle_action(action: HomeAction, items, now: datetime):
    item = deepcopy(items[action.item_id])
    ledger = item if action.station_id == action.item_id else deepcopy(items[action.station_id])
    state = ledger["state"]["home_household"]
    additions = []
    stamp = now.isoformat()
    if action.action == "wish_for_item":
        state.setdefault("wishlist", []).append(action.parameters["product"])
    elif action.action == "place_home_order":
        state["orders"] = (state.get("orders", []) + [{
            "id": uuid4().hex, "product": action.parameters["product"], "status": "ordered",
            "ordered_at": stamp, "dispatches_at": (now + timedelta(minutes=5)).isoformat(),
            "arrives_at": (now + timedelta(minutes=30)).isoformat(),
            "destination_id": action.parameters["destination_id"],
        }])[-32:]
        state["last_ordered_at"] = stamp
    elif action.action in {"cancel_home_order", "unpack_parcel"}:
        order = next(row for row in state["orders"] if row["id"] == action.parameters["order_id"])
        order["status"] = "cancelled" if action.action == "cancel_home_order" else "unpacked"
        order["updated_at"] = stamp
        if action.action == "unpack_parcel":
            name, kind, _home = PRODUCTS[order["product"]]
            new_state = {"home_origin": {"product": order["product"], "order_id": order["id"],
                                         "acquired_at": stamp}}
            if kind == "vessel":
                new_state["home_vessel"] = new_vessel(name)
            if order["product"] == "sketchbook":
                new_state["home_sketchbook"] = {"schema": 1, "pages": []}
            if order["product"] == "pencils":
                new_state["home_drawing_tools"] = ["pencil", "eraser"]
            additions.append({"slug": "home_order_" + order["id"], "name": name, "kind": kind,
                              "state": new_state, "location_id": order["destination_id"],
                              "given_by": "aiko", "description": "From the virtual home shop."})
    elif action.action in {"store_item", "discard_item"}:
        life = {"status": "stored" if action.action == "store_item" else "discarded",
                "owner_id": ledger["id"],
                "home_location_id": item["location_id"] or item.get("home_location_id"),
                "at": stamp}
        if action.action == "discard_item":
            life["expires_at"] = (now + timedelta(hours=48)).isoformat()
        item["state"]["home_lifecycle"] = life
        item["location_id"] = ledger["location_id"]
    elif action.action in {"retrieve_item", "restore_item"}:
        item["state"].pop("home_lifecycle", None)
        item["location_id"] = action.parameters["destination_id"]
    elif action.action == "keep_item":
        item["state"]["home_favorite"] = True
    else:
        raise ValueError("Unsupported belongings action")
    return ([item] if item["id"] == ledger["id"] else [item, ledger]), additions
