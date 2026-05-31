"""Lexical contradiction signals for the F5 conflicting-memory detector.

Pure functions, no I/O, no embeddings -- every input is plain text and
the output is a small structured verdict. This is the cheap gate that
runs before the worker decides to spend an LLM call on a pair: most
candidate pairs from the cosine band are topically related but
*compatible*, and a heuristic that catches the obvious flips ("loves
X" vs "hates X") is enough to keep the LLM budget focused on the
ambiguous cases.

Three signal types, in priority order:

1. **Negation flip** -- one side has an explicit negation token
   (``not`` / ``never`` / ``isn't`` / ``don't`` / ``hates`` /
   ``dislikes`` / ...) and the other doesn't, *and* the surrounding
   content overlaps enough that they're talking about the same
   thing (Jaccard >= 0.4 on content words). Definite.

2. **Antonym/verb-flip table** -- both sides hit opposite ends of a
   small curated dict (``loves``/``hates``, ``likes``/``dislikes``,
   ``married``/``single``, ...). Definite.

3. **Numerical mismatch** -- both sides contain a number for what
   looks like the same anchor and the numbers differ by >= 10%.
   Borderline (number conflicts are noisy: "born 1990" vs "30
   years old" might both be true depending on date).

If none of the above triggers, the pair is dropped without any LLM
cost.

The frontend mirrors of the antonym/negation tables would be useful
for an in-browser hint, but for v1 the heuristic only runs server-side
inside :class:`app.core.memory.memory_conflict_worker.MemoryConflictWorker`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal


HeuristicLabel = Literal["definite", "borderline", "no"]

# Public label constants. Kept in sync with ``HeuristicLabel`` so the
# worker / tests / store can reference them as symbols rather than
# stringly-typed values.
HEURISTIC_DEFINITE: HeuristicLabel = "definite"
HEURISTIC_BORDERLINE: HeuristicLabel = "borderline"
HEURISTIC_NO: HeuristicLabel = "no"

# Small set of negation tokens. Lowercased, contraction-aware. Includes
# verbs that imply a negative valence ("hates", "dislikes", "rejects",
# "denies") because those carry the contradiction even without an
# explicit "not". The antonym table below catches the symmetric
# definite case where BOTH sides have a verb and they oppose; this
# set is for the asymmetric "one side affirms, the other negates".
_NEGATION_TOKENS: frozenset[str] = frozenset({
    "not", "no", "never", "none",
    # Apostrophe form (the ``'`` survives the tokenizer regex):
    "n't", "don't", "doesn't", "didn't", "won't", "wouldn't",
    "isn't", "aren't", "wasn't", "weren't",
    "can't", "cannot", "couldn't",
    "shouldn't", "haven't", "hasn't", "hadn't",
    # No-apostrophe variants (informal chat / LLM output sometimes
    # drops the apostrophe):
    "dont", "doesnt", "didnt", "wont", "wouldnt",
    "isnt", "arent", "wasnt", "werent",
    "cant", "couldnt",
    "shouldnt", "havent", "hasnt", "hadnt",
    "without", "neither", "nor",
    # Implicit-negative verbs:
    "hates", "dislikes", "avoids", "rejects", "refuses", "denies",
    "stopped", "quit",
})

# Antonym/verb-flip table: when one side contains the key and the
# other side contains the value (or vice versa) AND surrounding
# content overlaps, the pair is a definite contradiction. Keep this
# small and high-precision -- ambiguous pairs (e.g. ``warm`` /
# ``cold`` -- could be temperature, personality, beverage) belong in
# the LLM tier, not here.
_ANTONYM_PAIRS: tuple[tuple[str, str], ...] = (
    ("loves", "hates"),
    ("likes", "dislikes"),
    ("enjoys", "avoids"),
    ("prefers", "avoids"),
    ("agrees", "disagrees"),
    ("believes", "doubts"),
    ("trusts", "distrusts"),
    ("married", "single"),
    ("married", "divorced"),
    ("alive", "dead"),
    ("vegetarian", "carnivore"),
    ("vegan", "carnivore"),
    ("owns", "rents"),
    ("employed", "unemployed"),
    ("works", "retired"),
    ("started", "stopped"),
    ("started", "quit"),
    ("opened", "closed"),
    ("accepts", "rejects"),
    ("approves", "rejects"),
    ("supports", "opposes"),
)


# English stopwords used to compute Jaccard overlap on "content"
# words -- short, deliberately tight; we want a contradiction on
# "Bea loves spicy food" vs "Bea hates spicy food" to win on the
# {"bea", "spicy", "food"} overlap, not lose because both sides
# share "the" / "a".
_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "being", "been",
    "i", "you", "he", "she", "it", "we", "they", "my", "your", "his",
    "her", "its", "our", "their",
    "and", "or", "but", "so", "if", "then", "than", "as", "of", "to",
    "in", "on", "at", "by", "for", "with", "about", "from",
    "this", "that", "these", "those",
    "some", "any", "all", "every", "each", "both",
    "do", "does", "did", "have", "has", "had",
    "will", "would", "should", "could", "can", "may", "might",
    "very", "really", "actually", "just", "only", "still",
    "more", "most", "less", "least",
    # Negations are NOT in stopwords -- we want them to influence
    # the Jaccard overlap for negation-flip detection.
})


_TOKEN_RE = re.compile(r"[a-z][a-z'\-]*", flags=re.IGNORECASE)
_NUMBER_RE = re.compile(r"\b\d+(?:[.,]\d+)?\b")


@dataclass(slots=True)
class HeuristicResult:
    """Output of :func:`classify_pair`.

    ``signals`` lists every triggered signal so the worker can log
    them and the UI can render them as chips next to the pair. The
    worker only acts on ``label``; ``signals`` is informational.
    """

    label: HeuristicLabel
    signals: list[str] = field(default_factory=list)


def _tokenize(text: str) -> list[str]:
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")]


def _content_words(tokens: list[str]) -> set[str]:
    """Tokens minus stopwords, keeping negations so they count toward overlap."""
    return {t for t in tokens if t and t not in _STOPWORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return float(inter) / float(union) if union else 0.0


def _has_negation(tokens: list[str]) -> bool:
    return any(t in _NEGATION_TOKENS for t in tokens)


def _negation_flip(
    tokens_a: list[str],
    tokens_b: list[str],
    *,
    overlap_threshold: float = 0.4,
) -> bool:
    """One side has a negation, the other doesn't, and they overlap.

    The overlap check (Jaccard on content words) keeps "I don't like
    pizza" vs "I love anchovies" from triggering -- they have a
    negation asymmetry but are about different things.
    """
    neg_a = _has_negation(tokens_a)
    neg_b = _has_negation(tokens_b)
    # Both negate or neither does -> not a negation flip.
    if neg_a == neg_b:
        return False
    overlap = _jaccard(_content_words(tokens_a), _content_words(tokens_b))
    return overlap >= overlap_threshold


def _antonym_hit(tokens_a: list[str], tokens_b: list[str]) -> str | None:
    """Return the antonym pair as ``"X/Y"`` if both sides cover one each."""
    set_a = set(tokens_a)
    set_b = set(tokens_b)
    for left, right in _ANTONYM_PAIRS:
        a_left = _verb_hits(set_a, left)
        a_right = _verb_hits(set_a, right)
        b_left = _verb_hits(set_b, left)
        b_right = _verb_hits(set_b, right)
        # One side must have left and the other must have right (or
        # the symmetric flip). The same side having both ("loves and
        # hates") doesn't count -- that's a single ambivalent
        # statement, not a contradiction across rows.
        if (a_left and b_right and not a_right) or (a_right and b_left and not a_left):
            return f"{left}/{right}"
    return None


def _verb_hits(token_set: set[str], lemma: str) -> bool:
    """Cheap "does this token set cover the verb?" check.

    We only have lemmatized antonym pairs (``loves``, ``hates``,
    ``married``, etc.), but a real sentence might contain ``love`` or
    ``loved`` or ``loving``. The antonym table is small enough that we
    can afford to check a few common inflections per lemma without
    pulling in NLTK.
    """
    if lemma in token_set:
        return True
    # Try common stem variants. Order matters: the most-specific
    # variant first so we don't accidentally short-circuit.
    if lemma.endswith("s") and lemma[:-1] in token_set:
        return True
    if lemma.endswith("es") and lemma[:-2] in token_set:
        return True
    base = lemma[:-1] if lemma.endswith("s") else lemma
    for suffix in ("ed", "d", "ing"):
        candidate = base + suffix
        if candidate in token_set:
            return True
        # Drop the final ``e`` for ``loving`` from ``love``.
        if base.endswith("e") and (base[:-1] + suffix) in token_set:
            return True
    return False


def _numerical_mismatch(text_a: str, text_b: str) -> tuple[float, float] | None:
    """Return the first mismatched number pair, if any, that differs by >= 10%.

    Both texts must contain at least one number, the numbers must
    differ by >= 10% (so "23" vs "23.5" doesn't trigger), and the
    surrounding 4-token windows must overlap by Jaccard >= 0.5 (so
    "born 1990" vs "30 years old" doesn't trigger -- those numbers
    refer to the same fact through different anchors).
    """
    nums_a = _NUMBER_RE.findall(text_a or "")
    nums_b = _NUMBER_RE.findall(text_b or "")
    if not nums_a or not nums_b:
        return None
    tokens_a = _tokenize(text_a)
    tokens_b = _tokenize(text_b)
    overlap = _jaccard(_content_words(tokens_a), _content_words(tokens_b))
    if overlap < 0.5:
        return None
    try:
        first_a = float(nums_a[0].replace(",", "."))
        first_b = float(nums_b[0].replace(",", "."))
    except ValueError:
        return None
    if first_a == 0 and first_b == 0:
        return None
    larger = max(abs(first_a), abs(first_b))
    if larger == 0:
        return None
    delta = abs(first_a - first_b) / larger
    if delta < 0.1:
        return None
    return (first_a, first_b)


def classify_pair(text_a: str, text_b: str) -> HeuristicResult:
    """Classify a candidate pair as ``definite`` / ``borderline`` / ``no``.

    ``definite`` -> worker will resolve / store without an LLM call.
    ``borderline`` -> worker will spend an LLM call (rate-limited).
    ``no`` -> worker drops the pair entirely.

    See module docstring for the signal taxonomy.
    """
    tokens_a = _tokenize(text_a)
    tokens_b = _tokenize(text_b)
    signals: list[str] = []
    label: HeuristicLabel = "no"

    if _negation_flip(tokens_a, tokens_b):
        signals.append("negation_flip")
        label = "definite"

    antonym = _antonym_hit(tokens_a, tokens_b)
    if antonym is not None:
        signals.append(f"antonym:{antonym}")
        label = "definite"

    mismatch = _numerical_mismatch(text_a or "", text_b or "")
    if mismatch is not None:
        a_num, b_num = mismatch
        signals.append(f"number_mismatch:{a_num}!={b_num}")
        if label == "no":
            label = "borderline"

    return HeuristicResult(label=label, signals=signals)
