"""Question/share balance (K47 personality backlog) — pure helpers.

Several workers and inner-life providers *push* questions ("maybe ask
{name}...", forward-curiosity, open-question follow-ups). The only
existing counterweight (the style-tracker ``question_saturation`` cue)
fires reactively after ~75% of recent turns end in "?". K47 is the
proactive complement: a cheap rolling ratio of Aiko's replies that
contain a question; once it crosses a threshold, the question-pushing
providers are suppressed for a couple of turns and a share-first cue is
injected *before* the next LLM call.

This module is intentionally dependency-free (no DB, no embedder, no
LLM) so the gate logic is trivially unit-testable. The per-session ring
+ suppress counter live on :class:`SessionController`; the wiring reads
these functions.
"""
from __future__ import annotations

from collections.abc import Iterable


def is_question_turn(text: str) -> bool:
    """True when an assistant reply contains a question.

    A "question-free" turn is the complement (no ``?`` at all), which is
    the unit the persona's "≥1/3 of turns question-free" goal is
    measured in. We deliberately count *any* ``?`` (not just a trailing
    one) so a reply that buries the ask mid-paragraph still registers as
    interviewing.
    """
    return "?" in (text or "")


def compute_ratio(flags: Iterable[bool]) -> float:
    """Fraction of recent turns that were question turns. 0.0 when empty."""
    items = list(flags)
    if not items:
        return 0.0
    return sum(1 for f in items if f) / len(items)


def should_suppress(
    flags: Iterable[bool],
    *,
    threshold: float,
    min_samples: int,
) -> bool:
    """Whether the question ratio warrants suppressing question-pushers.

    Requires at least ``min_samples`` observed turns (so a single
    question on turn one doesn't trip the gate) AND a ratio strictly
    above ``threshold``.
    """
    items = list(flags)
    if len(items) < max(1, min_samples):
        return False
    return compute_ratio(items) > threshold


SHARE_FIRST_CUE = (
    "Heads-up: your recent replies have leaned heavily on questions. "
    "This turn, share first \u2014 offer an observation, a small "
    "self-story, or your own take, and let it land without ending on a "
    "question. Curiosity can wait a beat; give {name} something of "
    "yours to react to."
)


def render_share_first_cue(user_name: str | None = None) -> str:
    name = (user_name or "them").strip() or "them"
    return SHARE_FIRST_CUE.format(name=name)


__all__ = [
    "is_question_turn",
    "compute_ratio",
    "should_suppress",
    "render_share_first_cue",
    "SHARE_FIRST_CUE",
]
