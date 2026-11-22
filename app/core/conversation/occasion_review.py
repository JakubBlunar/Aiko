"""Read-only comparison of two reviewed accounts of a reported occasion."""

from dataclasses import dataclass

from app.core.memory.memory_origin import memory_origin
from app.core.memory.shared_shorthand import source_receipt


@dataclass(frozen=True)
class Account:
    memory_id: int
    topic_id: int
    message_id: int
    session_id: str
    recorded_at: str
    statement: str
    source_revision: str


@dataclass(frozen=True)
class OccasionReview:
    first: Account
    later: Account
    relation: str


def reported_account(memory, topic_id, db):
    """Use the user's source statement, with its recording date and topic."""
    if (
        memory is None or memory.temporal_type != "past_event"
        or memory.provenance != "stated" or memory.source_message_id is None
        or memory_origin(memory).subject != "user"
    ):
        return None
    row = db.get_message_row(memory.source_message_id)
    if row is None or row.role != "user" or row.session_id != memory.source_session:
        return None
    # This pilot reviews short complete turns so a clipped quotation can't
    # leave behind a negation, relationship marker or hypothetical qualifier.
    if not row.content.strip() or len(row.content) > 500:
        return None
    receipt = source_receipt(row, row.content)
    return Account(
        memory.id, topic_id, row.id, row.session_id, str(row.created_at), row.content,
        receipt["revision"],
    )


def review_pair(first, later, relation):
    """Capture an explicit reviewer verdict. Unresolved links need no receipt."""
    if (
        first is None or later is None or first.message_id >= later.message_id
        or first.topic_id == later.topic_id
        or first.session_id.partition(":")[0] != later.session_id.partition(":")[0]
        or relation not in {"same", "distinct"}
    ):
        raise ValueError(
            "Expected two chronological accounts across topics and a reviewed relation"
        )
    return OccasionReview(first, later, relation)


def compare_accounts(first, later, review=None):
    """Keep both current reports and count only the occasions resolved by review."""
    accounts = tuple(account for account in (first, later) if account is not None)
    verified = review is not None and (first, later) == (review.first, review.later)
    relation = review.relation if verified else "unresolved"
    occasion_count = {"same": 1, "distinct": 2}.get(relation)
    account_count = len({account.message_id for account in accounts})
    frequency_reply = None
    if account_count == 2:
        frequency_reply = {
            1: "Both reports describe one incident.",
            2: "The reports describe two separate incidents.",
            None: "I have two reports. I can't tell whether they describe one incident or two.",
        }[occasion_count]
    return {
        "accounts": accounts,
        "account_count": account_count,
        "occasion_count": occasion_count,
        "frequency_reply": frequency_reply,
        "relation": relation,
        "review_stale": review is not None and not verified,
    }
