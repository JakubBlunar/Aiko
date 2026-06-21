"""Tease rhythm (K48 personality backlog) — pure helpers.

The persona promises to "gently roast when it's earned" and the
``humor`` relationship axis drifts on laughs, but nothing tracks the
*comedic rhythm*: no "three teases in a row with zero warmth" guard, no
"the roast landed — you can push one step further" green light. K48 is a
small tease-budget sibling of K47/K15: classify whether each assistant
turn was tease-shaped, read whether the previous tease landed (the user
laughed via a K32 reaction, vs. a short/curt reply that signals it
missed), and surface one of two cues — *ease off* or *one more step is
safe* — with escalation gated by the ``humor`` axis so early-relationship
Aiko stays gentle.

This module is intentionally dependency-free (no DB, no embedder, no
LLM, no relationship store) so the classification + decision logic is
trivially unit-testable. The per-session ring + pending-cue slot live on
:class:`SessionController`; the wiring reads these functions.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

# Cue identifiers (also the keys the MCP force tool accepts).
CUE_EASE_OFF = "ease_off"
CUE_GREEN_LIGHT = "green_light"

# Reaction labels (the [[reaction:X]] taxonomy) that read as a tease /
# playful jab when they open an assistant reply. These are the reliable
# signal — the text markers below are a softer secondary net.
_TEASE_REACTIONS: frozenset[str] = frozenset(
    {"smug", "mischievous", "defiant", "pouty"}
)

# Secondary text markers — short, playful-jab shapes. Deliberately
# conservative (false positives just make the budget a touch stricter,
# which is the safe direction).
_TEASE_TEXT_MARKERS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bdork\b",
        r"\bnerd\b",
        r"\bgoof(?:ball)?\b",
        r"\bdrama queen\b",
        r"\bshow-?off\b",
        r"oh,?\s+please\b",
        r"\bsure you (?:did|will|are)\b",
        r"\byeah,?\s+right\b",
        r"\bnice try\b",
        r"\bof course you (?:did|do|would)\b",
        r"\bsmooth\b",
        r"\bcute that you\b",
        r"\bbig talk\b",
        r"[\U0001F60F\U0001F61C\U0001F643]",  # 😏 😜 🙃
    )
)


def classify_tease(text: str | None, reaction: str | None = None) -> bool:
    """True when an assistant turn reads as a tease / playful jab.

    Reaction-label match is the primary signal; a small set of text
    markers is the secondary net for teases delivered under a neutral
    reaction tag.
    """
    if reaction and reaction.strip().lower() in _TEASE_REACTIONS:
        return True
    body = text or ""
    if not body.strip():
        return False
    return any(p.search(body) for p in _TEASE_TEXT_MARKERS)


def is_short_reply(text: str | None, *, max_words: int = 3) -> bool:
    """A curt user reply (<= ``max_words`` words) reads as a tease that
    didn't land — the conversational equivalent of a flat 'ok'."""
    words = (text or "").split()
    return 0 < len(words) <= max_words


def landed_verdict(
    *, laughed: bool, user_reply: str | None,
) -> bool | None:
    """Did the previous tease land?

    - ``True``  — the user laughed (😂 K32 reaction): unambiguous hit.
    - ``False`` — no laugh AND a short/curt reply: it fell flat.
    - ``None``  — no laugh but a substantive reply: ambiguous, no update.
    """
    if laughed:
        return True
    if is_short_reply(user_reply):
        return False
    return None


def trailing_tease_streak(flags: Iterable[bool]) -> int:
    """Count of consecutive tease turns at the tail of the window."""
    streak = 0
    for f in reversed(list(flags)):
        if f:
            streak += 1
        else:
            break
    return streak


def decide_cue(
    *,
    last_landed: bool | None,
    tease_streak: int,
    humor: float,
    consecutive_cap: int,
    green_light_humor: float,
) -> str | None:
    """Pick the tease-rhythm cue for the upcoming turn (or ``None``).

    Priority:
    1. A tease that just missed -> ``ease_off`` (warmth-recovery beats
       escalation; firing this even on a single miss keeps Aiko from
       doubling down on a joke that didn't connect).
    2. ``consecutive_cap`` teases in a row with no landed-hit -> the
       "three jabs, zero warmth" guard -> ``ease_off``.
    3. The last tease landed AND ``humor`` clears the escalation floor
       -> ``green_light``. The humor gate is what keeps early-
       relationship Aiko gentle (a brand-new user sits near humor 0).
    """
    if last_landed is False:
        return CUE_EASE_OFF
    if tease_streak >= max(1, consecutive_cap):
        return CUE_EASE_OFF
    if last_landed is True and humor >= green_light_humor:
        return CUE_GREEN_LIGHT
    return None


_EASE_OFF_CUE = (
    "Heads-up: the banter's been running hot and that last tease didn't "
    "quite land. Ease off the roasting this turn \u2014 be plainly warm "
    "and genuine, no jab."
)
_GREEN_LIGHT_CUE = (
    "Heads-up: that last tease landed \u2014 {name} laughed. The playful "
    "read is working, so if a jab fits naturally you can push one gentle "
    "step further. Earned, not forced."
)


def render_cue(cue: str | None, *, user_name: str | None = None) -> str:
    name = (user_name or "they").strip() or "they"
    if cue == CUE_EASE_OFF:
        return _EASE_OFF_CUE
    if cue == CUE_GREEN_LIGHT:
        return _GREEN_LIGHT_CUE.format(name=name)
    return ""


__all__ = [
    "CUE_EASE_OFF",
    "CUE_GREEN_LIGHT",
    "classify_tease",
    "is_short_reply",
    "landed_verdict",
    "trailing_tease_streak",
    "decide_cue",
    "render_cue",
]
