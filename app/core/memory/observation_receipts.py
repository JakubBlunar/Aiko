"""Literal situation/outcome facets, not inferred events or causal claims."""
from __future__ import annotations

import json
import re
import sqlite3

from app.core.infra import timephrase
from app.core.memory.strategy_outcomes import SUCCESS, FAILURE


EXTRACTION_RULE = """
Optional observation receipts enrich an ordinary memory without adding entries:
observation: {context: citation, action: citation, outcome: citation,
              exceptions: [citation]}. Omit unknown fields; never fill a quota.
Each citation is {message_id: int, quote: exact literal source span <=240 chars}. Only NEW
turns from the memory's actual owner can supply these facets. Keep the full
qualifier in content too. Context says when/where the preference or action
applies; action preserves what was actually stated, including plan modality.
Outcome requires an explicit reported result such as worked/helped/failed,
not thanks, silence, enthusiasm, an assistant prediction or imagined scene.
Exceptions require an explicit unless/except/not-when limitation. Do not invent
triggers, motives, results, exceptions or event dates. Raw quotes retain their
source timestamp; do not copy relative wording into the memory's content.
An outcome quote must preserve its complete source sentence/clause, including
negation and conditions. A hypothetical or predicted benefit is not an outcome.
"""
_FICTION = re.compile(r"\b(?:pretend|role.?play|hypothetical|fictional|imagine)\b", re.I)
_CONDITION = re.compile(r"\b(?:when|while|during|unless|except|if|for)\b", re.I)
_EXCEPTION = re.compile(r"\b(?:unless|except|not when|not while|not during)\b", re.I)
_PREDICTED = re.compile(r"\b(?:would|could|might|will|should|hope|maybe|if)\b", re.I)


def admit_observation(value, rows, *, session_key, subject):
    from app.core.memory.shared_shorthand import source_receipt

    if not isinstance(value, dict) or set(value) - {"context", "action", "outcome", "exceptions"}:
        raise ValueError("observation shape")
    owner = "assistant" if subject == "assistant" else "user"
    offered = {row.id: row for row in rows if row.session_id == session_key and row.role == owner}
    sources = []

    def citation(item):
        if not isinstance(item, dict) or type(item.get("message_id")) is not int:
            raise ValueError("observation citation")
        row = offered.get(item["message_id"])
        quote = item.get("quote")
        if (row is None or not isinstance(quote, str) or not 4 <= len(quote) <= 240
                or quote not in row.content or _FICTION.search(row.content) or "?" in quote):
            raise ValueError("unsupported observation facet")
        receipt = {**source_receipt(row, quote),
                   "source_at": str(getattr(row, "created_at", "") or "")}
        sources.append(receipt)
        return receipt

    result = {"version": 1, "subject": "aiko" if owner == "assistant" else "user",
              "scope": "reported_facets", "recorded_at": timephrase.utcnow().isoformat()}
    for field in ("context", "action", "outcome"):
        if value.get(field) is None:
            continue
        receipt = citation(value[field])
        quote = receipt["quote"]
        if field == "context" and not _CONDITION.search(quote):
            raise ValueError("context qualifier missing")
        if field == "outcome" and not (SUCCESS.search(quote) or FAILURE.search(quote)):
            raise ValueError("explicit reported outcome missing")
        if field == "outcome":
            content = offered[receipt["message_id"]].content
            clauses = re.split(r"[.!;\n]+", content)
            containing = [clause.strip() for clause in clauses if quote.strip(".!; ") in clause]
            if (not containing or not any(quote.strip(".!; ") == clause for clause in containing)
                    or any(_PREDICTED.search(clause) for clause in containing)):
                raise ValueError("outcome must preserve a reported clause")
        result[field] = receipt
    exceptions = value.get("exceptions", [])
    if not isinstance(exceptions, list) or len(exceptions) > 3:
        raise ValueError("observation exceptions")
    result["exceptions"] = []
    for item in exceptions:
        receipt = citation(item)
        if not _EXCEPTION.search(receipt["quote"]):
            raise ValueError("exception qualifier missing")
        result["exceptions"].append(receipt)
    if not sources:
        return None
    result["sources"] = list({json.dumps(row, sort_keys=True): row for row in sources}.values())
    return result


def observation_context(memory):
    value = (getattr(memory, "metadata", None) or {}).get("observation")
    if not isinstance(value, dict) or value.get("version") != 1:
        return ""
    def quoted(receipt):
        return {key: receipt.get(key) for key in ("message_id", "role", "source_at", "quote")}

    facets = {key: ([quoted(row) for row in value[key]] if key == "exceptions"
                    else quoted(value[key]))
              for key in ("context", "action", "outcome", "exceptions") if value.get(key)}
    return "Recorded source facets (quoted, not causal proof): " + json.dumps(
        facets, ensure_ascii=False, separators=(",", ":"),
    )


def facet_scope(memory):
    metadata = getattr(memory, "metadata", None) or {}
    report = metadata.get("follow_through")
    value = metadata.get("observation") or {}
    if not isinstance(value, dict) or value.get("version") != 1:
        return json.dumps(report, sort_keys=True) if report else None
    if report:
        return json.dumps({"observation": value, "follow_through": report}, sort_keys=True)
    return json.dumps({key: ([row.get("quote") for row in value[key]] if key == "exceptions"
                            else value[key].get("quote"))
                       for key in ("context", "action", "outcome", "exceptions") if value.get(key)},
                      sort_keys=True)


def longitudinal_sources_current(memory, store):
    """Verify preserved quotes against SQLite before using enriched evidence."""
    metadata = getattr(memory, "metadata", None) or {}
    if not receipt_sources_current(metadata.get("observation"), store):
        return False
    if metadata.get("follow_through") is not None:
        from app.core.memory.plan_reports import report_current

        return report_current(metadata["follow_through"], store)
    return True


def receipt_sources_current(value, store):
    if value is None:
        return True
    if not isinstance(value, dict) or value.get("version") != 1 or not value.get("sources"):
        return False
    from app.core.concepts.concept_support import revision_hash

    try:
        conn = store._get_conn()
        for source in value["sources"]:
            row = conn.execute("SELECT role, content, session_id FROM messages WHERE id = ?",
                               (source["message_id"],)).fetchone()
            if (row is None or source["revision"] != revision_hash(list(row))
                    or source["quote"] not in row[1]):
                return False
    except (AttributeError, KeyError, TypeError, ValueError, sqlite3.Error):
        return False
    return True
