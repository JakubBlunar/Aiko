"""Read and leave notes from the shared keepsake box in the World tab."""
from __future__ import annotations

from fastapi import HTTPException

from app.core.world.keepsake_box import KeepsakeBoxUnavailable


def register(app, session) -> None:
    @app.get("/api/world/items/{item_id}/keepsake-notes")
    def read_notes(item_id: int, before_id: int | None = None):
        store = session.world_store
        if store is None:
            raise HTTPException(503, "World store unavailable.")
        try:
            return store.read_keepsake_notes(item_id=item_id, before_id=before_id)
        except KeepsakeBoxUnavailable as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/world/items/{item_id}/keepsake-notes")
    def leave_note(item_id: int, payload: dict):
        store = session.world_store
        if store is None:
            raise HTTPException(503, "World store unavailable.")
        try:
            note = store.add_keepsake_note(
                payload.get("text"), author="user", request_key=payload.get("request_key"),
                item_id=item_id,
            )
        except KeepsakeBoxUnavailable as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        box = store.get_item(item_id)
        if box is not None:
            session._notify_world({"item": box.to_dict()})
        return {"note": note}
