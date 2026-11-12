"""Source identity and deadline precision for reported commitments."""
import re

from app.core.infra import timephrase


def source_clause(quote, source):
    """Expand a literal citation to its surrounding sentence, including conditions."""
    source = " ".join(source.split())
    quote = " ".join(quote.split())
    start = source.find(quote)
    if start < 0:
        return ""
    before = max(source.rfind(mark, 0, start) for mark in ".!?") + 1
    end = start + len(quote)
    after = [pos + 1 for mark in ".!?" if (pos := source.find(mark, end)) >= 0]
    return source[before:min(after) if after else len(source)].strip()


def ownership_refusal(quote, source, *, allow_conditional=False):
    """A speaker citing somebody else's action has not committed to doing it.

    Inspect the sentence containing a shortened quote so ``call the dentist``
    can still be supported by ``I keep meaning to call the dentist``. This is
    a conservative English ownership check, not semantic promise verification.
    """
    context = source_clause(quote, source)
    if not context:
        return "missing_quote"
    if re.search(
        r"\b(?:i|we)\s+(?:(?:have|had|did|do)\s+not|haven['\u2019]t|hadn['\u2019]t|"
        r"didn['\u2019]t|don['\u2019]t|never)\s+(?:asked?|promis\w*|committ\w*|intend\w*|plan\w*)\b",
        context, re.I,
    ) and not re.search(r"\b(?:but|and)\s+(?:i|we)(?:['\u2019]ll|\s+will)\b", context, re.I):
        return "negated_not_commitment"
    # The ledger creates follow-up obligations and has no contract for checking
    # whether a condition holds. Ordinary memory can retain the arrangement.
    if not allow_conditional and re.search(
        r"\b(?:if|unless|provided that|as long as)\b", context, re.I,
    ):
        return "conditional_not_unconditional"
    if re.search(r"\b(?:i|we)\s+(?:can|could|might)\b|\b(?:shall|can|could)\s+i\b",
                 context, re.I):
        return "offer_not_commitment"
    if re.search(r"\byou\s+(?:(?:just|only)\s+)?(?:should|must|need to|have to)\b",
                 context, re.I):
        return "advice_not_commitment"
    owning_clause = re.split(r"\bbefore\s+i\b", context, flags=re.I)[0]
    first_person = r"\b(?:i|we)(?:['\u2019](?:ll|m|re|ve|d))?\b"
    if not re.search(first_person, owning_clause, re.I):
        return "no_owned_intent"
    # Anticipating another person's offer is not accepting an obligation.
    if re.search(r"\b(?:look(?:ing)? forward|can['\u2019]?t wait|would (?:be|love))\b",
                 context, re.I) and not re.search(
                     r"\b(?:i|we)(?:['\u2019]ll|\s+(?:will|promise|intend|plan|commit))\b",
                     context, re.I):
        return "wish_not_commitment"
    return ""


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
