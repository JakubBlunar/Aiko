"""Aiko-callable agent tools for the brain-orchestration task layer.

Chunk 10 of the brain-orchestration refactor: Aiko can now reach into
the :class:`TaskOrchestrator` from her own turns. The chunk-9 work
shipped the orchestrator + the ``file_search`` reference handler + a
sandbox + four MCP debug tools; this module is what turns the
debug-only surface into a real agent capability.

Tool shape — all the *start_* / *cancel_* / *answer_* tools return
immediately with a small JSON payload so the streaming LLM reply can
reference the spawned task in its very next sentence ("I'm searching
for that — I'll let you know what comes back"). The actual filesystem
walk runs on the orchestrator's worker pool; results land on the
brain queue as a :class:`TaskResultEvent` and surface either via the
next turn's T6 cue block or via a proactive escalation after the
silence window.

``list_file_roots`` is the odd one out: it's **synchronous**, returns
the validated root list + a shallow top-level preview inline, and
spawns no task. Reason — answering "what can you see?" should be a
single round-trip, not "I'll go look (turn 1) → here's the result
(turn 2)". The work is microseconds: config inspection plus one
``os.listdir`` per active root, capped at a handful of entries.

Tools:

* ``list_file_roots`` — synchronous root catalogue + shallow peek.
  The discovery entry point.
* ``start_file_search`` — spawns a filename substring search.
* ``start_file_read`` — spawns a file content read.
* ``cancel_file_task`` — cancels by id; lets Aiko react if Jacob
  changes his mind ("never mind, stop searching") without making
  the user fish for the task id.
* ``answer_file_task`` — resolves an awaiting-input multi-root
  disambiguation.

Persona contract — the persona file (``aiko_companion.txt``) carries
the rule "spawn a task, mention you're working on it, don't pretend
to already have the result". The tool descriptions below echo the
same nudge so an LLM that hasn't internalised the persona block
still picks up the right behaviour from the tool catalogue.
"""
from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any

from app.llm.tools.base import Tool, ToolError, ToolSchema


if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.session.session_controller import SessionController


log = logging.getLogger("app.tools.file_tasks")


def _user_id(session: "SessionController") -> str:
    """Resolve the active user id, falling back to ``"default"``.

    All task rows are stamped with a ``user_id`` so the per-user cap
    in :class:`TaskOrchestrator` can enforce back-pressure. We
    deliberately stash the value off the session attribute rather
    than the (potentially synthetic) ``session_key`` so the cap
    holds across the noremember session variant too.
    """
    return str(getattr(session, "_user_id", "default") or "default")


def _orchestrator(session: "SessionController") -> Any | None:
    """Return the live orchestrator or ``None`` when the subsystem
    is off / not yet wired. Tools branch on this rather than raising
    so a partially-built session can still report a friendly error
    string back to the LLM.
    """
    return getattr(session, "_task_orchestrator", None)


# ── list_file_roots ──────────────────────────────────────────────────────

# Top-level preview cap per root. Small enough to keep the tool
# response cheap (a few KB of JSON), large enough to give Aiko a
# realistic sense of what's there. If the directory has more entries
# than this, the response carries ``"truncated": True`` so the LLM can
# tell the user "there's more, want me to search?".
_LIST_FILE_ROOTS_PREVIEW_CAP = 20


def _shallow_root_preview(
    abs_path: str, cap: int = _LIST_FILE_ROOTS_PREVIEW_CAP,
) -> tuple[list[dict[str, Any]], bool]:
    """Return (entries, truncated) for the top level of ``abs_path``.

    Each entry is ``{"name": str, "kind": "dir" | "file"}``. Hidden
    files (leading dot on POSIX, system-attribute on Windows isn't
    checked) and entries we can't stat are skipped silently. Sorted
    alphabetically, case-insensitive, with directories first so the
    LLM sees the layout at a glance.

    Returns ``([], False)`` for any path we can't open. Logs at DEBUG
    so a misconfigured root doesn't spam INFO.
    """
    try:
        names = os.listdir(abs_path)
    except (OSError, PermissionError) as exc:
        log.debug(
            "list_file_roots preview: listdir failed: path=%r exc=%s",
            abs_path, exc,
        )
        return [], False
    entries: list[dict[str, Any]] = []
    for name in names:
        if name.startswith("."):
            continue
        full = os.path.join(abs_path, name)
        try:
            is_dir = os.path.isdir(full)
        except OSError:
            continue
        entries.append({"name": name, "kind": "dir" if is_dir else "file"})
    # Dirs first, then files; within each group case-insensitive sort.
    entries.sort(
        key=lambda e: (0 if e["kind"] == "dir" else 1, e["name"].lower()),
    )
    truncated = len(entries) > cap
    return entries[:cap], truncated


