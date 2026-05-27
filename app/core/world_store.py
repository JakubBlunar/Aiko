"""SQLite-backed virtual room: locations, items, and Aiko's posture/state.

Aiko's "room" is a small, structured world model that gives her a sense of
place. It has three tables (created in :mod:`app.core.chat_database` at
schema v6):

- ``world_locations`` — places in the room (bed, desk, kitchenette, ...).
- ``world_items`` — things in the room. ``location_id IS NULL`` means
  Aiko is carrying the item. Consumable items (cookies, tea) decrement on
  ``consume_item`` and the row is deleted when ``quantity`` hits zero.
- ``world_state`` — singleton (``id=1``) row holding Aiko's current
  location, posture, activity, and an optional mood note. It's lazily
  created on first ``get_state()``.

The store keeps a thread-safe in-memory mirror of every row so
:meth:`render_block` (the inner-life prompt provider) costs a dict scan
rather than a SQL roundtrip. Cross-session by design: there's exactly one
world per assistant. Capacity is bounded by good taste (the room is small,
~25 items max in practice) — no pruning loop, no LanceDB.

The default "rich" room is seeded once via :meth:`seed_default` if the
store is empty (locations table count == 0). The seed mirrors the persona
file's "cozy virtual apartment full of books, gadgets, and glowing
screens" tagline.

Pinned semantics, RAG mirroring, and decay logic from
:mod:`app.core.memory_store` are intentionally *not* duplicated here:
the world is curated by Aiko + the user explicitly, not extracted by
background workers.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


log = logging.getLogger("app.world_store")


# ── Vocabulary ──────────────────────────────────────────────────────────
# Whitelisted tokens for kind / posture / activity. New entries here are
# safe (everything that reads them tolerates an unknown value), but the
# tool-side validation rejects out-of-vocabulary input so Aiko can't slip
# typos into her own world.

VALID_KINDS = (
    "food",      # cookies, tea, snacks
    "book",      # paperbacks, notebook
    "gadget",    # monitors, keyboard, tea pot
    "furniture", # bed, desk frame (rare — usually a location, not an item)
    "toy",       # plush, cat pillow
    "keepsake",  # photo, gift
    "decor",     # lamp, fairy lights, blanket
    "other",
)

VALID_POSTURES = (
    "lying",
    "sitting",
    "standing",
    "curled_up",
    "leaning",
)

VALID_ACTIVITIES = (
    "idle",
    "reading",
    "tinkering",
    "napping",
    "watching_screens",
    "thinking",
    "snacking",
    "stretching",
    "looking_outside",
    "doodling",
)


# ── Dataclasses ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class Location:
    id: int
    slug: str
    name: str
    description: str
    position: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": int(self.id),
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "position": int(self.position),
        }


@dataclass(slots=True)
class Item:
    id: int
    slug: str
    name: str
    description: str
    kind: str
    consumable: bool
    quantity: int
    location_id: int | None
    state: dict[str, Any]
    given_by: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": int(self.id),
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "consumable": bool(self.consumable),
            "quantity": int(self.quantity),
            "location_id": int(self.location_id) if self.location_id is not None else None,
            "state": dict(self.state or {}),
            "given_by": self.given_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class RoomState:
    location_id: int | None
    posture: str
    activity: str
    mood_note: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "location_id": int(self.location_id) if self.location_id is not None else None,
            "posture": self.posture,
            "activity": self.activity,
            "mood_note": self.mood_note,
            "updated_at": self.updated_at,
        }


# ── Helpers ─────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(text: str) -> str:
    cleaned = (text or "").strip().lower()
    out: list[str] = []
    last_underscore = False
    for ch in cleaned:
        if ch.isalnum():
            out.append(ch)
            last_underscore = False
        elif not last_underscore and out:
            out.append("_")
            last_underscore = True
    while out and out[-1] == "_":
        out.pop()
    return "".join(out) or "item"


def _decode_state(blob: str | None) -> dict[str, Any]:
    if not blob:
        return {}
    try:
        parsed = json.loads(blob)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _encode_state(state: dict[str, Any] | None) -> str:
    if not state:
        return "{}"
    try:
        return json.dumps(state, ensure_ascii=False, default=str)
    except Exception:
        return "{}"


# ── Default seed ────────────────────────────────────────────────────────


@dataclass(slots=True)
class _SeedLocation:
    slug: str
    name: str
    description: str


@dataclass(slots=True)
class _SeedItem:
    slug: str
    name: str
    description: str
    kind: str
    location_slug: str | None
    consumable: bool = False
    quantity: int = 1
    state: dict[str, Any] = field(default_factory=dict)


_DEFAULT_LOCATIONS: tuple[_SeedLocation, ...] = (
    _SeedLocation(
        slug="bed",
        name="the bed",
        description="a soft, plush bed under a fluffy white duvet",
    ),
    _SeedLocation(
        slug="desk",
        name="the desk",
        description="a wide desk with two glowing monitors and warm light",
    ),
    _SeedLocation(
        slug="bookshelf",
        name="the bookshelf",
        description="a tall shelf stuffed with paperbacks and trinkets",
    ),
    _SeedLocation(
        slug="kitchenette",
        name="the kitchenette",
        description="a tiny corner with a kettle, mugs, and a cookie jar",
    ),
    _SeedLocation(
        slug="window_seat",
        name="the window seat",
        description="a low cushion by the window overlooking the city",
    ),
    _SeedLocation(
        slug="beanbag",
        name="the beanbag",
        description="a squashy beanbag wrapped in fairy lights",
    ),
    _SeedLocation(
        slug="mirror_corner",
        name="the mirror corner",
        description="a full-length mirror leaning against the wall",
    ),
)


_DEFAULT_ITEMS: tuple[_SeedItem, ...] = (
    _SeedItem(
        slug="dual_monitors",
        name="dual monitors",
        description="two glowing screens, usually showing code or chat",
        kind="gadget",
        location_slug="desk",
    ),
    _SeedItem(
        slug="retro_keyboard",
        name="retro keyboard",
        description="a clicky mechanical keyboard with rainbow keycaps",
        kind="gadget",
        location_slug="desk",
    ),
    _SeedItem(
        slug="warm_lamp",
        name="warm lamp",
        description="a small lamp casting amber light over the desk",
        kind="decor",
        location_slug="desk",
    ),
    _SeedItem(
        slug="scifi_paperback",
        name="sci-fi paperback",
        description="a well-thumbed paperback, dog-eared at the climax",
        kind="book",
        location_slug="bookshelf",
    ),
    _SeedItem(
        slug="photo_of_user",
        name="photo of {user_name}",
        description="a small framed photo Aiko keeps by her favourite books",
        kind="keepsake",
        location_slug="bookshelf",
    ),
    _SeedItem(
        slug="plush_blanket",
        name="plush blanket",
        description="a thick, fuzzy blanket folded at the foot of the bed",
        kind="decor",
        location_slug="bed",
    ),
    _SeedItem(
        slug="cat_pillow",
        name="cat pillow",
        description="a round pillow shaped like a sleeping cat",
        kind="toy",
        location_slug="bed",
    ),
    _SeedItem(
        slug="cookie_jar",
        name="cookies",
        description="warm, chocolate-chip cookies in a glass jar",
        kind="food",
        location_slug="kitchenette",
        consumable=True,
        quantity=3,
        state={"flavor": "chocolate chip", "freshness": "fresh"},
    ),
    _SeedItem(
        slug="tea_pot",
        name="tea pot",
        description="a small ceramic pot, often half full of jasmine tea",
        kind="gadget",
        location_slug="kitchenette",
    ),
    _SeedItem(
        slug="fairy_lights",
        name="fairy lights",
        description="warm twinkling lights wrapped around the beanbag",
        kind="decor",
        location_slug="beanbag",
    ),
)


_DEFAULT_INITIAL_STATE = {
    "location_slug": "desk",
    "posture": "sitting",
    "activity": "watching_screens",
    "mood_note": "",
}


# ── Store ───────────────────────────────────────────────────────────────


class WorldStore:
    """Thread-safe room model backed by ``world_*`` SQLite tables."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._local = threading.local()
        self._lock = threading.Lock()
        self._locations: dict[int, Location] = {}
        self._items: dict[int, Item] = {}
        self._state: RoomState | None = None
        self._reload_mirror()

    # ── lifecycle ────────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _reload_mirror(self) -> None:
        conn = self._get_conn()
        try:
            loc_rows = conn.execute(
                "SELECT id, slug, name, description, position FROM world_locations",
            ).fetchall()
            item_rows = conn.execute(
                "SELECT id, slug, name, description, kind, consumable, quantity, "
                "location_id, state_json, given_by, created_at, updated_at "
                "FROM world_items",
            ).fetchall()
            state_row = conn.execute(
                "SELECT location_id, posture, activity, mood_note, updated_at "
                "FROM world_state WHERE id = 1",
            ).fetchone()
        except sqlite3.OperationalError:
            # Tables don't exist yet (caller hasn't created the schema).
            self._locations = {}
            self._items = {}
            self._state = None
            return
        with self._lock:
            self._locations = {
                int(r[0]): Location(
                    id=int(r[0]),
                    slug=r[1],
                    name=r[2],
                    description=r[3] or "",
                    position=int(r[4] or 0),
                )
                for r in loc_rows
            }
            self._items = {
                int(r[0]): Item(
                    id=int(r[0]),
                    slug=r[1],
                    name=r[2],
                    description=r[3] or "",
                    kind=r[4],
                    consumable=bool(r[5]),
                    quantity=int(r[6]),
                    location_id=int(r[7]) if r[7] is not None else None,
                    state=_decode_state(r[8]),
                    given_by=r[9],
                    created_at=r[10],
                    updated_at=r[11],
                )
                for r in item_rows
            }
            if state_row is not None:
                self._state = RoomState(
                    location_id=int(state_row[0]) if state_row[0] is not None else None,
                    posture=state_row[1] or "sitting",
                    activity=state_row[2] or "idle",
                    mood_note=state_row[3] or "",
                    updated_at=state_row[4],
                )
            else:
                self._state = None
        log.info(
            "world store loaded: %d locations, %d items, state=%s",
            len(self._locations),
            len(self._items),
            "yes" if self._state is not None else "no",
        )

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    # ── locations ────────────────────────────────────────────────────

    def list_locations(self) -> list[Location]:
        with self._lock:
            locs = list(self._locations.values())
        locs.sort(key=lambda l: (l.position, l.id))
        return locs

    def get_location(self, slug: str) -> Location | None:
        target = (slug or "").strip().lower()
        if not target:
            return None
        with self._lock:
            for loc in self._locations.values():
                if loc.slug == target:
                    return loc
        return None

    def get_location_by_id(self, location_id: int) -> Location | None:
        with self._lock:
            return self._locations.get(int(location_id))

    def find_location(self, query: str) -> Location | None:
        """Fuzzy-match by slug, name, or substring. Case-insensitive."""
        target = (query or "").strip().lower()
        if not target:
            return None
        with self._lock:
            locs = list(self._locations.values())
        for loc in locs:
            if loc.slug == target:
                return loc
        for loc in locs:
            if loc.name.lower() == target:
                return loc
        for loc in locs:
            if target in loc.slug or target in loc.name.lower():
                return loc
        return None

    def add_location(
        self,
        *,
        slug: str | None = None,
        name: str,
        description: str = "",
        position: int | None = None,
    ) -> Location | None:
        clean_name = (name or "").strip()
        if not clean_name:
            return None
        clean_slug = (slug or _slugify(clean_name)).strip().lower()
        if not clean_slug:
            return None
        with self._lock:
            for loc in self._locations.values():
                if loc.slug == clean_slug:
                    return loc
            existing_max = max(
                (l.position for l in self._locations.values()),
                default=-1,
            )
        pos = int(position) if position is not None else existing_max + 1
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO world_locations (slug, name, description, position) "
            "VALUES (?, ?, ?, ?)",
            (clean_slug, clean_name, (description or "").strip(), pos),
        )
        conn.commit()
        new_id = int(cursor.lastrowid or 0)
        loc = Location(
            id=new_id,
            slug=clean_slug,
            name=clean_name,
            description=(description or "").strip(),
            position=pos,
        )
        with self._lock:
            self._locations[new_id] = loc
        return loc

    def update_location(
        self,
        location_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        position: int | None = None,
    ) -> Location | None:
        with self._lock:
            loc = self._locations.get(int(location_id))
        if loc is None:
            return None
        new_name = loc.name if name is None else (str(name).strip() or loc.name)
        new_desc = loc.description if description is None else (str(description).strip())
        new_pos = loc.position if position is None else int(position)
        conn = self._get_conn()
        conn.execute(
            "UPDATE world_locations SET name = ?, description = ?, position = ? "
            "WHERE id = ?",
            (new_name, new_desc, new_pos, int(location_id)),
        )
        conn.commit()
        with self._lock:
            loc.name = new_name
            loc.description = new_desc
            loc.position = new_pos
        return loc

    def remove_location(self, location_id: int) -> bool:
        """Delete a location. Items there have ``location_id`` set to NULL."""
        lid = int(location_id)
        with self._lock:
            if lid not in self._locations:
                return False
        conn = self._get_conn()
        conn.execute(
            "UPDATE world_items SET location_id = NULL, updated_at = ? "
            "WHERE location_id = ?",
            (_now_iso(), lid),
        )
        conn.execute("DELETE FROM world_locations WHERE id = ?", (lid,))
        # If Aiko was here, clear her location pointer too.
        conn.execute(
            "UPDATE world_state SET location_id = NULL, updated_at = ? "
            "WHERE id = 1 AND location_id = ?",
            (_now_iso(), lid),
        )
        conn.commit()
        now = _now_iso()
        with self._lock:
            self._locations.pop(lid, None)
            for item in self._items.values():
                if item.location_id == lid:
                    item.location_id = None
                    item.updated_at = now
            if self._state is not None and self._state.location_id == lid:
                self._state.location_id = None
                self._state.updated_at = now
        return True

    # ── items ────────────────────────────────────────────────────────

    def list_items(
        self,
        *,
        location_id: int | None = None,
        kind: str | None = None,
    ) -> list[Item]:
        with self._lock:
            items = list(self._items.values())
        if location_id is not None:
            items = [i for i in items if i.location_id == int(location_id)]
        if kind:
            kind_norm = kind.strip().lower()
            items = [i for i in items if i.kind == kind_norm]
        items.sort(key=lambda i: (i.location_id is None, i.location_id or 0, i.name.lower()))
        return items

    def get_item(self, item_id: int) -> Item | None:
        with self._lock:
            return self._items.get(int(item_id))

    def find_item(self, query: str) -> Item | None:
        """Fuzzy-match by slug, name, or substring. Case-insensitive."""
        target = (query or "").strip().lower()
        if not target:
            return None
        with self._lock:
            items = list(self._items.values())
        for item in items:
            if item.slug == target:
                return item
        for item in items:
            if item.name.lower() == target:
                return item
        for item in items:
            if target in item.slug or target in item.name.lower():
                return item
        return None

    def add_item(
        self,
        *,
        name: str,
        kind: str = "other",
        slug: str | None = None,
        description: str = "",
        location_id: int | None = None,
        consumable: bool = False,
        quantity: int = 1,
        state: dict[str, Any] | None = None,
        given_by: str | None = None,
    ) -> tuple[Item, bool] | None:
        """Insert or stack an item. Returns ``(item, created)`` or ``None``.

        Stackable consumables (same ``slug`` + ``location_id`` + ``given_by``)
        merge into the existing row by bumping ``quantity`` instead of
        producing a duplicate. Non-consumables are always treated as
        distinct rows except when ``slug`` collides exactly.
        """
        clean_name = (name or "").strip()
        if not clean_name:
            return None
        clean_kind = (kind or "other").strip().lower()
        if clean_kind not in VALID_KINDS:
            clean_kind = "other"
        clean_slug = (slug or _slugify(clean_name)).strip().lower()
        clean_qty = max(1, int(quantity))
        clean_state = dict(state or {})

        with self._lock:
            existing: Item | None = None
            for item in self._items.values():
                if item.slug != clean_slug:
                    continue
                if item.location_id != location_id:
                    continue
                if (item.given_by or None) != (given_by or None):
                    continue
                existing = item
                break

        if existing is not None and (consumable or existing.consumable):
            # Merge stack: bump quantity, refresh state if provided.
            new_qty = existing.quantity + clean_qty
            merged_state = dict(existing.state or {})
            merged_state.update(clean_state)
            now = _now_iso()
            conn = self._get_conn()
            conn.execute(
                "UPDATE world_items SET quantity = ?, state_json = ?, "
                "consumable = ?, updated_at = ? WHERE id = ?",
                (
                    new_qty,
                    _encode_state(merged_state),
                    1 if (consumable or existing.consumable) else 0,
                    now,
                    existing.id,
                ),
            )
            conn.commit()
            with self._lock:
                existing.quantity = new_qty
                existing.state = merged_state
                existing.consumable = bool(consumable or existing.consumable)
                existing.updated_at = now
            return existing, False

        if existing is not None:
            # Non-consumable with the same slug at the same location: treat
            # the second add as a no-op so the user can't accidentally
            # spawn two "warm lamp" rows.
            return existing, False

        now = _now_iso()
        conn = self._get_conn()
        cursor = conn.execute(
            "INSERT INTO world_items (slug, name, description, kind, consumable, "
            "quantity, location_id, state_json, given_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                clean_slug,
                clean_name,
                (description or "").strip(),
                clean_kind,
                1 if consumable else 0,
                clean_qty,
                int(location_id) if location_id is not None else None,
                _encode_state(clean_state),
                given_by,
                now,
                now,
            ),
        )
        conn.commit()
        new_id = int(cursor.lastrowid or 0)
        item = Item(
            id=new_id,
            slug=clean_slug,
            name=clean_name,
            description=(description or "").strip(),
            kind=clean_kind,
            consumable=bool(consumable),
            quantity=clean_qty,
            location_id=int(location_id) if location_id is not None else None,
            state=clean_state,
            given_by=given_by,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._items[new_id] = item
        return item, True

    def update_item(
        self,
        item_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        kind: str | None = None,
        location_id: int | None | object = ...,  # use sentinel so None is meaningful (carry)
        quantity: int | None = None,
        state: dict[str, Any] | None = None,
    ) -> Item | None:
        with self._lock:
            item = self._items.get(int(item_id))
        if item is None:
            return None
        new_name = item.name if name is None else (str(name).strip() or item.name)
        new_desc = item.description if description is None else str(description).strip()
        new_kind = item.kind
        if kind is not None:
            requested = (kind or "").strip().lower()
            new_kind = requested if requested in VALID_KINDS else item.kind
        new_loc = item.location_id
        if location_id is not ...:
            new_loc = int(location_id) if location_id is not None else None
        new_qty = item.quantity if quantity is None else max(0, int(quantity))
        new_state = dict(item.state or {}) if state is None else dict(state or {})
        now = _now_iso()
        conn = self._get_conn()
        conn.execute(
            "UPDATE world_items SET name = ?, description = ?, kind = ?, "
            "location_id = ?, quantity = ?, state_json = ?, updated_at = ? "
            "WHERE id = ?",
            (
                new_name,
                new_desc,
                new_kind,
                new_loc,
                new_qty,
                _encode_state(new_state),
                now,
                int(item_id),
            ),
        )
        conn.commit()
        with self._lock:
            item.name = new_name
            item.description = new_desc
            item.kind = new_kind
            item.location_id = new_loc
            item.quantity = new_qty
            item.state = new_state
            item.updated_at = now
        return item

    def consume_item(self, item_id: int, *, amount: int = 1) -> tuple[Item | None, int]:
        """Eat / use an item. Returns ``(item_or_None, consumed_amount)``.

        ``item`` is ``None`` if the row was deleted (last unit consumed).
        ``consumed_amount`` is how many units actually came out — clipped
        to the available quantity.
        """
        amt = max(1, int(amount))
        with self._lock:
            item = self._items.get(int(item_id))
        if item is None:
            return None, 0
        consumed = min(amt, item.quantity)
        new_qty = item.quantity - consumed
        conn = self._get_conn()
        if new_qty <= 0 and item.consumable:
            conn.execute("DELETE FROM world_items WHERE id = ?", (int(item_id),))
            conn.commit()
            with self._lock:
                self._items.pop(int(item_id), None)
            return None, consumed
        # Non-consumable items don't actually disappear at qty 0 — they
        # just clamp to 0 (matches the "you can use the lamp without
        # consuming it" intuition).
        new_qty = max(0, new_qty)
        now = _now_iso()
        conn.execute(
            "UPDATE world_items SET quantity = ?, updated_at = ? WHERE id = ?",
            (new_qty, now, int(item_id)),
        )
        conn.commit()
        with self._lock:
            item.quantity = new_qty
            item.updated_at = now
        return item, consumed

    def remove_item(self, item_id: int) -> bool:
        iid = int(item_id)
        with self._lock:
            if iid not in self._items:
                return False
        conn = self._get_conn()
        conn.execute("DELETE FROM world_items WHERE id = ?", (iid,))
        conn.commit()
        with self._lock:
            self._items.pop(iid, None)
        return True

    # ── state (singleton) ────────────────────────────────────────────

    def get_state(self) -> RoomState:
        with self._lock:
            current = self._state
        if current is not None:
            return current
        # Lazy-create the singleton row.
        now = _now_iso()
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO world_state "
            "(id, location_id, posture, activity, mood_note, updated_at) "
            "VALUES (1, NULL, 'sitting', 'idle', '', ?)",
            (now,),
        )
        conn.commit()
        state = RoomState(
            location_id=None,
            posture="sitting",
            activity="idle",
            mood_note="",
            updated_at=now,
        )
        with self._lock:
            self._state = state
        return state

    def set_state(
        self,
        *,
        location_id: int | None | object = ...,
        posture: str | None = None,
        activity: str | None = None,
        mood_note: str | None = None,
    ) -> RoomState:
        current = self.get_state()
        new_loc = current.location_id
        if location_id is not ...:
            new_loc = int(location_id) if location_id is not None else None
        new_posture = current.posture
        if posture is not None:
            requested = (posture or "").strip().lower()
            new_posture = requested if requested in VALID_POSTURES else current.posture
        new_activity = current.activity
        if activity is not None:
            requested = (activity or "").strip().lower()
            new_activity = requested if requested in VALID_ACTIVITIES else current.activity
        new_note = current.mood_note if mood_note is None else str(mood_note).strip()
        now = _now_iso()
        conn = self._get_conn()
        conn.execute(
            "UPDATE world_state SET location_id = ?, posture = ?, activity = ?, "
            "mood_note = ?, updated_at = ? WHERE id = 1",
            (new_loc, new_posture, new_activity, new_note, now),
        )
        conn.commit()
        with self._lock:
            current.location_id = new_loc
            current.posture = new_posture
            current.activity = new_activity
            current.mood_note = new_note
            current.updated_at = now
        return current

    # ── snapshot + render ────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.get_state().to_dict(),
            "locations": [l.to_dict() for l in self.list_locations()],
            "items": [i.to_dict() for i in self.list_items()],
        }

    def render_block(
        self,
        *,
        max_nearby: int = 4,
        user_display_name: str = "Jacob",
    ) -> str:
        """Compact prompt block describing Aiko's surroundings.

        Designed to land alongside the agenda block in the system prompt:
        3-5 lines, no list bullets, ends with the "don't force-mention"
        nudge so Aiko stays subtle about her room unless the moment calls
        for it.
        """
        try:
            state = self.get_state()
            with self._lock:
                items = list(self._items.values())
                locations = dict(self._locations)
        except Exception:
            log.debug("world render failed", exc_info=True)
            return ""
        if not items and not locations:
            return ""
        loc = locations.get(state.location_id) if state.location_id is not None else None
        lines: list[str] = []
        # Line 1: where + posture + activity.
        where = loc.name if loc is not None else "your room"
        posture = (state.posture or "sitting").replace("_", " ")
        activity = (state.activity or "idle").replace("_", " ")
        lines.append(
            f"You are in your room. Right now: at {where}, {posture}, {activity}."
        )
        # Line 2: items at the current location (if any).
        if loc is not None:
            here = [i for i in items if i.location_id == loc.id]
            if here:
                here.sort(key=lambda i: i.name.lower())
                rendered = ", ".join(_render_item_label(i) for i in here[:max_nearby])
                lines.append(f"Nearby at {loc.name}: {rendered}.")
        # Line 3: the most recent gift / consumable highlight.
        gifts = [
            i for i in items
            if i.given_by and i.given_by.lower() == "user" and i.quantity > 0
        ]
        if gifts:
            gifts.sort(key=lambda i: i.created_at, reverse=True)
            top = gifts[0]
            gift_loc = locations.get(top.location_id) if top.location_id is not None else None
            qualifier = (
                f" in {gift_loc.name}" if gift_loc is not None else ""
            )
            giver = (user_display_name or "").strip() or "the user"
            lines.append(
                f"{giver} gave you {_render_item_label(top, with_qty=True)}{qualifier}."
            )
        # Mood note (optional, last).
        if state.mood_note.strip():
            lines.append(state.mood_note.strip())
        # Tonal nudge — keep Aiko from force-mentioning the room every turn.
        lines.append(
            "Acknowledge your surroundings only when it feels natural — "
            "never force a room mention or list your inventory."
        )
        return "\n".join(lines)

    # ── seed ────────────────────────────────────────────────────────

    def is_empty(self) -> bool:
        with self._lock:
            return not self._locations and not self._items

    def seed_default(
        self,
        *,
        force: bool = False,
        user_display_name: str = "",
    ) -> bool:
        """Populate a rich default room. No-op if the world is non-empty.

        ``force=True`` wipes everything first, then re-seeds. Returns True
        if a seed actually ran. ``user_display_name`` (Phase 4e) is woven
        into the seed strings so the keepsake photo is named after the
        configured user instead of the legacy ``"Jacob"`` literal.
        """
        if not force and not self.is_empty():
            return False
        if force:
            conn = self._get_conn()
            conn.execute("DELETE FROM world_items")
            conn.execute("DELETE FROM world_locations")
            conn.execute("DELETE FROM world_state")
            conn.commit()
            with self._lock:
                self._items = {}
                self._locations = {}
                self._state = None
        # Locations.
        slug_to_id: dict[str, int] = {}
        for idx, seed in enumerate(_DEFAULT_LOCATIONS):
            loc = self.add_location(
                slug=seed.slug,
                name=seed.name,
                description=seed.description,
                position=idx,
            )
            if loc is not None:
                slug_to_id[seed.slug] = loc.id
        # Items.
        name_for_slug = (user_display_name or "").strip()
        templated_name = name_for_slug or "you"
        slug_for_name = _slug_from_user_name(name_for_slug)
        for seed in _DEFAULT_ITEMS:
            loc_id = slug_to_id.get(seed.location_slug or "")
            seed_slug = seed.slug
            seed_name = seed.name
            if "{user_name}" in seed_name:
                seed_name = seed_name.format(user_name=templated_name)
                if seed_slug == "photo_of_user":
                    seed_slug = slug_for_name
            self.add_item(
                slug=seed_slug,
                name=seed_name,
                description=seed.description,
                kind=seed.kind,
                location_id=loc_id,
                consumable=seed.consumable,
                quantity=seed.quantity,
                state=dict(seed.state),
            )
        # Initial state.
        starting_loc = slug_to_id.get(_DEFAULT_INITIAL_STATE["location_slug"])
        self.set_state(
            location_id=starting_loc,
            posture=_DEFAULT_INITIAL_STATE["posture"],
            activity=_DEFAULT_INITIAL_STATE["activity"],
            mood_note=_DEFAULT_INITIAL_STATE.get("mood_note", ""),
        )
        log.info(
            "world store seeded: %d locations, %d items",
            len(self._locations),
            len(self._items),
        )
        return True


