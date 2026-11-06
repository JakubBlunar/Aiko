"""Conservative lexical backstops; source citation alone is not entailment."""
import re

from app.core.infra import timephrase


RULE = """
Source fidelity:
- Include basis: reported or inferred. A paraphrase of explicit testimony is
  reported. An inferred conclusion must be visibly tentative in content.
- A stated plan followed by 'I am back' reports returning, not proof that every
  planned step was completed. Store only the supported part; do not fill gaps.
- Do not add often/always/usually/repeatedly to a single episode or turn a
  situational request into an enduring preference. Preserve conditions and negation.
- event_time requires event_time_evidence:{message_id,quote}: a literal temporal
  phrase in a NEW owner's message. Message timestamps are recording times, never
  invented event times. Leave event_time null when occurrence time is unknown.
  The server resolves quoted timing against that message. A day is not an exact clock.
"""
_COMPLETE = re.compile(r"\b(?:completed?|finished?|submitted|accomplished|fulfilled)\b", re.I)
_RECUR = re.compile(r"\b(?:always|usually|often|repeatedly|routinely|regularly|every day)\b", re.I)
_CONDITION = re.compile(r"\b(?:when|while|during|unless|except)\b", re.I)
_NONACTUAL = re.compile(r"\b(?:not|never|didn't|haven't|would|could|might|will|if|plan|need to)\b",
                        re.I)


def refusal(content, quotes, basis):
    if not isinstance(basis, str) or basis not in {"reported", "inferred"}:
        return "invalid_claim_basis"
    if basis == "inferred" and not re.search(
        r"\b(?:may|might|seems?|appears?|possibly|possible|perhaps)\b", content, re.I,
    ):
        return "unhedged_inference"
    if _RECUR.search(content) and not any(_RECUR.search(quote) for quote in quotes):
        return "unsupported_frequency"
    if (_CONDITION.search(" ".join(quotes)) and not _CONDITION.search(content)
            and basis == "reported"):
        return "lost_condition"
    if _COMPLETE.search(content):
        actual = [q for q in quotes if not _NONACTUAL.search(q)]
        stated = any(_COMPLETE.search(q) or re.search(r"\b(?:done|all set)\b", q, re.I)
                     for q in actual)
        # Regular past-tense action can support 'completed': brushed -> brushing.
        past = {word[:-2].lower() for q in actual for word in re.findall(r"\b\w{5,}ed\b", q)}
        action = any(re.search(r"\b" + re.escape(root) + r"(?:ing|ed)?\b", content, re.I)
                     for root in past if len(root) > 3)
        if not stated and not action:
            return "unreported_completion"
    return None


def event_support(candidate, sources):
    """Unknown event time stays unknown; precision comes from a literal phrase."""
    from app.core.memory.promise_evidence import supported_deadline

    result = {"event_time": None, "precision": "unknown"}
    value = candidate.get("event_time_evidence")
    if not isinstance(value, dict) or type(value.get("message_id")) is not int:
        return result
    row = sources.get(value["message_id"])
    if row is None:
        return result
    quote = value.get("quote")
    when, precision = supported_deadline(quote, row.content, timephrase.parse_iso(row.created_at))
    if when is None:
        return result
    if precision == "day" and candidate.get("temporal_type") != "future_plan":
        when = when.replace(hour=0, minute=0)
    result.update(event_time=when.isoformat(), precision=precision,
                  message_id=row.id, quote=quote, source_recorded_at=str(row.created_at))
    return result
