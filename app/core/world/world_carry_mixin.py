"""Carry / home-spot helpers mixed into :class:`WorldStore`.

Keeps the pocketable-carrying rules out of the already-large store
module. Expects the store's lock, conn, item/location mirrors, and
``update_item`` / ``get_state``.
"""
from __future__ import annotations

from typing import Any

from app.core.infra import timephrase
from app.core.world.carry import (
    CARRY_CAP,
    SEED_HOME_SLUGS,
    counts_toward_cap,
    is_portable,
)


def _now_iso() -> str:
    return timephrase.utcnow().isoformat()


class WorldCarryMixin:
    """Home spots, carry cap, and put-back helpers for the room store."""

    def carried_items(self, *, include_seeds: bool = True) -> list[Any]:
        with self._lock:
            items = list(self._items.values())
        held = [i for i in items if i.location_id is None]
        if not include_seeds:
            held = [i for i in held if (i.kind or "") != "seed"]
        held.sort(key=lambda i: i.updated_at or "")
        return held

    def infer_home_location_id(
        self,
        *,
        slug: str = "",
        location_id: int | None = None,
        kind: str = "",
    ) -> int | None:
        """Best home for an item that does not have one yet."""
        if location_id is not None:
            loc = self.get_location_by_id(int(location_id))
            if loc is not None:
                return int(loc.id)
        home_slug = SEED_HOME_SLUGS.get((slug or "").strip().lower())
        if home_slug:
            loc = self.get_location(home_slug)
            if loc is None:
                loc = self.get_location(home_slug, scene_id=self._home_scene_id())
            if loc is not None:
                return int(loc.id)
        return self._fallback_spot_id(excluding_id=None, scene_id=None)

    def ensure_item_homes(self) -> int:
        """Backfill ``home_location_id`` on rows that still lack one.

        Returns how many rows were written.
        """
        writes = 0
        with self._lock:
            items = list(self._items.values())
        for item in items:
            if getattr(item, "home_location_id", None) is not None:
                continue
            home = self.infer_home_location_id(
                slug=item.slug,
                location_id=item.location_id,
                kind=item.kind,
            )
            if home is None:
                continue
            if self._persist_home(item.id, home):
                writes += 1
        return writes

    def tidy_illegal_carry(self) -> list[int]:
        """Snap illegal / over-cap carried rows home. Returns item ids."""
        snapped: list[int] = []
        for item in list(self.carried_items(include_seeds=True)):
            if is_portable(item.kind):
                continue
            restored = self.restore_home(item.id)
            if restored is not None:
                snapped.append(int(item.id))
        while True:
            held = [
                i for i in self.carried_items(include_seeds=False)
                if counts_toward_cap(i.kind)
            ]
            if len(held) <= CARRY_CAP:
                break
            oldest = held[0]
            restored = self.restore_home(oldest.id)
            if restored is None:
                break
            snapped.append(int(oldest.id))
        return snapped

    def tidy_carry_state(self) -> None:
        """Boot-time: fill homes, then snap anything that should not be held."""
        try:
            self.ensure_item_homes()
            self.tidy_illegal_carry()
        except Exception:
            pass

    def take_into_hands(self, item_id: int) -> Any | None:
        """Pick up a portable item. Non-portable / missing -> ``None``.

        Over-cap takes snap the oldest other held non-seed home first so
        the new take can succeed.
        """
        item = self.get_item(int(item_id))
        if item is None or not is_portable(item.kind):
            return None
        if item.location_id is None:
            return item
        self._enforce_carry_cap(except_id=int(item_id))
        return self.update_item(int(item_id), location_id=None)

    def put_down(
        self,
        item_id: int,
        *,
        location_id: int | None = None,
    ) -> Any | None:
        """Put a held (or any) item down at ``location_id``, current spot, or home."""
        item = self.get_item(int(item_id))
        if item is None:
            return None
        dest = location_id
        if dest is None:
            try:
                dest = self.get_state().location_id
            except Exception:
                dest = None
        if dest is None:
            dest = getattr(item, "home_location_id", None)
        if dest is None:
            dest = self.infer_home_location_id(
                slug=item.slug, location_id=item.location_id, kind=item.kind,
            )
        if dest is None:
            return item
        return self.update_item(int(item_id), location_id=int(dest))

    def restore_home(self, item_id: int) -> Any | None:
        """Return one item to its home spot."""
        item = self.get_item(int(item_id))
        if item is None:
            return None
        home = getattr(item, "home_location_id", None)
        if home is None or self.get_location_by_id(int(home)) is None:
            home = self.infer_home_location_id(
                slug=item.slug, location_id=item.location_id, kind=item.kind,
            )
        if home is None:
            return item
        if item.location_id == int(home):
            if getattr(item, "home_location_id", None) is None:
                self._persist_home(item.id, int(home))
            return item
        return self.update_item(int(item_id), location_id=int(home))

    def restore_strays_at(self, location_id: int) -> list[Any]:
        """Send portable items at ``location_id`` whose home is elsewhere home.

        The ``tidy_desk`` idle beat: undo clutter, never pick things up.
        """
        lid = int(location_id)
        moved: list[Any] = []
        with self._lock:
            items = list(self._items.values())
        for item in items:
            if item.location_id != lid:
                continue
            if not is_portable(item.kind):
                continue
            home = getattr(item, "home_location_id", None)
            if home is None:
                home = self.infer_home_location_id(
                    slug=item.slug, location_id=item.location_id, kind=item.kind,
                )
            if home is None or int(home) == lid:
                continue
            restored = self.restore_home(item.id)
            if restored is not None:
                moved.append(restored)
        return moved

    def relocate_from_deleted_location(
        self, location_id: int, *, scene_id: int | None,
    ) -> None:
        """Move items off a location about to be deleted. Never to NULL."""
        lid = int(location_id)
        fallback = self._fallback_spot_id(excluding_id=lid, scene_id=scene_id)
        with self._lock:
            items = [
                i for i in self._items.values() if i.location_id == lid
            ]
        for item in items:
            home = getattr(item, "home_location_id", None)
            dest = None
            if home is not None and int(home) != lid:
                if self.get_location_by_id(int(home)) is not None:
                    dest = int(home)
            if dest is None:
                dest = fallback
            if dest is None:
                # Last spot in the world: portable/seed may stay held;
                # everything else has nowhere to go, so leave the id and
                # let SQLite SET NULL — tidy_illegal_carry will try again
                # after reload if a spot reappears.
                if is_portable(item.kind):
                    self.update_item(item.id, location_id=None)
                continue
            self.update_item(item.id, location_id=int(dest))
            if getattr(item, "home_location_id", None) == lid:
                self._persist_home(item.id, int(dest))

    def coerce_carry_location(
        self,
        *,
        kind: str,
        location_id: int | None,
        home_location_id: int | None,
        slug: str = "",
        item_id: int | None = None,
    ) -> tuple[int | None, int | None]:
        """Return ``(location_id, home_location_id)`` obeying portable / cap."""
        home = home_location_id
        if home is None:
            home = self.infer_home_location_id(
                slug=slug, location_id=location_id, kind=kind,
            )
        loc = location_id
        if loc is None and not is_portable(kind):
            loc = home
        if loc is None and counts_toward_cap(kind):
            self._enforce_carry_cap(except_id=item_id)
        return loc, home

    # ── internals ────────────────────────────────────────────────────

    def _enforce_carry_cap(self, *, except_id: int | None = None) -> None:
        while True:
            held = [
                i for i in self.carried_items(include_seeds=False)
                if counts_toward_cap(i.kind)
                and (except_id is None or i.id != int(except_id))
            ]
            # +1 if we are about to add except_id as a new carried slot.
            upcoming = 1
            if except_id is not None:
                current = self.get_item(int(except_id))
                if current is not None and current.location_id is None:
                    upcoming = 0
            if len(held) + upcoming <= CARRY_CAP:
                return
            if not held:
                return
            self.restore_home(held[0].id)

    def _persist_home(self, item_id: int, home_id: int) -> bool:
        now = _now_iso()
        try:
            conn = self._get_conn()
            conn.execute(
                "UPDATE world_items SET home_location_id = ?, updated_at = ? "
                "WHERE id = ?",
                (int(home_id), now, int(item_id)),
            )
            conn.commit()
        except Exception:
            return False
        with self._lock:
            item = self._items.get(int(item_id))
            if item is None:
                return False
            item.home_location_id = int(home_id)
            item.updated_at = now
        return True

    def _home_scene_id(self) -> int | None:
        try:
            home = self.home_scene()
        except Exception:
            return None
        return None if home is None else int(home.id)

    def _fallback_spot_id(
        self,
        *,
        excluding_id: int | None,
        scene_id: int | None,
    ) -> int | None:
        sid = scene_id
        with self._lock:
            locs = list(self._locations.values())
        if sid is not None:
            in_scene = [
                loc for loc in locs
                if loc.scene_id == int(sid)
                and (excluding_id is None or loc.id != int(excluding_id))
            ]
            if in_scene:
                in_scene.sort(key=lambda loc: (loc.position, loc.id))
                return int(in_scene[0].id)
        hid = self._home_scene_id()
        home_spots = [
            loc for loc in locs
            if (hid is None or loc.scene_id == hid)
            and (excluding_id is None or loc.id != int(excluding_id))
        ]
        if home_spots:
            home_spots.sort(key=lambda loc: (loc.position, loc.id))
            return int(home_spots[0].id)
        leftover = [
            loc for loc in locs
            if excluding_id is None or loc.id != int(excluding_id)
        ]
        if leftover:
            leftover.sort(key=lambda loc: (loc.position, loc.id))
            return int(leftover[0].id)
        return None


__all__ = ["WorldCarryMixin"]
