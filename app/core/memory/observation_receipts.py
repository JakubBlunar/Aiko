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
turns from the memory's actual owner can supply an action or outcome. An earlier
offered turn from that same owner may supply context or an exception when a new
action or outcome explicitly names the same subject and is cited in evidence.
Bare assent cannot supply that link. Keep the full qualifier in content too.
Keep the earlier need and the new report separate. Earlier context does not establish
that the new action or outcome happened under those conditions.
Context says when/where the preference or action
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
_LINK_STOP = frozenset(
    "also always during exactly except helped helping only really sometimes that thanks when "
    "while would worked absolutely agreed".split()
)


def admit_observation(
    value, rows, *, session_key, subject, context_rows=(), evidence=(), content="",
):
    from app.core.memory.shared_shorthand import source_receipt

    if not isinstance(value, dict) or set(value) - {"context", "action", "outcome", "exceptions"}:
        raise ValueError("observation shape")
    owner = "assistant" if subject == "assistant" else "user"
    offered = {row.id: row for row in rows if row.session_id == session_key and row.role == owner}
    earlier = {row.id: row for row in context_rows
               if row.session_id == session_key and row.role == owner and row.id not in offered}
    sources = []

    def citation(item, *, qualifier=False):
        if not isinstance(item, dict) or type(item.get("message_id")) is not int:
            raise ValueError("observation citation")
        mid = item["message_id"]
        row = offered.get(mid) or (earlier.get(mid) if qualifier else None)
        quote = item.get("quote")
        if (row is None or not isinstance(quote, str) or not 4 <= len(quote) <= 240
                or quote not in row.content or _FICTION.search(row.content) or "?" in quote):
            raise ValueError("unsupported observation facet")
        receipt = {**source_receipt(row, quote),
                   "source_at": str(getattr(row, "created_at", "") or "")}
        if mid in earlier:
            receipt["context_only"] = True
        sources.append(receipt)
        return receipt

    result = {"version": 1, "subject": "aiko" if owner == "assistant" else "user",
              "scope": "reported_facets", "recorded_at": timephrase.utcnow().isoformat()}
    for field in ("context", "action", "outcome"):
        if value.get(field) is None:
            continue
        receipt = citation(value[field], qualifier=field == "context")
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
        receipt = citation(item, qualifier=True)
        if not _EXCEPTION.search(receipt["quote"]):
            raise ValueError("exception qualifier missing")
        result["exceptions"].append(receipt)
    if not sources:
        return None
    qualifiers = [receipt for receipt in sources if receipt["message_id"] in earlier]
    if qualifiers:
        from app.core.memory.learned_strategies import words

        anchors = [anchor for field in ("action", "outcome") if (anchor := result.get(field))
                   and any(item["message_id"] == anchor["message_id"]
                           and anchor["quote"] in item["quote"] for item in evidence)]
        for qualifier in qualifiers:
            terms = {word for word in words(qualifier["quote"])
                     if len(word) >= 4 and word not in _LINK_STOP}
            if not any(anchor["message_id"] > qualifier["message_id"]
                       and terms.intersection(words(anchor["quote"])) for anchor in anchors):
                raise ValueError("earlier qualifier lacks a fresh matching report")
        if not _CONDITION.search(content) or (
            any(receipt["message_id"] in earlier for receipt in result["exceptions"])
            and not _EXCEPTION.search(content)
        ):
            raise ValueError("earlier qualifier lost in content")
        result["earlier_context"] = True
    result["sources"] = list({json.dumps(row, sort_keys=True): row for row in sources}.values())
    return result


def observation_context(memory):
    value = (getattr(memory, "metadata", None) or {}).get("observation")
    if not isinstance(value, dict) or value.get("version") != 1:
        return ""
    def quoted(receipt):
        result = {key: receipt.get(key) for key in ("message_id", "role", "source_at", "quote")}
        if receipt.get("context_only") is True:
            result["context_only"] = True
        return result

    facets = {key: ([quoted(row) for row in value[key]] if key == "exceptions"
                    else quoted(value[key]))
              for key in ("context", "action", "outcome", "exceptions") if value.get(key)}
    header = "Recorded source facets (quoted, not causal proof): "
    if value.get("earlier_context") is True:
        header = "Recorded source facets (context_only means earlier; keep it separate): "
    return header + json.dumps(
        facets, ensure_ascii=False, separators=(",", ":"),
    )


