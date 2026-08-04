"""User-correction detector (F13 personality backlog).

Catches the moment the *user explicitly corrects Aiko* about a fact --
"no, it's my sister, not my brother", "I never said that", "actually it's
Tuesday". This is the contradiction family's missing fourth corner:

  * F5  (:mod:`app.core.memory.memory_conflict_worker`) -- two *stored*
    memories contradict each other.
  * K29 (opinion injection) -- Aiko's stored stance vs the *user's* claim.
  * K38 (:mod:`app.core.conversation.self_correction_detector`) -- Aiko's
    just-spoken *reply* vs her own stored fact.
  * F13 (here) -- the *user* correcting a stored note Aiko surfaced.

The user's correction is the highest-quality supervisory signal the system
will ever get, so the point of F13 is that the correction should
*supersede* the note it corrects, not sit beside it at equal confidence.

This module is only the cheap first stage: a pure, embedding-free function
that runs on the turn path, decides whether the user's message *looks like*
a correction of one of the candidate memories, and returns the single
strongest candidate pair. Precision here is intentionally loose because the
expensive, irreversible half -- the LLM confirmation and the memory
rewrite -- happens off the turn path in
:class:`app.core.memory.user_correction_worker.UserCorrectionWorker`, which
is the real gate against a false positive overwriting a true memory.

Two-part gate, both required:

1. An **explicit correction marker** in the user's text ("not X, Y",
   "I never said", "actually it's", ", not my ..."). Discourse markers do
   the work :func:`~app.core.memory.conflict_heuristics.classify_pair` cannot:
   "no, it's my sister not my brother" barely lexically overlaps the note
   "Jacob's brother plays guitar", so the shared heuristic alone would miss
   it. Deliberately excludes *disagreement* markers ("I disagree", "I don't
   think that's right") -- correction of fact, not opinion, which is K29's
   lane.
2. A **content-word overlap** with a candidate memory, so the marker is
   attached to something in particular rather than firing on a bare "no".

Only durable first-person claim kinds (``fact`` / ``preference`` /
``relationship`` / ``event``) are eligible targets; Aiko's own persona /
stance rows (``self`` / ``self_tagged``) are never correctable this way.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.core.memory.conflict_heuristics import (
    HEURISTIC_BORDERLINE,
    HEURISTIC_DEFINITE,
    _content_words,
    _tokenize,
    classify_pair,
)

log = logging.getLogger("app.user_correction_detector")


# Durable-truth kinds a user correction can target. Mirrors the F5
# conflict detector's allow-list -- process / journal kinds are not
# factual claims, and ``self`` / ``self_tagged`` are Aiko's own
# persona notes, which a user does not "correct" (that would be K29
# opinion territory, not a fact rewrite).
_ALLOWED_KINDS: frozenset[str] = frozenset({
    "fact",
    "preference",
    "relationship",
    "event",
})

_MIN_TEXT_CHARS = 6
_SNIPPET_CHARS = 120

_LABEL_RANK = {HEURISTIC_DEFINITE: 2, HEURISTIC_BORDERLINE: 1}

# Explicit correction / repair markers. Precision-first, and pointedly
# NOT disagreement of opinion ("I disagree", "I don't think so") -- those
# must not trigger a memory rewrite. Each is a standalone signal that the
# user is repairing a factual claim.
_CORRECTION_MARKERS: tuple[re.Pattern[str], ...] = (
    # "not X, (it's) Y" / "it's my sister, not my brother"
    re.compile(r",\s*not\s+(?:my|a|an|the|his|her|your|their)\b", re.IGNORECASE),
    re.compile(r"\bnot\s+\w+[\w\s]*?,\s*(?:it'?s|but|it\s+is)\b", re.IGNORECASE),
    re.compile(r"\bit'?s\s+not\b", re.IGNORECASE),
    re.compile(r"\bthat'?s\s+not\s+(?:right|correct|true|it|what)\b", re.IGNORECASE),
    re.compile(r"\bthat'?s\s+wrong\b", re.IGNORECASE),
    re.compile(r"\bno,?\s+it'?s\b", re.IGNORECASE),
    re.compile(r"\bno,?\s+(?:my|his|her|your|their|the)\b", re.IGNORECASE),
    re.compile(r"\bi\s+never\s+said\b", re.IGNORECASE),
    re.compile(r"\bi\s+did\s?n'?t\s+say\b", re.IGNORECASE),
    re.compile(r"\bactually,?\s+it'?s\b", re.IGNORECASE),
    re.compile(r"\bi\s+meant\b", re.IGNORECASE),
    re.compile(r"\byou'?re\s+wrong\b", re.IGNORECASE),
    re.compile(r"\byou\s+(?:got|have|had)\s+(?:it|that)\s+wrong\b", re.IGNORECASE),
    re.compile(r"\bwrong,?\s+it'?s\b", re.IGNORECASE),
    re.compile(r"\bcorrection[:,]\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class CorrectionHit:
    """One candidate user-correction-vs-memory pair, before confirmation.

    ``label`` / ``signals`` come from the shared F5 heuristic and may be
    ``"no"`` -- a marker plus content overlap is enough to be a candidate,
    because the worker's LLM pass is what actually decides whether to
    rewrite anything.
    """

    user_snippet: str
    memory_id: int
    memory_content: str
    kind: str
    marker: str
    overlap: int
    label: str
    signals: tuple[str, ...] = field(default_factory=tuple)


def _snippet(text: str) -> str:
    s = (text or "").strip()
    if len(s) <= _SNIPPET_CHARS:
        return s
    return s[: _SNIPPET_CHARS - 1].rsplit(" ", 1)[0] + "\u2026"


def _matched_marker(text: str) -> str | None:
    for pattern in _CORRECTION_MARKERS:
        m = pattern.search(text or "")
        if m is not None:
            return m.group(0).strip()
    return None


def detect_user_correction(
    user_text: str,
    memories: Sequence[Any],
    *,
    min_confidence: float = 0.4,
    min_overlap: int = 2,
    max_candidates: int = 50,
) -> CorrectionHit | None:
    """Return the strongest user-correction candidate pair, or ``None``.

    ``memories`` is any sequence of objects exposing ``id`` / ``content``
    / ``kind`` / ``confidence`` -- normally the rows surfaced on the
    previous turn, since a correction is a response to something Aiko just
    said. Two gates, both required: the message carries an explicit
    correction marker, and it shares at least ``min_overlap`` content words
    with a candidate memory. The shared F5 heuristic only ranks the result;
    a ``"no"`` verdict does not disqualify a candidate.
    """
    text = (user_text or "").strip()
    if len(text) < _MIN_TEXT_CHARS:
        return None
    marker = _matched_marker(text)
    if marker is None:
        return None

    sent_words = _content_words(_tokenize(text))
    if not sent_words:
        return None

    pool = [
        m
        for m in memories
        if str(getattr(m, "kind", "")).strip().lower() in _ALLOWED_KINDS
        and float(getattr(m, "confidence", 0.0)) >= min_confidence
        and (getattr(m, "content", "") or "").strip()
    ]
    # Highest confidence first so the cap keeps the strongest anchors --
    # the note the user is most likely correcting is one she stated
    # confidently enough to have surfaced.
    pool.sort(key=lambda m: float(getattr(m, "confidence", 0.0)), reverse=True)

    best: CorrectionHit | None = None
    best_key: tuple[int, int] = (0, 0)
    for mem in pool[: max(1, int(max_candidates))]:
        mem_words = _content_words(_tokenize(getattr(mem, "content", "")))
        overlap = len(sent_words & mem_words)
        if overlap < min_overlap:
            continue
        result = classify_pair(text, getattr(mem, "content", ""))
        rank = _LABEL_RANK.get(result.label, 0)
        # Rank prefers a heuristic-confirmed contradiction, then the
        # highest overlap; but a bare marker+overlap pair (rank 0) is
        # still a valid candidate for the worker to confirm.
        key = (rank, overlap)
        if key > best_key or best is None:
            best_key = key
            best = CorrectionHit(
                user_snippet=_snippet(text),
                memory_id=int(getattr(mem, "id", 0)),
                memory_content=(getattr(mem, "content", "") or "").strip(),
                kind=str(getattr(mem, "kind", "")).strip().lower(),
                marker=marker,
                overlap=overlap,
                label=result.label,
                signals=tuple(result.signals),
            )
    return best


def render_cue(*, wrong: str, corrected: str, user_display_name: str = "") -> str:
    """The next-turn acknowledgment line, or ``""`` if it has nothing to say.

    Composed once the correction is settled (in the worker, after the LLM
    confirms the corrected fact), for the same reason K38 composes its cue
    at arm time: the pool needs the text and the subject complete at write
    time, and re-deriving either at render would split the decision.

    The subject the pool matches against is the *corrected* fact, not the
    wrong one -- Aiko is likely to quote the old version while owning the
    slip ("I had you down as..."), so matching on it would score a hit for
    repeating the mistake.
    """
    wrong_s = (wrong or "").strip()
    corrected_s = (corrected or "").strip()
    if not corrected_s:
        return ""
    who = (user_display_name or "").strip() or "they"
    had = f'you had noted "{wrong_s}", but ' if wrong_s else ""
    return (
        f"Heads-up: {who} just corrected you -- {had}"
        f'it\'s actually "{corrected_s}". Own it once, naturally -- '
        "'ah, I had that backwards' -- never a grovel, then move on."
    )


__all__ = ["CorrectionHit", "detect_user_correction", "render_cue"]
