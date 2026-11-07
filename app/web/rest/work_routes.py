"""Owned work detail, discussion and controls for the work drawer."""

from __future__ import annotations

from dataclasses import asdict
import hashlib

from fastapi import HTTPException

from app.core.tasks.task_execution_mixin import task_snapshot
from app.core.tasks.workflow.harness_results import result_text


def register_work_routes(app, session) -> None:
    def owned(task_id: int, *, root: bool = False):
        store = session.tasks.store
        if store is None:
            raise HTTPException(503, "Work is unavailable")
        row = store.get(task_id)
        if (
            row is None
            or not row.visible_to_user
            or row.user_id != session.user_id
            or (root and row.parent_task_id is not None)
        ):
            raise HTTPException(404, "Work not found")
        return row

    @app.get("/api/work")
    def work_index(offset: int = 0):
        store = session.tasks.store
        if store is None:
            return {"tasks": [], "has_more": False}
        if offset < 0:
            raise HTTPException(400, "Offset must be non-negative")
        # Active roots are included independently of history pagination so a
        # multi-day job cannot disappear behind newer, completed jobs.
        active = [
            row
            for row in store.list_non_terminal()
            if row.user_id == session.user_id and row.visible_to_user and row.parent_task_id is None
        ]
        recent = store.list_for_user(session.user_id, roots_only=True, limit=31, offset=offset)
        rows = {row.id: row for row in (active if offset == 0 else []) + recent[:30]}
        return {
            "tasks": [task_snapshot(row) for row in rows.values()],
            "has_more": len(recent) > 30,
        }

    @app.get("/api/tasks/{task_id}/work")
    def work_detail(task_id: int, before: int | None = None):
        row = owned(task_id, root=True)
        store, events = session.tasks.store, session.tasks.event_store
        messages = events.latest_work_messages(task_id, before=before, limit=100) if events else []
        children = []
        for child in store.list_children(task_id):
            if child.user_id != row.user_id or not child.visible_to_user:
                continue
            brief = task_snapshot(child)
            brief["args"] = {}
            brief["result"] = (
                {"summary": str((child.result or {}).get("summary") or "")[:500]}
                if child.result
                else None
            )
            children.append(brief)
        return {
            "task": task_snapshot(row),
            "children": children,
            "messages": [asdict(event) for event in messages],
            "oldest_event_id": messages[0].id if messages else None,
            "has_older": bool(
                messages
                and events.latest_work_messages(
                    task_id,
                    before=messages[0].id,
                    limit=1,
                )
            ),
        }

    @app.get("/api/tasks/{task_id}/result")
    def work_result(task_id: int, offset: int = 0, revision: str | None = None):
        row = owned(task_id)
        if row.parent_task_id is not None:
            owned(row.parent_task_id, root=True)
        text = result_text(row)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if revision is not None and revision != digest:
            raise HTTPException(409, "The result changed; reopen it to read the current version")
        if not 0 <= offset <= len(text):
            raise HTTPException(400, "Result offset is out of range")
        end = min(len(text), offset + 12000)
        return {
            "task_id": row.id,
            "content": text[offset:end],
            "offset": offset,
            "revision": digest,
            "next_offset": end if end < len(text) else None,
            "total_characters": len(text),
            "truncated": bool((row.result or {}).get("truncated")),
            "receipt": (row.result or {}).get("receipt"),
            "error": row.error,
        }

    @app.post("/api/tasks/{task_id}/work/messages")
    def work_message(task_id: int, payload: dict):
        row = owned(task_id, root=True)
        content, mode, client_id = (
            payload.get("content"),
            payload.get("mode", "discussion"),
            payload.get("client_id"),
        )
        if (
            not isinstance(content, str)
            or not 1 <= len(content.strip()) <= 8000
            or mode not in {"discussion", "guidance"}
            or not isinstance(client_id, str)
            or not 1 <= len(client_id) <= 128
        ):
            raise HTTPException(400, "Provide a message, mode and client message ID")
        orchestrator = session.tasks.orchestrator
        if orchestrator is None or row.handler_name != "goal_workflow":
            raise HTTPException(409, "This task does not support a work conversation")
        try:
            message_id, created = orchestrator.post_work_message(
                task_id,
                content.strip(),
                mode=mode,
                client_id=client_id,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"message_id": message_id, "created": created}

    @app.post("/api/tasks/{task_id}/work/control")
    def work_control(task_id: int, payload: dict):
        owned(task_id, root=True)
        action = payload.get("action")
        orchestrator = session.tasks.orchestrator
        if orchestrator is None:
            raise HTTPException(503, "Work is unavailable")
        if action == "pause":
            changed = orchestrator.pause_work(task_id)
        elif action == "resume":
            changed = orchestrator.resume_work(task_id)
        else:
            raise HTTPException(400, "Action must be pause or resume")
        if not changed:
            raise HTTPException(409, "Work cannot make that transition; refresh its current state")
        return {"task": task_snapshot(orchestrator.get(task_id))}
