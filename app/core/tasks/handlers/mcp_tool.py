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
import hashlib
import json
from typing import TYPE_CHECKING, Any, Sequence

from app.core.tasks.handler_names import HANDLER_MCP_TOOL
from app.core.tasks.task_handler import (
    TaskCompleted,
    TaskEmitFn,
    TaskFailed,
    TaskState,
)
from app.core.tasks.approval import APPROVE_ALL, DENY, build_request, parse_decision
from app.core.tasks.workflow import mcp_operation_gate as gate

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.mcp.client.manager import ExternalMcpManager
    from app.plugins.sdk import ToolResultMiddleware


log = logging.getLogger("app.tasks.mcp_tool")

# Per-call content cap so a huge file/page can't blow the prompt when the
# result is rendered. Generous (this is a real answer), bounded for safety.
_CONTENT_CAP = 100000
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
        middlewares: "Sequence[ToolResultMiddleware] | None" = None,
        middlewares_provider: Any = None,
        perception: Any = None,
        resolve_approval: Any = None,
        mark_session_approved: Any = None,
        can_dispatch: Any = None,
        result_artifacts: Any = None,
    ) -> None:
        self._manager = manager
        self._middlewares_provider = middlewares_provider
        self._resolve_approval = resolve_approval
        self._mark_session_approved = mark_session_approved
        self._can_dispatch = can_dispatch
        self._result_artifacts = result_artifacts
        # Ordered tool-result middleware chain. Each entry may reshape a
        # claimed (server_id, tool_name) result (parse -> dedup -> group ->
        # rank -> diff, etc.) before it reaches the planner; the first that
        # claims AND returns a non-None transform wins. Empty chain = every
        # tool result flattened as-is. ``perception`` is a back-compat alias
        # that is prepended as one middleware.
        chain: list[Any] = []
        if perception is not None:
            chain.append(perception)
        if middlewares:
            chain.extend(middlewares)
        self._middlewares: list[Any] = chain

    # ── lifecycle ────────────────────────────────────────────────────

    def start(self, args: dict[str, Any], emit: TaskEmitFn) -> TaskState:
        if self._resolve_approval is not None:
            prepared, error = gate.prepare(self._manager, args)
            if error:
                emit(TaskFailed(error=error))
                return {"args": args, "phase": "rejected"}
            if (prepared["effect"] != "read"
                    and self._resolve_approval(prepared["approval_id"]) != "auto"):
                emit(build_request(gate.capability(args), gate.action_preview(args)))
                return {"args": args, "phase": "awaiting_approval", "contract": prepared}
        return self._perform(args, emit)

    def _perform(self, args: dict[str, Any], emit: TaskEmitFn,
                 approved_contract: dict | None = None) -> TaskState:
        server_id = str((args or {}).get("server_id", "") or "").strip()
        tool_name = str((args or {}).get("tool_name", "") or "").strip()
        tool_args = (args or {}).get("tool_args") or {}
        if not server_id or not tool_name:
            emit(TaskFailed(error="mcp_tool requires server_id and tool_name"))
            return {"args": args, "phase": "rejected"}
        if not isinstance(tool_args, dict):
            emit(TaskFailed(error="mcp_tool tool_args must be an object"))
            return {"args": args, "phase": "rejected"}

        prepared = None
        if self._resolve_approval is not None:
            prepared, error = gate.prepare(self._manager, args)
            if error or (approved_contract is not None
                         and prepared["signature"] != approved_contract["signature"]):
                emit(TaskFailed(error=error or "The approved operation contract changed."))
                return {"args": args, "phase": "rejected"}
        if self._can_dispatch is not None and not self._can_dispatch():
            emit(TaskFailed(error="This operation was cancelled before dispatch."))
            return {"args": args, "phase": "cancelled"}
        uncertain = prepared is not None and prepared["effect"] != "read"
        receipt = {"effect": prepared["effect"] if prepared else "unknown",
                   "server_id": server_id, "tool_name": tool_name,
                   "args_sha256": hashlib.sha256(
                       json.dumps(tool_args, sort_keys=True).encode()).hexdigest(),
                   "status": "acknowledged"}
        middleware_snapshot = tuple(self._middlewares_provider() if self._middlewares_provider
                                    else self._middlewares)
        try:
            options = ({"generation": prepared["generation"]}
                       if prepared and prepared.get("generation") else {})
            result = self._manager.call_tool(server_id, tool_name, tool_args, **options)
        except Exception as exc:  # noqa: BLE001 - surface as a clean failure
            emit(TaskFailed(error=f"MCP call failed: {exc}"[:200], uncertain_effect=uncertain))
            log.info(
                "mcp_tool failed: server=%s tool=%s err=%s",
                server_id, tool_name, exc,
            )
            return {"args": args, "phase": "rejected", "uncertain_effect": uncertain}

        is_error = bool(getattr(result, "isError", False))
        text, non_text = _flatten_content(result)
        artifact = self._save_artifact(text, partial=non_text > 0)
        if is_error:
            emit(TaskFailed(error=(text or "MCP tool reported an error")[:200],
                            uncertain_effect=uncertain))
            log.info(
                "mcp_tool tool-error: server=%s tool=%s", server_id, tool_name,
            )
            return {"args": args, "phase": "failed", "uncertain_effect": uncertain}

        # Tool-result middleware chain: the first middleware that claims this
        # (server_id, tool_name) and returns a non-None transform reshapes the
        # result (e.g. browser perception's compact ranked render); otherwise
        # we fall through to the raw flatten so every other tool (and an
        # unparseable / unclaimed result) is byte-identical to the no-middleware
        # path.
        perceived = self._run_middlewares(server_id, tool_name, text, tool_args,
                                         chain=middleware_snapshot)
        if perceived is not None:
            content = perceived.content[:_CONTENT_CAP]
            if len(perceived.content) > _CONTENT_CAP:
                content = content.rstrip() + "\n…(truncated)"
            summary = perceived.summary[:_SUMMARY_CAP]
            log.info(
                "mcp_tool perceived: server=%s tool=%s elements=%d",
                server_id, tool_name, getattr(perceived, "element_count", 0),
            )
            payload = {
                "server_id": server_id,
                "tool_name": tool_name,
                "content": content,
                "summary": summary,
                "receipt": receipt,
                "source_artifact": artifact,
                "truncated": len(perceived.content) > _CONTENT_CAP,
            }
            projection = getattr(perceived, "planner_context", None)
            if isinstance(projection, dict):
                # Optional SDK result projection. Meaning/formatting belong to
                # the plugin; core attaches provenance and enforces size only.
                projection = dict(projection)
                projected_text = str(projection.get("content", ""))
                if len(projected_text) > _CONTENT_CAP:
                    projection["artifact"] = self._save_artifact(projected_text, view="projection")
                projection["content"] = projected_text[:_CONTENT_CAP]
                if len(projected_text) > _CONTENT_CAP:
                    projection["partial"] = True
                payload["planner_context"] = projection
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
            "receipt": receipt,
            "source_artifact": artifact,
            "truncated": len(text) > _CONTENT_CAP or non_text > 0,
            "non_text_blocks": non_text,
        }
        log.info(
            "mcp_tool completed: server=%s tool=%s chars=%d non_text=%d",
            server_id, tool_name, len(content), non_text,
        )
        emit(TaskCompleted(result=payload))
        return {"args": args, "phase": "done"}

    def _save_artifact(self, text, *, view="source", partial=False):
        if self._result_artifacts is None:
            return None
        from app.core.tasks.workflow.goal_workflow_handler import _task_id_from_context

        task_id = _task_id_from_context()
        if task_id is None:
            return None
        try:
            return self._result_artifacts.save(task_id, text, view=view, partial=partial)
        except Exception:
            log.warning("result artifact unavailable: task=%s", task_id, exc_info=True)
            return {"available": False, "source_partial": True}

    def _run_middlewares(
        self,
        server_id: str,
        tool_name: str,
        text: str,
        tool_args: dict[str, Any],
        *, chain=None,
    ) -> Any | None:
        """First middleware that claims + returns non-None wins. Best-effort."""
        if chain is None:
            chain = (self._middlewares_provider() if self._middlewares_provider
                     else self._middlewares)
        for mw in chain:
            try:
                if not mw.claims(server_id, tool_name):
                    continue
                result = mw.transform(server_id, tool_name, text, tool_args)
            except Exception:  # noqa: BLE001 - a broken mw never breaks the tool
                log.debug(
                    "mcp_tool middleware raised: server=%s tool=%s",
                    server_id, tool_name, exc_info=True,
                )
                continue
            if result is not None:
                return result
        return None

    @staticmethod
    def _summary(tool_name: str, content: str, non_text: int) -> str:
        stripped = content.strip() if content else ""
        if stripped:
            lines = stripped.splitlines()
            # Don't hide multi-line results (e.g. a directory listing)
            # behind their first line — call out the line count so the
            # UI + any fallback observation shows there's more than one.
            if len(lines) > 1:
                base = f"{tool_name}: {len(lines)} lines — {lines[0]}"
            else:
                base = f"{tool_name}: {lines[0]}"
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
        if state.get("phase") != "awaiting_approval":
            emit(TaskFailed(error="This operation is not awaiting approval."))
            return state
        decision = parse_decision(answer)
        if decision == DENY:
            emit(TaskFailed(error="The operation was declined."))
            return state
        if decision == APPROVE_ALL and self._mark_session_approved is not None:
            self._mark_session_approved(state["contract"].get(
                "approval_id", state["contract"]["capability_id"]))
        return self._perform(state["args"], emit, state["contract"])

    def cancel(self, state: TaskState) -> None:
        # The MCP call has its own read timeout; a cancelled row just
        # suppresses any late emit at the orchestrator level.
        return None


__all__ = ["McpToolHandler"]
