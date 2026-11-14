"""Additive bathroom provisioning; never rewrites an existing apartment."""
from __future__ import annotations

import json

from app.core.infra import timephrase
from app.core.world.home_routines import bathroom_state, routine_actions

BATHROOM_SEED_KEY = "home_actions.bathroom_seed.v1"


class HomeBathroomMixin:
    def ensure_home_bathroom(self) -> bool:
        conn = self._get_conn()
        with self._home_action_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("SELECT 1 FROM kv_meta WHERE key=?",
                                (BATHROOM_SEED_KEY,)).fetchone():
                    conn.rollback()
                    return False
                scene = conn.execute(
                    "SELECT id FROM world_scenes WHERE origin='builtin' LIMIT 1",
                ).fetchone()
                if scene is None:
                    conn.rollback()
                    return False
                loc = conn.execute("SELECT id FROM world_locations WHERE scene_id=? "
                                   "AND slug='bathroom'", (scene[0],)).fetchone()
                if loc is None:
                    position = conn.execute("SELECT COALESCE(MAX(position),0)+1 FROM "
                                            "world_locations WHERE scene_id=?",
                                            (scene[0],)).fetchone()[0]
                    cursor = conn.execute(
                        "INSERT INTO world_locations "
                        "(slug,name,description,position,scene_id,locked)"
                        " VALUES ('bathroom','the bathroom',?,?,?,1)",
                        ("A compact bathroom with a sink, mirror, bath/shower and towel hook.",
                         position, scene[0]),
                    )
                    loc = (cursor.lastrowid,)
                state = {"home_bathroom": {
                    "schema": 1,
                    "equipment": ["sink", "mirror", "toothbrush", "hairbrush", "tub",
                                  "shower", "towel"],
                    "supplies": {"toothpaste": 20, "soap": 20}, "bath": {"phase": "empty"},
                }}
                at = timephrase.utcnow().isoformat()
                if not conn.execute("SELECT 1 FROM world_items WHERE slug='home_bathroom_kit'")\
                        .fetchone():
                    conn.execute(
                        "INSERT INTO world_items (slug,name,description,kind,consumable,quantity,"
                        "location_id,home_location_id,state_json,given_by,created_at,updated_at) "
                        "VALUES ('home_bathroom_kit','bathroom fixtures',?,'gadget',0,1,?,?,?,"
                        "'household',?,?)",
                        ("A sink and mirror, toothbrush, hairbrush, small tub, towel and supplies.",
                         loc[0], loc[0], json.dumps(state), at, at),
                    )
                conn.execute("INSERT INTO kv_meta (key,value,updated_at) VALUES (?,'1',?)",
                             (BATHROOM_SEED_KEY, at))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            self._reload_mirror()
        return True

    def home_activity_followup(self, *, now=None):
        """An uninterrupted bath owns idle activity until its next supported step."""
        if self.get_state().activity != "start_bath":
            return None
        at = now or timephrase.utcnow()
        items, locations, room = self._home_rows(self._get_conn())
        for item in items:
            state = bathroom_state(item)
            if (state is None or state["bath"].get("phase") != "soaking"
                    or item["location_id"] != room.get("location_id")):
                continue
            finish = next((
                action for action in routine_actions(items, locations, room, at)
                if action.action == "finish_bath" and action.item_id == item["id"]
            ), None)
            return {"waiting": finish is None,
                    "action": finish.to_dict(now=at) if finish is not None else None}
        return None
