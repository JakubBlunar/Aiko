"""L36: transfer an explicitly reported approach under matching conditions.

This is a conservative memory-backed pilot, not a calibrated success model.
Message receipts are revalidated at admission, queued write and retrieval.
"""
from __future__ import annotations

import re
from dataclasses import replace

from app.core.concepts.concept_support import revision_hash
from app.core.infra import timephrase
from app.core.memory.shared_shorthand import source_receipt, sources_current

EXTRACTION_RULE = """
L36 optional learned approaches: after an explicit USER report that a concrete
approach worked/helped/fixed the stated problem (or failed/didn't work), a memory
may have kind 'strategy' and a 'strategy' object:
{goal: citation, approach: citation, constraints: [citation], exceptions: [citation],
 outcome: {text: str, message_id: int, quote: str, success: bool}}.
Every citation is {text: str, message_id: int, quote: str}; text must be a literal
span of the exact source quote. Goal must name a specific objective, not engagement
or keeping someone talking. Require at least one explicit constraint, at most
three constraints and three exceptions. Do not invent exceptions. Approach may
come from either speaker; goal, constraints, exceptions and outcome must be USER
testimony. Outcome must be a concrete reported result, not enthusiasm, consent,
silence, an assistant prediction or a role-play event. Preserve a failed outcome;
do not infer the opposite approach works. Set revises_memory_id only when an
explicit user correction overturns an existing strategy listed in context;
then copy its goal/approach/conditions server-side and cite only the new outcome.
These entries share the existing memory count/output budget. Emit none when
the exchange lacks a reusable approach with an explicit outcome and conditions.
"""
_NONLITERAL = re.compile(
    r"\b(?:role.?play|hypothetical|pretend|imagine|fictional|would|might)\b", re.I,
)
_SUCCESS = re.compile(r"\b(?:worked|helped|fixed|solved|succeeded|successful)\b", re.I)
_FAILURE = re.compile(r"\b(?:failed|didn'?t work|did not work|didn'?t help|did not help)\b", re.I)
_STOP = frozenset("a an the to of in on for with and or i we you my our is it this that "
                  "want need please can could help how me get make do lets let's".split())


def words(text):
    return {w for w in re.findall(r"[a-z0-9]+", text.casefold()) if w not in _STOP}


