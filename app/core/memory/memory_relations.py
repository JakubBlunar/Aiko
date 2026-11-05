"""Reported corrections, counterexamples and context changes preserve both records."""
from __future__ import annotations

import json
import re
import sqlite3
from types import SimpleNamespace

from app.core.memory.memory_origin import memory_origin


KINDS = {"correction", "changed_preference", "different_context", "counterexample"}
RULE = """
Optional contextual_relation: {prior_memory_id: an offered [memory:ID],
 relation: correction|changed_preference|different_context|counterexample,
 report: {message_id: a NEW owner message, quote: complete literal clause <=240 chars}}.
Only link a specific claim explicitly qualified/corrected in the source, not a
merely related topic. Correction requires explicit wrong/not-true/I-meant wording;
changed_preference requires an explicitly reported change. A preference in a
different situation is different_context, not a correction or universal reversal.
A single contrasting episode is a counterexample, not a new universal trait.
Keep the context and exceptions in content and observation facets. Preserve both
records; do not also supersedes_memory_id a different context or counterexample.
Use no link when no clear claim-specific relation is stated. These fields share
the ordinary memory budget, require no extra entries, and never justify hidden motives.
"""
_MARKERS = {
    "correction": r"\b(?:was wrong|were wrong|not true|correction|I meant|misunderstood)\b",
    "changed_preference": r"\b(?:no longer|now prefer|used to|changed my mind|prefer.*instead)\b",
    "different_context": r"\b(?:when|while|during|for|if|unless|except)\b",
    "counterexample": (r"\b(?:except|but|however|not always|this time|on that occasion|"
                      r"unlike|despite)\b"),
}


def reference_revision(memory):
    from app.core.concepts.concept_support import memory_revision, revision_hash

    return revision_hash([memory_revision(memory), memory_origin(memory),
                          getattr(memory, "temporal_type", None)])


def admit_relation(value, rows, offered, store, *, session_key, subject):
    from app.core.memory.learned_strategies import words
    from app.core.memory.shared_shorthand import source_receipt

    if not isinstance(value, dict) or type(value.get("prior_memory_id")) is not int:
        raise ValueError("relation reference")
    mid, relation = value["prior_memory_id"], value.get("relation")
    if not isinstance(relation, str) or relation not in KINDS or mid not in offered:
        raise ValueError("unoffered relation target")
    prior = store.get(mid)
    actor = "aiko" if subject == "assistant" else "user"
    if (prior is None or reference_revision(prior) != offered[mid]["revision"]
            or memory_origin(prior).subject != actor
            or memory_origin(prior).mode not in {"observation", "declaration", "interpretation"}
            or getattr(prior, "temporal_type", "durable") == "future_plan"):
        raise ValueError("changed or incompatible relation target")
    item = value.get("report")
    if not isinstance(item, dict) or type(item.get("message_id")) is not int:
        raise ValueError("relation citation")
    role = "assistant" if actor == "aiko" else "user"
    row = next((row for row in rows if row.id == item["message_id"]
                and row.session_id == session_key and row.role == role), None)
    quote = item.get("quote")
    def normalized_words(text):
        return {word[:-1] if word.endswith("s") and len(word) > 4 else word
                for word in words(text)}

    if (row is None or not isinstance(quote, str) or not 8 <= len(quote) <= 240
            or quote not in row.content or "?" in quote
            or re.search(r"\b(?:pretend|imagine|hypothetical|role.?play)\b", row.content, re.I)
            or not re.search(_MARKERS[relation], quote, re.I)
            or quote.strip(".!; ") not in [c.strip() for c in re.split(r"[.!;\n]+", row.content)]
            or len(normalized_words(quote) & normalized_words(prior.content))
            < min(2, len(normalized_words(prior.content)))):
        raise ValueError("unsupported claim-specific relation")
    original = getattr(prior, "source_message_id", None)
    if type(original) is int and row.id <= original:
        raise ValueError("relation is not later")
    receipt = {**source_receipt(row, quote), "source_at": str(row.created_at)}
    return {"version": 1, "scope": "reported_contextual_relation", "prior_memory_id": mid,
            "prior_revision": offered[mid]["revision"], "relation": relation, "subject": actor,
            "report": receipt, "sources": [receipt]}


