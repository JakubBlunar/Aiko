"""Append-only, source-cited reports about previously recorded intentions."""
from __future__ import annotations

import json
import re
import sqlite3
from copy import deepcopy

from app.core.infra import timephrase
from app.core.memory.memory_origin import memory_origin


STATUSES = {"planned", "reported_action", "reported_completed", "cancelled",
            "changed_plan", "unknown"}
CLOSED = {"reported_completed", "cancelled", "changed_plan"}
RULE = """
Optional follow_through: {prior_memory_id: int, status: planned|reported_action|
reported_completed|cancelled|changed_plan|unknown, report: {message_id, quote}}.
Use only an intention ID actually offered below and a NEW explicit report by
the same actor. Quote a complete clause <=240 chars naming the specific action.
I did it / thanks / elapsed time / repeated promises / an assistant prediction
cannot settle a plan. Started or tried is reported_action, not completion.
Finished/completed is reported_completed; cancellation and rescheduling stay
distinct. Keep unknown when the source explicitly says the outcome is unknown.
Completed/cancelled reports are past_event; new plans/rescheduling are future_plan.
Do not erase or rewrite the original intention. These fields share the ordinary
memory budget, add no entries, and are optional when no clear link exists.
"""
_PATTERNS = {
    "planned": r"\b(?:plan|planning|intend|will|going to)\b",
    "reported_action": r"\b(?:started|tried|went|visited|worked on|practised|practiced)\b",
    "reported_completed": r"\b(?:finished|completed|submitted|sent|done)\b",
    "cancelled": r"\b(?:cancelled|canceled|called off|decided not to)\b",
    "changed_plan": r"\b(?:rescheduled|postponed|changed the plan|changed my plan)\b",
    "unknown": r"\b(?:not sure|don'?t know|do not know|uncertain)\b",
}


def plan_revision(memory):
    from app.core.concepts.concept_support import memory_revision, revision_hash

    return revision_hash([memory_revision(memory), memory_origin(memory),
                          getattr(memory, "temporal_type", None)])


def offer_intentions(store):
    reader = getattr(store, "list_by_temporal_type", None)
    rows = reader("future_plan") if callable(reader) else []
    promises = getattr(store, "iter_by_kind", None)
    if callable(promises):
        rows = [*rows, *promises("promise")]
    offered = {}
    for row in sorted(rows, key=lambda row: (str(row.created_at), int(row.id)), reverse=True):
        metadata = row.metadata or {}
        if (metadata.get("superseded_by") or metadata.get("consolidated_into")
                or metadata.get("promise_status") in {"fulfilled", "dropped"}
                or memory_origin(row).subject not in {"user", "aiko"}
                or len(row.content) > 1000):
            continue
        offered[int(row.id)] = {"row": deepcopy(row), "revision": plan_revision(row)}
        if len(offered) >= 8:
            break
    return offered


def render_intentions(offered):
    from app.core.memory.memory_origin import origin_annotation

    return "\n".join(f"[intention:{mid}] " + timephrase.format_memory_line(value["row"])
                     + " [" + origin_annotation(value["row"]) + "]"
                     for mid, value in offered.items())


