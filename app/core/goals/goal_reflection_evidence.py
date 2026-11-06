"""Goal-local evidence and novelty checks, without a second model call."""
from collections import Counter
import json
import re

import numpy as np

from app.core.concepts.concept_support import revision_hash
from app.core.infra import timephrase
from app.core.memory.learned_strategies import words
from app.core.memory.memory_origin import memory_origin
from app.core.memory.reflective_evidence import capture, revision
from app.core.memory.observation_receipts import longitudinal_sources_current


def context(goal, goal_store, session_key=None):
    store = goal_store._memory_store
    wanted = words((goal.metadata or {}).get("summary") or goal.content)
    recent = store.list_recent(limit=96)
    selected = [row for row in goal_store.list_progress(goal.id)[:4]
                if (row.metadata or {}).get("source") != "worker"]
    for row in recent:
        if row.id == goal.id or row.kind in {
            "goal", "goal_progress", "reflection", "diary", "topic_digest", "pre_thought",
            "open_question", "knowledge_gap", "callback",
        }:
            continue
        source_session = getattr(row, "source_session", None)
        if (session_key and source_session and ":" in session_key and ":" in source_session
                and session_key.split(":", 1)[0] != source_session.split(":", 1)[0]):
            continue
        if not longitudinal_sources_current(row, store):
            continue
        lexical = len(wanted & words(row.content)) >= 2
        vectors = (getattr(goal, "embedding", None), getattr(row, "embedding", None))
        similarity = 0.
        if all(v is not None for v in vectors):
            a, b = (np.asarray(v).reshape(-1) for v in vectors)
            if a.size == b.size and np.linalg.norm(a) * np.linalg.norm(b):
                similarity = float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b)))
        if lexical or similarity >= .55:
            selected.append(row)
    text, receipt, offered = capture(selected, limit=6, max_chars=4500)
    receipt["goal"] = {"memory_id": goal.id, "revision": revision(goal)}
    fingerprint = revision_hash([receipt, goal.content, (goal.metadata or {}).get("summary")])
    return text, receipt, offered, fingerprint


def parse(raw):
    try:
        value = json.loads(re.search(r"\{.*\}", raw, re.S).group())
        if not isinstance(value, dict) or not isinstance(value.get("note"), str):
            return None
        kind = value.get("note_kind", "reflection")
        ids = value.get("evidence_memory_ids", [])
        if (not isinstance(kind, str) or kind not in {
            "reflection", "intention", "reported_step", "reported_outcome"}
                or not isinstance(ids, list) or len(ids) > 4
                or any(type(mid) is not int for mid in ids)):
            return None
        return {"note": value["note"].strip(), "note_kind": kind, "evidence_memory_ids": ids}
    except (AttributeError, TypeError, ValueError):
        return None


def admissible(draft, offered):
    ids = draft["evidence_memory_ids"]
    if any(mid not in offered for mid in ids):
        return False
    if draft["note_kind"] in {"reported_step", "reported_outcome"}:
        if not ids:
            return False
        for mid in ids:
            row = offered[mid]
            if (row.temporal_type == "future_plan" or memory_origin(row).mode not in {
                "observation", "declaration", "virtual_event"}):
                return False
            if draft["note_kind"] == "reported_outcome" and not (
                (row.metadata or {}).get("observation", {}).get("outcome")
                or (row.metadata or {}).get("follow_through", {}).get("status")
                == "reported_completed"
            ):
                return False
    return True


def repetitive(note, rows):
    from app.core.memory.conflict_heuristics import classify_pair

    def tokens(text):
        return Counter(re.findall(r"[a-z]+", text.casefold()))

    a = tokens(note)
    for row in rows:
        if classify_pair(note, row.content).label != "no":
            continue
        def conditions(text):
            return re.findall(r"\b(?:when|while|during|unless|except)\b[^.!?;]*",
                              text.casefold())

        if conditions(note) != conditions(row.content):
            continue
        b = tokens(row.content)
        common = sum((a & b).values())
        if a == b or common / max(1, max(sum(a.values()), sum(b.values()))) >= .78:
            return True
        x, y = words(note), words(row.content)
        if len(x) >= 4 and len(y) >= 4 and len(x & y) / max(1, len(x | y)) >= .7:
            return True
    return False


def assessed(goal_store, goal, fingerprint, outcome):
    goal_store._memory_store.update(goal.id, metadata={
        "reflection_assessment": {"version": 1, "fingerprint": fingerprint, "outcome": outcome},
        "last_reflected_at": timephrase.utcnow().isoformat(),
    }, metadata_merge=True)
