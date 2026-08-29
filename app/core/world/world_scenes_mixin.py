"""H5 scene CRUD mixed into :class:`WorldStore`.

Kept beside the store rather than inlined because ``world_store.py`` is
already past the file-size guideline and scene lifetime (create / travel
/ lock) is a separate job from item/location rows.
"""
from __future__ import annotations

from typing import Any

from app.core.infra import timephrase
from app.core.world.scene import (
    APARTMENT_DESCRIPTION,
    APARTMENT_NAME,
    APARTMENT_SLUG,
    ORIGIN_BUILTIN,
    ORIGIN_CUSTOM,
    Scene,
    slugify_scene,
)


def _now_iso() -> str:
    return timephrase.utcnow().isoformat()


class WorldScenesMixin:
    """Scene table + travel. Expects WorldStore's conn / lock / mirrors."""

    _scenes: dict[int, Scene]

    def list_scenes(self) -> list[Scene]:
        with self._lock:
            scenes = list(self._scenes.values())
        scenes.sort(key=lambda s: (0 if s.origin == ORIGIN_BUILTIN else 1, s.id))
        return scenes

    def get_scene(self, scene_id: int) -> Scene | None:
        with self._lock:
            return self._scenes.get(int(scene_id))

    def get_scene_by_slug(self, slug: str) -> Scene | None:
        target = (slug or "").strip().lower()
        if not target:
            return None
        with self._lock:
            for scene in self._scenes.values():
                if scene.slug == target:
                    return scene
        return None

    def find_scene(self, query: str) -> Scene | None:
        """Match a scene by slug, name, or substring (case-insensitive)."""
        target = (query or "").strip().lower()
        if not target:
            return None
        with self._lock:
            scenes = list(self._scenes.values())
        for scene in scenes:
            if scene.slug == target:
                return scene
        for scene in scenes:
            if scene.name.lower() == target:
                return scene
        for scene in scenes:
            if target in scene.slug or target in scene.name.lower():
                return scene
        return None

    def current_scene(self) -> Scene | None:
        state = self.get_state()
        if state.scene_id is not None:
            scene = self.get_scene(state.scene_id)
            if scene is not None:
                return scene
        loc = (
            self.get_location_by_id(state.location_id)
            if state.location_id is not None
            else None
        )
        if loc is not None:
            return self.get_scene(loc.scene_id)
        return self.home_scene()

    def home_scene(self) -> Scene | None:
        found = self.get_scene_by_slug(APARTMENT_SLUG)
        if found is not None:
            return found
        with self._lock:
            for scene in self._scenes.values():
                if scene.origin == ORIGIN_BUILTIN:
                    return scene
        return None

    def current_scene_id(self) -> int | None:
        scene = self.current_scene()
        return None if scene is None else int(scene.id)

    def in_home_scene(self) -> bool:
        scene = self.current_scene()
        return scene is not None and scene.origin == ORIGIN_BUILTIN

    def ensure_home_scene(self) -> Scene:
        existing = self.get_scene_by_slug(APARTMENT_SLUG)
        if existing is not None:
            return existing
        now = _now_iso()
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO world_scenes "
            "(slug, name, description, origin, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                APARTMENT_SLUG, APARTMENT_NAME, APARTMENT_DESCRIPTION,
                ORIGIN_BUILTIN, now, now,
            ),
        )
        conn.commit()
        scene = Scene(
            id=int(cursor.lastrowid or 0),
            slug=APARTMENT_SLUG,
            name=APARTMENT_NAME,
            description=APARTMENT_DESCRIPTION,
            origin=ORIGIN_BUILTIN,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._scenes[scene.id] = scene
        return scene

    def add_scene(
        self,
        *,
        name: str,
        description: str = "",
        slug: str | None = None,
        origin: str = ORIGIN_CUSTOM,
        with_default_spot: bool = True,
    ) -> Scene | None:
        clean_name = (name or "").strip()
        if not clean_name:
            return None
        if origin == ORIGIN_BUILTIN:
            return self.ensure_home_scene()
        clean_slug = (slug or slugify_scene(clean_name)).strip().lower()
        if not clean_slug or clean_slug == APARTMENT_SLUG:
            return None
        existing = self.get_scene_by_slug(clean_slug)
        if existing is not None:
            return existing
        now = _now_iso()
        conn = self._get_conn()
        try:
            cursor = conn.execute(
                "INSERT INTO world_scenes "
                "(slug, name, description, origin, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    clean_slug, clean_name, (description or "").strip(),
                    ORIGIN_CUSTOM, now, now,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            return self.get_scene_by_slug(clean_slug)
        scene = Scene(
            id=int(cursor.lastrowid or 0),
            slug=clean_slug,
            name=clean_name,
            description=(description or "").strip(),
            origin=ORIGIN_CUSTOM,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._scenes[scene.id] = scene
        if with_default_spot:
            self.add_location(
                slug="here",
                name=f"in {clean_name}",
                description=(description or "").strip()
                or f"somewhere in {clean_name}",
                scene_id=scene.id,
                locked=False,
            )
        return scene

    def update_scene(
        self,
        scene_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Scene | None:
        with self._lock:
            scene = self._scenes.get(int(scene_id))
        if scene is None:
            return None
        if scene.origin == ORIGIN_BUILTIN and name is not None:
            return scene
        new_name = scene.name if name is None else (str(name).strip() or scene.name)
        new_desc = (
            scene.description if description is None else str(description).strip()
        )
        now = _now_iso()
        conn = self._get_conn()
        conn.execute(
            "UPDATE world_scenes SET name = ?, description = ?, updated_at = ? "
            "WHERE id = ?",
            (new_name, new_desc, now, int(scene_id)),
        )
        conn.commit()
        with self._lock:
            scene.name = new_name
            scene.description = new_desc
            scene.updated_at = now
        return scene

    def remove_scene(self, scene_id: int) -> bool:
        """Delete a custom scene and everything in it.

        Builtin home cannot be removed. If Aiko is in the scene she is
        walked back to the apartment first.
        """
        sid = int(scene_id)
        with self._lock:
            scene = self._scenes.get(sid)
        if scene is None or scene.origin == ORIGIN_BUILTIN:
            return False
        home = self.ensure_home_scene()
        state = self.get_state()
        if state.scene_id == sid:
            home_spot = self._first_spot(home.id)
            self.set_state(
                location_id=None if home_spot is None else home_spot.id,
                scene_id=home.id,
            )
        with self._lock:
            loc_ids = [
                loc.id for loc in self._locations.values() if loc.scene_id == sid
            ]
        conn = self._get_conn()
        if loc_ids:
            placeholders = ",".join("?" * len(loc_ids))
            conn.execute(
                f"DELETE FROM world_items WHERE location_id IN ({placeholders})",
                loc_ids,
            )
            conn.execute(
                f"DELETE FROM world_locations WHERE id IN ({placeholders})",
                loc_ids,
            )
        conn.execute("DELETE FROM world_scenes WHERE id = ?", (sid,))
        conn.commit()
        with self._lock:
            self._scenes.pop(sid, None)
            for lid in loc_ids:
                self._locations.pop(lid, None)
            gone = set(loc_ids)
            self._items = {
                iid: item
                for iid, item in self._items.items()
                if item.location_id not in gone
            }
        return True

    def travel_to_scene(self, scene_id: int) -> dict[str, Any] | None:
        """Move Aiko into ``scene_id``, standing at its first spot."""
        scene = self.get_scene(int(scene_id))
        if scene is None:
            return None
        spot = self._first_spot(scene.id)
        state = self.set_state(
            location_id=None if spot is None else spot.id,
            scene_id=scene.id,
        )
        return {
            "scene": scene.to_dict(),
            "state": state.to_dict(),
            "location": None if spot is None else spot.to_dict(),
        }

    def _first_spot(self, scene_id: int) -> Any:
        spots = self.list_locations(scene_id=int(scene_id))
        return spots[0] if spots else None

    def _load_scenes(self, conn: Any) -> dict[int, Scene]:
        try:
            rows = conn.execute(
                "SELECT id, slug, name, description, origin, "
                "created_at, updated_at FROM world_scenes",
            ).fetchall()
        except Exception:
            return {}
        out: dict[int, Scene] = {}
        for row in rows:
            origin = (
                row[4] if row[4] in (ORIGIN_BUILTIN, ORIGIN_CUSTOM)
                else ORIGIN_CUSTOM
            )
            out[int(row[0])] = Scene(
                id=int(row[0]),
                slug=row[1],
                name=row[2],
                description=row[3] or "",
                origin=origin,
                created_at=row[5] or "",
                updated_at=row[6] or "",
            )
        return out
