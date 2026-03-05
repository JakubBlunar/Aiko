from __future__ import annotations

from collections.abc import Callable
import re

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

    def read_summary(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        max_chars: int = 420,
    ) -> tuple[str, int]:
        entries = self._read_entries(limit=limit, offset=offset)
        if not entries:
            return "", 0
        text = self._build_summary(entries=entries, max_chars=max_chars)
        return text, len(entries)

    @staticmethod
    def compact_summary(*, text: str, max_chars: int = 420) -> str:
        compacted = HistoryRuntime._normalize_text(text)
        if len(compacted) <= max(60, int(max_chars)):
            return compacted
        return f"{compacted[: max(60, int(max_chars)) - 3].rstrip()}..."

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

    @staticmethod
    def _normalize_text(value: str) -> str:
        cleaned = re.sub(r"\s+", " ", str(value or "")).strip()
        return cleaned

    @staticmethod
    def _truncate(value: str, *, max_chars: int) -> str:
        safe_max = max(60, int(max_chars))
        cleaned = HistoryRuntime._normalize_text(value)
        if len(cleaned) <= safe_max:
            return cleaned
        return f"{cleaned[: safe_max - 3].rstrip()}..."

    def _build_summary(self, *, entries: list[MemoryEntry], max_chars: int) -> str:
        user_goals: list[str] = []
        assistant_commitments: list[str] = []

        goal_markers = (
            "my goal is",
            "i need",
            "i want",
            "i am trying to",
            "i'm trying to",
            "please help",
            "can you help",
        )
        commitment_markers = ("i will", "i can", "i have", "i suggest", "next step", "let us")

        for entry in entries:
            text = self._normalize_text(entry.content)
            lowered = text.lower()
            if entry.role == "user" and any(marker in lowered for marker in goal_markers):
                user_goals.append(self._truncate(text, max_chars=140))
            if entry.role == "assistant" and any(marker in lowered for marker in commitment_markers):
                assistant_commitments.append(self._truncate(text, max_chars=140))

        sections: list[str] = []
        if user_goals:
            sections.append("User intent: " + " | ".join(user_goals[-2:]))
        if assistant_commitments:
            sections.append("Assistant commitments: " + " | ".join(assistant_commitments[-2:]))

        tail = entries[-3:]
        if tail:
            tail_lines = [f"{item.role}: {self._truncate(item.content, max_chars=120)}" for item in tail]
            sections.append("Recent exchange: " + " | ".join(tail_lines))

        if not sections:
            sections.append(
                "Recent exchange: "
                + " | ".join(
                    [f"{item.role}: {self._truncate(item.content, max_chars=120)}" for item in entries[-3:]]
                )
            )

        return self._truncate("\n".join(sections), max_chars=max_chars)


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


class HistoryReadSummaryTool:
    def __init__(self, runtime: HistoryRuntime) -> None:
        self._runtime = runtime
        self.spec = ToolSpec(
            name="history.read_summary",
            description=(
                "Read a compact deterministic summary of history. "
                "Use offset to summarize older segments."
            ),
            is_mutating=False,
            input_schema={"properties": {"limit": "int", "offset": "int", "max_chars": "int"}},
            output_schema={"summary": "str", "entry_count": "int"},
        )

    def run(
        self,
        context: ToolContext,
        args: dict,
        cancel_token: Callable[[], bool] | None = None,
    ) -> ToolResult:
        if cancel_token and cancel_token():
            return ToolResult(success=False, error=ToolError(code="cancelled", message="Tool call cancelled."))

        summary, entry_count = self._runtime.read_summary(
            limit=args.get("limit"),
            offset=args.get("offset", 0),
            max_chars=int(args.get("max_chars", 420) or 420),
        )
        return ToolResult(success=True, data={"summary": summary, "entry_count": entry_count})


class HistoryCompactSummaryTool:
    def __init__(self, runtime: HistoryRuntime) -> None:
        self._runtime = runtime
        self.spec = ToolSpec(
            name="history.compact_summary",
            description="Compact a summary string to a bounded length.",
            is_mutating=False,
            input_schema={"required": ["text"], "properties": {"text": "str", "max_chars": "int"}},
            output_schema={"summary": "str"},
        )

    def run(
        self,
        context: ToolContext,
        args: dict,
        cancel_token: Callable[[], bool] | None = None,
    ) -> ToolResult:
        if cancel_token and cancel_token():
            return ToolResult(success=False, error=ToolError(code="cancelled", message="Tool call cancelled."))

        compacted = self._runtime.compact_summary(
            text=str(args.get("text", "")),
            max_chars=int(args.get("max_chars", 420) or 420),
        )
        return ToolResult(success=True, data={"summary": compacted})
