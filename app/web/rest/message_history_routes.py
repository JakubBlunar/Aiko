"""Per-message deletion and replacement of the latest assistant response."""
from __future__ import annotations

import logging
import threading

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from app.core.session.message_history import HistoryConflict, HistoryMessageNotFound

log = logging.getLogger("app.web.server")


def register(app, session, hub) -> None:
    from app.web.server import _spawn_chat_turn

    @app.delete("/api/sessions/{session_id}/messages/{message_id}")
    def delete_message(session_id: str, message_id: int) -> JSONResponse:
        try:
            deleted = session.delete_chat_message(session_id, message_id)
        except HistoryMessageNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except HistoryConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception:
            log.exception("message deletion failed: session=%s message=%d", session_id, message_id)
            raise
        return JSONResponse({"session": session_id, "message_ids": deleted})

    @app.post("/api/sessions/{session_id}/messages/{message_id}/retry")
    def retry_message(session_id: str, message_id: int) -> JSONResponse:
        try:
            row = session.reserve_chat_retry(session_id, message_id)
        except HistoryMessageNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except HistoryConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        try:
            _spawn_chat_turn(
                session, hub, row.content, threading.Event(), retry_message_id=row.id,
            )
        except Exception:
            session.finish_chat_retry(row.id)
            log.exception("message resend could not start: session=%s message=%d", session_id, row.id)
            raise
        return JSONResponse({"session": session_id, "message_id": row.id}, status_code=202)
