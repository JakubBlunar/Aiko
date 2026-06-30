"""K69 — implicit-need reading (vent vs fix vs reassure vs celebrate).

The most common companion failure is answering the *literal* message
instead of the **need** behind it: reaching for problem-solving when the
user just wants to be heard, or offering flat empathy when they actually
want help thinking. K4 arc-tagging classifies the *topic* of a turn
(`support` / `planning` / `playful`), and `user_state` / `vocal_tone`
read affect *magnitude* — but nothing classifies the **response mode**
the user is implicitly asking for. K69 fills that gap.

This is a cheap, **pure heuristic** per-turn classifier over the live
user message. It scores four candidate modes from three signal sources —
lexical cue words (the bulk), the live K14 affect read (perceived mood /
energy / vocal tone), and the K4 conversation arc as a weak prior — and
picks the strongest above a confidence floor, falling back to the silent
`neutral` on the common, unremarkable turn:

* ``witness`` — they're venting / sharing a hard feeling; be heard, not
  fixed. *The strongest beat is not solving when they didn't ask.*
* ``problem_solve`` — they're explicitly asking for help/options; here
  fixing IS the need.
* ``reassure`` — they're anxious / full of worry / self-doubt; quiet the
  spin, don't pile on caveats.
* ``celebrate`` — they're sharing something good; match the high, let it
  land before anything else.

Tie-breaks favour **restraint** (witness > reassure > celebrate >
problem_solve) so an ambiguous emotional turn errs toward listening
rather than fixing. There is deliberately **no LLM fallback** on the hot
path (the provider runs synchronously during prompt assembly); genuinely
ambiguous turns stay ``neutral`` and silent, which is the safe default. A
background LLM disambiguation pass is a possible fast-follow but is not
built here.

Pure + dependency-light: all inputs are plain strings, so the whole
classifier is unit-testable in milliseconds. The controller glue (the
provider that gathers arc / user-state / vocal-tone and renders the cue)
lives in
[`inner_life_part2.py`](../session/inner_life_part2.py).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger("app.implicit_need")


# ── Modes ───────────────────────────────────────────────────────────

MODE_WITNESS = "witness"
MODE_PROBLEM_SOLVE = "problem_solve"
MODE_REASSURE = "reassure"
MODE_CELEBRATE = "celebrate"
MODE_NEUTRAL = "neutral"

# Tie-break priority, restraint-first: when two modes score equal, the
# earlier one wins. Witness over problem_solve is the whole point —
# err toward listening, not fixing.
_PRIORITY: tuple[str, ...] = (
    MODE_WITNESS,
    MODE_REASSURE,
    MODE_CELEBRATE,
    MODE_PROBLEM_SOLVE,
)


# ── Result ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NeedResult:
    """The classified response mode for one user turn.

    ``mode`` is one of the ``MODE_*`` constants. ``confidence`` is the
    raw winning score (only meaningful relative to the floor). ``scores``
    is the full per-mode breakdown (for MCP / tests). ``reasons`` is a
    short list of the signals that fired (for logs / debug).
    """

    mode: str
    confidence: float
    scores: dict[str, float]
    reasons: tuple[str, ...]


# ── Lexicons ────────────────────────────────────────────────────────
#
# Each entry is a (regex, weight) pair. Weights: 2.0 for a strong,
# unambiguous marker; 1.0 for a softer cue that needs corroboration to
# clear the confidence floor. Patterns are matched case-insensitively
# against the raw message with ``\b`` where word-boundaries help.


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# Explicit help-seeking — when fixing IS the need.
_PROBLEM_SOLVE: tuple[tuple[re.Pattern[str], float], ...] = (
    (_rx(r"\bhow (?:do|can|should|could|would) i\b"), 2.0),
    (_rx(r"\bwhat should i\b"), 2.0),
    (_rx(r"\bwhat would you do\b"), 2.0),
    (_rx(r"\bany (?:advice|ideas|tips|suggestions|thoughts)\b"), 2.0),
    (_rx(r"\bhelp me\b"), 2.0),
    (_rx(r"\bcan you help\b"), 2.0),
    (_rx(r"\bwhat'?s the best way\b"), 2.0),
    (_rx(r"\bhow would (?:you|i)\b"), 1.0),
    (_rx(r"\b(?:figure|figuring) (?:this|it|that) out\b"), 1.0),
    (_rx(r"\bnot sure how to\b"), 1.0),
    (_rx(r"\bstuck on\b"), 1.0),
    (_rx(r"\bshould i\b.*\bor\b"), 1.0),
    (_rx(r"\bwhat do i do\b"), 1.0),  # often rhetorical -> weak
    (_rx(r"\brecommend\b"), 1.0),
    (_rx(r"\bwalk me through\b"), 1.0),
)

# Venting / sharing a hard feeling — be heard, not fixed.
_WITNESS: tuple[tuple[re.Pattern[str], float], ...] = (
    (_rx(r"\b(?:need to|just|gotta) vent\b"), 2.0),
    (_rx(r"\bjust venting\b"), 2.0),
    (_rx(r"\b(?:so|really) (?:frustrat|annoy|exhaust|drain)"), 2.0),
    (_rx(r"\bi'?m so (?:done|over it|tired of)\b"), 2.0),
    (_rx(r"\bfed up\b"), 2.0),
    (_rx(r"\bburn(?:t|ed) out\b"), 2.0),
    (_rx(r"\boverwhelmed\b"), 2.0),
    (_rx(r"\bugh\b"), 1.0),
    (_rx(r"\bi (?:hate|can'?t stand)\b"), 1.0),
    (_rx(r"\b(?:rough|awful|terrible|the worst) (?:day|week|night|morning)\b"), 2.0),
    (_rx(r"\bso (?:unfair|stupid|ridiculous)\b"), 1.0),
    (_rx(r"\bit'?s not fair\b"), 1.0),
    (_rx(r"\bi'?m so (?:angry|mad|upset|sad|frustrated)\b"), 2.0),
    (_rx(r"\bcan'?t believe\b"), 1.0),
    (_rx(r"\bsick of\b"), 1.0),
    (_rx(r"\bi just (?:want|need) (?:to|someone)\b"), 1.0),
)

# Worry / anxiety / self-doubt — quiet the spin.
_REASSURE: tuple[tuple[re.Pattern[str], float], ...] = (
    (_rx(r"\b(?:i'?m|i am|so|really) (?:worried|anxious|nervous|scared|afraid|terrified)\b"), 2.0),
    (_rx(r"\banxiety\b"), 1.0),
    (_rx(r"\bwhat if\b"), 1.0),
    (_rx(r"\bcan'?t stop thinking\b"), 2.0),
    (_rx(r"\boverthinking\b"), 2.0),
    (_rx(r"\bfreaking out\b"), 2.0),
    (_rx(r"\bpanic(?:king|ing)?\b"), 2.0),
    (_rx(r"\bi feel like a failure\b"), 2.0),
    (_rx(r"\b(?:not|never) (?:good|smart) enough\b"), 2.0),
    (_rx(r"\bwhat'?s wrong with me\b"), 2.0),
    (_rx(r"\bimposter\b"), 1.0),
    (_rx(r"\b(?:am i|do you think i'?m?)\b.*\?"), 1.0),
    (_rx(r"\bis it bad that\b"), 1.0),
    (_rx(r"\bi don'?t know if i can\b"), 1.0),
    (_rx(r"\bdread(?:ing)?\b"), 1.0),
    (_rx(r"\bscared (?:that|i|of)\b"), 2.0),
)

# Good news / achievement / excitement — match the high.
_CELEBRATE: tuple[tuple[re.Pattern[str], float], ...] = (
    (_rx(r"\bi (?:got|landed) the (?:job|offer|role|gig|part)\b"), 2.0),
    (_rx(r"\bgot (?:promoted|accepted|in)\b"), 2.0),
    (_rx(r"\bi (?:passed|aced|nailed)\b"), 2.0),
    (_rx(r"\bi (?:finally|just) (?:finished|did it|got)\b"), 2.0),
    (_rx(r"\bwe won\b"), 2.0),
    (_rx(r"\b(?:she|he|they) said yes\b"), 2.0),
    (_rx(r"\bguess what\b"), 1.0),
    (_rx(r"\b(?:great|amazing|wonderful|good|exciting) news\b"), 2.0),
    (_rx(r"\bso (?:happy|excited|proud|thrilled)\b"), 2.0),
    (_rx(r"\bover the moon\b"), 2.0),
    (_rx(r"\bbest (?:day|news)\b"), 1.0),
    (_rx(r"\bnailed it\b"), 2.0),
    (_rx(r"\bcan'?t wait\b"), 1.0),
    (_rx(r"\b(?:yay+|woo+hoo+|woohoo|yess+)\b"), 1.0),
    (_rx(r"\bi did it\b"), 2.0),
)


_LEXICONS: dict[str, tuple[tuple[re.Pattern[str], float], ...]] = {
    MODE_PROBLEM_SOLVE: _PROBLEM_SOLVE,
    MODE_WITNESS: _WITNESS,
    MODE_REASSURE: _REASSURE,
    MODE_CELEBRATE: _CELEBRATE,
}

# Arc -> small prior nudge (the topic Aiko already believes the
# conversation is in). Deliberately weak (0.5) so a live lexical signal
# always dominates a stale arc.
_ARC_PRIOR: dict[str, dict[str, float]] = {
    "support": {MODE_WITNESS: 0.5, MODE_REASSURE: 0.5},
    "reflection": {MODE_WITNESS: 0.5},
    "planning": {MODE_PROBLEM_SOLVE: 0.5},
    "playful": {MODE_CELEBRATE: 0.5},
    "silly": {MODE_CELEBRATE: 0.5},
}


# ── Classifier ──────────────────────────────────────────────────────


def classify(
    user_text: str,
    *,
    arc: str | None = None,
    perceived_mood: str | None = None,
    perceived_energy: str | None = None,
    vocal_tags: "tuple[str, ...] | list[str] | None" = None,
    min_confidence: float = 2.0,
) -> NeedResult:
    """Classify the response mode the live ``user_text`` is asking for.

    All affect inputs are optional — the lexicons alone can fire — and
    every argument is a plain string / sequence, so this stays pure.

    * ``arc`` — the K4 conversation arc label (weak prior).
    * ``perceived_mood`` / ``perceived_energy`` — the K14 user-state read
      (``"low"`` / ``"high"`` / ``"unknown"`` …).
    * ``vocal_tags`` — paralinguistic tags from voice mode
      (``"anxious"``, ``"excited"``, ``"tired"`` …).
    * ``min_confidence`` — the score floor below which we stay
      ``neutral`` (restraint: a single soft cue isn't enough).

    Returns a :class:`NeedResult`; ``mode == MODE_NEUTRAL`` on the common
    unremarkable turn.
    """
    text = (user_text or "").strip()
    scores: dict[str, float] = {
        MODE_WITNESS: 0.0,
        MODE_PROBLEM_SOLVE: 0.0,
        MODE_REASSURE: 0.0,
        MODE_CELEBRATE: 0.0,
    }
    reasons: list[str] = []
    if not text:
        return NeedResult(MODE_NEUTRAL, 0.0, scores, ())

    # 1) Lexical cues (the bulk of the signal).
    for mode, lexicon in _LEXICONS.items():
        for pattern, weight in lexicon:
            if pattern.search(text):
                scores[mode] += weight
                reasons.append(f"{mode}:lex:{pattern.pattern[:24]}")

    # 2) Affect read (corroborates / breaks ties).
    mood = (perceived_mood or "").strip().lower()
    energy = (perceived_energy or "").strip().lower()
    if mood in ("low", "sad", "down", "negative"):
        scores[MODE_WITNESS] += 1.0
        scores[MODE_REASSURE] += 0.5
        reasons.append("mood:low")
    elif mood in ("high", "happy", "positive", "excited", "good"):
        scores[MODE_CELEBRATE] += 1.0
        reasons.append("mood:high")
    if energy == "low":
        scores[MODE_WITNESS] += 0.5
        reasons.append("energy:low")

    tags = tuple((t or "").strip().lower() for t in (vocal_tags or ()))
    if any(t in ("anxious", "nervous", "shaky", "worried") for t in tags):
        scores[MODE_REASSURE] += 1.0
        reasons.append("vocal:anxious")
    if any(t in ("excited", "happy", "elated", "upbeat") for t in tags):
        scores[MODE_CELEBRATE] += 1.0
        reasons.append("vocal:excited")
    if any(t in ("tired", "flat", "low", "drained", "sad") for t in tags):
        scores[MODE_WITNESS] += 0.5
        reasons.append("vocal:tired")

    # 3) Exclamation only *amplifies* an existing celebrate hit (so a
    # venting "I'm so done!!!" doesn't read as celebration).
    if scores[MODE_CELEBRATE] > 0 and text.count("!") >= 2:
        scores[MODE_CELEBRATE] += 0.5
        reasons.append("celebrate:bang")

    # 4) Arc prior (weakest — never beats a live lexical signal).
    arc_key = (arc or "").strip().lower()
    for mode, bump in _ARC_PRIOR.get(arc_key, {}).items():
        scores[mode] += bump
        reasons.append(f"arc:{arc_key}->{mode}")

    # Pick the winner, restraint-first on ties.
    best_mode = MODE_NEUTRAL
    best_score = 0.0
    for mode in _PRIORITY:
        if scores[mode] > best_score:
            best_score = scores[mode]
            best_mode = mode

    if best_score < float(min_confidence):
        return NeedResult(MODE_NEUTRAL, round(best_score, 4), scores, tuple(reasons))
    return NeedResult(best_mode, round(best_score, 4), scores, tuple(reasons))


# ── Render ──────────────────────────────────────────────────────────


def _steer(mode: str, name: str) -> str:
    if mode == MODE_WITNESS:
        return (
            f"Read: {name} sounds like he needs to be heard, not fixed -- "
            "this is a vent. Be a witness first: reflect it back, name the "
            "feeling, sit in it with him. Hold off on solutions, silver "
            "linings, or 'have you tried' unless he actually asks."
        )
    if mode == MODE_PROBLEM_SOLVE:
        return (
            f"Read: {name} is actually asking for help thinking this "
            "through -- it's okay to get concrete here (a clear take, real "
            "options, a next step). Still lead with that you get it, then "
            "be useful."
        )
    if mode == MODE_REASSURE:
        return (
            f"Read: {name} sounds anxious -- worry doing laps. The need is "
            "to steady him, not to problem-solve or stack on caveats. Warm, "
            "present, certain: it's okay, he's okay, you're here. Slow the "
            "spin before anything practical."
        )
    if mode == MODE_CELEBRATE:
        return (
            f"Read: {name} is sharing something good. Match the high -- be "
            "genuinely happy with him and let the win land fully before "
            "anything else. No cautions, no 'but', no pivot to what's next."
        )
    return ""


def render_inner_life_block(
    result: NeedResult | None,
    *,
    user_display_name: str = "them",
) -> str:
    """One-line response-mode steer, or ``""`` on a neutral turn.

    A private steer (never said aloud) — it shapes *how* Aiko replies,
    not *what* she says. ``None`` / ``MODE_NEUTRAL`` render empty so the
    assembler skips the block entirely on the common turn.
    """
    if result is None or result.mode == MODE_NEUTRAL:
        return ""
    name = (user_display_name or "them").strip() or "them"
    return _steer(result.mode, name)


__all__ = [
    "MODE_CELEBRATE",
    "MODE_NEUTRAL",
    "MODE_PROBLEM_SOLVE",
    "MODE_REASSURE",
    "MODE_WITNESS",
    "NeedResult",
    "classify",
    "render_inner_life_block",
]
