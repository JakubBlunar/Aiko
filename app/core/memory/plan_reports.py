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
Explicitly finished/completed or got out of a named appointment is
reported_completed; cancellation and rescheduling stay
distinct. Keep unknown when the source explicitly says the outcome is unknown.
Completed/cancelled reports are past_event; new plans/rescheduling are future_plan.
Do not erase or rewrite the original intention. These fields share the ordinary
memory budget, add no entries, and are optional when no clear link exists.
"""
_PATTERNS = {
    "planned": r"\b(?:plan|planning|intend|will|going to)\b",
    "reported_action": r"\b(?:started|tried|went|visited|worked on|practised|practiced)\b",
    "reported_completed": (r"\b(?:finished|completed|submitted|sent|done)\b|"
                           r"\b(?:got|came|walked|finally) out of "
                           r"(?:the|my) (?:\w+ )?appointment\b"),
    "cancelled": r"\b(?:cancelled|canceled|called off|decided not to)\b",
    "changed_plan": r"\b(?:rescheduled|postponed|changed the plan|changed my plan)\b",
    "unknown": r"\b(?:not sure|don'?t know|do not know|uncertain)\b",
}
_PLAN_WORDS = frozenset(
    "after also another around before cancelled canceled completed decided during finished "
    "going intend later need next plan planned planning reported rescheduled started this "
    "time today tomorrow tried want was went will would yesterday".split()
)


def _action_words(text):
    from app.core.memory.learned_strategies import words

    return {word.removesuffix("s") if len(word) > 4 else word
            for word in words(text) - _PLAN_WORDS if len(word) > 2 and not word.isdigit()}


def plan_revision(memory):
    from app.core.memory.memory_relations import reference_revision

    return reference_revision(memory)


def offer_intentions(store, *, new_rows=None, context_rows=(), session_key=""):
    reader = getattr(store, "list_by_temporal_type", None)
    rows = reader("future_plan") if callable(reader) else []
    promises = getattr(store, "iter_by_kind", None)
    if callable(promises):
        rows = [*rows, *promises("promise")]
    new_terms, context_terms, latest = {}, {}, {}
    if new_rows is not None:
        new_terms = {role: [_action_words(row.content) for row in new_rows if row.role == role]
                     for role in ("user", "assistant")}
        context_terms = {role: [_action_words(row.content) for row in context_rows
                                if row.role == role] for role in new_terms}
        latest = {role: max((row.id for row in new_rows if row.role == role), default=0)
                  for role in new_terms}
    ranked = []
    for row in {int(row.id): row for row in rows}.values():
        metadata = row.metadata or {}
        actor = memory_origin(row).subject
        source_session = getattr(row, "source_session", None) or ""
        if (metadata.get("superseded_by") or metadata.get("consolidated_into")
                or metadata.get("promise_status") in {"fulfilled", "dropped"}
                or actor not in {"user", "aiko"}
                or (":" in session_key and ":" in source_session
                    and session_key.split(":", 1)[0] != source_session.split(":", 1)[0])
                or len(row.content) > 1000):
            continue
        direct = context = 0
        if new_rows is not None:
            role = "user" if actor == "user" else "assistant"
            if not latest[role] or (getattr(row, "source_message_id", None) or 0) >= latest[role]:
                continue
            terms = _action_words(row.content)
            direct = max((len(terms & entry) for entry in new_terms[role]), default=0)
            context = max((len(terms & entry) for entry in context_terms[role]), default=0)
            if not direct and context < 2:
                continue
        ranked.append((direct, context, str(row.created_at), int(row.id), row))
    ranked.sort(key=lambda item: item[:4], reverse=True)
    return {mid: {"row": deepcopy(row), "revision": plan_revision(row)}
            for _, _, _, mid, row in ranked[:8]}


def render_intentions(offered):
    from app.core.memory.memory_origin import origin_annotation

    return "\n".join(f"[intention:{mid}] " + timephrase.format_memory_line(value["row"])
                     + " [" + origin_annotation(value["row"]) + "]"
                     for mid, value in offered.items())


def admit_report(value, rows, offered, store, *, session_key, subject, temporal_type):
    from app.core.memory.shared_shorthand import source_receipt

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
    marker = re.search(_PATTERNS[status], quote, re.I) if isinstance(quote, str) else None
    if (row is None or not isinstance(quote, str) or not 8 <= len(quote) <= 240
            or quote not in row.content or "?" in quote
            or re.search(r"\b(?:pretend|imagine|hypothetical|role.?play)\b", row.content, re.I)
            or marker is None):
        raise ValueError("unsupported plan report")
    if status in {"reported_action", "reported_completed"} and re.search(
        r"\b(?:not|never|didn'?t|would|could|might|will|should|if)\b", quote, re.I,
    ):
        raise ValueError("uncompleted or hypothetical action")
    clauses = [clause.strip() for clause in re.split(r"[.!;\n]+", row.content)]
    if quote.strip(".!; ") not in clauses:
        raise ValueError("plan report must preserve its clause")
    terms = _action_words(prior.content)
    action = quote
    if status in {"reported_completed", "reported_action"} and marker.group().lower() != "done":
        action = re.split(
            r"\b(?:before|after|while|because|then)\b", quote[marker.start():],
            maxsplit=1, flags=re.I,
        )[0]
    if len(terms & _action_words(action)) < 2:
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
    from app.core.memory.memory_relations import linked_source_current

    if (not isinstance(value, dict) or value.get("version") != 1
            or type(value.get("prior_memory_id")) is not int
            or not isinstance(value.get("status"), str) or value["status"] not in STATUSES):
        return False
    return linked_source_current(value, store, _seen)


def plan_progress(prior, store):
    """Latest explicit report; unavailable/changed history never means completion."""
    result = {"status": "planned", "outcome_known": False, "report_memory_id": None}
    retirement = (getattr(prior, "metadata", None) or {}).get("plan_retirement")
    if (isinstance(retirement, dict) and retirement.get("version") == 1
            and retirement.get("intended_for") == getattr(prior, "event_time", None)):
        result.update(status="unknown", reason="planning_window_elapsed")
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
