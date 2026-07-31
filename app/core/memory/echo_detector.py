"""Did Aiko's reply actually use the thing that was handed to her? (F12)

Two consumers, one answer. The memory layer's `revival_score` and the L37
surfacing ledger both need to know whether a surfaced item was *used* by
the reply, and both previously asked it with their own copy of the same
keyword-overlap test. This module is the single place that decides.

The lexical test alone is badly biased
--------------------------------------
Tokenise the reply and the item, drop stopwords and anything under four
characters, and call it an echo when enough content words are shared.
That is strict enough to almost never produce a false positive, which
sounds good until you notice the consequence: nearly all of its errors
are *misses*. Aiko paraphrases constantly -- that is the entire reason to
hand a memory to a language model rather than pasting it -- and a
paraphrase shares no credit. "You mentioned wanting to get back into film
photography" against a stored "user shot 35mm in college and misses it"
is a perfect use of the memory and scores zero overlap.

So the memories that accumulate credit are the ones Aiko happens to
*quote*, not the ones she uses well.

Why the semantic test is weaker than it looks
---------------------------------------------
The obvious fix is cosine between the reply and the stored item, and both
vectors are already in hand (memory embeddings live on
:class:`~app.core.memory.memory_store.MemoryStore`'s in-process mirror,
and post-turn already embeds the reply for K22). But the discriminative
power is much lower than the raw numbers suggest, for a reason worth
stating plainly:

**The candidates were selected for topical similarity in the first
place.** A surfaced memory is one of the top-k nearest to the turn, and
the reply is about that same turn. So a high cosine is close to
guaranteed, and it mostly measures "this was on topic" rather than "she
used it". A floor picked by intuition would fire on almost everything.

That is why the verdict carries its `score` and `kind` rather than just a
boolean: the ledger records them, and the floor gets chosen from real
distributions later instead of guessed now. Until then the semantic
verdict is deliberately treated as *weaker evidence* than a lexical one
by its callers -- see `EchoKind`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("app.echo_detector")


# ``echo_kind`` values. An open enum, stored as text in the ledger.
ECHO_NONE = "none"
# The reply shares enough content words with the item -- it was very
# likely quoted or closely restated. Unambiguous, and the historical
# meaning of "revived", so callers may treat it as full-strength evidence.
ECHO_LEXICAL = "lexical"
# The reply is semantically close to the item but shares no words. Real
# evidence of use, but *weak*, because the item was retrieved for topical
# similarity to begin with (see the module docstring). Callers must treat
# this as a lesser signal than ``lexical`` until the floor is calibrated
# against recorded data.
ECHO_SEMANTIC = "semantic"


# Tiny stopword list scoped to the overlap check. We only need to suppress
# the most common "free" matches so a memory and a reply don't clear the
# threshold purely on filler. Not a full NLP pipeline -- the threshold
# itself does the heavy lifting.
STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "then", "so", "of",
    "in", "on", "at", "to", "for", "with", "by", "as", "is", "are",
    "was", "were", "be", "been", "being", "do", "does", "did", "have",
    "has", "had", "you", "your", "i", "me", "my", "we", "our", "us",
    "he", "she", "they", "them", "this", "that", "these", "those",
    "it", "its", "from", "about", "into", "than", "what", "when",
    "where", "who", "how", "why", "not", "no", "yes", "ok", "okay",
    "just", "really", "very", "much", "like", "would", "could",
    "should", "will", "can", "may", "might", "also", "too", "any",
    "all", "some", "more", "most", "less", "such", "there", "here",
    "now", "again", "still", "even", "only", "yet",
})

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'_-]+")


@dataclass(frozen=True, slots=True)
class EchoVerdict:
    """Whether the reply used an item, how it was decided, and how strongly.

    ``score`` is the overlap word count for a lexical verdict and the
    cosine for a semantic one -- deliberately not normalised into a single
    scale, because they are not commensurable and pretending otherwise is
    what would let a guessed threshold slip in. Read it together with
    ``kind``.
    """

    kind: str = ECHO_NONE
    score: float = 0.0

    @property
    def echoed(self) -> bool:
        return self.kind != ECHO_NONE

    @property
    def is_lexical(self) -> bool:
        return self.kind == ECHO_LEXICAL


def tokens(text: str) -> set[str]:
    """Lowercase content-word set used by the lexical overlap check.

    Tokens shorter than 4 chars and members of :data:`STOPWORDS` are
    dropped -- short and common words light up too many incidental
    overlaps to be useful as evidence of use.
    """
    if not text:
        return set()
    out: set[str] = set()
    for token in _TOKEN_RE.findall(str(text).lower()):
        token = token.strip("'-_")
        if len(token) < 4 or token in STOPWORDS:
            continue
        out.add(token)
    return out


def _cosine(reply_vec: Any, item_vec: Any) -> float | None:
    """Cosine of two (already unit-norm) vectors, or ``None`` if unusable.

    Mirrors the dot-product shortcut K22's callback detector already uses
    on the same vectors, rather than the general
    :func:`app.llm.embedder.cosine_similarity`, since both sides are
    normalised on write.
    """
    if reply_vec is None or item_vec is None:
        return None
    try:
        import numpy as np

        a = np.asarray(reply_vec, dtype=np.float32)
        b = np.asarray(item_vec, dtype=np.float32)
        if a.size == 0 or b.size == 0 or a.shape != b.shape:
            return None
        return float((a * b).sum())
    except Exception:
        return None


def detect(
    *,
    reply_tokens: set[str],
    item_text: str,
    min_overlap: int,
    reply_vec: Any = None,
    item_vec: Any = None,
    min_cosine: float | None = None,
) -> EchoVerdict:
    """Decide whether the reply echoed one item.

    Lexical first, because a quoted item is unambiguous and the test is
    cheap. The semantic fallback only runs when the lexical test misses
    *and* both vectors are present *and* ``min_cosine`` is set, so the
    caller enables it by supplying a floor rather than via a flag.

    Returns :data:`ECHO_NONE` with a zero score when nothing matched;
    callers that need to distinguish "no echo" from "could not look"
    should check whether they had the item's text or vector at all.
    """
    item_tokens = tokens(item_text)
    if reply_tokens and item_tokens:
        overlap = len(reply_tokens & item_tokens)
        if overlap >= max(1, int(min_overlap)):
            return EchoVerdict(ECHO_LEXICAL, float(overlap))

    if min_cosine is None:
        return EchoVerdict()
    similarity = _cosine(reply_vec, item_vec)
    if similarity is None:
        return EchoVerdict()
    if similarity >= float(min_cosine):
        return EchoVerdict(ECHO_SEMANTIC, similarity)
    # A sub-floor cosine is still worth reporting as the *strength* of a
    # miss: it is exactly the distribution the deferred full-credit
    # decision needs, and a floor cannot be calibrated from verdicts that
    # discard their near misses.
    return EchoVerdict(ECHO_NONE, similarity)


__all__ = [
    "ECHO_LEXICAL",
    "ECHO_NONE",
    "ECHO_SEMANTIC",
    "STOPWORDS",
    "EchoVerdict",
    "detect",
    "tokens",
]
