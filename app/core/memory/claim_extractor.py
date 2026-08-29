"""Cheap regex-based factual-claim extractor (F1 personality backlog).

Used by :class:`IdleFactChecker` to decide whether a freshly-written
memory contains anything worth fact-checking against the web. The
heuristic is deliberately conservative — false negatives (claims we
skip) cost nothing, false positives (claims we send to the LLM for
distillation) cost a Lance + Ollama roundtrip each.

The four pattern classes we care about:
  - **year** — 4-digit years in the 19xx/20xx range. Most cheap-to-verify
    claims have a year ("Python 3.12 was released in 2023") and most
    hallucinations involve incorrect years.
  - **measurement** — numeric quantities with a unit suffix.
  - **date** — slash- or dash-separated calendar dates.
  - **proper_noun** — sequences of capitalised words (names of people,
    places, products). Picks up "Saturn V" / "Yosemite National Park".

``find_claims`` returns up to ``max_claims`` spans (default 3) per
memory so a single chatty observation can't enqueue dozens of checks.

**A span is a search query, not a claim.** Every pattern here matches a
sub-sentence token, so no span is ever a proposition — measured over the
live corpus, 0 of 90 extracted spans contained so much as a verb. Asking
a model to return support/contradict on ``"2026"`` or ``"The Rent"`` is
not a question, and a spurious ``contradict`` rewrites the memory. So
each candidate also carries the **enclosing sentence**, which is the
thing that actually asserts something, and a span whose sentence has no
predicate is not returned at all. Consumers verify ``sentence`` and
search with ``text``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


_CLAIM_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Years 19xx / 20xx. Bounded with word breaks so dates like
    # ``20/01/2024`` don't match twice; the date pattern handles those.
    (re.compile(r"\b(?:19|20)\d{2}\b"), "year"),
    # Measurements: number + (optional decimal) + unit. Whitelist the
    # common units we expect to see in casual chat; anything weirder
    # falls through.
    #
    # ``%`` sits outside the group that ends in ``\b``, and it has to.
    # ``\b`` is a word/non-word transition, so a trailing ``\b`` after a
    # percent sign requires the *next* character to be a word character
    # -- which means "94% of couples" and a sentence-final "94%" both
    # failed to match, and the only percentage this ever caught was the
    # malformed "50%and". Percentages are the most common measurement in
    # the corpus, so the one unit that could not match was the one that
    # mattered: of the four rows carrying a percentage, all four
    # extracted nothing. The letter units keep their ``\b`` (it is what
    # stops "5 mission" matching "5 mi").
    (
        re.compile(
            r"\b\d+(?:\.\d+)?\s*"
            r"(?:%|(?:km|miles|mi|kg|kgs|lbs|°C|°F|degrees|years|year|"
            r"days|day|hours|hour|minutes|minute|seconds|second|"
            r"gb|mb|tb|kb)\b)",
            flags=re.IGNORECASE,
        ),
        "measurement",
    ),
    # Dates: dd/mm or dd-mm with optional year.
    (
        re.compile(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b"),
        "date",
    ),
    # Proper-noun chains: 2-4 capitalised words in a row. We tolerate
    # the trailing noun being capitalised too because the chain pattern
    # is recursive on word boundaries.
    (
        re.compile(r"\b(?:[A-Z][a-z]+\s+){1,3}[A-Z][a-z]+\b"),
        "proper_noun",
    ),
]

_DEFAULT_MAX_CLAIMS = 3

# Sentence boundaries: terminator + whitespace. Deliberately naive --
# "Dr. Smith" splits wrongly, which costs us one over-short sentence and
# never a wrong verdict, because a fragment without a predicate is
# dropped by ``_has_predicate`` anyway.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# A sentence needs something asserted in it to be checkable. Copulas and
# auxiliaries cover most factual statements ("X is Y", "X was released
# in 1992"); the -ed / -s heuristics catch the rest without a POS
# tagger. This is a recall filter, not a parser -- it only has to be
# right often enough that we stop shipping bare noun phrases.
_PREDICATE_RE = re.compile(
    r"\b(?:is|are|was|were|be|been|being|has|have|had|does|did|"
    r"can|could|will|would|shall|should|may|might|must|"
    r"\w{3,}(?:ed|es|s))\b",
    flags=re.IGNORECASE,
)

# Minimum characters for a sentence to be worth verifying. Below this
# there is no context beyond the span itself.
_MIN_SENTENCE_CHARS = 20


def _has_predicate(sentence: str) -> bool:
    """True when ``sentence`` appears to assert something.

    Excludes the span-only case: a bare capitalised chain like
    ``"Magical Shopping Arcade Abenobashi"`` has no verb and is not a
    claim, however much it looks like a title.
    """
    return bool(_PREDICATE_RE.search(sentence or ""))


def _enclosing_sentence(source: str, start: int, end: int) -> str:
    """Return the sentence of ``source`` containing ``[start, end)``.

    Falls back to the whole string when splitting finds no boundary,
    which is the common case for the one-line memories this runs on.
    """
    cursor = 0
    for piece in _SENTENCE_SPLIT_RE.split(source):
        piece_start = source.find(piece, cursor)
        if piece_start < 0:
            continue
        piece_end = piece_start + len(piece)
        if piece_start <= start < piece_end:
            return piece.strip()
        cursor = piece_end
    return source.strip()


@dataclass(frozen=True)
class ClaimCandidate:
    """A single span identified as fact-checkable.

    ``text`` is the matched span -- a good *search query* and a useless
    *claim*. ``sentence`` is the enclosing sentence, which is what a
    verifier should actually be asked to adjudicate.
    """

    text: str
    kind: str  # one of "year" / "measurement" / "date" / "proper_noun"
    start: int
    end: int
    sentence: str = ""


def find_claims(text: str, *, max_claims: int = _DEFAULT_MAX_CLAIMS) -> list[ClaimCandidate]:
    """Return up to ``max_claims`` factual spans found in ``text``.

    Identical spans (same start/end) are deduped. Spans are returned in
    document order, each carrying the sentence that encloses it.

    A span whose sentence asserts nothing is **not returned**: there is
    no question to ask about it, and a verifier handed a bare noun
    phrase can only guess.
    """
    source = (text or "").strip()
    if not source:
        return []
    seen_spans: set[tuple[int, int]] = set()
    out: list[ClaimCandidate] = []
    for pattern, kind in _CLAIM_PATTERNS:
        for match in pattern.finditer(source):
            span = (match.start(), match.end())
            if span in seen_spans:
                continue
            seen_spans.add(span)
            sentence = _enclosing_sentence(source, match.start(), match.end())
            if len(sentence) < _MIN_SENTENCE_CHARS or not _has_predicate(sentence):
                continue
            out.append(
                ClaimCandidate(
                    text=match.group(0).strip(),
                    kind=kind,
                    start=match.start(),
                    end=match.end(),
                    sentence=sentence,
                )
            )
            if len(out) >= max_claims:
                # Return early in document order to keep behaviour
                # deterministic when the cap kicks in.
                out.sort(key=lambda c: c.start)
                return out
    out.sort(key=lambda c: c.start)
    return out
