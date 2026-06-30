"""K46 — stance persistence (don't cave on taste pushback).

The system actively *teaches* Aiko to fold: K20 calibration drops her
trust score (and renders a "soften your claims" hedge cue) whenever the
user double-checks her, and K29 opinion injection fires her stance once
then sits out a long cooldown. Net effect on a *preference* the user
mildly questions ("really? you don't like horror?"): she states the take
once, then the very next mild push reads to K20 as "I might be wrong"
and she hedges — the signature chatbot-agreeability tell.

K46 draws the missing line between **taste and facts**. Pushback on a
*fact* should raise hedging (K20 is right there). Pushback on a
*preference* should not — you don't stop disliking horror because
someone said "really??". When Aiko has just stated a taste (a K29 cue
fired in the last few turns) and the user's reply is a *mild* pushback
(not a strong correction), K46:

1. surfaces a "hold your take" cue (rendered here), and
2. shields the K20 calibration from a factual-trust hit on that turn
   (the caller skips ``apply_signal`` — the "preference axis" that stops
   the two detectors fighting).

A *strong* correction ("no, that's wrong", "let me check") is left to
K20 untouched — that's a factual signal even mid-taste-talk.

Pure + dependency-free: the caller classifies the pushback band (reusing
the K20 :mod:`app.core.affect.calibration_detector` regex) and tells K46
whether a stance is recent; this module only owns the predicate and the
cue copy.
"""
from __future__ import annotations

from dataclasses import dataclass


# The one band that K46 acts on. A mild pushback right after a taste
# statement is a taste disagreement; a strong correction is a factual
# signal and stays K20's job.
MILD_BAND = "pushback_mild"

# Cap on the stored stance snippet rendered into the cue, so a long
# self-memory can't bloat the prompt line.
STANCE_SNIPPET_MAXLEN = 160


@dataclass(slots=True, frozen=True)
class StanceVerdict:
    """Result of the K46 gate. ``hold`` true means "render the cue AND
    shield calibration this turn"; ``reason`` is a diagnostic label."""

    hold: bool
    reason: str


def evaluate(*, recent_stance: bool, pushback_band: str | None) -> StanceVerdict:
    """Decide whether the live turn is a taste pushback worth holding.

    Fires only when Aiko has a *recent* stance on the table AND the
    user's message is a *mild* pushback. Everything else (no recent
    stance, a strong correction, an affirmation, a softening, or no
    signal at all) returns ``hold=False``.
    """
    band = (pushback_band or "").strip()
    if not recent_stance:
        return StanceVerdict(False, "no_recent_stance")
    if band != MILD_BAND:
        return StanceVerdict(False, f"band:{band or 'none'}")
    return StanceVerdict(True, "mild_taste_pushback")


def _snippet(stance_text: str) -> str:
    text = (stance_text or "").strip()
    if len(text) > STANCE_SNIPPET_MAXLEN:
        return text[: STANCE_SNIPPET_MAXLEN - 1].rstrip() + "\u2026"
    return text


def render_block(stance_text: str, *, user_display_name: str = "the user") -> str:
    """Render the "hold your take" cue, or ``""`` when there's nothing
    to anchor on.

    The stored stance is quoted for Aiko's reading (so she knows which
    take to hold); the persona block forbids echoing it back. The cue
    deliberately frames the distinction — taste, not a fact being
    corrected — so the LLM doesn't over-generalise into stubbornness on
    everything.
    """
    name = (user_display_name or "the user").strip() or "the user"
    snippet = _snippet(stance_text)
    anchor = f" (you noted: '{snippet}')" if snippet else ""
    return (
        f"Heads-up: {name} is pushing back a little on a taste you just "
        f"shared{anchor} — but this is your *preference*, not a fact he's "
        "correcting. One easy restatement is plenty; you don't need to "
        "hedge, walk it back, or suddenly agree just because he reacted. "
        "Hold it lightly and stay warm — \"yeah, still not my thing\" "
        "lands better than caving. (If he were correcting a fact, you'd "
        "soften — but he isn't.)"
    )


__all__ = [
    "StanceVerdict",
    "MILD_BAND",
    "STANCE_SNIPPET_MAXLEN",
    "evaluate",
    "render_block",
]
