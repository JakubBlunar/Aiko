"""Read an exact saved memory without turning inspection into a write."""
import json

from app.llm.tools.base import ToolError, ToolSchema


class InspectMemoryTool:
    def __init__(self, provider):
        self._provider = provider

    def schema(self):
        return ToolSchema("inspect_memory", (
            "Read one exact saved memory synchronously and locally by memory_id returned by "
            "recall or concept evidence. Returns qualified content, subject, source mode/domain, "
            "recorded/event dates, revision, current/withdrawn source state and bounded original "
            "messages. No memory is created, corrected or confirmed. Source receipts are not "
            "proof that the interpretation is true. For long text follow next_offset using "
            "content_offset and the returned revision; fragments are incomplete. Open long "
            "original messages with conversation_history. Never invent IDs."
        ), {"type": "object", "properties": {
            "memory_id": {"type": "integer", "minimum": 1},
            "content_offset": {"type": "integer", "minimum": 0},
            "revision": {"type": "string", "maxLength": 128},
        }, "required": ["memory_id"], "additionalProperties": False})

    def run(self, arguments):
        try:
            return json.dumps(self._provider(**arguments), ensure_ascii=False)
        except Exception as exc:
            raise ToolError(f"inspect_memory unavailable: {exc}") from exc