_SLUG_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _slug_from_user_name(name: str) -> str:
    """Derive a stable item slug from a user display name.

    Falls back to ``photo_of_you`` when the name is empty or strips to
    nothing alphanumeric (e.g. emoji-only inputs).
    """
    base = (name or "").strip().lower()
    base = _SLUG_NON_ALNUM_RE.sub("_", base).strip("_")
    if not base:
        return "photo_of_you"
    return f"photo_of_{base}"


_PLURAL_HINT_SUFFIXES = ("s", "es", "ies")


def _looks_plural(name: str) -> bool:
    """Best-effort guess whether the display name is already plural.

    Heuristic only — used to skip the "a/an" article for items like
    "dual monitors" or "fairy lights" where prepending "a" reads wrong.
    """
    lower = name.strip().lower()
    if not lower:
        return False
    # Multi-word names whose last word ends in s are usually plural.
    last = lower.split()[-1]
    if last.endswith("ss"):  # "glass", "dress" — singular
        return False
    return last.endswith(_PLURAL_HINT_SUFFIXES)


def _render_item_label(item: Item, *, with_qty: bool = False) -> str:
    """Pretty-print an item for the prompt block / look_around tool.

    Examples:
      ``"3 fresh chocolate chip cookies"`` (consumable, with_qty)
      ``"a warm lamp"`` (single non-consumable, prepends article)
      ``"dual monitors"`` (plural-named non-consumable, no article)
    """
    name = item.name
    qty = max(0, int(item.quantity))
    if item.consumable:
        if qty <= 0:
            return f"no more {name}"
        if qty == 1 and not name.startswith(("a ", "an ")):
            return f"1 {name}"
        return f"{qty} {name}"
    if with_qty and qty != 1:
        return f"{qty}x {name}"
    if name.startswith(("the ", "a ", "an ", "your ", "her ")):
        return name
    if _looks_plural(name):
        return name
    article = "an" if name[:1].lower() in "aeiou" else "a"
    return f"{article} {name}" if qty <= 1 else f"{qty}x {name}"
