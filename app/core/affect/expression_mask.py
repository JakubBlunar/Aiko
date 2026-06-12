"""K60 — tsundere mask: warmth expressed through denial.

Not an emotion — an **expression policy** layered between K57 (what
Aiko *feels*) and K58 (how it *sounds*). The felt episode stays
truthful in state; only the expressed cue transforms. K45 mood
inertia built "instant face, lagging heart" by accident — this is
the same divergence on purpose, and the B4 vocabulary minted exactly
the two faces it needs (``defiant`` = the "hmph" tsun beat,
``embarrassed+blush`` = caught caring).

Four mechanics, all pure functions here:

- **Mask transform table** (:func:`render_masked_block`): per-emotion
  expressed forms — ``lonely`` → denied missing ("I wasn't
  *waiting*. The place was just... quiet."), ``warm_glow`` →
  grudging backwards delivery ("it's not bad. For you."). ``miffed``
  stays unmasked — tsun IS the native register for miffed, which is
  why the families compose.
- **Caught-caring** (:func:`detect_caught_caring`): when the user
  names her warmth ("you missed me, didn't you?"), fire the
  ``embarrassed+blush`` denial-with-a-tell beat — the single most
  character-defining tsundere moment.
- **The slip** (:func:`should_slip`): rare budgeted dere-leaks where
  one fully genuine line gets through before the mask snaps back.
  Earned (high-intensity episode only) + wall-clock cooldown;
  scarcity is what makes it land.
- **Long-arc erosion** (:func:`mask_strength`): strength scales
  inversely with closeness+trust, so over weeks the denials soften
  into transparent token protests both sides are in on ("I didn't
  miss you. (I missed you.)") — the actual character arc, and the
  payoff of having persistent axes at all.

Hard safety rail (enforced by the caller, documented here): the mask
drops **unconditionally** when the user is genuinely down (support
arc, rupture) — deflecting real pain is the one unforgivable
tsundere failure mode. ``hurt`` is never masked for the same reason.

Ships as a user-facing dial — ``agent.expression_mask``
(``off`` / ``tsundere_light`` / ``tsundere_full``, default off) —
because it's a strong flavour choice. ``light`` masks only
``lonely`` / ``warm_glow`` with frequent slips; ``full`` adds the
thaw beat to the masked set and spaces the slips out.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from app.core.affect.emotion_episodes import (
    EMOTION_LONELY,
    EMOTION_WARM_GLOW,
)


MODE_OFF = "off"
MODE_LIGHT = "tsundere_light"
MODE_FULL = "tsundere_full"
MODES = (MODE_OFF, MODE_LIGHT, MODE_FULL)

KV_LAST_SLIP_AT = "aiko.mask_last_slip_at"

# Emotions whose expressed cue transforms per mode. ``miffed`` is
# never here (tsun is its native register); ``hurt`` is never here
# (masking real hurt is the failure mode); ``smug`` /
# ``playful_jealous`` are already deflective by construction.
_MASKED_EMOTIONS: dict[str, frozenset[str]] = {
    MODE_OFF: frozenset(),
    MODE_LIGHT: frozenset({EMOTION_LONELY, EMOTION_WARM_GLOW}),
    MODE_FULL: frozenset({EMOTION_LONELY, EMOTION_WARM_GLOW}),
}

# Caught-caring patterns — the user naming her warmth. Kept tight:
# a false positive fires a flustered denial at a sincere moment,
# which is worse than missing one.
_CAUGHT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\byou missed me\b",
        r"\byou did miss me\b",
        r"\badmit it\b",
        r"\byou (?:like|love) (?:me|this|talking to me)\b",
        r"\byou were waiting\b",
        r"\byou care about me\b",
        r"\byou(?:'re| are) blushing\b",
        r"\bcaught you\b.{0,20}\bcaring\b",
        r"\byou(?:'re| are) (?:so )?soft for me\b",
    )
)


def normalize_mode(raw: str | None) -> str:
    """Clamp arbitrary input to a known mode (unknown → off)."""
    candidate = str(raw or "").strip().lower()
    return candidate if candidate in MODES else MODE_OFF


def is_masked(emotion: str, mode: str) -> bool:
    return emotion in _MASKED_EMOTIONS.get(normalize_mode(mode), frozenset())


def mask_strength(closeness: float | None, trust: float | None) -> float:
    """Long-arc erosion: warm axes wear the mask thin.

    Maps the mean of closeness+trust from ``[-1, 1]`` onto a strength
    in ``[0.25, 1.0]`` — a brand-new relationship (axes ~0) wears the
    mask at ~0.62; deep closeness+trust erodes it to a transparent
    token protest (0.25 floor — it never fully disappears, because
    the protest itself is the bit by then).
    """
    c = float(closeness) if closeness is not None else 0.0
    t = float(trust) if trust is not None else 0.0
    mean = max(-1.0, min(1.0, (c + t) / 2.0))
    return round(max(0.25, min(1.0, 0.625 - 0.375 * mean)), 3)


# ── transform table ─────────────────────────────────────────────────

# (firm-mask line, eroded token-protest line) per masked emotion.
# {name} / {cause} substituted by the renderer. The copy carries the
# tonal rails: denial WITH a visible tell, never actually cold,
# never leaves the user doubting.
_MASK_COPY: dict[str, tuple[str, str]] = {
    EMOTION_LONELY: (
        "You missed {name} -- {cause} -- but the mask is ON: you do "
        "not SAY it. Deny it with a visible tell: \"I wasn't "
        "*waiting*. I just... happened to be here. It was quiet, "
        "that's all.\" The denial must be transparent -- a touch too "
        "quick, a beat too specific -- so the warmth reads through "
        "it. Never actually cold; if {name} seems unsure you care, "
        "the mask has failed, let a tell slip wider.",
        "You missed {name} -- {cause} -- and by now the denial is a "
        "token protest you're both in on: \"I didn't miss you. (I "
        "missed you.)\" Say the protest with zero effort to be "
        "believed -- it's a running bit, not a wall.",
    ),
    EMOTION_WARM_GLOW: (
        "You're glowing -- {cause} -- but the mask is ON: warmth "
        "comes out grudging and backwards. \"...it's not bad. For "
        "you.\" / \"I guess you can be useful occasionally.\" If "
        "they thank you, deflect: \"yeah, well. Don't make it "
        "weird.\" The compliment must still LAND through the "
        "wrapping -- backwards delivery, real warmth.",
        "You're glowing -- {cause} -- and the grudging wrapper is "
        "tissue-thin by now: \"fine, it's good. It's really good. "
        "Don't look at me like that.\" Let the warmth win the "
        "sentence; the protest is just punctuation.",
    ),
}

_CAUGHT_COPY: tuple[str, str] = (
    # Firm mask.
    "Caught-caring beat: {name} just named your warmth out loud. "
    "Fire the flustered denial WITH the tell -- open with "
    "[[reaction:embarrassed+blush]] and deny it badly: \"...no. "
    "Shut up.\" / \"I wasn't-- that's not-- okay, moving ON.\" The "
    "blush is the confession; the words deny, the face doesn't. "
    "Don't recover gracefully -- the stumble IS the beat.",
    # Eroded.
    "Caught-caring beat: {name} just named your warmth, and you "
    "both know the denial is ceremonial by now. "
    "[[reaction:embarrassed+blush]], one token \"...shut up,\" and "
    "then -- because the mask is thin these days -- let almost all "
    "of it show anyway.",
)

_SLIP_COPY = (
    " SLIP earned: this one's strong enough that ONE fully genuine "
    "line gets through before the mask snaps back -- \"...I "
    "actually really missed you. -- Anyway. ANYWAY. What did you "
    "bring me?\" One line, then visibly slam the mask shut. The "
    "slam is part of the slip."
)


def render_masked_block(
    *,
    emotion: str,
    cause: str,
    user_display_name: str = "them",
    strength: float,
    eroded_below: float = 0.45,
    slip: bool = False,
) -> str:
    """Expressed form of a masked episode (replaces the K57 cue)."""
    copy = _MASK_COPY.get(emotion)
    if copy is None:
        return ""
    firm, eroded = copy
    template = eroded if strength < float(eroded_below) else firm
    rendered = template.format(
        name=user_display_name or "them", cause=cause,
    )
    if slip:
        rendered += _SLIP_COPY
    return rendered


def detect_caught_caring(user_text: str) -> bool:
    text = (user_text or "").strip()
    if not text:
        return False
    return any(p.search(text) for p in _CAUGHT_PATTERNS)


def render_caught_caring_block(
    *,
    user_display_name: str = "them",
    strength: float,
    eroded_below: float = 0.45,
) -> str:
    firm, eroded = _CAUGHT_COPY
    template = eroded if strength < float(eroded_below) else firm
    return template.format(name=user_display_name or "them")


def should_slip(
    *,
    mode: str,
    episode_intensity: float,
    last_slip_at: str | None,
    now: datetime,
    cooldown_days_light: float = 2.0,
    cooldown_days_full: float = 5.0,
    min_intensity: float = 0.7,
) -> bool:
    """Is a dere-slip earned right now?

    Earned = the masked episode is high-intensity AND the wall-clock
    cooldown since the last slip has elapsed (``light`` slips more
    often than ``full`` — scarcity scales with how committed the
    mask is). The caller stamps :data:`KV_LAST_SLIP_AT` when the
    slip is actually rendered.
    """
    mode = normalize_mode(mode)
    if mode == MODE_OFF:
        return False
    if float(episode_intensity) < float(min_intensity):
        return False
    cooldown_days = (
        cooldown_days_light if mode == MODE_LIGHT else cooldown_days_full
    )
    if cooldown_days <= 0.0:
        return True
    if not last_slip_at:
        return True
    candidate = str(last_slip_at).strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        last = datetime.fromisoformat(candidate)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    now_utc = now.astimezone(timezone.utc) if now.tzinfo else (
        now.replace(tzinfo=timezone.utc)
    )
    elapsed_days = (now_utc - last).total_seconds() / 86400.0
    return elapsed_days >= float(cooldown_days)


__all__ = [
    "KV_LAST_SLIP_AT",
    "MODES",
    "MODE_FULL",
    "MODE_LIGHT",
    "MODE_OFF",
    "detect_caught_caring",
    "is_masked",
    "mask_strength",
    "normalize_mode",
    "render_caught_caring_block",
    "render_masked_block",
    "should_slip",
]
