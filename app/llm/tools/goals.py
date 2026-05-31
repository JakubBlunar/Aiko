"""Goal tools (K1 personality backlog).

Three small tools that let Aiko *act* on her own long-term goals
inside a turn:

- ``add_goal`` to declare a new long-term goal mid-turn (an alternative
  to the ``[[goal:summary]]`` self-tag for cases where the LLM prefers
  the tool path).
- ``update_goal_progress`` to record a fresh reflection note on an
  existing goal during the conversation (e.g. when {user_name} asks
  "how's the piano practice going?" and she wants to mark that she
  reflected on it).
- ``archive_goal`` to retire a goal she no longer cares about.

The tools are gated on ``settings.tools.goals`` (default True) and
registered by :func:`SessionController.rebuild_tool_registry` whenever
``self._goal_store`` is wired. They're optional — the
:class:`GoalWorker` covers the autonomous path; these tools are for
the in-turn agent surface.

All three return a small JSON blob so the LLM can ground its spoken
reply on what actually changed. Errors propagate via
:class:`ToolError` and become the tool message body verbatim.
"""
from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING

from app.llm.tools.base import ToolError, ToolSchema


if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController


log = logging.getLogger("app.tools.goals")


def _format_goal(mem: Any) -> dict[str, Any]:
    """Pull the JSON-friendly fields off a goal :class:`Memory` row."""
    meta = getattr(mem, "metadata", None) or {}
    if not isinstance(meta, dict):
        meta = {}
    return {
        "id": int(getattr(mem, "id", 0)),
        "summary": str(meta.get("summary") or getattr(mem, "content", "") or ""),
        "last_reflected_at": meta.get("last_reflected_at"),
        "reflection_count": int(meta.get("reflection_count", 0) or 0),
        "last_progress_note": meta.get("last_progress_note"),
        "archived_at": meta.get("archived_at"),
        "source": meta.get("source"),
        "pinned": bool(getattr(mem, "pinned", False)),
        "tier": getattr(mem, "tier", "long_term"),
    }


# ── add_goal ────────────────────────────────────────────────────────────


