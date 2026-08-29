"""Dropped-sub-topic detector (K82 personality backlog).

Catches the moment a user message had two genuinely separable asks and
Aiko's just-finished reply covered only one of them, so she can circle
back once on the next turn ("also -- you asked about X").

This is a completeness cue, not a capability. Nothing else in the runtime
compares "what he asked" to "what the reply covered": K54 / agenda track
*her* threads, K95 only asks "was this a direct question?", and K96
``second_thought`` can ruminate about talking past something but is
LLM-drafted, rare, and off by default.

Detection is intentionally **embedding-free** (lexical only) and
**conservative**. The failure mode the backlog names is a companion who
itemises ordinary multi-clause messages like a support ticket, which is
worse than occasionally missing a point. v1 therefore:

  * splits on ``.!?`` plus light ``also`` / ``and also`` / ``;`` breaks,
    then **merges** adjacent fragments that share content words so
    "it was long and tiring and I need tea" stays one ask;
  * fires only when there are at least two separable asks **and** at
    least one of them is question-like (ends with ``?``, or a short
    request opener: ``can you`` / ``could you`` / ``what about``);
  * scores coverage against the **whole** reply -- a later sentence that
    picks an ask up counts;
  * names **one** skipped thing (the most question-like uncovered ask),
    never a numbered list.

The function is pure: it takes the user text plus the finished reply and
returns a :class:`DroppedTopicHit` or ``None``. The post-turn hook in
:class:`PostTurnMixin` decides whether to arm the cue.

K95's :func:`turn_shape.is_direct_question` stays a one-bool ceiling;
``extract_asks`` lives here so that reader is not grown into a multi-ask
parser.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.memory.conflict_heuristics import _content_words, _tokenize


# Snippet cap so the rendered cue line stays short.
_SNIPPET_CHARS = 80

# Sentence terminators keep the trailing mark on the fragment so
# question-likeness can still see a ``?``.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Light clause breaks the backlog names. ``and also`` is tried before
# ``also`` by the ``(?:and\s+)?`` optional.
_ALSO_SPLIT_RE = re.compile(r"\s+(?:and\s+)?also\s+", flags=re.IGNORECASE)

_SEMI_SPLIT_RE = re.compile(r"\s*;\s*")

# A compound "I went to the store and got milk, how was your day?" is
# two asks glued by a comma, not a period. Only split when the right
# side looks like a question / request, so "long, tiring, and I need
# tea" stays one fragment.
_COMMA_QUESTION_RE = re.compile(
    r",\s+(?=(?:how|what|why|when|where|who|can you|could you|"
    r"would you|will you|what about|how about)\b)",
    flags=re.IGNORECASE,
)

_REQUEST_OPENER_RE = re.compile(
    r"^(?:can you|could you|would you|will you|what about|how about)\b",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class DroppedTopicHit:
    """One detected missed ask from a multi-ask user turn."""

    skipped_ask: str
    covered_asks: tuple[str, ...]
    uncovered_asks: tuple[str, ...]
    all_asks: tuple[str, ...]


def is_question_like(text: str) -> bool:
    """True when the fragment is an explicit question or a short request.

    The conservative gate: two statements with no ask stay silent -- that
    is almost always one intent. ``?`` is the strong signal; the openers
    catch "can you grab tea" / "what about Friday" without a mark.
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped.endswith("?"):
        return True
    return bool(_REQUEST_OPENER_RE.search(stripped))


def extract_asks(user_text: str) -> list[str]:
    """Split a user message into separable asks, merging related fragments.

    Empty / whitespace-only input returns ``[]``. Fragments that survive
    splitting but have no content words are dropped.
    """
    text = (user_text or "").strip()
    if not text:
        return []
    fragments: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        for semi in _SEMI_SPLIT_RE.split(sentence):
            semi = semi.strip()
            if not semi:
                continue
            for also in _ALSO_SPLIT_RE.split(semi):
                also = also.strip()
                if not also:
                    continue
                for part in _COMMA_QUESTION_RE.split(also):
                    part = part.strip()
                    if part:
                        fragments.append(part)
    merged = _merge_related(fragments)
    return [frag for frag in merged if _content_words(_tokenize(frag))]


