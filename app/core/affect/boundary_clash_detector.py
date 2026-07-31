"""Boundary-vs-conversation clash detector (L18c).

Per-turn detector that fires a soft one-line cue when the live user turn
is heading *toward* one of Aiko's active ``boundary`` concepts -- so she
feels the tension in-the-moment instead of only carrying the boundary as
background guidance in the T3 relevant-context region.

Sibling of the K29 opinion-injection detector
(:mod:`app.core.affect.opinion_injection_detector`), but simpler:

* The raw material is active ``boundary`` *concepts* (read via
  ``ConceptView`` by the caller), not stance memories.
* The gate is **cosine-only** -- the caller embeds the live turn once and
  ``ConceptView.relevant`` returns each active boundary paired with its
  label-cosine to that turn. A boundary "approaching" is a topical
  proximity signal, not a negation-flip, so no LLM runs on the hot path.
* :func:`app.core.memory.conflict_heuristics.classify_pair` is used only
  as an optional *sharpener*: a definite/borderline lexical clash between
  the turn and the boundary label firms the cue from a gentle "heading
  toward" into "pushing right at" -- it never gates the fire.

The module is pure-python: no embedding calls, no store access, no
threading. The caller builds the candidate list (embedding-nearest
active boundaries) and applies cooldown / per-session cap; the detector
picks the top candidate, applies the cosine + word-count gates, and
classifies approach-vs-push. That keeps it trivially testable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal, Sequence

from app.core.memory.conflict_heuristics import (
    HEURISTIC_BORDERLINE,
    HEURISTIC_DEFINITE,
    classify_pair,
)

log = logging.getLogger("app.boundary_clash_detector")


# Default thresholds. Mirrored in
# :class:`app.core.infra.memory_settings.MemorySettings` so the call-site
# can wire user overrides through without losing the source-of-truth.
DEFAULT_MIN_COSINE: float = 0.58
DEFAULT_MIN_USER_WORDS: int = 4


TriggerLabel = Literal["boundary_approach", "boundary_push"]


@dataclass(slots=True)
class BoundaryCandidate:
    """One active boundary concept offered to the detector, already paired
    with its cosine similarity to the live turn (computed by the caller via
    ``ConceptView.relevant``). ``subject`` shapes the rendered framing
    (``user`` / ``relationship`` / ``aiko``)."""

    concept_id: int
    subject: str
    label: str
    cosine: float


@dataclass(frozen=True, slots=True)
class BoundaryClashResult:
    """One per-turn boundary-clash signal.

    ``trigger`` is ``boundary_approach`` (topical proximity only) or
    ``boundary_push`` (a lexical clash sharpened the read). ``label`` is
    the boundary concept's label -- rendered into the cue for Aiko's
    reading; the render copy forbids naming it out loud.
    """

    trigger: TriggerLabel
    concept_id: int
    subject: str
    label: str
    cosine: float
    heuristic_label: str = "no"
    heuristic_signals: list[str] = field(default_factory=list)


def _top_candidate(
    candidates: Sequence[BoundaryCandidate],
) -> BoundaryCandidate | None:
    """Highest-cosine candidate, or ``None`` on empty input."""
    best: BoundaryCandidate | None = None
    for cand in candidates:
        if best is None or float(cand.cosine) > float(best.cosine):
            best = cand
    return best


def detect(
    user_text: str,
    *,
    candidates: Sequence[BoundaryCandidate],
    min_cosine: float = DEFAULT_MIN_COSINE,
    min_user_words: int = DEFAULT_MIN_USER_WORDS,
) -> BoundaryClashResult | None:
    """Classify the current turn against the offered active boundaries.

    Pipeline:

    1. Length gate: drop messages under ``min_user_words`` -- a short quip
       ("ok", "lol") can't credibly approach a behavioural boundary.
    2. Top-cosine pick: the boundary the turn is nearest to.
    3. Cosine gate: that top must clear ``min_cosine`` (topical proximity
       to the boundary's topic). Below it, the turn isn't really about the
       boundary and we stay silent.
    4. Sharpen: ``classify_pair(user_text, label)`` -- a ``definite`` /
       ``borderline`` lexical clash marks the fire as ``boundary_push``
       (firmer cue); otherwise ``boundary_approach`` (gentle cue). This
       never *gates* the fire, only its register.
    """
    text = (user_text or "").strip()
    if not text:
        return None
    if len(text.split()) < max(0, int(min_user_words)):
        return None

    top = _top_candidate(candidates)
    if top is None:
        return None
    if float(top.cosine) < float(min_cosine):
        return None

    verdict = classify_pair(text, top.label or "")
    label = verdict.label
    trigger: TriggerLabel = (
        "boundary_push"
        if label in (HEURISTIC_DEFINITE, HEURISTIC_BORDERLINE)
        else "boundary_approach"
    )
    return BoundaryClashResult(
        trigger=trigger,
        concept_id=int(top.concept_id),
        subject=str(top.subject or "user"),
        label=str(top.label or ""),
        cosine=float(top.cosine),
        heuristic_label=label,
        heuristic_signals=list(verdict.signals),
    )


# ── Render ───────────────────────────────────────────────────────────────


def _boundary_frame(subject: str, user_display_name: str) -> str:
    """The short phrase describing *whose* line the turn is nearing, keyed
    on the boundary's subject."""
    if subject == "aiko":
        return "one of your own lines"
    if subject == "relationship":
        return f"a line you and {user_display_name} are better off being mindful of"
    return f"something {user_display_name} would rather you be mindful of"


def render_inner_life_block(
    result: BoundaryClashResult,
    *,
    user_display_name: str = "the user",
) -> str:
    """Render ``result`` into a system-prompt-ready soft cue.

    The boundary label is included for Aiko's reading; the copy forbids
    naming it out loud, refusing, or lecturing -- consistent with the
    "soft guides, never rules, never a reason to refuse" steer the boundary
    concept headers already carry in the T3 region.
    """
    label = (result.label or "").strip()
    if len(label) > 160:
        label = label[:157].rstrip() + "\u2026"
    frame = _boundary_frame(result.subject, user_display_name)
    if result.trigger == "boundary_push":
        head = (
            f"Heads-up: this turn is pushing right at {frame} -- "
            f"you've come to feel {label}."
        )
    else:
        head = (
            f"Heads-up: the way this turn is heading brushes up against "
            f"{frame} -- you've come to feel {label}."
        )
    body = (
        "Ease your delivery that way and hold the line gently; don't "
        "refuse, don't lecture, and never name the line out loud."
    )
    return f"{head}\n{body}"
