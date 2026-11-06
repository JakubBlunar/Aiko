"""Bounded, user-owned work/result reads; inspecting never starts a task."""
from app.core.infra.read_paging import json_page


def brief(row):
    result = {key: getattr(row, key, None) for key in (
        "title", "handler_name", "status", "phase", "progress", "last_message",
        "parent_task_id", "created_at", "updated_at", "completed_at", "heartbeat_at",
    )}
    result["task_id"] = int(row.id)
    result["handler"] = result.pop("handler_name")
    if len(str(result["last_message"] or "")) > 1600:
        result.update(last_message=None, detail_inspection_required=True)
    return result


def inspect_work(orch, user_id, task_id, *, content_offset=0, revision=None):
    row = orch.get(task_id)
    if row is None or getattr(row, "user_id", None) != user_id:
        return {"status": "not_found", "read_only": True}
    details = {key: getattr(row, key, None) for key in (
        "result", "error", "input_request", "last_message",
    )}
    page = json_page({**brief(row), **details}, content_offset=content_offset, revision=revision)
    task = page.pop("record") or brief(row)
    task.update(page)
    return {"status": "ok", "read_only": True, "task": task,
            "note": "Task status is the saved execution state. Empty active lists do not prove "
                    "completion. Read result/error and any continuation before claiming outcomes."}