def detect_dropped_topic(
    user_text: str,
    assistant_text: str,
    *,
    min_asks: int = 2,
    min_overlap: int = 2,
    require_question: bool = True,
) -> DroppedTopicHit | None:
    """Return a hit when the reply clearly missed one separable ask.

    ``min_asks`` is the floor on how many asks the user message must
    contain (default 2 -- a single ask has nothing to drop). ``min_overlap``
    is the content-word overlap a longer ask needs against the whole
    reply to count as covered; short asks (1-2 content words after
    stopword stripping) need only one shared word so "how was your day?"
    is covered by "my day was quiet". ``require_question`` (default True)
    is the "at least one ask is question-like" gate.
    """
    asks = extract_asks(user_text)
    if len(asks) < max(2, int(min_asks)):
        return None
    if require_question and not any(is_question_like(ask) for ask in asks):
        return None

    reply_words = _content_words(_tokenize(assistant_text or ""))
    covered: list[str] = []
    uncovered: list[str] = []
    need = max(1, int(min_overlap))
    for ask in asks:
        if _is_covered(ask, reply_words, need):
            covered.append(ask)
        else:
            uncovered.append(ask)
    if not uncovered:
        return None

    skipped = _pick_skipped(uncovered)
    if not skipped:
        return None
    return DroppedTopicHit(
        skipped_ask=skipped,
        covered_asks=tuple(covered),
        uncovered_asks=tuple(uncovered),
        all_asks=tuple(asks),
    )


def render_cue(hit: DroppedTopicHit) -> str:
    """The prompt line for one hit, or ``""`` if it has nothing to say.

    Lives beside the detector rather than in the provider because the cue
    is composed when it is *armed*, one turn before it renders -- the
    same reason K38's ``render_cue`` moved out of the provider. The pool
    needs the text at write time.

    Names one skipped snippet, never a list of his points.
    """
    snippet = _snippet(hit.skipped_ask)
    if not snippet:
        return ""
    return (
        f'Heads-up: last turn they also asked about "{snippet}" and you '
        "skipped it. Circle back once, lightly -- 'also -- you asked "
        "about that' is enough. Don't recap the whole message or list "
        "their points."
    )


def _merge_related(fragments: list[str]) -> list[str]:
    """Collapse adjacent fragments that share content words into one ask.

    "The store was closed. The store had no milk." is one intent about
    the store, not two asks. Non-adjacent fragments are left alone --
    merging across a genuine second ask would hide the miss this
    detector exists to catch.
    """
    if not fragments:
        return []
    merged = [fragments[0]]
    for frag in fragments[1:]:
        prev_words = _content_words(_tokenize(merged[-1]))
        cur_words = _content_words(_tokenize(frag))
        if prev_words and cur_words and (prev_words & cur_words):
            merged[-1] = f"{merged[-1].rstrip()} {frag}".strip()
        else:
            merged.append(frag)
    return merged


def _is_covered(ask: str, reply_words: set[str], min_overlap: int) -> bool:
    ask_words = _content_words(_tokenize(ask))
    if not ask_words:
        return True
    overlap = len(ask_words & reply_words)
    # Short questions lose most of their tokens to stopwords ("how was
    # your day?" -> {how, day}). Requiring ``min_overlap`` of 2 would
    # miss a reply that clearly answered it ("my day was quiet").
    if len(ask_words) <= 2:
        need = 1
    else:
        need = min(min_overlap, len(ask_words))
    return overlap >= need


def _pick_skipped(uncovered: list[str]) -> str:
    """Most question-like uncovered ask; longest snippet as the tie-break."""
    if not uncovered:
        return ""
    ranked = sorted(
        uncovered,
        key=lambda ask: (is_question_like(ask), len(ask.strip())),
        reverse=True,
    )
    return ranked[0]


def _snippet(text: str) -> str:
    stripped = re.sub(r"\s+", " ", (text or "").strip())
    if len(stripped) <= _SNIPPET_CHARS:
        return stripped
    cut = stripped[:_SNIPPET_CHARS].rsplit(" ", 1)[0].rstrip(".,;:")
    return f"{cut}..."


__all__ = [
    "DroppedTopicHit",
    "detect_dropped_topic",
    "extract_asks",
    "is_question_like",
    "render_cue",
]