def earlier_observation_current(memory, store):
    """Before recalling an earlier qualifier, recheck both ends of the receipt."""
    meta = getattr(memory, "metadata", None) or {}
    value = meta.get("observation")
    if not isinstance(value, dict) or value.get("earlier_context") is not True:
        return True
    return receipt_sources_current(value, store) and receipt_sources_current(
        meta.get("claim_fidelity"), store,
    )


def facet_scope(memory):
    from app.core.memory.receipt_contracts import receipt_fields

    metadata = getattr(memory, "metadata", None) or {}
    existing = _ordinary_facet_scope(metadata)
    receipts = receipt_fields(metadata)
    return json.dumps([existing, receipts], sort_keys=True) if receipts else existing


def _ordinary_facet_scope(metadata):
    if metadata.get("moment_evidence"):
        value = metadata["moment_evidence"]
        return json.dumps({key: value.get(key) for key in
                           ("version", "event_scope", "when_basis", "precision", "sources")},
                          sort_keys=True)
    report = metadata.get("follow_through")
    if metadata.get("contextual_relation"):
        return json.dumps({key: metadata.get(key) for key in
                           ("observation", "follow_through", "contextual_relation")},
                          sort_keys=True)
    value = metadata.get("observation") or {}
    if not isinstance(value, dict) or value.get("version") != 1:
        return json.dumps(report, sort_keys=True) if report else None
    if report:
        return json.dumps({"observation": value, "follow_through": report}, sort_keys=True)
    return json.dumps({key: ([row.get("quote") for row in value[key]] if key == "exceptions"
                            else value[key].get("quote"))
                       for key in ("context", "action", "outcome", "exceptions") if value.get(key)},
                      sort_keys=True)


def longitudinal_sources_current(memory, store, _seen=None):
    """Verify preserved quotes against SQLite before using enriched evidence."""
    metadata = getattr(memory, "metadata", None) or {}
    answer = metadata.get("hypothesis_answer")
    if metadata.get("catchphrase_evidence") is not None:
        if not receipt_sources_current(metadata["catchphrase_evidence"], store):
            return False
    if isinstance(answer, dict) and answer.get("version") == 1:
        if not receipt_sources_current(answer, store):
            return False
    image = metadata.get("image_evidence")
    if isinstance(image, dict) and image.get("sources"):
        if not receipt_sources_current(image, store):
            return False
    if metadata.get("pursuit_evidence") is not None:
        from app.core.memory.pursuit_evidence import current

        if not current(metadata["pursuit_evidence"], store):
            return False
    support = metadata.get("hypothesis_support")
    if support is not None:
        from app.core.memory.reflective_evidence import current

        seen = set(_seen or ())
        mid = getattr(memory, "id", None)
        if (mid in seen or not isinstance(support, dict) or support.get("version") != 1
                or not isinstance(support.get("memory_inputs"), list)
                or not support["memory_inputs"]):
            return False
        # Support packets contain answer receipts, never recursively anchored
        # world hypotheses. This also makes cyclic/imported packets fail closed.
        for item in support.get("memory_inputs", []):
            if not isinstance(item, dict):
                return False
            source = store.get(item.get("memory_id"))
            meta = getattr(source, "metadata", None) or {}
            if (source is None or not meta.get("hypothesis_answer")
                    or meta.get("hypothesis_support")):
                return False
        if not current(support, store):
            return False
    if not receipt_sources_current(metadata.get("claim_fidelity"), store):
        return False
    if not receipt_sources_current(metadata.get("moment_evidence"), store):
        return False
    if not receipt_sources_current(metadata.get("observation"), store):
        return False
    if metadata.get("follow_through") is not None:
        from app.core.memory.plan_reports import report_current

        if not report_current(metadata["follow_through"], store, _seen):
            return False
    if metadata.get("contextual_relation") is not None:
        from app.core.memory.memory_relations import relation_current

        if not relation_current(metadata["contextual_relation"], store, _seen):
            return False
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
            row = conn.execute("SELECT role, content, session_id, created_at "
                               "FROM messages WHERE id = ?",
                               (source["message_id"],)).fetchone()
            if (row is None or source["revision"] != revision_hash(list(row[:3]))
                    or source["quote"] not in row[1]
                    or source.get("source_at") and source["source_at"] != str(row[3])):
                return False
    except (AttributeError, KeyError, TypeError, ValueError, sqlite3.Error):
        return False
    return True
