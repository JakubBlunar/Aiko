"""Deterministic diary browsing and intentional journal writing on the brain lane."""
from __future__ import annotations

import json
from typing import Any

from app.core.session.memory_facade_mixin import MemoryFacadeMixin
from app.core.memory.diary_journal import append_chosen_entry, journal_authorship
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
                "You can revisit your own earlier perspective, not only answer diary requests. "
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
            "authorship": journal_authorship(row.get("metadata") or {}),
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


class WriteDiaryTool:
    def __init__(self, session: Any) -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="write_diary",
            description=(
                "Save one intentional entry in YOUR own diary synchronously and locally. "
                "Use when you choose to keep a meaningful feeling, uncertainty, realization "
                "or changed perspective, or the user asks you to journal. Optional, never a "
                "quota or an automatic consequence of reading an entry. Write 1-3 concrete "
                "first-person sentences (16-2000 characters). Distinguish your feelings from "
                "what the user actually said; shared imagined scenes are not physical evidence. "
                "The date is recorded automatically; use concrete dates in prose instead of "
                "today, tonight or tomorrow. No inline tags. An optional revisits_entry_id "
                "links an earlier entry returned by read_diary without overwriting it. "
                "This writes only a subjective diary entry, never a fact, concept or research "
                "finding. The user can read it in Diary. Only claim success after this tool "
                "returns saved=true. Do not also emit a diary tag for the same entry. No "
                "background task is needed. Repeating the same dated write returns its receipt."
            ),
            parameters={"type": "object", "properties": {
                "content": {"type": "string", "minLength": 16, "maxLength": 2000},
                "revisits_entry_id": {"type": "integer", "minimum": 1},
            }, "required": ["content"], "additionalProperties": False},
        )

    def run(self, arguments: dict[str, Any]) -> str:
        try:
            if set(arguments) - {"content", "revisits_entry_id"}:
                raise ToolError("write_diary: unsupported arguments")
            session = self._session
            store = session.memory_store
            embedder = getattr(session, "_embedder", None)
            if (store is None or embedder is None
                    or not getattr(getattr(session._settings, "memory", None), "enabled", False)):
                raise ToolError("write_diary: memory writing is unavailable")
            sleep = getattr(session, "_sleep_store", None)
            if sleep is not None and sleep.get_state().status == "asleep":
                raise ToolError("write_diary: writing is unavailable while asleep")
            earlier = arguments.get("revisits_entry_id")
            if "revisits_entry_id" in arguments:
                earlier = ReadDiaryTool._integer(earlier, "revisits_entry_id", 1)
                row = store.get(earlier)
                if row is None or row.kind not in MemoryFacadeMixin.DIARY_KINDS:
                    raise ToolError("write_diary: the earlier diary entry was not found")
            row, created = append_chosen_entry(
                store, embedder.embed, arguments.get("content"), session_key=session.session_key,
                revisits_entry_id=earlier,
            )
            notify = getattr(session, "_notify_memory_added", None)
            if created and notify is not None:
                try:
                    notify(row)
                except Exception:
                    # A listener failure cannot undo a successful durable write.
                    import logging
                    logging.getLogger(__name__).debug("diary notification failed", exc_info=True)
            return json.dumps({"saved": True, "created": created,
                               "entry": ReadDiaryTool._entry(row.to_dict())}, ensure_ascii=False)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"write_diary failed: {exc}") from exc
