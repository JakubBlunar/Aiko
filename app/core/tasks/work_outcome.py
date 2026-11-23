"""One objective-outcome contract for API, speech, promises and learned methods.

Task status describes execution. Only a source-bound completion certificate
establishes success; old model booleans are retained as unverified history.
"""
from __future__ import annotations

import hashlib
import json


def digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()


def certified_answer(result) -> bool:
    if not isinstance(result, dict):
        return False
    check = result.get("verification") or {}
    if not isinstance(check, dict):
        return False
    certificate = check.get("certificate") or {}
    if not isinstance(certificate, dict):
        return False
    sources, criteria = certificate.get("sources"), certificate.get("criteria")
    if (not isinstance(sources, dict) or not sources or not isinstance(criteria, list)
            or not criteria or any(not isinstance(item, dict)
                                  or type(item.get("index")) is not int
                                  or type(item.get("supported")) is not bool for item in criteria)):
        return False
    return bool(
        check.get("status") == "supported"
        and check.get("missing") == []
        and type(certificate.get("version")) is int and certificate["version"] == 1
        and certificate.get("answer_digest") == digest(result.get("content", ""))
        and certificate.get("sources")
        and certificate.get("criteria")
    )


def certified_result(result) -> bool:
    if not certified_answer(result):
        return False
    return all(item["supported"] for item in result["verification"]["certificate"]["criteria"])


def saved_submissions(result):
    check = result.get("verification")
    receipts = check.get("submissions") if isinstance(check, dict) else None
    if (not isinstance(receipts, list) or not receipts
            or any(not isinstance(item, dict) or type(item.get("task_id")) is not int
                   or not isinstance(item.get("operation"), str) or not item["operation"]
                   or not isinstance(item.get("revision"), str) or not item["revision"]
                   for item in receipts)):
        return []
    return receipts


def submission_message(receipts):
    if not receipts:
        return "I could not verify that the operation was submitted."
    operations = ", ".join(f"{item['operation']} (#{item['task_id']})" for item in receipts)
    return f"I received acknowledgement for {operations}. I haven't verified the final outcome."


def outcome_for(status, result=None, *, workflow=False) -> dict:
    result = result if isinstance(result, dict) else {}
    state = status
    verified = False
    if status == "done":
        value = result.get("outcome")
        value = value if isinstance(value, str) else None
        if value == "submitted":
            state = "submitted" if saved_submissions(result) else "unverified"
        elif value in {"partial", "blocked", "uncertain", "missing_capability"}:
            state = value
        elif value in {"success", "nothing_found"}:
            verified = certified_result(result)
            state = ("succeeded" if value == "success" else "nothing_found") if verified else (
                "unverified"
            )
        else:
            state = "unverified" if workflow else "finished"
    labels = {
        "succeeded": "Completed", "nothing_found": "No matching result",
        "submitted": "Submitted; outcome unverified", "partial": "Partial result",
        "blocked": "Blocked", "uncertain": "Needs review",
        "missing_capability": "Capability unavailable", "unverified": "Result unverified",
        "finished": "Execution finished", "running": "Working", "paused": "Waiting",
        "awaiting_input": "Needs your answer", "failed": "Failed",
        "cancelled": "Cancelled", "interrupted": "Interrupted",
    }
    return {"version": 1, "state": state, "verified": verified,
            "label": labels.get(state, "Outcome unknown")}


def task_outcome(row) -> dict:
    return outcome_for(row.status, getattr(row, "result", None), workflow=(
        getattr(row, "handler_name", "") == "goal_workflow"
        or (getattr(row, "state", None) or {}).get("harness_version") == 1
    ))
