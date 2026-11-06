"""Bounded provenance shared by idle and conversation-owned research."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any


def researchable_question(content: str, metadata: dict[str, Any]) -> bool:
    purpose = metadata.get("curiosity_purpose")
    if purpose in {"ask_user", "reflection"}:
        return False
    # Older conversational follow-ups predate the purpose field.
    return not bool(re.match(r"^\s*maybe\s+ask\b", content, re.IGNORECASE))


def source_indices(value: Any) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(dict.fromkeys(
        index for index in value
        if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < 4
    ))


def evidence_metadata(
    snippets: list[dict[str, str]],
    indices: tuple[int, ...],
    now: datetime,
) -> dict[str, Any]:
    evidence = [
        {
            "title": str(row.get("title") or "")[:120],
            "url": str(row.get("url") or "")[:2048],
            "snippet": str(row.get("snippet") or "")[:400],
        }
        for row in snippets[:4]
    ]
    supported = [index for index in source_indices(list(indices)) if index < len(evidence)]
    urls = list(dict.fromkeys(row["url"] for row in evidence if row["url"]))
    # A legacy completion without citations must not attribute its conclusion
    # to the first of several unrelated search results.
    support_urls = list(dict.fromkeys(evidence[i]["url"] for i in supported if evidence[i]["url"]))
    primary = support_urls[0] if support_urls else ""
    return {
        "evidence_type": "search_excerpt",
        "research_evidence": evidence,
        "supporting_source_indices": supported,
        "evidence_support": "attributed" if supported else "unattributed",
        "source_url": primary,
        "source_urls": support_urls,
        "available_source_urls": urls,
        "retrieved_at": now.isoformat(),
    }


def supporting_urls(metadata):
    """None is the legacy contract; [] means no attributed excerpt support."""
    rows = metadata.get("research_evidence")
    if not isinstance(rows, list):
        return None
    if metadata.get("evidence_support") != "attributed":
        return []
    indices = source_indices(metadata.get("supporting_source_indices"))
    return list(dict.fromkeys(str(rows[i].get("url") or "") for i in indices
                             if i < len(rows) and isinstance(rows[i], dict)))


def question_revision(question):
    from app.core.concepts.concept_support import memory_revision, revision_hash

    return revision_hash([memory_revision(question), {
        key: (question.metadata or {}).get(key) for key in (
            "curiosity_purpose", "curiosity_resolved_at", "research_origin",
            "hobby_key", "hobby_started_at",
        )}])


def question_current(question, store):
    current = store.get(question.id)
    return (current is not None and current.kind == "open_question"
            and question_revision(current) == question_revision(question)
            and not (current.metadata or {}).get("curiosity_resolved_at")
            and researchable_question(current.content, current.metadata or {}))
