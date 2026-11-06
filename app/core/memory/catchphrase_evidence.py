"""Bounded phrase occurrences prove observed use, not original authorship."""
import re

from app.core.infra import timephrase
from app.core.memory.shared_shorthand import source_receipt


def literal_span(phrase, text):
    words = list(re.finditer(r"[\w'-]+", text))
    wanted = phrase.lower().split()
    for start in range(len(words) - len(wanted) + 1):
        part = words[start:start + len(wanted)]
        if [m.group().lower() for m in part] == wanted:
            return text[part[0].start():part[-1].end()]
    return ""


def occurrence(phrase, row):
    if row is None or type(getattr(row, "id", None)) is not int:
        return None
    span = literal_span(phrase, row.content)
    if not span or len(span) > 200:
        return None
    return {**source_receipt(row, span), "source_at": row.created_at}


def mined(candidate, messages):
    found = []
    counts = {"user": 0, "assistant": 0}
    for row in messages:
        role = row.role.lower()
        if role not in counts or counts[role] >= 6:
            continue
        source = occurrence(candidate.phrase, row)
        if source:
            counts[role] += 1
            found.append(source)
    if not found:
        return None
    return {"version": 1, "scope": "sampled_window_occurrences", "sources": found,
            "first_observed_speaker": candidate.first_speaker,
            "count": candidate.count, "user_count": candidate.user_count,
            "assistant_count": candidate.assistant_count,
            "window_message_ids": [r.id for r in messages if type(getattr(r, "id", None)) is int],
            "authorship": "unproven"}


def birth_sources(birth, db, echo_id, session_key):
    if not callable(getattr(db, "get_message_row", None)):
        return None
    origin, echo = db.get_message_row(birth.origin_message_id), db.get_message_row(echo_id)
    if (origin is None or echo is None or origin.role != "assistant" or echo.role != "user"
            or origin.session_id != echo.session_id or echo.session_id != session_key
            or origin.id >= echo.id):
        return None
    sources = [occurrence(birth.phrase, row) for row in (origin, echo)]
    if any(source is None for source in sources):
        return None
    return {"version": 1, "scope": "observed_assistant_echo_exchange", "sources": sources,
            "count": 2, "user_count": 1, "assistant_count": 1, "authorship": "unproven"}


def current(receipt, store):
    if receipt is None:
        return True
    from app.core.memory.observation_receipts import receipt_sources_current

    return receipt_sources_current(receipt, store)


def memory_current(memory, store):
    return current((getattr(memory, "metadata", None) or {}).get("catchphrase_evidence"), store)


def moment_receipt(receipt):
    sources = receipt["sources"]
    stamp = timephrase.parse_iso(sources[-1].get("source_at"))
    known = stamp is not None and stamp <= timephrase.utcnow()
    return (stamp.isoformat() if known else None), {
        "version": 1, "scope": "observed_phrase_exchange", "sources": sources,
        "event_scope": "exchange", "when_basis": (
            "cited_exchange_time" if known else "unknown_occurrence"),
        "precision": "minute" if known else "unknown",
    }
