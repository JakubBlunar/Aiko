"""Transactional home actions and their bounded durable execution receipts."""
from __future__ import annotations

from datetime import datetime
from collections.abc import Callable
import json
from typing import Any

from app.core.infra import timephrase
from app.core.world.home_drinks import RECIPES, apply_drink_action, drink_actions

RECEIPTS_KEY = "home_actions.receipts.v1"
SEED_KEY = "home_actions.drinks_seed.v1"
RECEIPT_LIMIT = 64


class HomeActionsMixin:
    def _home_rows(self, conn):
        cursor = conn.execute("SELECT * FROM world_items ORDER BY id")
        names = [column[0] for column in cursor.description]
        items = []
        for row in cursor:
            item = dict(zip(names, row, strict=True))
            raw = item.pop("state_json") or "{}"
            try:
                state = json.loads(raw)
            except (ValueError, TypeError):
                state = {}
            item["state"] = state if isinstance(state, dict) else {}
            items.append(item)
        cursor = conn.execute("SELECT * FROM world_locations")
        names = [column[0] for column in cursor.description]
        locations = [dict(zip(names, row, strict=True)) for row in cursor]
        cursor = conn.execute("SELECT * FROM world_state WHERE id=1")
        names = [column[0] for column in cursor.description]
        row = cursor.fetchone()
        room = dict(zip(names, row, strict=True)) if row else {}
        return items, locations, room

    def available_home_actions(self, *, now: datetime | None = None) -> dict[str, Any]:
        conn = self._get_conn()
        items, locations, room = self._home_rows(conn)
        at = now or timephrase.utcnow()
        actions = drink_actions(items, locations, room, at)
        return {
            "capabilities": (
                "Reusable vessels: prepare tea, coffee, hot chocolate, water or juice; "
                "sip, empty, wash, reheat compatible drinks, open/close an actual lid. "
                "Recipes require stored supplies and equipment. Temperature changes "
                "with elapsed simulated indoor time; insulation slows that change. "
                "Actions are small synchronous simulated steps. Move to the named "
                "location first; the target vessel must be there or carried. "
                "Only a successful execution establishes an experience."
            ),
            "actions": [action.to_dict(now=at) for action in actions],
            "here": room.get("location_id"),
            "unavailable_hint": (
                "Missing recipes need a clean heat-compatible vessel and a drink station "
                "with supplies/equipment. Wash empty used vessels; replenish low supplies "
                "at most once per 24 hours. Objects in another scene are unavailable."
            ),
        }

    def perform_home_action(
        self, token: str, *, now: datetime | None = None,
        autonomous: bool = False, admit: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(token, str) or len(token) != 64:
            raise ValueError("Choose an exact token from available_home_actions")
        at = now or timephrase.utcnow()
        conn = self._get_conn()
        with self._home_action_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT value FROM kv_meta WHERE key=?", (RECEIPTS_KEY,))
                saved = row.fetchone()
                receipts = json.loads(saved[0]) if saved else []
                prior = next((entry for entry in receipts if entry["token"] == token), None)
                if prior is not None:
                    conn.rollback()
                    self._reload_mirror()
                    return {**prior, "already_applied": True}
                items, locations, room = self._home_rows(conn)
                action = next((candidate for candidate in drink_actions(
                    items, locations, room, at,
                ) if candidate.token == token), None)
                if action is None:
                    raise ValueError("Home action is stale or unavailable; inspect actions again")
                if admit is not None and not admit():
                    raise ValueError("Autonomous home action is blocked by the current situation")
                if not autonomous and room.get("location_id") != action.location_id:
                    raise ValueError("Move to the action's location first")
                target = next(item for item in items if item["id"] == action.item_id)
                if target["location_id"] not in (None, action.location_id):
                    raise ValueError("Bring the vessel to the action's location first")
                changes = apply_drink_action(action, {item["id"]: item for item in items}, at)
                if autonomous:
                    conn.execute(
                        "UPDATE world_state SET location_id=?,posture='standing',activity=?,"
                        "updated_at=? WHERE id=1",
                        (action.location_id, action.action, at.isoformat()),
                    )
                else:
                    conn.execute("UPDATE world_state SET activity=?,updated_at=? WHERE id=1",
                                 (action.action, at.isoformat()))
                    conn.execute(
                        "INSERT INTO kv_meta (key,value,updated_at) VALUES "
                        "('world.intentional_state_at',?,?) ON CONFLICT(key) DO UPDATE SET "
                        "value=excluded.value,updated_at=excluded.updated_at",
                        (at.isoformat(), at.isoformat()),
                    )
                for item in changes:
                    item["state"]["home_action_revision"] = (
                        int(item["state"].get("home_action_revision", 0)) + 1
                    )
                    conn.execute(
                        "UPDATE world_items SET state_json=?, updated_at=? WHERE id=?",
                        (json.dumps(item["state"], ensure_ascii=False), at.isoformat(), item["id"]),
                    )
                receipt = {
                    "token": token, "action": action.action, "summary": action.summary,
                    "item_id": action.item_id, "location_id": action.location_id,
                    "at": at.isoformat(), "mode": "simulated_home_action",
                    "changed_item_ids": [item["id"] for item in changes],
                }
                conn.execute(
                    "INSERT INTO kv_meta (key,value,updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (RECEIPTS_KEY, json.dumps((receipts + [receipt])[-RECEIPT_LIMIT:]),
                     at.isoformat()),
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            self._reload_mirror()
        return {**receipt, "already_applied": False}

    def ensure_home_drinks(self) -> bool:
        """One additive household kit, never a user gift or automatic resurrection."""
        conn = self._get_conn()
        with self._home_action_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("SELECT 1 FROM kv_meta WHERE key=?", (SEED_KEY,)).fetchone():
                    conn.rollback()
                    return False
                loc = conn.execute(
                    "SELECT l.id FROM world_locations l JOIN world_scenes s ON s.id=l.scene_id "
                    "WHERE s.origin='builtin' AND l.slug='kitchenette' LIMIT 1",
                ).fetchone()
                if loc is None:
                    conn.rollback()
                    return False
                at = timephrase.utcnow().isoformat()
                station = {"home_drink_station": {
                    "schema": 1, "equipment": ["tap", "kettle", "coffee_press", "fridge"],
                    "supplies": {key: 8 for key in RECIPES if key != "water"},
                }}
                kit = [
                    ("home_drink_station", "household drink station", "gadget",
                     "A drink cupboard with a kettle, coffee press, tap and small fridge.",
                     station),
                    ("home_ceramic_mug", "ceramic mug", "vessel", "A simple reusable ceramic mug.",
                     {"home_vessel": {"schema": 1, "capacity_ml": 250, "heat_safe": True,
                                      "clean": True, "drink": None}}),
                    ("home_drinking_glass", "drinking glass", "vessel",
                     "A reusable cold-drink glass.",
                     {"home_vessel": {"schema": 1, "capacity_ml": 250, "heat_safe": False,
                                      "clean": True, "drink": None}}),
                ]
                for slug, name, kind, description, state in kit:
                    if conn.execute("SELECT 1 FROM world_items WHERE slug=?", (slug,)).fetchone():
                        continue
                    conn.execute(
                        "INSERT INTO world_items (slug,name,kind,description,consumable,quantity,"
                        "location_id,home_location_id,state_json,given_by,created_at,updated_at) "
                        "VALUES (?,?,?,?,0,1,?,?,?,'household',?,?)",
                        (slug, name, kind, description, loc[0], loc[0], json.dumps(state), at, at),
                    )
                conn.execute("INSERT INTO kv_meta (key,value,updated_at) VALUES (?,'1',?)",
                             (SEED_KEY, at))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            self._reload_mirror()
        return True

    def reset_home_drinks(self) -> None:
        """Only the explicit whole-room reset may recreate discarded provisions."""
        conn = self._get_conn()
        conn.execute("DELETE FROM kv_meta WHERE key IN (?,?)", (SEED_KEY, RECEIPTS_KEY))
        conn.commit()
        self.ensure_home_drinks()
