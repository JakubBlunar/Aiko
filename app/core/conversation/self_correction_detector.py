"""Self-correction detector (K38 personality backlog).

Catches the moment Aiko's just-finished reply contradicts one of her
own high-confidence ``fact`` / ``preference`` memories, so she can own
the slip naturally on her NEXT turn ("oh wait -- earlier I said X,
that's not right, it's actually Y").

This is the missing third corner of the contradiction family:

  * F5 (:mod:`app.core.memory.memory_conflict_worker`) finds two
    *stored* memories that contradict each other.
  * K29 (opinion injection) finds Aiko's stored stance vs the *user's*
    claim.
  * K38 (here) finds Aiko's just-spoken *reply* contradicting her own
    stored fact.

Detection is intentionally **embedding-free** (lexical only): a
content-word overlap shortlist picks candidate memories per sentence,
then the shared F5 contradiction heuristic
(:func:`app.core.memory.conflict_heuristics.classify_pair`) decides
whether the sentence and the memory actually clash. No per-sentence
embed call, so it adds no mid-stream latency. The embedding-based,
higher-recall same-reply variant is tracked as a separate backlog item
(K41).

The function is pure: it takes the reply text plus a list of
memory-like objects and returns the single strongest contradiction hit
(``definite`` outranks ``borderline``) or ``None``. The post-turn hook
in :class:`PostTurnMixin` decides whether to arm the cue.
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

log = logging.getLogger("app.self_correction_detector")


# Memory kinds eligible to "correct toward". Only durable first-person
# claims -- process / journal kinds (reflection, goal, callback, ...)
# are not factual statements Aiko could contradict.
_ALLOWED_KINDS: frozenset[str] = frozenset({"fact", "preference"})

# Sentences shorter than this are skipped -- too little signal for a
# meaningful contradiction check, and they're usually interjections
# ("oh!", "hm.") rather than factual claims.
_MIN_SENTENCE_CHARS = 12

# Snippet cap so the rendered cue line stays short.
_SNIPPET_CHARS = 100

_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+(?:\s+|$)")

_LABEL_RANK = {HEURISTIC_DEFINITE: 2, HEURISTIC_BORDERLINE: 1}


@dataclass(frozen=True)
class SelfCorrectionHit:
    """One detected reply-vs-memory contradiction."""

    reply_snippet: str
    memory_id: int
    memory_content: str
    label: str
    overlap: int
    signals: tuple[str, ...] = field(default_factory=tuple)


def _split_sentences(text: str) -> list[str]:
    out: list[str] = []
    for piece in _SENTENCE_SPLIT_RE.split(text or ""):
        s = piece.strip()
        if len(s) >= _MIN_SENTENCE_CHARS:
            out.append(s)
    return out


def _snippet(text: str) -> str:
    s = (text or "").strip()
    if len(s) <= _SNIPPET_CHARS:
        return s
    return s[: _SNIPPET_CHARS - 1].rsplit(" ", 1)[0] + "\u2026"


def detect_self_correction(
    assistant_text: str,
    memories: Sequence[Any],
    *,
    min_confidence: float = 0.6,
    min_overlap: int = 2,
    max_candidates: int = 50,
) -> SelfCorrectionHit | None:
    """Return the strongest reply-vs-memory contradiction, or ``None``.

    ``memories`` is any sequence of objects exposing ``id`` / ``content``
    / ``kind`` / ``confidence``. Only ``fact`` / ``preference`` rows at
    or above ``min_confidence`` are considered; a sentence and a memory
    must share at least ``min_overlap`` content words before the
    contradiction heuristic is run.
    """
    sentences = _split_sentences(assistant_text)
    if not sentences:
        return None

    # Build the candidate pool once: allow-listed kind, confident enough,
    # non-empty content. Highest confidence first so the cap keeps the
    # strongest anchors.
    candidates: list[tuple[Any, set[str]]] = []
    pool = [
        m
        for m in memories
        if str(getattr(m, "kind", "")).strip().lower() in _ALLOWED_KINDS
        and float(getattr(m, "confidence", 0.0)) >= min_confidence
        and (getattr(m, "content", "") or "").strip()
    ]
    pool.sort(key=lambda m: float(getattr(m, "confidence", 0.0)), reverse=True)
    for mem in pool[: max(1, int(max_candidates))]:
        words = _content_words(_tokenize(getattr(mem, "content", "")))
        if words:
            candidates.append((mem, words))
    if not candidates:
        return None

    best: SelfCorrectionHit | None = None
    best_key: tuple[int, int] = (0, 0)
    for sentence in sentences:
        sent_words = _content_words(_tokenize(sentence))
        if not sent_words:
            continue
        for mem, mem_words in candidates:
            overlap = len(sent_words & mem_words)
            if overlap < min_overlap:
                continue
            result = classify_pair(sentence, getattr(mem, "content", ""))
            rank = _LABEL_RANK.get(result.label, 0)
            if rank == 0:
                continue
            key = (rank, overlap)
            if key > best_key:
                best_key = key
                best = SelfCorrectionHit(
                    reply_snippet=_snippet(sentence),
                    memory_id=int(getattr(mem, "id", 0)),
                    memory_content=(getattr(mem, "content", "") or "").strip(),
                    label=result.label,
                    overlap=overlap,
                    signals=tuple(result.signals),
                )
    return best


__all__ = ["SelfCorrectionHit", "detect_self_correction"]