class ListFileRootsTool:
    """List configured file roots + a shallow preview of each.

    Synchronous. Returns immediately with the full root catalogue.
    Use this when the user asks "what files can you see?" / "what
    do you have access to?" / "look around the disk" — it's the
    entry point for the filesystem capability.

    Output JSON shape::

        {
          "roots": [
            {
              "label": "Documents",
              "path": "F:/MyDocs",
              "active": true,
              "read_only": true,
              "warnings": ["sensitive_directory"],
              "reason": "",
              "preview": [
                {"name": "Notes", "kind": "dir"},
                {"name": "q4.md", "kind": "file"}
              ],
              "preview_truncated": false
            }
          ],
          "total_roots": 2,
          "active_roots": 2
        }

    Inactive roots (the path doesn't exist, isn't a directory, etc.)
    are returned with ``active: false`` + a populated ``reason`` so
    Aiko can tell the user "Documents is configured but missing on
    disk" instead of just dropping it silently.

    After this returns, the natural next step is ``start_file_search``
    to look for something specific, or ``start_file_read`` if Aiko
    already knows which file to open.
    """

    name = "list_file_roots"

    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="list_file_roots",
            description=(
                "List the user's configured file roots that you have "
                "read access to, with a shallow preview of each "
                "root's top-level contents. SYNCHRONOUS — returns "
                "the result inline; no task is spawned. Use this "
                "first when the user asks what files / folders you "
                "can see, or before you guess a path with "
                "start_file_search / start_file_read. Each preview "
                "is capped at 20 entries; if a root has more, "
                "preview_truncated will be true and you should "
                "follow up with start_file_search for that root. "
                "Returns JSON: {roots: [{label, path, active, "
                "read_only, warnings, reason, preview, "
                "preview_truncated}], total_roots, active_roots}."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        # Import lazily so this module stays cheap to import in
        # environments where the task subsystem isn't built (tests).
        from app.core.tasks.sandbox import FileTaskRoot, validate_roots

        settings = getattr(self._session, "_settings", None)
        agent_cfg = getattr(settings, "agent", None) if settings else None
        raw_roots = getattr(agent_cfg, "task_file_allowed_roots", ()) or ()
        roots: list[FileTaskRoot] = []
        for entry in raw_roots:
            if not isinstance(entry, dict):
                continue
            label = str(entry.get("label", "")).strip()
            path = str(entry.get("path", "")).strip()
            if not label or not path:
                continue
            roots.append(
                FileTaskRoot(
                    label=label,
                    path=path,
                    read_only=bool(entry.get("read_only", True)),
                )
            )
        try:
            verdicts = validate_roots(roots)
        except Exception as exc:
            log.exception("list_file_roots: validate_roots failed")
            raise ToolError(
                f"list_file_roots: root validation failed: {exc}",
            ) from exc
        out_roots: list[dict[str, Any]] = []
        active_count = 0
        for vr in verdicts:
            entry: dict[str, Any] = {
                "label": vr.root.label,
                "path": vr.abs_path,
                "active": bool(vr.active),
                "read_only": bool(vr.root.read_only),
                "warnings": list(vr.warnings),
                "reason": vr.reason or "",
                "preview": [],
                "preview_truncated": False,
            }
            if vr.active:
                active_count += 1
                preview, truncated = _shallow_root_preview(vr.abs_path)
                entry["preview"] = preview
                entry["preview_truncated"] = truncated
            out_roots.append(entry)
        payload = {
            "roots": out_roots,
            "total_roots": len(out_roots),
            "active_roots": active_count,
        }
        log.info(
            "list_file_roots: total=%d active=%d",
            len(out_roots), active_count,
        )
        return json.dumps(payload, ensure_ascii=False)


# ── start_file_search ────────────────────────────────────────────────────


class StartFileSearchTool:
    """Spawn an asynchronous filename substring search.

    Args:

    * ``query`` (str, required) — substring to search for in file
      basenames.
    * ``root_label`` (str, optional) — scope to a single configured
      root (``Documents`` / ``Notes`` / etc.). Empty searches all
      active roots.
    * ``max_results`` (int, optional, 1–500, default 50) — cap on
      returned matches.
    * ``case_sensitive`` (bool, optional, default False) — case
      sensitivity on the substring match.

    Returns immediately with ``{"task_id": N, "handler": "file_search",
    "note": "..."}``. The actual results land in a later turn via
    the T6 task-cue block — Aiko should mention she's started the
    search but **not** invent a result.
    """

    name = "start_file_search"

    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="start_file_search",
            description=(
                "Search the user's configured file roots for files whose "
                "filename contains a substring. Runs ASYNCHRONOUSLY in the "
                "background — the call returns a task id immediately and "
                "the actual matches arrive in a later turn as a 'task cue' "
                "in your prompt. Tell the user you're searching, then "
                "MOVE ON; do not pretend you already have the result. "
                "Returns JSON: {task_id, handler, note}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Substring to match against filenames "
                            "(basename only, not the full path). "
                            "Required and non-empty."
                        ),
                    },
                    "root_label": {
                        "type": "string",
                        "description": (
                            "Optional. Scope to a single configured "
                            "root by label (e.g. 'Documents'). Leave "
                            "empty to search every active root."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": (
                            "Optional cap on returned matches. "
                            "1-500, default 50."
                        ),
                        "minimum": 1,
                        "maximum": 500,
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": (
                            "Optional. Default false (case-insensitive)."
                        ),
                    },
                },
                "required": ["query"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        query = (arguments.get("query") or "").strip()
        if not query:
            raise ToolError("start_file_search: 'query' is required")
        root_label = (arguments.get("root_label") or "").strip()
        try:
            max_results = int(arguments.get("max_results", 50))
        except (TypeError, ValueError):
            max_results = 50
        max_results = max(1, min(500, max_results))
        case_sensitive = bool(arguments.get("case_sensitive", False))
        orch = _orchestrator(self._session)
        if orch is None:
            raise ToolError(
                "start_file_search: filesystem task subsystem is disabled "
                "(agent.tasks_enabled=False)"
            )
        # Verify the handler is registered. ``handler_for`` returns
        # ``None`` if a future config flip de-registered it; surfacing
        # the right error here helps debugging vs. waiting for an
        # opaque ``unknown_handler`` log later.
        if orch.handler_for("file_search") is None:
            raise ToolError(
                "start_file_search: handler is not registered "
                "(no file roots configured?)"
            )
        title = f"file search: {query[:60]}"
        if root_label:
            title += f" (in {root_label})"
        try:
            task_id = orch.start_task(
                user_id=_user_id(self._session),
                handler_name="file_search",
                args={
                    "query": query,
                    "root_label": root_label,
                    "max_results": max_results,
                    "case_sensitive": case_sensitive,
                },
                title=title,
                initiated_by="aiko",
            )
        except Exception as exc:
            log.exception(
                "start_file_search: orchestrator.start_task failed: query=%r",
                query,
            )
            raise ToolError(f"start_file_search failed: {exc}") from exc
        if task_id is None:
            # Per-user cap or unknown handler; the orchestrator
            # already logged the WARNING with the structured reason.
            raise ToolError(
                "start_file_search: spawn rejected (per-user cap or "
                "missing handler)"
            )
        log.info(
            "start_file_search spawned: task_id=%d query=%r root=%r "
            "max_results=%d case_sensitive=%s",
            task_id,
            query,
            root_label,
            max_results,
            case_sensitive,
        )
        return json.dumps(
            {
                "task_id": task_id,
                "handler": "file_search",
                "note": (
                    "Search started. Results will arrive in a later turn "
                    "as a task cue. Tell the user you're searching; do not "
                    "invent results."
                ),
            },
            ensure_ascii=False,
        )


# ── cancel_file_task ─────────────────────────────────────────────────────


class CancelFileTaskTool:
    """Cancel an in-flight task by id.

    Args:

    * ``task_id`` (int, required) — the id returned by
      :class:`StartFileSearchTool`.

    Returns ``{"cancelled": bool, "task_id": N}``. Cancellation is
    best-effort — the orchestrator marks the row ``cancelled`` and
    fires the handler's ``cancel`` callback; a running synchronous
    walker may still complete one final iteration before noticing.
    """

    name = "cancel_file_task"

    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="cancel_file_task",
            description=(
                "Cancel a running file-search task by id (the id returned "
                "by start_file_search). Use when the user clearly says "
                "they no longer want the search — 'never mind', 'forget "
                "it', 'cancel that'. Returns JSON: {cancelled, task_id}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": (
                            "The task id to cancel. Required."
                        ),
                    },
                },
                "required": ["task_id"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        try:
            task_id = int(arguments.get("task_id", 0))
        except (TypeError, ValueError):
            raise ToolError(
                "cancel_file_task: 'task_id' must be an integer"
            )
        if task_id <= 0:
            raise ToolError(
                "cancel_file_task: 'task_id' must be a positive integer"
            )
        orch = _orchestrator(self._session)
        if orch is None:
            raise ToolError(
                "cancel_file_task: filesystem task subsystem is disabled "
                "(agent.tasks_enabled=False)"
            )
        try:
            ok = orch.cancel(task_id)
        except Exception as exc:
            log.exception(
                "cancel_file_task: orchestrator.cancel failed: task_id=%d",
                task_id,
            )
            raise ToolError(f"cancel_file_task failed: {exc}") from exc
        log.info(
            "cancel_file_task: task_id=%d cancelled=%s", task_id, ok
        )
        return json.dumps(
            {"cancelled": bool(ok), "task_id": int(task_id)},
            ensure_ascii=False,
        )


# ── start_file_read ──────────────────────────────────────────────────────


class StartFileReadTool:
    """Spawn an asynchronous file content read.

    Args:

    * ``path`` (str, required) — label-prefixed (``"Documents:notes/q4.md"``)
      or bare (``"notes/q4.md"``). Bare paths that match multiple
      roots will land in ``awaiting_input`` and Aiko will surface the
      question next turn.
    * ``max_bytes`` (int, optional) — soft cap on bytes to read.
      Clamped server-side to the configured
      ``agent.task_file_read_max_bytes`` (default 256 KiB).

    Returns immediately with ``{"task_id": N, "handler": "file_read",
    "note": "..."}``. The actual content lands in a later turn via
    the T6 task-cue block — Aiko should mention she's reading the
    file but **not** invent the contents. When the path is
    ambiguous, the next turn's prompt will surface the candidate
    list as an awaiting-input cue and Aiko should ask the user
    which root they meant.
    """

    name = "start_file_read"

    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="start_file_read",
            description=(
                "Read a text file from one of the user's configured "
                "file roots. Runs ASYNCHRONOUSLY in the background — "
                "the call returns a task id immediately and the file "
                "content arrives in a later turn as a 'task cue' in "
                "your prompt. Tell the user you're opening it, then "
                "MOVE ON; do not pretend to already know the content. "
                "If the path is ambiguous (matches multiple roots), "
                "you'll get an awaiting-input cue NEXT turn — ask the "
                "user which root they meant and call answer_file_task "
                "with their reply. Returns JSON: {task_id, handler, note}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "The path to read. Either label-prefixed "
                            "('Documents:notes/q4.md') or bare "
                            "('notes/q4.md'). Required."
                        ),
                    },
                    "max_bytes": {
                        "type": "integer",
                        "description": (
                            "Optional soft cap on bytes to read. "
                            "Server clamps to the configured ceiling."
                        ),
                        "minimum": 1024,
                    },
                },
                "required": ["path"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        path = (arguments.get("path") or "").strip()
        if not path:
            raise ToolError("start_file_read: 'path' is required")
        try:
            max_bytes = int(arguments.get("max_bytes", 0) or 0)
        except (TypeError, ValueError):
            max_bytes = 0
        orch = _orchestrator(self._session)
        if orch is None:
            raise ToolError(
                "start_file_read: filesystem task subsystem is disabled "
                "(agent.tasks_enabled=False)"
            )
        if orch.handler_for("file_read") is None:
            raise ToolError(
                "start_file_read: handler is not registered "
                "(no file roots configured?)"
            )
        title = f"file read: {path[:80]}"
        args: dict[str, Any] = {"path": path}
        if max_bytes > 0:
            args["max_bytes"] = max_bytes
        try:
            task_id = orch.start_task(
                user_id=_user_id(self._session),
                handler_name="file_read",
                args=args,
                title=title,
                initiated_by="aiko",
            )
        except Exception as exc:
            log.exception(
                "start_file_read: orchestrator.start_task failed: path=%r",
                path,
            )
            raise ToolError(f"start_file_read failed: {exc}") from exc
        if task_id is None:
            raise ToolError(
                "start_file_read: spawn rejected (per-user cap or "
                "missing handler)"
            )
        log.info(
            "start_file_read spawned: task_id=%d path=%r max_bytes=%d",
            task_id,
            path,
            max_bytes,
        )
        return json.dumps(
            {
                "task_id": task_id,
                "handler": "file_read",
                "note": (
                    "Read started. Content will arrive in a later turn "
                    "as a task cue. If the path is ambiguous, you'll "
                    "get an awaiting-input cue asking which root the "
                    "user meant — ask them, then call "
                    "answer_file_task with their reply."
                ),
            },
            ensure_ascii=False,
        )


# ── answer_file_task ─────────────────────────────────────────────────────


class AnswerFileTaskTool:
    """Resolve an ``awaiting_input`` file task with the user's answer.

    Used to disambiguate a bare-path read whose path matched in
    multiple roots. The previous-turn prompt cue exposes the
    candidates as label-prefixed strings (``"Documents:notes/q4.md"``);
    pass exactly one of those strings back. Aiko may also pass just a
    label (``"Documents"``) when the candidate list has unique labels.

    Args:

    * ``task_id`` (int, required) — the id of the ``awaiting_input``
      task to resolve.
    * ``answer`` (str, required) — the user's chosen candidate.

    Returns ``{"answered": bool, "task_id": N}``. ``answered=False``
    means the orchestrator rejected the answer (task no longer
    waiting, unknown task id, etc.); the handler may also reject the
    answer text downstream and emit another ``TaskInputNeeded`` —
    that surfaces as a fresh cue on the next turn.
    """

    name = "answer_file_task"

    def __init__(self, session: "SessionController") -> None:
        self._session = session

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="answer_file_task",
            description=(
                "Resolve a file-read task that is awaiting input "
                "(typically the multi-root disambiguation case). "
                "Pass the user's chosen candidate verbatim. Returns "
                "JSON: {answered, task_id}."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "integer",
                        "description": (
                            "The id of the awaiting-input task."
                        ),
                    },
                    "answer": {
                        "type": "string",
                        "description": (
                            "The user's chosen candidate. Usually a "
                            "label-prefixed path from the cue's "
                            "options list, e.g. 'Documents:notes/q4.md'."
                        ),
                    },
                },
                "required": ["task_id", "answer"],
            },
        )

    def run(self, arguments: dict[str, Any]) -> str:
        try:
            task_id = int(arguments.get("task_id", 0))
        except (TypeError, ValueError):
            raise ToolError("answer_file_task: 'task_id' must be an integer")
        if task_id <= 0:
            raise ToolError(
                "answer_file_task: 'task_id' must be a positive integer"
            )
        answer = (arguments.get("answer") or "").strip()
        if not answer:
            raise ToolError("answer_file_task: 'answer' is required")
        orch = _orchestrator(self._session)
        if orch is None:
            raise ToolError(
                "answer_file_task: filesystem task subsystem is disabled "
                "(agent.tasks_enabled=False)"
            )
        try:
            ok = orch.answer(task_id, answer)
        except Exception as exc:
            log.exception(
                "answer_file_task: orchestrator.answer failed: task_id=%d",
                task_id,
            )
            raise ToolError(f"answer_file_task failed: {exc}") from exc
        log.info(
            "answer_file_task: task_id=%d answered=%s answer_chars=%d",
            task_id, ok, len(answer),
        )
        return json.dumps(
            {"answered": bool(ok), "task_id": int(task_id)},
            ensure_ascii=False,
        )


# ── factory ──────────────────────────────────────────────────────────────


def build_file_task_tools(session: "SessionController") -> list[Tool]:
    """Construct the file-task tool set bound to ``session``.

    Returned in registration order so the registry exposes them
    consistently in :func:`ToolRegistry.names`. Empty list when the
    task subsystem is wired but the master switch happens to be off
    (defence in depth — :func:`rebuild_tool_registry` also gates on
    ``tools.file_tasks``).

    Order: discovery first (``list_file_roots``), then async create
    (search + read), then async control (cancel + answer). An LLM
    that scans the catalogue top-to-bottom reads the natural flow.
    """
    return [
        ListFileRootsTool(session),
        StartFileSearchTool(session),
        StartFileReadTool(session),
        CancelFileTaskTool(session),
        AnswerFileTaskTool(session),
    ]


__all__ = [
    "ListFileRootsTool",
    "StartFileSearchTool",
    "StartFileReadTool",
    "CancelFileTaskTool",
    "AnswerFileTaskTool",
    "build_file_task_tools",
]
