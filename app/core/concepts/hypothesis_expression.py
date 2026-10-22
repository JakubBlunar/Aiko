"""Conservative, call-free receipts for a question about a particular hunch.

Whole-reply similarity is not an ask. Match only an answer-seeking span, and
leave unsupported paraphrases unverified rather than spending the sole ask.
The answer adjudicator still owns semantic interpretation with this span in view.
"""
from __future__ import annotations

import re

from app.core.memory.echo_detector import tokens

_REQUEST = re.compile(
    r"\?|\b(?:tell me (?:if|whether)|let me know (?:if|whether)|"
    r"(?:can|could|would) you (?:confirm|explain)|correct me if)\b", re.I,
)
_NON_REQUEST = re.compile(
    r"\b(?:he|she|they) (?:asked|asks|wondered)|\b(?:imagine|suppose|hypothetically)|"
    r"\b(?:you (?:asked|said)|i (?:asked|said))\b", re.I,
)
_GENERIC = frozenset({
    "aiko", "jacob", "user", "seem", "feel", "wonder", "tell", "know",
    "right", "correct", "confirm", "whether", "always", "never", "sometimes",
})


def subject_words(text: str) -> set[str]:
    """Small inflection tolerance, not a semantic entailment claim."""
    result = set()
    for word in tokens(text):
        for suffix in ("ing", "ed", "s"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                word = word[:-len(suffix)]
                break
        if word not in _GENERIC:
            result.add(word)
    return result


def question_span(belief: str, reply: str) -> str:
    """Return a grounded request verbatim; silence/ambiguity returns empty.

    No full-reply embedding is reused for a short question about something else.
    A deictic confirmation question may include its immediately preceding claim.
    Quoted/hypothetical questions cannot establish an ask receipt.
    """
    target = subject_words(belief)
    if not target:
        return ""
    spans = list(re.finditer(r"[^.!?\n]+[.!?]?", reply or ""))
    for index, match in enumerate(spans):
        text = match.group().strip()
        if not _REQUEST.search(text) or _NON_REQUEST.search(text):
            continue
        if any(mark in text for mark in ('"', '\u201c', '\u201d')):
            continue
        start = match.start()
        if re.fullmatch(r"(?:is|does) that (?:sound |seem )?(?:right|true|accurate)\?", text, re.I):
            if not index:
                continue
            start = spans[index - 1].start()
            text = reply[start:match.end()].strip()
            if _NON_REQUEST.search(text) or any(mark in text for mark in ('"', '\u201c', '\u201d')):
                continue
        overlap = target & subject_words(text)
        if len(overlap) >= min(2, len(target)) and len(text) <= 1000:
            return reply[start:match.end()].strip()
    return ""
