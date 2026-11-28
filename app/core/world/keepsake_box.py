"""The shared keepsake box and its append-only paper notes."""
from __future__ import annotations

import json
from typing import Any

from app.core.infra import timephrase

KEEPSAKE_BOX_SLUG = "shared_keepsake_box"
NOTE_MAX_LENGTH = 600
_PAGE_SIZE = 10


class ProtectedKeepsakeBox(ValueError):
    def __init__(self) -> None:
        super().__init__("Our keepsake box is protected and stays on the bookshelf.")


class KeepsakeBoxUnavailable(LookupError):
    pass


def is_keepsake_box(item: Any) -> bool:
    return item.slug == KEEPSAKE_BOX_SLUG and item.given_by == "household"


def _note(row: tuple) -> dict[str, Any]:
    note_id, author, text, created_at = row
    return {"id": note_id, "author": author, "text": text, "created_at": created_at}


class KeepsakeBoxMixin:
    def keepsake_box_item(self):
        with self._lock:
            return next((item for item in self._items.values() if is_keepsake_box(item)), None)

    def ensure_keepsake_box(self) -> bool:
        """Add the fixture to an existing apartment or restore it after a reset."""
        conn = self._get_conn()
        with self._home_action_lock:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                shelf = conn.execute(
                    "SELECT l.id FROM world_locations l JOIN world_scenes s ON s.id=l.scene_id "
                    "WHERE s.origin='builtin' AND l.slug='bookshelf'",
                ).fetchone()
                if shelf is None or conn.execute(
                    "SELECT 1 FROM world_items WHERE slug=? AND given_by='household'",
                    (KEEPSAKE_BOX_SLUG,),
                ).fetchone():
                    return False
                latest_id = conn.execute("SELECT max(id) FROM world_keepsake_notes").fetchone()[0]
                state = json.dumps({"home_essential": True, "keepsake_latest_note_id": latest_id})
                at = timephrase.utcnow().isoformat()
                conn.execute(
                    "INSERT INTO world_items (slug,name,description,kind,location_id,"
                    "home_location_id,state_json,given_by,created_at,updated_at) "
                    "VALUES (?,?,?,'decor',?,?,?,'household',?,?)",
                    (KEEPSAKE_BOX_SLUG, "Our keepsake box",
                     "A small box for notes about our moments.",
                     shelf[0], shelf[0], state, at, at),
                )
            self._reload_mirror()
        return True

    def _keepsake_box_id(self, conn, item_id: int | None) -> int:
        row = conn.execute(
            "SELECT id FROM world_items WHERE slug=? AND given_by='household' "
            "AND (? IS NULL OR id=?)",
            (KEEPSAKE_BOX_SLUG, item_id, item_id),
        ).fetchone()
        if row is None:
            raise KeepsakeBoxUnavailable("The shared keepsake box is unavailable.")
        return row[0]

    def read_keepsake_notes(self, *, before_id: int | None = None,
                            item_id: int | None = None) -> dict[str, Any]:
        if before_id is not None and (
            type(before_id) is not int or not 0 < before_id <= 2**63 - 1
        ):
            raise ValueError("before_id must be a positive note ID.")
        conn = self._get_conn()
        query = "SELECT id,author,text,created_at FROM world_keepsake_notes"
        params = (_PAGE_SIZE + 1,)
        if before_id is not None:
            query += " WHERE id < ?"
            params = (before_id, *params)
        with self._home_action_lock:
            self._keepsake_box_id(conn, item_id)
            rows = conn.execute(query + " ORDER BY id DESC LIMIT ?", params).fetchall()
        return {
            "notes": [_note(row) for row in rows[:_PAGE_SIZE]],
            "next_before_id": rows[_PAGE_SIZE - 1][0] if len(rows) > _PAGE_SIZE else None,
        }

    def keepsake_note_counts(self) -> dict[str, int]:
        """Count signed slips across the whole box, independently of the page or chat turn."""
        counts = {"aiko": 0, "user": 0}
        conn = self._get_conn()
        self._keepsake_box_id(conn, None)
        counts.update(conn.execute(
            "SELECT author, count(*) FROM world_keepsake_notes GROUP BY author"
        ).fetchall())
        return {"total": sum(counts.values()), **counts}

    def add_keepsake_note(self, text: str, *, author: str, request_key: str | None = None,
                          item_id: int | None = None) -> dict[str, Any]:
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= NOTE_MAX_LENGTH:
            raise ValueError(f"Write a note of 1 to {NOTE_MAX_LENGTH} characters.")
        if request_key is not None and (
            not isinstance(request_key, str) or not 1 <= len(request_key) <= 128
        ):
            raise ValueError("Invalid note request key.")
        text = text.strip()
        at = timephrase.utcnow().isoformat()
        conn = self._get_conn()
        with self._home_action_lock:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                box_id = self._keepsake_box_id(conn, item_id)
                cursor = conn.execute(
                    "INSERT INTO world_keepsake_notes (author,text,created_at,request_key) "
                    "VALUES (?,?,?,?) ON CONFLICT(author,request_key) DO NOTHING",
                    (author, text, at, request_key),
                )
                if cursor.rowcount == 0:
                    saved = _note(conn.execute(
                        "SELECT id,author,text,created_at FROM world_keepsake_notes "
                        "WHERE author=? AND request_key=?", (author, request_key),
                    ).fetchone())
                    if saved["text"] != text:
                        raise ValueError("That request key already saved a different note.")
                    return saved
                note_id = cursor.lastrowid
                conn.execute(
                    "UPDATE world_items SET state_json=json_set(state_json,"
                    "'$.keepsake_latest_note_id',?), updated_at=? WHERE id=?",
                    (note_id, at, box_id),
                )
            with self._lock:
                box = self._items.get(box_id)
                if box is not None:
                    box.state = {**box.state, "keepsake_latest_note_id": note_id}
                    box.updated_at = at
        return _note((note_id, author, text, at))
