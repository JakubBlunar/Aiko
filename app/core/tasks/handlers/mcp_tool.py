"""Generic external-MCP tool handler.

One handler proxies *every* external MCP tool call: the specific
``server_id`` / ``tool_name`` ride in the task args, and the call is
dispatched through :class:`app.mcp.client.manager.ExternalMcpManager`.
The handler runs synchronously on a worker thread (like
:class:`WebSearchHandler`) -- ``manager.call_tool`` blocks until the
manager loop returns the result.

MCP results are content blocks (text / image / embedded resource). Phase 1
flattens text blocks into the result ``content`` (truncated) and exposes a
one-line ``summary`` the cue/escalation path lifts. Non-text content is
noted in the summary and left for a later phase.

Reachable only as a ``WorkflowSkill`` child of a goal workflow (MCP tools
are surfaced to the background-worker lane only), so ``on_input`` /
``resume`` are defensive terminals like the other workflow handlers.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.core.tasks.handler_names import HANDLER_MCP_TOOL
from app.core.tasks.task_handler import (
    TaskCompleted,
    TaskEmitFn,
    TaskFailed,
    TaskState,
)

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.browser.perception import BrowserPerception
    from app.mcp.client.manager import ExternalMcpManager


log = logging.getLogger("app.tasks.mcp_tool")

# Per-call content cap so a huge file/page can't blow the prompt when the
# result is rendered. Generous (this is a real answer), bounded for safety.
_CONTENT_CAP = 6000
_SUMMARY_CAP = 200


def _flatten_content(result: Any) -> tuple[str, int]:
    """Flatten a ``CallToolResult`` into (text, non_text_block_count).

    Pulls ``.text`` off every text block; counts other block kinds
    (image / audio / resource) so the summary can mention them.
    """
    text_parts: list[str] = []
    non_text = 0
    content = getattr(result, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            text_parts.append(text)
        else:
            non_text += 1
    return "\n".join(p for p in text_parts if p).strip(), non_text


class McpToolHandler:
    """Proxy a single external MCP tool call as a background task."""

    name: str = HANDLER_MCP_TOOL

    def __init__(
        self,
        *,
        manager: "ExternalMcpManager",
        perception: "BrowserPerception | None" = None,
    ) -> None:
        self._manager = manager
        # Optional server-agnostic middleware that reshapes accessibility
        # snapshots (parse -> dedup -> group -> rank -> diff) before they
        # reach the planner. None = every tool result flattened as-is.
        self._perception = perception

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self, args: dict[str, Any], emit: TaskEmitFn) -> TaskState:
        server_id = str((args or {}).get("server_id", "") or "").strip()
        tool_name = str((args or {}).get("tool_name", "") or "").strip()
        tool_args = (args or {}).get("tool_args") or {}
        if not server_id or not tool_name:
            emit(TaskFailed(error="mcp_tool requires server_id and tool_name"))
            return {"args": args, "phase": "rejected"}
        if not isinstance(tool_args, dict):
            emit(TaskFailed(error="mcp_tool tool_args must be an object"))
            return {"args": args, "phase": "rejected"}

        try:
            result = self._manager.call_tool(server_id, tool_name, tool_args)
        except Exception as exc:  # noqa: BLE001 - surface as a clean failure
            emit(TaskFailed(error=f"MCP call failed: {exc}"[:200]))
            log.info(
                "mcp_tool failed: server=%s tool=%s err=%s",
                server_id, tool_name, exc,
            )
            return {"args": args, "phase": "rejected"}

        is_error = bool(getattr(result, "isError", False))
        text, non_text = _flatten_content(result)
        if is_error:
            emit(TaskFailed(error=(text or "MCP tool reported an error")[:200]))
            log.info(
                "mcp_tool tool-error: server=%s tool=%s", server_id, tool_name,
            )
            return {"args": args, "phase": "failed"}

        # Browser perception hook: when a perception layer claims this
        # (server_id, tool_name) and successfully parses the snapshot, use
        # its compact ranked render; otherwise fall through to the raw
        # flatten so every other tool (and an unparseable snapshot) is
        # byte-identical to the no-perception path.
        perceived = None
        if self._perception is not None and self._perception.claims(
            server_id, tool_name
        ):
            perceived = self._perception.transform(
                server_id, tool_name, text, tool_args
            )
        if perceived is not None:
            content = perceived.content[:_CONTENT_CAP]
            if len(perceived.content) > _CONTENT_CAP:
                content = content.rstrip() + "\n…(truncated)"
            summary = perceived.summary[:_SUMMARY_CAP]
            log.info(
                "mcp_tool perceived: server=%s tool=%s elements=%d",
                server_id, tool_name, perceived.element_count,
            )
            payload = {
                "server_id": server_id,
                "tool_name": tool_name,
                "content": content,
                "summary": summary,
            }
            emit(TaskCompleted(result=payload))
            return {"args": args, "phase": "done"}

        content = text[:_CONTENT_CAP]
        if text and len(text) > _CONTENT_CAP:
            content = content.rstrip() + "\n…(truncated)"
        summary = self._summary(tool_name, content, non_text)
        payload = {
            "server_id": server_id,
            "tool_name": tool_name,
            "content": content,
            "summary": summary,
        }
        log.info(
            "mcp_tool completed: server=%s tool=%s chars=%d non_text=%d",
            server_id, tool_name, len(content), non_text,
        )
        emit(TaskCompleted(result=payload))
        return {"args": args, "phase": "done"}

    @staticmethod
    def _summary(tool_name: str, content: str, non_text: int) -> str:
        if content:
            first = content.strip().splitlines()[0] if content.strip() else ""
            base = f"{tool_name}: {first}"
        elif non_text:
            base = f"{tool_name}: returned {non_text} non-text item(s)"
        else:
            base = f"{tool_name}: done (no content)"
        return base[:_SUMMARY_CAP]

    def resume(self, state: TaskState, emit: TaskEmitFn) -> TaskState:
        emit(
            TaskFailed(
                error="mcp_tool does not support resume; re-run the tool"
            )
        )
        return state

    def on_input(
        self, state: TaskState, answer: str, emit: TaskEmitFn
    ) -> TaskState:
        emit(TaskFailed(error="mcp_tool does not accept input"))
        return state

    def cancel(self, state: TaskState) -> None:
        # The MCP call has its own read timeout; a cancelled row just
        # suppresses any late emit at the orchestrator level.
        return None


__all__ = ["McpToolHandler"]
