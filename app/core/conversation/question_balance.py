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
import re


CURIOSITY_CUE_TYPES = frozenset({
    "curiosity_seed", "curiosity_gradient", "forward_curiosity",
    "knowledge_gap_notice", "concept_hypothesis", "dormant_interest",
})


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


_CLOSE = re.compile(
    r"^\s*(?:i(?:['’]ve| have)? already (?:answered(?: (?:that|this|it|your question))?|"
    r"told you)|i(?:['’]d| would) rather not(?: (?:say|talk about it|discuss it))?|"
    r"(?:(?:please|can we|let['’]s)\s+)?(?:drop (?:it|that)(?!\s+off\b)|"
    r"leave (?:it|that) (?:there|alone)|move on|change the subject|talk about something else))"
    r"\b(?:,? please)?", re.I,
)
_PIVOT = re.compile(
    r"(?:^|[.!?;,])\s*(anyway|on (?:another|a different) note|changing (?:the )?subject|"
    r"speaking of something else)\b", re.I,
)


def yields_curiosity(text: str) -> bool:
    return bool(_CLOSE.match(text or "") or _PIVOT.search(text or ""))


def closes_curiosity(text: str) -> bool:
    """A bare correction or refusal, with no new request to answer."""
    match = _CLOSE.match(text or "")
    return bool(match and not text[match.end():].strip(" .!?\t\r\n"))


def render_curiosity_pacing_cue(
    user_text: str = "", *, answered: bool = False, opens_on_question: bool = False,
) -> str:
    if closes_curiosity(user_text):
        return (
            "The user has closed your last question. Reply with just a brief acknowledgment "
            "in your own voice, such as 'Got it.' End the message immediately after that."
        )
    pivot = _PIVOT.search(user_text)
    if pivot:
        settled = "Your own question is settled. " if answered else ""
        return (
            f"Notice the handoff after '{pivot.group(1).lower()}'. {settled}"
            "The interesting thing here may be what just happened to somebody. "
            "Respond to that moment on its own if that's where their excitement is. "
            "Let them tell you the parts you don't know yet."
        )
    if opens_on_question and "?" not in user_text:
        return (
            "Listen to the turn as a whole. The thing they spend a second sentence on, "
            "especially why it matters to them, is often what they came to share. You "
            "can go straight there. Let your own judgment decide whether the first "
            "detail belongs in that moment."
        )
    if answered and "?" not in user_text:
        return (
            "The user just answered your question. Your whole reply is one brief "
            "statement acknowledging their answer. Finish on that statement."
        )
    return (
        "Curiosity pacing: you asked a question in your last reply. Stay with the answer "
        "or the subject they choose. If they answer in passing and then tell you news, "
        "which part would you care about if you were there with them? You can go straight "
        "to it. Let the earlier detail matter as much as it matters to their story. "
        "If they invite you to hear more, take them up on it."
    )


__all__ = [
    "CURIOSITY_CUE_TYPES",
    "is_question_turn",
    "compute_ratio",
    "should_suppress",
    "render_share_first_cue",
    "render_curiosity_pacing_cue",
    "SHARE_FIRST_CUE",
]
