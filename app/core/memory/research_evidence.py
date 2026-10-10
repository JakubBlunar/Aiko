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
    supported = [index for index in indices if index < len(evidence)]
    urls = list(dict.fromkeys(row["url"] for row in evidence if row["url"]))
    # A legacy completion without citations must not attribute its conclusion
    # to the first of several unrelated search results.
    primary = evidence[supported[0]]["url"] if supported else (
        urls[0] if len(evidence) == 1 and urls else ""
    )
    return {
        "evidence_type": "search_excerpt",
        "research_evidence": evidence,
        "supporting_source_indices": supported,
        "evidence_support": "attributed" if supported else "unattributed",
        "source_url": primary,
        "source_urls": urls,
        "retrieved_at": now.isoformat(),
    }
