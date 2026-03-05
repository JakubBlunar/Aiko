from __future__ import annotations

from collections.abc import Callable

from app.core.conversation_memory import ConversationMemoryStore, MemoryEntry
from app.core.tooling.types import ToolContext, ToolError, ToolResult, ToolSpec


class HistoryRuntime:
    def __init__(
        self,
        store: ConversationMemoryStore,
        *,
        default_limit: int = 50,
        max_limit: int = 400,
    ) -> None:
        self._store = store
        self._default_limit = max(1, int(default_limit))
        self._max_limit = max(self._default_limit, int(max_limit))

    def read_messages(self, *, limit: int | None = None, offset: int = 0) -> list[dict[str, str]]:
        entries = self._read_entries(limit=limit, offset=offset)
        return [{"role": item.role, "content": item.content} for item in entries]

    def read_entries(self, *, limit: int | None = None, offset: int = 0) -> list[MemoryEntry]:
        return self._read_entries(limit=limit, offset=offset)

    def _read_entries(self, *, limit: int | None, offset: int) -> list[MemoryEntry]:
        safe_offset = max(0, int(offset))
        resolved_limit = self._resolve_limit(limit)
        requested_total = safe_offset + resolved_limit
        recent = self._store.recent_entries(max_entries=requested_total)
        if not recent:
            return []
        if safe_offset == 0:
            return recent[-resolved_limit:]
        if len(recent) <= safe_offset:
            return []
        end = len(recent) - safe_offset
        start = max(0, end - resolved_limit)
        return recent[start:end]

    def _resolve_limit(self, limit: int | None) -> int:
        if limit is None:
            return self._default_limit
        try:
            parsed = int(limit)
        except Exception:
            parsed = self._default_limit
        return max(1, min(parsed, self._max_limit))


class HistoryReadMessagesTool:
    def __init__(self, runtime: HistoryRuntime) -> None:
        self._runtime = runtime
        self.spec = ToolSpec(
            name="history.read_messages",
            description=(
                "Read conversation history as role/content messages. "
                "Use offset to skip newest items for lookback."
            ),
            is_mutating=False,
            input_schema={"properties": {"limit": "int", "offset": "int"}},
            output_schema={"messages": "list", "count": "int"},
        )

    def run(
        self,
        context: ToolContext,
        args: dict,
        cancel_token: Callable[[], bool] | None = None,
    ) -> ToolResult:
        if cancel_token and cancel_token():
            return ToolResult(success=False, error=ToolError(code="cancelled", message="Tool call cancelled."))

        limit = args.get("limit")
        offset = args.get("offset", 0)
        messages = self._runtime.read_messages(limit=limit, offset=offset)
        return ToolResult(success=True, data={"messages": messages, "count": len(messages)})


class HistoryReadEntriesTool:
    def __init__(self, runtime: HistoryRuntime) -> None:
        self._runtime = runtime
        self.spec = ToolSpec(
            name="history.read_entries",
            description=(
                "Read conversation history including timestamps. "
                "Use offset to skip newest items for lookback."
            ),
            is_mutating=False,
            input_schema={"properties": {"limit": "int", "offset": "int"}},
            output_schema={"entries": "list", "count": "int"},
        )

    def run(
        self,
        context: ToolContext,
        args: dict,
        cancel_token: Callable[[], bool] | None = None,
    ) -> ToolResult:
        if cancel_token and cancel_token():
            return ToolResult(success=False, error=ToolError(code="cancelled", message="Tool call cancelled."))

        limit = args.get("limit")
        offset = args.get("offset", 0)
        entries = self._runtime.read_entries(limit=limit, offset=offset)
        payload = [
            {"role": entry.role, "content": entry.content, "timestamp": entry.timestamp}
            for entry in entries
        ]
        return ToolResult(success=True, data={"entries": payload, "count": len(payload)})