class AddGoalTool:
    """Persist a new long-term goal for Aiko."""

    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="add_goal",
            description=(
                "Declare one of Aiko's OWN long-term personal goals -- the "
                "kind of thing she wants to grow into / get better at / "
                "explore over months. NOT a TODO for the user (use the "
                "agenda tag for those) and NOT a one-shot self-fact. Use "
                "this when the conversation surfaces something she "
                "genuinely wants to keep working on across many "
                "sessions. The summary should be a single short sentence "
                "in her own voice. Returns the persisted goal row. The "
                "store dedupes near-identical entries so calling this "
                "twice with the same intent is safe."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": (
                            "The goal in 4-200 characters, written in Aiko's "
                            "own voice (\"get fluent at sketching small "
                            "everyday objects\", \"practice listening for "
                            "sevenths and ninths\")."
                        ),
                    },
                },
                "required": ["summary"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        store = getattr(self._session, "_goal_store", None)
        if store is None:
            raise ToolError("add_goal: goal store is unavailable")
        summary = (arguments.get("summary") or "").strip()
        if not summary:
            raise ToolError("add_goal: 'summary' is required")
        try:
            mem = store.add_goal(
                summary=summary,
                source="tool",
                source_session=self._session.session_key,
            )
        except Exception as exc:
            raise ToolError(f"add_goal failed: {exc}") from exc
        if mem is None:
            # The store returned None: either dedupe collision or
            # validation failure. Surface a clean message so the LLM
            # knows the call was a no-op without crashing the turn.
            return json.dumps(
                {
                    "added": False,
                    "reason": "duplicate_or_invalid",
                    "summary": summary,
                },
                ensure_ascii=False,
            )
        notify = getattr(self._session, "_notify_memory_added", None)
        if notify is not None:
            try:
                notify(mem.to_dict())
            except Exception:
                log.debug("add_goal notify_memory_added raised", exc_info=True)
        return json.dumps(
            {"added": True, "goal": _format_goal(mem)},
            ensure_ascii=False,
        )


# ── update_goal_progress ─────────────────────────────────────────────────


class UpdateGoalProgressTool:
    """Append a reflection note to one of Aiko's existing goals."""

    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="update_goal_progress",
            description=(
                "Record a fresh 1-3 sentence reflection on one of Aiko's "
                "existing long-term goals. Call this when the current "
                "conversation actually surfaces a goal -- the user "
                "asked how the practice is going, or Aiko realised "
                "something new about a goal she's been carrying. Returns "
                "the updated goal row (with bumped reflection_count and "
                "fresh last_progress_note). To find the right goal_id, "
                "call ``list_goals`` first or pick it from the prompt's "
                "long-term goals block."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "goal_id": {
                        "type": "integer",
                        "description": "Memory id of the goal to update.",
                    },
                    "note": {
                        "type": "string",
                        "description": (
                            "A short reflection on the goal in Aiko's "
                            "voice (4-280 characters). What she noticed, "
                            "how it's been going, or one small next step."
                        ),
                    },
                },
                "required": ["goal_id", "note"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        store = getattr(self._session, "_goal_store", None)
        if store is None:
            raise ToolError("update_goal_progress: goal store is unavailable")
        try:
            goal_id = int(arguments.get("goal_id"))
        except (TypeError, ValueError):
            raise ToolError("update_goal_progress: 'goal_id' must be an integer")
        note = (arguments.get("note") or "").strip()
        if not note:
            raise ToolError("update_goal_progress: 'note' is required")
        try:
            progress = store.add_progress(
                goal_id=goal_id,
                note=note,
                source="tool",
                source_session=self._session.session_key,
            )
        except Exception as exc:
            raise ToolError(f"update_goal_progress failed: {exc}") from exc
        if progress is None:
            return json.dumps(
                {
                    "updated": False,
                    "reason": "unknown_goal_or_invalid_note",
                    "goal_id": goal_id,
                },
                ensure_ascii=False,
            )
        notify_added = getattr(self._session, "_notify_memory_added", None)
        if notify_added is not None:
            try:
                notify_added(progress.to_dict())
            except Exception:
                log.debug(
                    "update_goal_progress notify_added raised",
                    exc_info=True,
                )
        # The goal row's mirror metadata moved -- broadcast so the
        # Memory tab refreshes the "last reflection" line live.
        notify_updated = getattr(self._session, "_notify_memory_updated", None)
        refreshed_goal = None
        try:
            mem_store = getattr(self._session, "_memory_store", None)
            if mem_store is not None:
                refreshed_goal = mem_store.get(goal_id)
        except Exception:
            refreshed_goal = None
        if refreshed_goal is not None and notify_updated is not None:
            try:
                notify_updated(refreshed_goal.to_dict())
            except Exception:
                log.debug(
                    "update_goal_progress notify_updated raised",
                    exc_info=True,
                )
        payload: dict[str, Any] = {
            "updated": True,
            "progress_id": int(progress.id),
            "note": (progress.metadata or {}).get("note", note),
        }
        if refreshed_goal is not None:
            payload["goal"] = _format_goal(refreshed_goal)
        return json.dumps(payload, ensure_ascii=False)


# ── archive_goal ────────────────────────────────────────────────────────


class ArchiveGoalTool:
    """Retire one of Aiko's goals so it stops surfacing in the prompt."""

    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="archive_goal",
            description=(
                "Retire one of Aiko's long-term goals (the goal stays in "
                "memory for audit, but it stops appearing in her "
                "prompt's active goals block). Use this when she "
                "realises a goal no longer feels like hers -- not when "
                "she completes a single milestone (use "
                "``update_goal_progress`` instead). The history of "
                "reflections on the goal is preserved."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "goal_id": {
                        "type": "integer",
                        "description": "Memory id of the goal to archive.",
                    },
                },
                "required": ["goal_id"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        store = getattr(self._session, "_goal_store", None)
        if store is None:
            raise ToolError("archive_goal: goal store is unavailable")
        try:
            goal_id = int(arguments.get("goal_id"))
        except (TypeError, ValueError):
            raise ToolError("archive_goal: 'goal_id' must be an integer")
        try:
            ok = store.archive_goal(goal_id)
        except Exception as exc:
            raise ToolError(f"archive_goal failed: {exc}") from exc
        if not ok:
            return json.dumps(
                {
                    "archived": False,
                    "reason": "unknown_goal",
                    "goal_id": goal_id,
                },
                ensure_ascii=False,
            )
        notify_updated = getattr(self._session, "_notify_memory_updated", None)
        refreshed = None
        try:
            mem_store = getattr(self._session, "_memory_store", None)
            if mem_store is not None:
                refreshed = mem_store.get(goal_id)
        except Exception:
            refreshed = None
        if refreshed is not None and notify_updated is not None:
            try:
                notify_updated(refreshed.to_dict())
            except Exception:
                log.debug(
                    "archive_goal notify_updated raised", exc_info=True,
                )
        payload: dict[str, Any] = {"archived": True, "goal_id": goal_id}
        if refreshed is not None:
            payload["goal"] = _format_goal(refreshed)
        return json.dumps(payload, ensure_ascii=False)


# ── list_goals ──────────────────────────────────────────────────────────


class ListGoalsTool:
    """Return Aiko's current active goals so the LLM can pick a goal_id."""

    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="list_goals",
            description=(
                "List Aiko's currently active long-term goals with their "
                "memory ids and recent reflection notes. Useful when the "
                "LLM needs a ``goal_id`` for ``update_goal_progress`` or "
                "``archive_goal`` but only sees the goals block in the "
                "prompt (which omits ids). Read-only; skip on ordinary "
                "turns -- the goals block in the prompt already covers "
                "casual surfacing."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
        )

    def run(self, arguments: dict[str, Any]) -> str:
        store = getattr(self._session, "_goal_store", None)
        if store is None:
            raise ToolError("list_goals: goal store is unavailable")
        try:
            active = store.list_active()
        except Exception as exc:
            raise ToolError(f"list_goals failed: {exc}") from exc
        return json.dumps(
            {"goals": [_format_goal(mem) for mem in active]},
            ensure_ascii=False,
        )


# ── factory ─────────────────────────────────────────────────────────────


def build_goal_tools(session: "SessionController") -> list[Any]:
    """Construct the goal tool set bound to ``session``.

    Returned in registration order: ``list_goals`` first so the LLM
    discovers it before reaching for one of the mutators.
    """
    return [
        ListGoalsTool(session),
        AddGoalTool(session),
        UpdateGoalProgressTool(session),
        ArchiveGoalTool(session),
    ]
