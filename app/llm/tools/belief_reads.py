"""Focused access to Aiko's current tentative interpretations."""
import json

from app.llm.tools.base import ToolError, ToolSchema


class RecallBeliefsTool:
    def __init__(self, provider):
        self._provider = provider

    def schema(self):
        return ToolSchema("recall_beliefs", (
            "Read current, relevant tentative interpretations of this user's conversation "
            "synchronously and locally. query is the situation/topic to check, not a request "
            "to invent a reading. Returns interpretation, reason, uncertainty, revise_when, "
            "situation, expiry and cited user words. These are Aiko's provisional readings, "
            "not settled concepts, facts or open invented hypotheses. Old/irrelevant/expired "
            "readings are omitted. Quotes do not verify an inference or prove context unchanged; "
            "open source message IDs with conversation_history when needed. No writes or LLM call."
        ), {"type": "object", "properties": {
            "query": {"type": "string", "minLength": 2, "maxLength": 200},
            "kind": {"type": "string", "enum": ["mood", "opinion"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 8},
        }, "required": ["query"], "additionalProperties": False})

    def run(self, arguments):
        try:
            return json.dumps(self._provider(**arguments), ensure_ascii=False)
        except Exception as exc:
            raise ToolError(f"recall_beliefs unavailable: {exc}") from exc
