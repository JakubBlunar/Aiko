"""D8 read-only brain tool: archive navigation and original message drill-down."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core.conversation.archive import ConversationArchive, resolve_range
from app.llm.tools.base import ToolError, ToolSchema


log = logging.getLogger("app.tools.conversation_history")


class ConversationHistoryTool:
    def __init__(self, db: Any, user_id: str, tz_name: str = "system") -> None:
        self._db = db
        self._user_id = user_id
        self._tz_name = tz_name

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="conversation_history",
            description=(
                "Read retained conversations synchronously from the local archive, without "
                "an LLM or embedding call. Use for 'what did we talk about last week/month?', "
                "date recaps, and finding exact older exchanges absent from memory. Start "
                "with overviews for broad ranges; these are selective source outlines, not "
                "generated summaries. Use messages to page chronologically, optionally with "
                "a literal topic filter; use message + message_id for exact quotes and full "
                "long-message character pages. Check coverage/missing_days before making "
                "a period-wide claim. Follow next_cursor using only cursor and mode. "
                "Missing overviews never prevent reading messages."
            ),
            parameters={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "mode": {"type": "string", "enum": ["messages", "overviews", "message"]},
                    "when": {
                        "type": "string",
                        "description": "E.g. last week, last month, yesterday.",
                    },
                    "start": {"type": "string", "description": "Inclusive local date YYYY-MM-DD."},
                    "end": {"type": "string", "description": "Inclusive local date YYYY-MM-DD."},
                    "timezone": {"type": "string", "description": "IANA zone or system (default)."},
                    "session_id": {
                        "type": "string",
                        "description": "Optional full user:session key.",
                    },
                    "topic": {
                        "type": "string",
                        "description": "Literal substring; selected matches only.",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Opaque next_cursor; omit other filters.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    "message_id": {"type": "integer", "minimum": 1},
                    "content_offset": {"type": "integer", "minimum": 0},
                },
                "required": ["mode"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        mode = arguments.get("mode")
        allowed = {
            "messages": {
                "mode",
                "when",
                "start",
                "end",
                "timezone",
                "session_id",
                "topic",
                "cursor",
                "limit",
            },
            "overviews": {"mode", "when", "start", "end", "timezone", "cursor"},
            "message": {"mode", "message_id", "content_offset", "cursor"},
        }
        if not isinstance(mode, str) or mode not in allowed or set(arguments) - allowed[mode]:
            raise ToolError("conversation_history: unsupported mode or arguments.")
        for name in allowed[mode] - {"mode", "limit", "message_id", "content_offset"}:
            if name in arguments and (
                not isinstance(arguments[name], str)
                or len(arguments[name]) > (5000 if name == "cursor" else 200)
            ):
                raise ToolError(f"conversation_history: {name} must be a bounded string.")
        for name, default, minimum, maximum in (
            ("limit", 20, 1, 20),
            ("message_id", 0, 1, 2**63 - 1),
            ("content_offset", 0, 0, 2**31 - 1),
        ):
            if name not in arguments:
                continue
            value = arguments.get(name, default)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ToolError(
                    f"conversation_history: {name} must be an integer {minimum}-{maximum}."
                )
        cursor = arguments.get("cursor", "")
        if cursor and set(arguments) - {"mode", "cursor", "limit"}:
            raise ToolError("conversation_history: cursor already fixes the lookup filters.")
        if self._db is None:
            return json.dumps({"status": "store_unavailable"})
        try:
            archive = ConversationArchive(self._db, self._user_id)
            if mode == "message":
                if "message_id" not in arguments and not cursor:
                    raise ValueError("message_id is required for message drill-down.")
                result = archive.message(
                    arguments.get("message_id", 0),
                    arguments.get("content_offset", 0),
                    cursor=cursor,
                )
            else:
                span = (
                    None
                    if cursor
                    else resolve_range(
                        when=arguments.get("when", ""),
                        start=arguments.get("start", ""),
                        end=arguments.get("end", ""),
                        tz_name=arguments.get("timezone", self._tz_name),
                    )
                )
                if mode == "messages":
                    result = archive.messages(
                        span,
                        session=arguments.get("session_id", ""),
                        topic=arguments.get("topic", ""),
                        cursor=cursor,
                        limit=arguments.get("limit", 20),
                    )
                else:
                    result = archive.overviews(span, cursor=cursor)
        except (ValueError, KeyError) as exc:
            raise ToolError(f"conversation_history: {exc}") from exc
        except Exception:
            log.warning("archive store unavailable", exc_info=True)
            result = {"status": "store_unavailable"}
        return json.dumps(result, ensure_ascii=False)
