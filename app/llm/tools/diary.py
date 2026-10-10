"""Read-only, deterministic diary browsing on the brain tool lane."""
from __future__ import annotations

import json
from typing import Any

from app.core.session.memory_facade_mixin import MemoryFacadeMixin
from app.llm.tools.base import ToolError, ToolSchema


class ReadDiaryTool:
    def __init__(self, session: Any) -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="read_diary",
            description=(
                "Read Aiko's saved diary synchronously, locally, and without changing anything. "
                "Use list to browse what she wrote or researched, newest first; use get with an "
                "entry_id returned by list to inspect it. Never invent an ID or an entry. "
                "List accepts one diary kind and an optional literal case-insensitive content "
                "substring q (not semantic search, not wildcards); filtering precedes pagination. "
                "Defaults to 5 entries, maximum 20; offset 0-10000. Returns id, date, kind, "
                "content and metadata, including source links. Reflections, dreams and questions "
                "are subjective journal records, NOT established facts. curiosity_finding is "
                "a research note: research_evidence contains search excerpts, not verified "
                "full-page evidence. Attribute claims to the note and its sources; do not "
                "treat a saved finding as proof. An empty list means no matching saved entries."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "get"]},
                    "entry_id": {
                        "type": "integer", "minimum": 1,
                        "description": "Required for get; use an ID returned by list.",
                    },
                    "kind": {
                        "type": "string", "enum": list(MemoryFacadeMixin.DIARY_KINDS),
                        "description": "List only: omit for all diary kinds.",
                    },
                    "q": {
                        "type": "string", "maxLength": 200,
                        "description": "List only: literal substring of content, ignoring case.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    "offset": {"type": "integer", "minimum": 0, "maximum": 10000},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        )

    @staticmethod
    def _integer(value: Any, name: str, minimum: int, maximum: int | None = None) -> int:
        if (
            isinstance(value, bool) or not isinstance(value, int)
            or value < minimum or (maximum is not None and value > maximum)
        ):
            bounds = f"{minimum}-{maximum}" if maximum is not None else f">= {minimum}"
            raise ToolError(f"read_diary: {name} must be an integer {bounds}.")
        return value

    @staticmethod
    def _entry(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"], "date": row["created_at"], "kind": row["kind"],
            "content": row["content"], "metadata": row.get("metadata") or {},
        }

    def run(self, arguments: dict[str, Any]) -> str:
        try:
            store = self._session.memory_store
            if store is None:
                raise ToolError("read_diary: memory storage is unavailable.")
            action = arguments.get("action")
            allowed = (
                {"action", "entry_id"} if action == "get"
                else {"action", "kind", "q", "limit", "offset"}
            )
            if set(arguments) - allowed:
                raise ToolError("read_diary: unsupported arguments for this action.")
            if action == "get":
                entry_id = self._integer(arguments.get("entry_id"), "entry_id", 1)
                memory = store.get(entry_id)
                if memory is None or memory.kind not in MemoryFacadeMixin.DIARY_KINDS:
                    raise ToolError(f"read_diary: diary entry {entry_id} not found.")
                payload = {"entry": self._entry(memory.to_dict())}
            elif action == "list":
                limit = self._integer(arguments.get("limit", 5), "limit", 1, 20)
                offset = self._integer(arguments.get("offset", 0), "offset", 0, 10000)
                kind = arguments.get("kind")
                if kind is not None and kind not in MemoryFacadeMixin.DIARY_KINDS:
                    raise ToolError("read_diary: kind must be a diary kind.")
                query = arguments.get("q", "")
                if not isinstance(query, str) or len(query) > 200:
                    raise ToolError("read_diary: q must be a string of at most 200 characters.")
                query = query.strip() or None
                entries = self._session.list_diary(
                    limit=limit, offset=offset, kind=kind, q=query,
                )
                payload = {
                    "entries": [self._entry(row) for row in entries],
                    "total": self._session.diary_count(kind=kind, q=query),
                    "limit": limit, "offset": offset,
                }
            else:
                raise ToolError("read_diary: action must be list or get.")
            return json.dumps({
                "read_only": True,
                "note": (
                    "Journal records are not established facts. Research evidence is "
                    "search excerpts, not verified full pages."
                ),
                **payload,
            }, ensure_ascii=False)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"read_diary failed: {exc}") from exc