def admit_strategy(candidate, rows, context_rows=(), *, session_key, owner, memories=None, db=None):
    value = candidate.get("strategy")
    if not isinstance(value, dict):
        return None
    offered = {row.id: row for row in [*context_rows, *rows] if row.session_id == session_key}
    sources = []

    def citation(item, *, user=True):
        if not isinstance(item, dict) or type(item.get("message_id")) is not int:
            raise ValueError("citation")
        row = offered.get(item["message_id"])
        text, quote = item.get("text"), item.get("quote")
        if (row is None or (user and row.role != "user")
                or row.role not in {"user", "assistant"}
                or not isinstance(text, str) or not 3 <= len(text) <= 160
                or not isinstance(quote, str) or not 3 <= len(quote) <= 700
                or text not in quote or quote not in row.content
                or _NONLITERAL.search(row.content) or "?" in quote):
            raise ValueError("unsupported field")
        sources.append(source_receipt(row, quote))
        return text, row

    try:
        outcome = value.get("outcome", {})
        result, result_row = citation(outcome)
        success = outcome.get("success")
        if (type(success) is not bool or result_row.id not in {r.id for r in rows}
                or (success and (not _SUCCESS.search(result)
                                 or re.search(r"\b(?:not|never|didn'?t)\b", result, re.I)))
                or (not success and not _FAILURE.search(result))):
            return None
        revises = value.get("revises_memory_id")
        if revises is not None:
            prior = memories.get(revises) if memories is not None and type(revises) is int else None
            old = (prior.metadata or {}).get("learned_strategy", {}) if prior else {}
            if (not old or old.get("owner") != owner or success or not sources_current(old, db)
                    or len(words(old.get("approach", "")) & words(result_row.content)) < 2):
                return None
            goal, approach = old["goal"], old["approach"]
            constraints, exceptions = old["constraints"], old["exceptions"]
            sources.extend(old["sources"])
        else:
            goal, _ = citation(value.get("goal"))
            approach, approach_row = citation(value.get("approach"), user=False)
            if approach_row.id >= result_row.id:
                return None
            constraints, exceptions = [], []
            for field, target in (("constraints", constraints), ("exceptions", exceptions)):
                items = value.get(field, [])
                if not isinstance(items, list) or len(items) > 3:
                    return None
                target.extend(citation(item)[0] for item in items)
        if (not constraints or len(words(goal)) < 2
                or sum(map(len, [goal, approach, result, *constraints, *exceptions])) > 500
                or re.search(r"\b(?:engagement|keep.{0,20}(?:talking|chatting))\b", goal, re.I)
                or timephrase.has_relative_deictic(
                    " ".join([goal, approach, *constraints, *exceptions]))):
            return None
        metadata = {"version": 1, "owner": owner, "goal": goal, "approach": approach,
                    "constraints": constraints, "exceptions": exceptions,
                    "outcome": result, "success": success, "sources": sources,
                    "outcome_message_id": result_row.id}
        metadata["key"] = revision_hash([sorted(words(goal)), sorted(words(approach)),
                                         sorted(constraints)])
        content = f"Approach for {goal}: {approach}; reported {'helpful' if success else 'failed'}."
        return {**candidate, "content": content, "kind": "strategy", "provenance": "stated",
                "source_message_id": result_row.id, "temporal_type": "durable",
                "metadata": {"learned_strategy": metadata,
                             "source_message_ids": sorted({s["message_id"] for s in sources}),
                             "admission_key": revision_hash(["strategy", owner, metadata])}}
    except (ValueError, TypeError, AttributeError, KeyError):
        return None


def strategy_hits(memories, db, rag, *, user_text, owner, goal_context=""):
    if memories is None or db is None or rag is None:
        return []
    rows = memories.list_recent(limit=256, kind="strategy")
    rows.sort(key=lambda row: int(row.source_message_id or 0), reverse=True)
    context = words(user_text + " " + goal_context)
    hits, seen = [], set()
    for row in rows:
        value = (row.metadata or {}).get("learned_strategy", {})
        key = value.get("key")
        if value.get("owner") != owner or not key or key in seen:
            continue
        seen.add(key)
        if not value.get("success") or not sources_current(value, db):
            continue
        if not words(value["goal"]) <= context:
            continue
        if any(not words(c) <= context for c in value["constraints"]):
            continue
        if any(words(e) & context for e in value["exceptions"]):
            continue
        if re.search(r"\b(?:not|no longer|stop)\b", user_text, re.I):
            continue
        hit = rag._memory_to_hit(row, score=.85)
        text = (f"Prior approach [memory:{row.id}, {row.created_at}]: for {value['goal']!r}, "
                f"under {value['constraints']!r}, {value['approach']!r} was reported helpful: "
                f"{value['outcome']!r}. Exceptions: {value['exceptions']!r}. "
                "Consider transfer only while these conditions hold; "
                "one outcome is not a guarantee.")
        hit.record = replace(hit.record, content=text)
        hit.memory_provenance = "validated_pattern"
        hits.append(hit)
        if len(hits) >= 1:
            break
    return hits


def fresh_goal_context(host, user_text):
    # Substantial new requests and explicit pivots must stand on their own.
    if len(user_text) > 120 or re.search(
        r"\b(?:instead|new task|different|actually)\b", user_text, re.I,
    ):
        return ""
    reader = getattr(host, "conversation_situation_snapshot", None)
    if not callable(reader):
        return ""
    snapshot = reader(user_text=user_text)
    state = getattr(snapshot, "inferred", None)
    working = getattr(state, "working_set", None)
    if (state is None or working is None or working.suspended
            or state.status != "active" or getattr(snapshot, "inferred_stale", True)
            or not getattr(snapshot, "world_compatible", False)):
        return ""
    return " ".join([working.question.text, *(c.text for c in working.constraints)])
