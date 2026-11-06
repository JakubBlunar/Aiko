"""Bounded, user-owned work/result reads; inspecting never starts a task."""
import hashlib
import json


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
    text = json.dumps(details, ensure_ascii=False, sort_keys=True, default=str)
    key = hashlib.sha256(json.dumps([brief(row), text], sort_keys=True,
                                   default=str).encode()).hexdigest()
    if (revision is not None and revision != key) or content_offset and not revision:
        raise ValueError("Work changed or revision missing; restart at content_offset 0.")
    if not 0 <= content_offset <= len(text):
        raise ValueError("content_offset is outside this task result.")
    task = brief(row)
    end = min(len(text), content_offset + 6000)
    if content_offset == 0 and end == len(text):
        task.update(details)
    else:
        task.update(details_fragment=text[content_offset:end], fragment_encoding="json",
                    is_fragment=True)
    task.update(revision=key, content_offset=content_offset,
                next_offset=end if end < len(text) else None)
    return {"status": "ok", "read_only": True, "task": task,
            "note": "Task status is the saved execution state. Empty active lists do not prove "
                    "completion. Read result/error and any continuation before claiming outcomes."}
