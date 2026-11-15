"""Bounded source envelopes and host postconditions, independent of model prose."""
from __future__ import annotations

from app.core.tasks.work_outcome import digest
from app.core.tasks.handler_names import (
    HANDLER_FAST_TOOL, HANDLER_WEB_SEARCH, HANDLER_VISION_DESCRIBE,
)
from dataclasses import asdict, is_dataclass
from datetime import datetime
import json


def postcondition_payload(value):
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if not isinstance(value, dict):
        return None
    if not isinstance(value.get("resource"), str) or not 1 <= len(value["resource"]) <= 2048:
        return None
    requested = value.get("requested_resource")
    if requested is not None and (
        not isinstance(requested, str) or not 1 <= len(requested) <= 2048
    ):
        return None
    if any(value.get(key) is not None and not isinstance(value[key], dict)
           for key in ("expected", "observed")):
        return None
    try:
        if len(json.dumps(value, allow_nan=False)) > 8192:
            return None
    except (TypeError, ValueError):
        return None
    return {key: value.get(key) for key in (
        "resource", "expected", "observed", "requested_resource",
    )}


def observed_after(observation, mutation):
    try:
        return datetime.fromisoformat(observation) >= datetime.fromisoformat(mutation)
    except (TypeError, ValueError):
        return False


def result_text(row):
    result = row.result or {}
    content = result.get("content")
    if isinstance(content, str):
        return content
    return json.dumps(result, ensure_ascii=False)


def evidence_record(row, *, operation="", effect="", limit=6000):
    result, args = row.result or {}, getattr(row, "args", None) or {}
    receipt = result.get("receipt")
    receipt = receipt if isinstance(receipt, dict) else {}
    artifact = result.get("source_artifact")
    artifact = artifact if isinstance(artifact, dict) else {}
    text = result_text(row)
    handler = getattr(row, "handler_name", "")
    declared_effect = receipt.get("effect") or effect
    if not declared_effect and handler in {
        HANDLER_FAST_TOOL, HANDLER_WEB_SEARCH, HANDLER_VISION_DESCRIBE,
    }:
        # These host handlers have always admitted reads only. Recover their
        # declared contract for saved pre-envelope jobs; never guess MCP effects.
        declared_effect = "read"
    source_partial = bool(artifact.get("source_partial") or (
        result.get("truncated") and not artifact.get("available")
    ))
    record = {
        "task_id": row.id, "status": row.status, "operation": operation or args.get("tool_name"),
        "effect": declared_effect or "unknown",
        "generation": args.get("_config_generation", args.get("generation", "")),
        "started_at": receipt.get("dispatched_at") or getattr(row, "created_at", None),
        "recorded_at": row.completed_at, "revision": digest({
            "result": result, "status": row.status, "error": getattr(row, "error", None),
            "args": args,
        }),
        "receipt": receipt, "postcondition": postcondition_payload(result.get("postcondition")),
        "source_partial": source_partial,
        "partial": bool(source_partial or result.get("truncated") or len(text) > limit),
        "content": text[:limit], "error": getattr(row, "error", None),
    }
    search = result if handler == "web_search" else None
    if handler == "fast_tool" and args.get("tool_name") == "web_search":
        try:
            search = json.loads(text)
        except (TypeError, ValueError):
            pass
    if isinstance(search, dict) and isinstance(search.get("results"), list):
        record["search_coverage"] = {"returned": len(search["results"]), "exhaustive": False}
    return record


def owned_records(orchestrator, parent_id, steps, *, limit=6000):
    """Include uncited failures and later observations, not just planner-selected successes."""
    by_id = {step.get("child_id"): step for step in steps if type(step.get("child_id")) is int}
    getter = getattr(orchestrator, "list_children", None)
    rows = getter(parent_id) if callable(getter) else [orchestrator.get(cid) for cid in by_id]
    lookup = getattr(orchestrator, "get", None)
    parent = lookup(parent_id) if callable(lookup) else None
    owner = getattr(parent, "user_id", None)
    return [evidence_record(row, operation=by_id.get(row.id, {}).get("skill", ""),
                            effect=by_id.get(row.id, {}).get("effect", ""), limit=limit)
            for row in rows if row and row.parent_task_id == parent_id
            and (owner is None or getattr(row, "user_id", None) == owner)]


def check_postconditions(records, *, cited_ids=None):
    """Trusted plugin expectations require a later owned read of the same resource.

    Unknown mutation adapters cannot certify persisted/delivered effects. They
    can still report submission, and read-only research uses semantic review.
    """
    missing = []
    for record in records:
        proof = record.get("postcondition") or {}
        if (proof.get("requested_resource")
                and proof["requested_resource"] != proof.get("resource")):
            if cited_ids is None or record["task_id"] in cited_ids:
                missing.append(f"Result #{record['task_id']} observed a different resource "
                               "from the admitted request.")
            continue
        if record["status"] != "done" or record["effect"] == "read":
            continue
        # Missing effect metadata must not silently turn an old operation into a read.
        if (record["effect"] == "unknown" and not record.get("receipt")
                and not record.get("postcondition")):
            missing.append(f"Result #{record['task_id']} has no declared effect contract.")
            continue
        expected = proof.get("expected")
        if not proof.get("resource") or not isinstance(expected, dict) or not expected:
            missing.append(f"Result #{record['task_id']} only acknowledges an operation; "
                           "a typed postcondition and later observation are required.")
            continue
        later = [candidate for candidate in records
                 if candidate["task_id"] > record["task_id"] and candidate["status"] == "done"
                 and candidate["effect"] == "read" and not candidate["source_partial"]
                 and candidate["generation"] == record["generation"]
                 and candidate.get("recorded_at") and record.get("recorded_at")
                 and observed_after(candidate.get("started_at"), record["recorded_at"])
                 and observed_after(candidate["recorded_at"], record["recorded_at"])
                 and (candidate.get("postcondition") or {}).get("requested_resource")
                 in (None, proof["resource"])
                 and (candidate.get("postcondition") or {}).get("resource") == proof["resource"]]
        latest = max(later, key=lambda item: item["task_id"], default=None)
        actual = ((latest or {}).get("postcondition") or {}).get("observed") or {}
        if not latest or any(key not in actual or digest(actual[key]) != digest(value)
                             for key, value in expected.items()):
            missing.append(f"Result #{record['task_id']} has no matching later observation "
                           "of its required postcondition.")
    return missing


def certificate_current(certificate, records):
    sources = {str(record["task_id"]): record["revision"] for record in records}
    saved = (certificate or {}).get("sources") or {}
    return bool(saved) and sources == saved
