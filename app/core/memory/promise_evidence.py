"""Source identity and deadline precision for reported commitments."""
import re

from app.core.infra import timephrase


def source_revision(source):
    from app.core.concepts.concept_support import revision_hash

    return revision_hash([source["role"], source["content"], str(source["created_at"])])


def supported_deadline(quote, source, anchor):
    """Resolve the quoted timing, never the model's independently invented clock."""
    if (not isinstance(quote, str) or not 3 <= len(quote) <= 100 or anchor is None
            or quote not in source or "?" in quote):
        return None, ""
    # A vague interval does not name a due day. Retain its quote without a clock.
    if re.search(r"\b(?:soon|sometime|eventually|weekend|next week|this week)\b", quote, re.I):
        return None, ""
    when = timephrase.parse_loose_datetime(quote, anchor=anchor, day_end=True)
    if when is None:
        return None, ""
    clock = bool(re.search(r"\b\d{1,2}:\d{2}\b|\b\d{1,2}\s*(?:am|pm)\b", quote, re.I))
    if not clock:
        when = when.replace(hour=23, minute=59, second=0, microsecond=0)
    return when, "minute" if clock else "day"


def same_source_actions(promise, memories, duplicate):
    """One source cannot mint new commitments through rewording or clock changes."""
    from app.core.memory.conflict_heuristics import _content_words, _tokenize
    from app.core.memory.promise_lifecycle import promise_what, is_assistant_promise

    words = _content_words(_tokenize(promise.text))
    for memory in memories:
        meta = memory.metadata or {}
        receipt = meta.get("commitment_source") or {}
        mid = getattr(memory, "source_message_id", None) or receipt.get("message_id")
        if (mid == promise.source_turn_id and mid is not None
                and is_assistant_promise(memory) == (promise.who == "assistant")
                and duplicate(words, [_content_words(_tokenize(promise_what(memory)))],
                              min_overlap=2)):
            return True
    return False
