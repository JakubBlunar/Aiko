"""Narrow moment citations and explicit event-time basis."""
from types import SimpleNamespace

from app.core.infra import timephrase
from app.core.memory.claim_fidelity import event_support, refusal
from app.core.memory.shared_shorthand import source_receipt
from app.core.memory.promise_evidence import source_clause


RULE = """
Also return claims:[{text:"one complete clause",evidence:[{message_id,quote}]}]
(1-3 clauses; quotes literal, <=400 chars) and event_scope:
exchange|recalled_event|shared_virtual. Only the supported clauses are stored.
Exchange describes the conversation itself (we laughed/discussed), not invented
physical actions. Shared virtual scenes remain virtual; they can be meaningful.
Recalled offline events require a user report. Never turn a plan into completion.
For a recalled event's time, optionally give event_time_evidence:{message_id,quote}
with a literal timing phrase in a supporting message. Unknown occurrence time is
allowed. Message timestamps date the exchange, not an earlier recalled event.
Advice, unaccepted offers and future intentions are not a shared action already
experienced. A citation must support the actor, action status and every condition
in the stored clause, not just contain related words.
"""


def admit(candidate, offered):
    data = candidate.evidence
    if not isinstance(data, dict):
        return False
    claims, scope = data.get("claims"), data.get("event_scope")
    if (not isinstance(claims, list) or not 1 <= len(claims) <= 3
            or not isinstance(scope, str) or scope not in {
                "exchange", "recalled_event", "shared_virtual"}):
        return False
    def get(row, key):
        return row.get(key) if isinstance(row, dict) else getattr(row, key, None)

    rows = {get(row, "id"): SimpleNamespace(**{key: get(row, key) for key in
            ("id", "role", "content", "created_at", "session_id")}) for row in offered
            if type(get(row, "id")) is int}
    receipts, texts, supported = {}, [], []
    for claim in claims:
        if not isinstance(claim, dict):
            return False
        text, citations = claim.get("text"), claim.get("evidence")
        if (not isinstance(text, str) or not 8 <= len(text) <= 400
                or timephrase.has_relative_deictic(text) or not isinstance(citations, list)
                or not 1 <= len(citations) <= 3):
            return False
        quotes = []
        for citation in citations:
            if not isinstance(citation, dict) or type(citation.get("message_id")) is not int:
                return False
            row = rows.get(citation["message_id"])
            quote = citation.get("quote")
            if (row is None or row.role not in {"user", "assistant"}
                    or not isinstance(quote, str) or not 4 <= len(quote) <= 400
                    or quote not in row.content):
                return False
            receipts[(row.id, quote)] = {**source_receipt(row, quote),
                                        "source_at": str(row.created_at or "")}
            supported.append(row)
            quotes.append(source_clause(quote, row.content))
        if refusal(text, quotes, "reported"):
            return False
        if scope == "recalled_event" and not any(
            rows[item["message_id"]].role == "user" for item in citations
        ):
            return False
        texts.append(text.strip())
    if scope == "recalled_event" and not any(row.role == "user" for row in supported):
        return False
    candidate.summary = " ".join(texts)
    if len(candidate.summary) > 600:
        return False
    candidate.source_message_ids = sorted({row.id for row in supported})
    candidate.source_scope = "validated_moment_claims"
    candidate.when = None
    basis, precision = "unknown_occurrence", "unknown"
    if scope in {"exchange", "shared_virtual"}:
        known = [timephrase.parse_iso(row.created_at) for row in supported]
        if known and all(stamp is not None for stamp in known):
            candidate.when = max(known).isoformat()
            basis, precision = "cited_exchange_time", "minute"
    else:
        timing = event_support({"temporal_type": "past_event",
            "event_time_evidence": data.get("event_time_evidence")},
            {row.id: row for row in supported if row.role == "user"})
        candidate.when = timing["event_time"]
        if candidate.when:
            basis, precision = "source_time_phrase", timing["precision"]
    if candidate.when and timephrase.parse_iso(candidate.when) > timephrase.utcnow():
        return False
    candidate.evidence = {"version": 1, "scope": "reported_moment_clauses",
        "claims": claims, "event_scope": scope, "when_basis": basis,
        "precision": precision, "sources": list(receipts.values())}
    return True
