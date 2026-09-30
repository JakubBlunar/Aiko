"""Synchronous user-requested reminders on the brain tool lane."""
from __future__ import annotations

import json
from typing import Any

from app.core.goals.reminders import ReminderStore
from app.llm.tools.base import ToolError, ToolSchema


class RemindersTool:
    def __init__(self, db, user_id: str, session_id: str) -> None:
        self.store = ReminderStore(db, user_id)
        self.session_id = session_id

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="reminders",
            description=(
                "Set, list, or cancel one-shot LOCAL reminders explicitly requested by the user. "
                "A reminder is an alarm at its due time, not a calendar appointment. "
                "Use get_time to resolve relative dates; ask about ambiguous dates, timezone "
                "offsets, or intended reminder rather than guessing. Set requires a future "
                "ISO-8601 date/time with explicit offset. List first to find an ID to cancel. "
                "Never set a reminder for a hypothetical or inferred plan. No recurrence, "
                "external notifications, or delivery when Aiko is not running."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["set", "list", "cancel"]},
                    "text": {"type": "string", "description": "Set: reminder text (1-500 chars)."},
                    "due_at": {"type": "string", "description": "Set: ISO time with offset."},
                    "reminder_id": {
                        "type": "integer", "description": "Cancel: ID returned by list.",
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        try:
            action = arguments.get("action")
            if action == "set":
                result = self.store.create(
                    arguments.get("text"), arguments.get("due_at"),
                    source_session=self.session_id,
                )
            elif action == "list":
                result = self.store.list_pending()
            elif action == "cancel":
                reminder_id = arguments.get("reminder_id")
                if not isinstance(reminder_id, int) or isinstance(reminder_id, bool):
                    raise ValueError("List reminders first and supply a reminder_id.")
                result = self.store.cancel(reminder_id)
            else:
                raise ValueError("Unknown reminder action.")
        except (ValueError, TypeError, OverflowError) as exc:
            raise ToolError(str(exc)) from None
        return json.dumps({"local_only": True, "result": result})
