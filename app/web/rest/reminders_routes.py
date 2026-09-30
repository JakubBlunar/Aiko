"""Local reminder management for the web and desktop clients."""
from __future__ import annotations

from fastapi import HTTPException

def register(app, session) -> None:
    @app.get("/api/reminders")
    def list_reminders() -> dict:
        return {"reminders": session.reminder_store.list_pending()}

    @app.post("/api/reminders")
    def set_reminder(payload: dict) -> dict:
        try:
            reminder = session.reminder_store.create(
                payload.get("text"), payload.get("due_at"),
                source_session=session.session_key,
            )
        except (ValueError, TypeError, OverflowError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"reminder": reminder}

    @app.delete("/api/reminders/{reminder_id}")
    def cancel_reminder(reminder_id: int) -> dict:
        try:
            reminder = session.reminder_store.cancel(reminder_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"reminder": reminder}