def admit_report(value, rows, offered, store, *, session_key, subject, temporal_type):
    from app.core.memory.shared_shorthand import source_receipt
    from app.core.memory.learned_strategies import words

    if not isinstance(value, dict) or type(value.get("prior_memory_id")) is not int:
        raise ValueError("plan reference")
    mid, status = value["prior_memory_id"], value.get("status")
    if not isinstance(status, str) or status not in STATUSES or mid not in offered:
        raise ValueError("unoffered plan or status")
    prior = store.get(mid)
    if prior is None or plan_revision(prior) != offered[mid]["revision"]:
        raise ValueError("plan changed")
    actor = "aiko" if subject == "assistant" else "user"
    if memory_origin(prior).subject != actor:
        raise ValueError("different plan actor")
    item = value.get("report")
    if not isinstance(item, dict) or type(item.get("message_id")) is not int:
        raise ValueError("plan report citation")
    role = "assistant" if actor == "aiko" else "user"
    row = next((row for row in rows if row.id == item["message_id"]
                and row.session_id == session_key and row.role == role), None)
    quote = item.get("quote")
    if (row is None or not isinstance(quote, str) or not 8 <= len(quote) <= 240
            or quote not in row.content or "?" in quote
            or re.search(r"\b(?:pretend|imagine|hypothetical|role.?play)\b", row.content, re.I)
            or not re.search(_PATTERNS[status], quote, re.I)):
        raise ValueError("unsupported plan report")
    if status in {"reported_action", "reported_completed"} and re.search(
        r"\b(?:not|never|didn'?t|would|could|might|will|should|if)\b", row.content, re.I,
    ):
        raise ValueError("uncompleted or hypothetical action")
    clauses = [clause.strip() for clause in re.split(r"[.!;\n]+", row.content)]
    if quote.strip(".!; ") not in clauses:
        raise ValueError("plan report must preserve its clause")
    common = words(prior.content) & words(quote)
    if len(common) < min(2, len(words(prior.content))):
        raise ValueError("ambiguous action reference")
    if (status in {"reported_completed", "cancelled"} and temporal_type != "past_event"
            or status in {"planned", "changed_plan"} and temporal_type != "future_plan"):
        raise ValueError("plan report time scope")
    admission = (prior.metadata or {}).get("admission") or {}
    ids = [ident for ident in admission.get("source_message_ids", []) if type(ident) is int]
    original = getattr(prior, "source_message_id", None)
    if type(original) is int:
        ids.append(original)
    if ids and row.id <= max(ids):
        raise ValueError("report is not later than intention")
    receipt = {**source_receipt(row, quote), "source_at": str(row.created_at)}
    return {"version": 1, "scope": "reported_follow_through", "prior_memory_id": mid,
            "prior_revision": offered[mid]["revision"], "subject": actor, "status": status,
            "ordering": "later_message" if ids else "original_source_unknown",
            "report": receipt, "sources": [receipt]}


def report_current(value, store, _seen=None):
    from app.core.memory.observation_receipts import receipt_sources_current

    if (not isinstance(value, dict) or value.get("version") != 1
            or type(value.get("prior_memory_id")) is not int
            or not isinstance(value.get("status"), str) or value["status"] not in STATUSES):
        return False
    mid = value["prior_memory_id"]
    seen = set(_seen or ())
    if mid in seen or len(seen) >= 8:
        return False
    prior = store.get(mid)
    if prior is not None:
        metadata = getattr(prior, "metadata", None) or {}
        if not receipt_sources_current(metadata.get("observation"), store):
            return False
        parent = metadata.get("follow_through")
        if parent is not None and not report_current(parent, store, seen | {mid}):
            return False
    return (prior is not None and plan_revision(prior) == value.get("prior_revision")
            and receipt_sources_current(value, store))


def plan_progress(prior, store):
    """Latest explicit report; unavailable/changed history never means completion."""
    result = {"status": "planned", "outcome_known": False, "report_memory_id": None}
    try:
        rows = store._get_conn().execute(
            "SELECT id, metadata FROM memories WHERE json_valid(metadata) AND "
            "json_extract(metadata, '$.follow_through.prior_memory_id') = ? "
            "ORDER BY json_extract(metadata, '$.follow_through.report.message_id') DESC LIMIT 33",
            (prior.id,),
        ).fetchall()
        if rows:
            mid, metadata = rows[0]
            value = json.loads(metadata)["follow_through"]
            if report_current(value, store):
                result.update(status=value["status"], outcome_known=value["status"] in CLOSED,
                              report_memory_id=mid, report=value["report"])
            else:
                result.update(status="unknown", reason="report_source_changed")
        result["truncated"] = len(rows) > 32
    except (AttributeError, KeyError, TypeError, ValueError, sqlite3.Error):
        result.update(status="unknown", reason="report_history_unavailable")
    return result


def report_annotation(memory):
    value = (getattr(memory, "metadata", None) or {}).get("follow_through")
    if not isinstance(value, dict) or value.get("version") != 1:
        return ""
    return "Reported intention link (not independent verification): " + json.dumps(
        {key: value.get(key) for key in ("prior_memory_id", "status", "ordering", "report")},
        ensure_ascii=False, separators=(",", ":"),
    )
