"""Archive reads and explicit idle-time summary requests, scoped by the facade."""

from typing import Literal

from fastapi import HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.llm.tools.base import ToolError


class SummaryRequest(BaseModel):
    day: str = Field(min_length=10, max_length=10)
    timezone: str = Field(default="", max_length=100)


def register(app, session):
    @app.get("/api/archive")
    def read_archive(
        mode: Literal["overviews", "messages", "message"] = "overviews",
        start: str = Query(default="", max_length=10),
        end: str = Query(default="", max_length=10),
        timezone: str = Query(default="", max_length=100),
        topic: str = Query(default="", max_length=200),
        cursor: str = Query(default="", max_length=5000),
        message_id: int | None = Query(default=None, ge=1),
    ):
        arguments = {"mode": mode}
        for key, value in (
            ("start", start),
            ("end", end),
            ("timezone", timezone),
            ("topic", topic),
            ("cursor", cursor),
            ("message_id", message_id),
        ):
            if value is not None and value != "":
                arguments[key] = value
        try:
            result = session.read_conversation_archive(arguments)
        except ToolError as exc:
            raise HTTPException(400, str(exc)) from exc
        return JSONResponse(
            result, status_code=503 if result["status"] == "store_unavailable" else 200
        )

    def request(payload, cancel=False):
        try:
            return JSONResponse(
                session.request_archive_summary(
                    payload.day,
                    payload.timezone,
                    cancel=cancel,
                ),
                status_code=200 if cancel else 202,
            )
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/archive/summary")
    def queue_summary(payload: SummaryRequest):
        return request(payload)

    @app.delete("/api/archive/summary")
    def cancel_summary(payload: SummaryRequest):
        return request(payload, cancel=True)
