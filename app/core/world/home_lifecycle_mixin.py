"""Persistent household ledger and passive expiry/delivery housekeeping."""
from __future__ import annotations

from copy import deepcopy
import json

from app.core.infra import timephrase
from app.core.world.home_belongings import (
    PRODUCTS, active_item, occupies_space, organizer, parse_at, protected_item,
)

LIFECYCLE_SEED_KEY = "home_actions.lifecycle_seed.v1"


class HomeLifecycleMixin:
    def ensure_home_lifecycle(self) -> bool:
        conn = self._get_conn()
        with self._home_action_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("SELECT 1 FROM kv_meta WHERE key=?",
                                (LIFECYCLE_SEED_KEY,)).fetchone():
                    conn.rollback()
                    return False
                items, locations, _room = self._home_rows(conn)
                home = conn.execute("SELECT id FROM world_scenes WHERE origin='builtin' LIMIT 1")
                home = home.fetchone()
                local = [loc for loc in locations if home and loc["scene_id"] == home[0]]
                desk = next((loc for loc in local if loc["slug"] == "desk"), None)
                if desk is None:
                    conn.rollback()
                    return False
                ids = [loc["id"] for loc in local]
                possessions = [item for item in items if active_item(item) and occupies_space(item)
                               and (item["location_id"] in ids or item["location_id"] is None)]
                state = {"home_essential": True, "home_household": {
                    "schema": 1, "orders": [], "wishlist": [], "disposals": [],
                    "location_ids": ids, "capacity": max(16, len(possessions) + 6),
                    "surface_capacity": {str(loc["id"]): max(4, 2 + sum(
                        item["location_id"] == loc["id"] for item in possessions
                    )) for loc in local},
                }}
                at = timephrase.utcnow().isoformat()
                existing = conn.execute(
                    "SELECT 1 FROM world_items WHERE slug='home_household'",
                ).fetchone()
                if not existing:
                    conn.execute(
                        "INSERT INTO world_items (slug,name,description,kind,consumable,quantity,"
                        "location_id,home_location_id,state_json,given_by,created_at,updated_at) "
                        "VALUES ('home_household','household organizer',?,'gadget',0,1,?,?,?,"
                        "'household',?,?)",
                        ("The computer's virtual shop, a small storage box and recoverable bin.",
                         desk["id"], desk["id"], json.dumps(state), at, at),
                    )
                conn.execute("INSERT INTO kv_meta (key,value,updated_at) VALUES (?,'1',?)",
                             (LIFECYCLE_SEED_KEY, at))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            self._reload_mirror()
        return True

    def home_household_snapshot(self, *, now=None):
        at = now or timephrase.utcnow()
        items, _locations, _room = self._home_rows(self._get_conn())
        ledger = organizer(items)
        if ledger is None:
            return {"enabled": False, "catalogue": [], "orders": [], "stored": [], "bin": []}
        state = deepcopy(ledger["state"]["home_household"])
        for order in state["orders"]:
            if order["status"] in {"cancelled", "unpacked"}:
                continue
            if at >= parse_at(order["arrives_at"]):
                order["status"] = "delivered"
            elif at >= parse_at(order["dispatches_at"]):
                order["status"] = "dispatched"
        hidden = {"stored": [], "discarded": []}
        for item in items:
            life = item["state"].get("home_lifecycle", {})
            if life.get("status") in hidden:
                hidden[life["status"]].append({
                    "id": item["id"], "name": item["name"], "lifecycle": life,
                    "protected": protected_item(item),
                })
        return {
            "enabled": True, "catalogue": [{"id": key, "name": value[0], "kind": value[1]}
                                            for key, value in PRODUCTS.items()],
            "orders": state["orders"], "wishlist": state["wishlist"],
            "stored": hidden["stored"], "bin": hidden["discarded"],
            "disposals": state["disposals"], "capacity": state["capacity"],
            "active_space_used": sum(
                occupies_space(item) and active_item(item) for item in items
                if item["location_id"] in state["location_ids"] or item["location_id"] is None
            ),
        }

    def home_lifecycle_maintenance_due(self, *, now=None) -> bool:
        at = now or timephrase.utcnow()
        items, _, _ = self._home_rows(self._get_conn())
        ledger = organizer(items)
        if ledger is None:
            return False
        return any(
            item["state"].get("home_lifecycle", {}).get("status") == "discarded"
            and item["state"]["home_lifecycle"].get("owner_id") == ledger["id"]
            and (deadline := parse_at(
                item["state"]["home_lifecycle"].get("expires_at")
            )) is not None
            and at >= deadline and not protected_item(item)
            for item in items
        )

    def maintain_home_lifecycle(self, *, now=None):
        at = now or timephrase.utcnow()
        conn = self._get_conn()
        with self._home_action_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                items, _, _ = self._home_rows(conn)
                ledger = organizer(items)
                if ledger is None:
                    conn.rollback()
                    return {"discarded": 0}
                removed = []
                for item in items:
                    life = item["state"].get("home_lifecycle", {})
                    expires = parse_at(life.get("expires_at"))
                    if (life.get("status") != "discarded" or life.get("owner_id") != ledger["id"]
                            or expires is None or at < expires
                            or protected_item(item)):
                        continue
                    conn.execute("DELETE FROM world_items WHERE id=?", (item["id"],))
                    removed.append({"name": item["name"], "discarded_at": life["at"],
                                    "cleared_at": at.isoformat()})
                if removed:
                    state = ledger["state"]
                    state["home_household"]["disposals"] = (
                        state["home_household"].get("disposals", []) + removed
                    )[-32:]
                    state["home_action_revision"] = state.get("home_action_revision", 0) + 1
                    conn.execute("UPDATE world_items SET state_json=?,updated_at=? WHERE id=?",
                                 (json.dumps(state), at.isoformat(), ledger["id"]))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            self._reload_mirror()
        return {"discarded": len(removed)}
