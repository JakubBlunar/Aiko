"""One blank household sketchbook and portable drawing supplies."""
from __future__ import annotations

import json

from app.core.infra import timephrase
from app.core.world.home_sketches import new_sketchbook

SKETCH_SEED_KEY = "home_actions.sketch_seed.v1"


class HomeSketchbookMixin:
    def ensure_home_sketchbook(self):
        conn = self._get_conn()
        with self._home_action_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("SELECT 1 FROM kv_meta WHERE key=?", (SKETCH_SEED_KEY,)).fetchone():
                    conn.rollback()
                    return False
                desk = conn.execute("SELECT l.id FROM world_locations l JOIN world_scenes s "
                                    "ON s.id=l.scene_id WHERE s.origin='builtin' AND l.slug='desk'")
                desk = desk.fetchone()
                if desk is None:
                    conn.rollback()
                    return False
                at = timephrase.utcnow().isoformat()
                for slug, name, kind, state in (
                    ("home_sketchbook", "sketchbook", "sketchbook",
                     {"home_sketchbook": new_sketchbook()}),
                    ("home_sketch_tools", "pencil and eraser set", "art_supply",
                     {"home_drawing_tools": ["pencil", "eraser"]}),
                ):
                    if conn.execute("SELECT 1 FROM world_items WHERE slug=?", (slug,)).fetchone():
                        continue
                    conn.execute(
                        "INSERT INTO world_items (slug,name,description,kind,consumable,quantity,"
                        "location_id,home_location_id,state_json,given_by,created_at,updated_at) "
                        "VALUES (?,?,? ,?,0,1,?,?,?,'household',?,?)",
                        (slug, name, "Household drawing materials; no historical work is implied.",
                         kind, desk[0], desk[0], json.dumps(state), at, at),
                    )
                conn.execute("INSERT INTO kv_meta (key,value,updated_at) VALUES (?,'1',?)",
                             (SKETCH_SEED_KEY, at))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            self._reload_mirror()
        return True