def linked_source_current(value, store, seen=None):
    from app.core.memory.observation_receipts import (
        receipt_sources_current, longitudinal_sources_current,
    )

    mid = value.get("prior_memory_id") if isinstance(value, dict) else None
    visited = set(seen or ())
    if type(mid) is not int or mid in visited or len(visited) >= 8:
        return False
    prior = store.get(mid)
    return (prior is not None and reference_revision(prior) == value.get("prior_revision")
            and receipt_sources_current(value, store)
            and longitudinal_sources_current(prior, store, _seen=visited | {mid}))


def relation_current(value, store, seen=None):
    return (isinstance(value, dict) and value.get("version") == 1
            and isinstance(value.get("relation"), str) and value["relation"] in KINDS
            and linked_source_current(value, store, seen))


def incoming_relations(memory, store, *, limit=8):
    try:
        cap = min(16, max(1, int(limit)))
        rows = store._get_conn().execute(
            "SELECT id, metadata FROM memories WHERE json_valid(metadata) AND "
            "json_extract(metadata, '$.contextual_relation.prior_memory_id') = ? "
            "ORDER BY json_extract(metadata, '$.contextual_relation.report.message_id') "
            "DESC LIMIT ?",
            (memory.id, cap + 1),
        ).fetchall()
        return [{"memory_id": mid, "receipt": json.loads(metadata)["contextual_relation"],
                 "current": relation_current(json.loads(metadata)["contextual_relation"], store)}
                for mid, metadata in rows[:cap]], len(rows) > cap
    except (AttributeError, KeyError, TypeError, ValueError, sqlite3.Error):
        return [], False


def review_state(memory, store):
    rows, truncated = incoming_relations(memory, store)
    for row in rows:
        kind = row["receipt"]["relation"]
        if row["current"] and kind in {"correction", "changed_preference"}:
            return {"review_required": True, "reason": "explicit_" + kind + "_reported",
                    "memory_id": row["memory_id"], "truncated": truncated}
    return {"review_required": False, "truncated": truncated}


class SqlMemoryReader:
    """A cold read adapter for the dependency ledger, without a second mirror."""
    def __init__(self, conn, cached=None):
        self.conn = conn
        self.cached = cached or {}

    def _get_conn(self):
        return self.conn

    def get(self, mid):
        if mid in self.cached:
            return self.cached[mid]
        row = self.conn.execute(
            "SELECT id, content, kind, metadata, source_message_id, source_session, "
            "created_at, event_time, temporal_type, provenance FROM memories WHERE id = ?", (mid,),
        ).fetchone()
        if row is None:
            return None
        names = ("id", "content", "kind", "metadata", "source_message_id", "source_session",
                 "created_at", "event_time", "temporal_type", "provenance")
        value = dict(zip(names, row, strict=True))
        value["metadata"] = json.loads(value["metadata"] or "{}")
        return SimpleNamespace(**value)


def context_scope(memory):
    value = (getattr(memory, "metadata", None) or {}).get("observation") or {}
    if not isinstance(value, dict):
        return None
    context = value.get("context") or {}
    exceptions = value.get("exceptions") or []
    if not isinstance(context, dict) or not isinstance(exceptions, list):
        return ("invalid_scope", ())
    return (context.get("quote"), tuple(item.get("quote") for item in exceptions
                                        if isinstance(item, dict)))


def relation_annotation(memory):
    value = (getattr(memory, "metadata", None) or {}).get("contextual_relation")
    if not isinstance(value, dict) or value.get("version") != 1:
        return ""
    return "Reported context relation (preserve both perspectives): " + json.dumps(
        {key: value.get(key) for key in ("prior_memory_id", "relation", "report")},
        ensure_ascii=False, separators=(",", ":"),
    )
