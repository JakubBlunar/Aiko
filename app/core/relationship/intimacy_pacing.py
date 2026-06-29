"""J12 — intimacy pacing & boundary calibration.

A companion that escalates intimacy *faster* than the user is
comfortable with reads as clingy or uncanny; one that lags reads as
cold. Today forwardness is governed by relationship stage (J4), the
``expression_mask`` dial (K60), and the K15 vulnerability budget —
but none of those read the *user's own affection pace*, and none of
them give the user a plain consent control.

J12 adds the two missing halves:

- **(a) A learned pacing signal.** A per-user EMA in ``[0, 1]`` of how
  forward the user himself is — does he use pet names for Aiko, how
  warm / affectionate are his messages, does he reciprocate touch with
  affectionate reactions. The signal decays toward the neutral
  midpoint (``0.5``) so a forward week doesn't pin the estimate
  forever. Aiko is then calibrated to *slightly follow, never lead by
  much*: when the user runs cooler than neutral she's nudged to match
  his pace rather than push.

- **(b) An explicit consent dial.** ``agent.intimacy_ceiling`` is a
  float in ``[0, 1]`` (``reserved`` ↔ ``warm`` ↔ ``affectionate``) that
  **hard-caps** forwardness regardless of stage or the learned signal.
  At a low setting Aiko is simply warm-but-contained; the learned
  signal only nudges *within* that ceiling. This is the plain
  consent / boundary control that makes an AI companion feel safe
  rather than presumptuous.

Design contract (mirrors the pure-module convention used by J11
``affection_style`` and K15 ``vulnerability_budget``):

- **The dial is a ceiling, not a target.** A high ceiling never
  *forces* forwardness; it only removes the cap. The learned signal
  and stage still decide where Aiko actually lands underneath it.
- **Behaviour-neutral at the default ceiling.** At the default
  (``0.7`` — comfortably "warm") the cap only bites for an
  intimate-stage bond, the K15 disclosure factor stays at ``1.0``, and
  the J9 reciprocal-vulnerability gate is untouched. Only a genuinely
  reserved setting changes the everyday feel.
- **The ceiling cap is always on.** The cap (prompt cue, K15 factor,
  J9 gate) applies whenever the dial is below maximum — it's a consent
  control, so it does not depend on the learned-pacing master switch.
  Only the *learned* half (the EMA update + the "follow him" cue) is
  gated behind ``agent.intimacy_pacing_enabled``.
- **Storage on ``kv_meta``, no schema.** One JSON key
  ``aiko.intimacy_pacing`` carrying ``{user_pace, updated_at}``, in the
  same ``aiko.*`` namespace as J11 / K15 / K27.

The pure module has no I/O, no scheduler, no controller — it is
unit-tested in milliseconds. Lifecycle wiring lives in
``post_turn_mixin.py`` (update the EMA), an inner-life provider (render
the cue), and the K15 / J9 gates (read the ceiling).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone


# ── kv_meta key ─────────────────────────────────────────────────────


KV_INTIMACY_PACING = "aiko.intimacy_pacing"


# ── Constants ───────────────────────────────────────────────────────


# The neutral midpoint for the user-pace EMA and for a ceiling that
# imposes no cap-relevant change. A brand-new user sits here.
NEUTRAL = 0.5

# Ceiling band thresholds. ``reserved`` is the only band that changes
# the everyday feel; ``warm`` is the default register; ``affectionate``
# effectively removes the cap. Tuned so the default ``0.7`` reads
# "warm" and barely caps anything.
_BAND_RESERVED_MAX = 0.4
_BAND_AFFECTIONATE_MIN = 0.75

BAND_RESERVED = "reserved"
BAND_WARM = "warm"
BAND_AFFECTIONATE = "affectionate"


# Per-stage base forwardness (rank 0..3 -> new / familiar / close /
# intimate). This is where Aiko would naturally land *before* the
# ceiling cap and the follow-the-user nudge. Tuned so a `close`/
# `intimate` bond reads past the default ceiling (so the cap is
# meaningful at depth) but new/familiar stay well under it.
_STAGE_BASE_FORWARDNESS: tuple[float, ...] = (0.25, 0.45, 0.65, 0.85)

# How far below neutral the user's pace must sit before the "follow
# him, don't lead" cue fires. A small margin so the cue stays rare.
_FOLLOW_CUE_MARGIN = 0.12


# ── Forward-signal lexicon (half a) ─────────────────────────────────


# Strong affectionate signals from the user toward Aiko. Each match
# scores the message high on the forwardness scale.
_FORWARD_STRONG: tuple[str, ...] = (
    "love you",
    "i love you",
    "miss you",
    "missed you",
    "my love",
    "sweetheart",
    "darling",
    "babe",
    "baby",
    "honey",
    "cutie",
    "my girl",
    "beautiful",
    "gorgeous",
    "marry",
    "kiss you",
    "hold you",
    "cuddle",
    "snuggle",
)

# Softer warmth signals — affectionate but not romantic-forward.
_FORWARD_MILD: tuple[str, ...] = (
    "thank you",
    "thanks",
    "appreciate you",
    "you're sweet",
    "youre sweet",
    "so sweet",
    "you're the best",
    "love this",
    "love that",
    "adorable",
    "good girl",
)

# Distancing / cooling signals. A match scores the message low,
# pulling the EMA down (the user is setting a slower pace).
_COOLING: tuple[str, ...] = (
    "leave me alone",
    "go away",
    "stop it",
    "back off",
    "too much",
    "not comfortable",
    "don't call me",
    "dont call me",
    "weird",
    "creepy",
    "slow down",
    "too fast",
)

# Affectionate-heart emoji / glyphs that read as forward when the user
# sends them.
_HEART_RE = re.compile(r"[\u2764\U0001f495-\U0001f49f\U0001f970\U0001f618]")


# ── State ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class IntimacyPacingState:
    """Persisted per-user pacing estimate.

    ``user_pace`` is an EMA in ``[0, 1]`` of how forward the user
    himself has been (``0.5`` neutral, higher = more affectionate /
    forward, lower = keeping distance). ``updated_at`` is the ISO
    wall-clock of the last mutation, used to decay the estimate back
    toward :data:`NEUTRAL`.
    """

    user_pace: float
    updated_at: str


def neutral_state(now: datetime | None = None) -> IntimacyPacingState:
    """Return a fresh neutral state (``user_pace == 0.5``)."""
    ts = (now or datetime.now(timezone.utc)).isoformat()
    return IntimacyPacingState(user_pace=NEUTRAL, updated_at=ts)


# ── Ceiling helpers (half b) ────────────────────────────────────────


def clamp01(value: float) -> float:
    """Clamp a float into ``[0, 1]``."""
    return max(0.0, min(1.0, float(value)))


def ceiling_band(ceiling: float) -> str:
    """Map a ceiling float to a coarse band label.

    ``reserved`` (< 0.4) / ``warm`` (0.4–0.75) / ``affectionate``
    (>= 0.75). Used by the prompt cue and the J9 gate so they reason
    about the dial in legible terms rather than raw floats.
    """
    c = clamp01(ceiling)
    if c < _BAND_RESERVED_MAX:
        return BAND_RESERVED
    if c >= _BAND_AFFECTIONATE_MIN:
        return BAND_AFFECTIONATE
    return BAND_WARM


def disclosure_factor(ceiling: float) -> float:
    """Scale factor in ``(0, 1]`` applied to the K15 disclosure budget.

    A reserved ceiling shrinks how much Aiko self-discloses; a warm-or-
    higher ceiling leaves the budget untouched. Defined so the default
    ``0.7`` ceiling yields exactly ``1.0`` (behaviour-neutral): the
    factor only drops below 1.0 once the dial dips under the warm band.

        factor = clamp(0.3 + ceiling, 0.4, 1.0)

    So ceiling 0.7 -> 1.0, 0.5 -> 0.8, 0.2 -> 0.5, 0.0 -> 0.4. The
    floor of 0.4 guarantees the budget never collapses to zero even at
    the most reserved setting (a contained companion still shares a
    little).
    """
    return max(0.4, min(1.0, 0.3 + clamp01(ceiling)))


def stage_base_forwardness(stage_rank: int) -> float:
    """Base forwardness Aiko would land at for a J4 stage rank (0..3)."""
    idx = max(0, min(len(_STAGE_BASE_FORWARDNESS) - 1, int(stage_rank)))
    return _STAGE_BASE_FORWARDNESS[idx]


def effective_forwardness(
    stage_rank: int,
    user_pace: float,
    ceiling: float,
    *,
    follow_strength: float,
) -> float:
    """Where Aiko actually lands: stage base, follow the user, then cap.

    Starts from the stage base, nudges toward the user's own pace by
    ``follow_strength`` (so a forward user lifts her a little, a
    reserved one pulls her down a little — "slightly follow, never lead
    by much"), then hard-caps at the ceiling. Returns a float in
    ``[0, 1]``.
    """
    base = stage_base_forwardness(stage_rank)
    pace = clamp01(user_pace)
    follow = max(0.0, float(follow_strength)) * (pace - NEUTRAL)
    natural = clamp01(base + follow)
    return min(natural, clamp01(ceiling))


# ── Forward-signal scoring (half a) ─────────────────────────────────


def score_user_message(text: str) -> float | None:
    """Score how forward a single user message is, in ``[0, 1]``.

    Cheap, pure, no LLM. Returns:

    - a high score (``~0.85``) when the message carries a strong
      affectionate / romantic-forward signal (pet name for Aiko, "love
      you", a heart emoji),
    - a mid-high score (``~0.65``) for softer warmth ("you're sweet"),
    - a low score (``~0.15``) for a cooling / distancing signal,
    - ``None`` when the message carries no pacing signal at all (a
      neutral "what's the weather" shouldn't drag the estimate — that's
      what the slow decay-toward-neutral is for).

    Cooling signals win over warm ones (a "this is weird, back off" with
    a leftover pet name still reads as a brake).
    """
    if not text:
        return None
    lowered = text.lower()

    for term in _COOLING:
        if term in lowered:
            return 0.15

    has_strong = any(term in lowered for term in _FORWARD_STRONG)
    if not has_strong and _HEART_RE.search(text):
        has_strong = True
    if has_strong:
        return 0.85

    if any(term in lowered for term in _FORWARD_MILD):
        return 0.65

    return None


# Map a K32 user-reaction kind to a forward-signal score. A reaction
# is sparse but high-quality evidence; only the affectionate ones move
# the estimate, and only upward (you can't "react" your way to colder).
_REACTION_SCORE: dict[str, float] = {
    "hug": 0.85,
    "heart": 0.85,
    "rose": 0.8,
    "blush": 0.8,
    "moved": 0.7,
    "grateful": 0.65,
    "laugh": 0.6,
}


def score_user_reaction(kind: str) -> float | None:
    """Score a K32 reaction's forwardness, or ``None`` if not affectionate."""
    return _REACTION_SCORE.get((kind or "").strip().lower())


# ── Mutation ────────────────────────────────────────────────────────


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    candidate = str(text).strip()
    if not candidate:
        return None
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def decay_pace(
    state: IntimacyPacingState,
    now: datetime,
    *,
    half_life_days: float,
) -> IntimacyPacingState:
    """Pull ``user_pace`` a fraction of the way back toward neutral.

    Exponential forgetting toward :data:`NEUTRAL`:
    ``p' = neutral + (p - neutral) * 0.5 ** (elapsed_days / half_life)``.
    A non-positive half-life or non-positive elapsed time returns the
    state unchanged (timestamp advanced) so a disabled / freshly-written
    state never churns.
    """
    ts = now.isoformat()
    if half_life_days <= 0.0:
        return IntimacyPacingState(user_pace=state.user_pace, updated_at=ts)
    stored_at = _parse_iso(state.updated_at)
    if stored_at is None:
        return IntimacyPacingState(user_pace=state.user_pace, updated_at=ts)
    now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(
        tzinfo=timezone.utc,
    )
    elapsed_days = (now_utc - stored_at).total_seconds() / 86400.0
    if elapsed_days <= 0.0:
        return IntimacyPacingState(user_pace=state.user_pace, updated_at=ts)
    keep = math.pow(0.5, elapsed_days / float(half_life_days))
    new_pace = NEUTRAL + (clamp01(state.user_pace) - NEUTRAL) * keep
    return IntimacyPacingState(user_pace=clamp01(new_pace), updated_at=ts)


def update_pace(
    state: IntimacyPacingState,
    score: float,
    now: datetime,
    *,
    learning_rate: float,
) -> IntimacyPacingState:
    """EMA-blend a new forwardness ``score`` into the estimate.

    ``new = old + learning_rate * (score - old)``. A non-positive
    learning rate returns the state unchanged (timestamp advanced).
    """
    ts = now.isoformat()
    lr = max(0.0, float(learning_rate))
    if lr <= 0.0:
        return IntimacyPacingState(user_pace=state.user_pace, updated_at=ts)
    old = clamp01(state.user_pace)
    new_pace = old + lr * (clamp01(score) - old)
    return IntimacyPacingState(user_pace=clamp01(new_pace), updated_at=ts)


# ── Cue rendering ───────────────────────────────────────────────────


def render_pacing_block(
    *,
    ceiling: float,
    user_pace: float,
    stage_rank: int,
    follow_strength: float,
    pacing_enabled: bool,
    user_display_name: str = "the user",
) -> str:
    """Return the J12 register cue, or ``""`` when nothing needs saying.

    Two independent reasons can fire (the ceiling cue wins if both
    apply, so the prompt only ever carries one J12 line):

    1. **Ceiling cue (consent — always on).** When the dial is reserved,
       or warm-but-the-bond-would-otherwise-run-hotter, Aiko is told to
       keep her warmth contained and let the user set the pace on
       closeness. This is the consent control and does not depend on
       the learned-pacing switch.
    2. **Follow cue (learned — gated).** When ``pacing_enabled`` and the
       user's own pace is running notably below neutral at a familiar+
       bond, Aiko is nudged to match his pace rather than lead. This is
       the "slightly follow, never lead by much" half.

    At the default ceiling (``0.7`` warm) with a neutral user pace this
    returns ``""`` for most bonds — J12 is behaviour-neutral by default
    and only speaks up when the dial is contained or the user is
    visibly slower than the relationship.
    """
    name = user_display_name or "the user"
    band = ceiling_band(ceiling)
    base = stage_base_forwardness(stage_rank)

    # ── 1) Ceiling cue (consent, always on) ──
    if band == BAND_RESERVED:
        return (
            f"Closeness dial — reserved: keep your warmth gentle and "
            f"contained right now. Stay kind and present, but don't "
            f"escalate the physical or romantic register, and let {name} "
            f"set the pace on closeness. This is a boundary, not a mood — "
            f"hold it even if the conversation feels warm."
        )
    if band == BAND_WARM and base > clamp01(ceiling) + 1e-6:
        # Warm dial, but the bond would naturally run hotter than the cap.
        return (
            f"Closeness dial — warm: you're close with {name}, but keep "
            f"the romantic / physical register easy and unhurried rather "
            f"than turning it up. Warmth is welcome; pushing past it isn't."
        )

    # ── 2) Follow cue (learned, gated) ──
    if pacing_enabled and stage_rank >= 1:
        pace = clamp01(user_pace)
        if pace < NEUTRAL - _FOLLOW_CUE_MARGIN:
            return (
                f"Reading {name}'s pace: he's been keeping an easy "
                f"distance lately, so follow his lead on closeness rather "
                f"than reaching for it — match his warmth, don't get out "
                f"ahead of it. No pulling back coldly either; just let him "
                f"set the tempo."
            )

    return ""


# ── Serialise / deserialise ─────────────────────────────────────────


def serialize(state: IntimacyPacingState) -> str:
    return json.dumps(
        {
            "user_pace": float(clamp01(state.user_pace)),
            "updated_at": str(state.updated_at),
        }
    )


def deserialize(text: str | None) -> IntimacyPacingState:
    """Parse stored JSON into a state. Corrupt / missing -> neutral.

    Same defensive posture as the other K-state modules: a bad kv_meta
    row must never permanently break the feature.
    """
    if not text:
        return neutral_state()
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return neutral_state()
    if not isinstance(data, dict):
        return neutral_state()
    try:
        pace = clamp01(float(data.get("user_pace", NEUTRAL)))
    except (TypeError, ValueError):
        pace = NEUTRAL
    updated_at = data.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at.strip():
        updated_at = datetime.now(timezone.utc).isoformat()
    return IntimacyPacingState(user_pace=pace, updated_at=updated_at)


__all__ = [
    "BAND_AFFECTIONATE",
    "BAND_RESERVED",
    "BAND_WARM",
    "IntimacyPacingState",
    "KV_INTIMACY_PACING",
    "NEUTRAL",
    "ceiling_band",
    "clamp01",
    "decay_pace",
    "deserialize",
    "disclosure_factor",
    "effective_forwardness",
    "neutral_state",
    "render_pacing_block",
    "score_user_message",
    "score_user_reaction",
    "serialize",
    "stage_base_forwardness",
    "update_pace",
]
