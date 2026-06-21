"""Persistent emotional state for Aiko.

Stores a low-dimensional valence/arousal pair that drifts over turns, plus
a derived named mood label and 24h trend buffers. Updated POST-TURN by
cheap math (no LLM). Read on the hot path by :class:`PromptAssembler` to
inject a 1-2 line ambient block, and by the Phase 5b prosody mapper.

Schema lives in :mod:`app.core.infra.chat_database` (one row per ``user_id``).

Design notes:
  - Smoothing uses an exponential blend toward the event-weighted target
    on update (``alpha``), and another exponential decay back to baseline
    when read is called between events. We store ``updated_at`` so the
    decay term is correct regardless of how long it's been.
  - Trend buffers are EWMAs of (valence, arousal) deltas over a 24h
    window; the trend appears in the prompt only when |delta| is above a
    threshold so we don't spam meaningless "you've been about the same"
    lines.
  - Reaction → (valence_delta, arousal_delta) lookup mirrors the existing
    reaction tags emitted by the LLM via ``[[reaction:X]]``.
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase
    from app.core.affect.vocal_tone import VocalTone


log = logging.getLogger("app.affect")


# Reactions that the LLM can emit via [[reaction:X]] mapped to a small
# (valence_delta, arousal_delta) impulse. Calibrated so an "excited"
# turn moves valence by +0.15, never enough to flip the mood label all on
# its own — needs sustained reactions to drift the baseline.
_REACTION_IMPULSE: dict[str, tuple[float, float]] = {
    "excited":      (+0.15, +0.20),
    "enthusiastic": (+0.13, +0.18),
    "cheerful":     (+0.12, +0.10),
    "amused":       (+0.10, +0.05),
    "warm":         (+0.10, -0.05),
    "tender":       (+0.08, -0.10),
    "friendly":     (+0.06, +0.02),
    "calm":         (+0.04, -0.10),
    "neutral":      ( 0.00,  0.00),
    "thoughtful":   (+0.02, -0.05),
    "serious":      (-0.04, +0.02),
    "concerned":    (-0.08, +0.04),
    "sad":          (-0.15, -0.10),
    "melancholy":   (-0.12, -0.08),
    # ``cry`` is the most distressed reaction in the canonical set — a
    # touch more negative valence than ``sad`` and slightly higher
    # arousal because crying is an emotionally activated state, not a
    # withdrawn one.
    "cry":          (-0.18, -0.05),
    "angry":        (-0.10, +0.20),
    "frustrated":   (-0.08, +0.12),
    "surprised":    ( 0.00, +0.15),
    # K58 (emotion speech weighting): impulses for the four minted
    # directed-emotion shades plus ``wistful`` (in the canonical set
    # but previously missing here, so it read as a zero impulse).
    # ``smug`` / ``mischievous`` are pleased-and-energised;
    # ``pouty`` is displeasure with a bit of spark; ``sulky`` is the
    # same displeasure gone low-energy.
    "smug":         (+0.08, +0.06),
    "pouty":        (-0.06, +0.06),
    "sulky":        (-0.08, -0.06),
    "mischievous":  (+0.10, +0.12),
    "wistful":      (-0.05, -0.08),
}


# Cheap keyword hints for user valence (no LLM). Appended to the reaction
# impulse to capture the user's apparent mood, with a weaker weight.
_USER_HINTS_POSITIVE = (
    "thanks", "thank you", "love", "great", "awesome", "amazing", "nice",
    "good job", "perfect", "haha", "lol", "fun",
)
_USER_HINTS_NEGATIVE = (
    "sorry", "tired", "exhausted", "stressed", "worried", "frustrated",
    "annoyed", "sad", "hate", "ugh", "fed up",
)


@dataclass(slots=True)
class AffectState:
    """Snapshot of Aiko's emotional state.

    ``valence`` is in [-1, +1]; ``arousal`` is in [0, 1]. Baselines drift
    on much slower timescales (only nudged by daily rollups). The trend
    fields are EWMAs that compare current valence/arousal against the
    24h-ago value.
    """

    user_id: str
    valence: float = 0.0
    arousal: float = 0.4
    baseline_valence: float = 0.0
    baseline_arousal: float = 0.4
    mood_label: str = "content"
    mood_intensity: float = 0.5
    valence_trend_24h: float = 0.0
    arousal_trend_24h: float = 0.0
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
    )

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe dict (no Python-only types) for WS broadcast."""
        return asdict(self)


# ── lookup helpers ──────────────────────────────────────────────────────


def _classify_mood(valence: float, arousal: float) -> tuple[str, float]:
    """Map (valence, arousal) into one of our named mood labels + intensity.

    Bands are tuned so the *labels* update once the underlying state has
    drifted visibly. EWMA smoothing in the updater keeps even sustained
    impulses around |0.15-0.18|, so thresholds need to be modest. The
    intensity is the L2 distance from the (0, 0.4) neutral point.
    """
    # Magnitude-based intensity, gently clamped.
    mag = math.sqrt(valence * valence + (arousal - 0.4) ** 2)
    intensity = float(min(1.0, mag * 1.4))

    # Decision tree: walk the (valence, arousal) plane with a few thresholds.
    if valence >= 0.30 and arousal >= 0.55:
        return "playful", intensity
    if valence >= 0.30 and arousal < 0.40:
        return "tender", intensity
    if valence >= 0.10 and arousal >= 0.55:
        return "curious", intensity
    if valence >= 0.10:
        return "warm", intensity
    if valence >= -0.08 and arousal >= 0.55:
        return "focused", intensity
    if valence >= -0.08:
        return "content", intensity
    if valence >= -0.30 and arousal >= 0.55:
        return "restless", intensity
    if valence >= -0.30:
        return "melancholy", intensity
    return "tired", intensity


def _user_hint_delta(user_text: str) -> tuple[float, float]:
    """Return a tiny (val, aro) delta from cheap keyword detection.

    Bounded to ±0.05 valence so the hints don't dominate; the user's
    actual mood mostly comes through Aiko's reaction.
    """
    text = (user_text or "").lower()
    if not text:
        return (0.0, 0.0)
    score = 0
    for token in _USER_HINTS_POSITIVE:
        if token in text:
            score += 1
    for token in _USER_HINTS_NEGATIVE:
        if token in text:
            score -= 1
    if score == 0:
        return (0.0, 0.0)
    val = max(-0.05, min(0.05, 0.025 * score))
    aro = 0.0
    return (val, aro)


# ── K37: emotional contagion ─────────────────────────────────────────────

# Absolute (valence_contribution, arousal_contribution) that each readable
# user-mood band implies, layered on top of the neutral baseline
# (val 0.0, aro 0.4). Only "low" / "high" count as a real read — "neutral"
# / "unknown" leave no pull (so contagion stays silent when we can't tell).
_USER_MOOD_AFFECT: dict[str, tuple[float, float]] = {
    "low": (-0.40, -0.05),
    "high": (0.40, 0.20),
}
# Perceived-energy band -> arousal contribution.
_USER_ENERGY_AROUSAL: dict[str, float] = {
    "low": -0.15,
    "high": 0.15,
}
# Dialogue-act sentiment -> valence contribution. ``vent`` is the strongest
# negative-emotion signal; ``banter`` reads as mildly upbeat.
_USER_DACT_VALENCE: dict[str, float] = {
    "vent": -0.30,
    "banter": 0.20,
}
# How much of a confident vocal-tone arousal_hint (already ±0.10) folds into
# the user-affect arousal estimate.
_CONTAGION_TONE_WEIGHT = 1.0


def estimate_user_affect(
    *,
    mood: str | None = None,
    energy: str | None = None,
    dialogue_act: str | None = None,
    tone: "VocalTone | None" = None,
) -> tuple[float, float] | None:
    """Estimate the user's current ``(valence, arousal)`` from cheap signals.

    Combines the perceived-mood / perceived-energy bands (from
    :class:`app.core.affect.user_state.UserStateEstimator`), dialogue-act
    sentiment (``vent`` / ``banter``), and a confident vocal-tone
    arousal hint into an absolute affect point on the same scale Aiko
    uses (valence ``[-1, 1]``, arousal ``[0, 1]``).

    Returns ``None`` when no signal is readable (mood/energy unknown, no
    sentiment-bearing dialogue act, no confident tone) so the K37
    contagion pass can stay silent rather than pulling toward neutral.
    """
    val_contrib = 0.0
    aro_contrib = 0.0
    seen = False

    mood_key = (mood or "").strip().lower()
    if mood_key in _USER_MOOD_AFFECT:
        mv, ma = _USER_MOOD_AFFECT[mood_key]
        val_contrib += mv
        aro_contrib += ma
        seen = True

    energy_key = (energy or "").strip().lower()
    if energy_key in _USER_ENERGY_AROUSAL:
        aro_contrib += _USER_ENERGY_AROUSAL[energy_key]
        seen = True

    dact_key = (dialogue_act or "").strip().lower()
    if dact_key in _USER_DACT_VALENCE:
        val_contrib += _USER_DACT_VALENCE[dact_key]
        seen = True

    if tone is not None and getattr(tone, "confident", False):
        hint = float(getattr(tone, "arousal_hint", 0.0))
        if hint:
            aro_contrib += hint * _CONTAGION_TONE_WEIGHT
            seen = True

    if not seen:
        return None

    user_val = max(-1.0, min(1.0, 0.0 + val_contrib))
    user_aro = max(0.0, min(1.0, 0.4 + aro_contrib))
    return (user_val, user_aro)


def _apply_user_contagion(
    valence: float,
    arousal: float,
    user_affect: tuple[float, float],
    *,
    strength: float,
    cap: float,
) -> tuple[float, float]:
    """Tilt ``(valence, arousal)`` a small, capped amount toward the user.

    Moves a ``strength`` fraction of the gap to ``user_affect`` each
    call, with the per-axis step clamped to ``±cap`` so a big mismatch
    can only ever pull Aiko ``cap`` per turn. Result is re-clamped to the
    valid affect ranges.
    """
    uv, ua = user_affect
    dv = max(-cap, min(cap, strength * (uv - valence)))
    da = max(-cap, min(cap, strength * (ua - arousal)))
    new_val = max(-1.0, min(1.0, valence + dv))
    new_aro = max(0.0, min(1.0, arousal + da))
    return (new_val, new_aro)


# ── store ───────────────────────────────────────────────────────────────


class AffectStore:
    """Thin SQLite-backed read/write helper for the ``affect_state`` table.

    The table holds at most one row per user; reads return the row (or a
    fresh ``AffectState`` with defaults if the row doesn't exist yet).
    """

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db
        self._db._ensure_affect_state_schema()  # type: ignore[attr-defined]

    def get(self, user_id: str) -> AffectState:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        row = conn.execute(
            "SELECT user_id, valence, arousal, baseline_valence, "
            "baseline_arousal, mood_label, mood_intensity, valence_trend_24h, "
            "arousal_trend_24h, updated_at FROM affect_state WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return AffectState(user_id=user_id)
        return AffectState(
            user_id=str(row[0]),
            valence=float(row[1]),
            arousal=float(row[2]),
            baseline_valence=float(row[3]),
            baseline_arousal=float(row[4]),
            mood_label=str(row[5]),
            mood_intensity=float(row[6]),
            valence_trend_24h=float(row[7]),
            arousal_trend_24h=float(row[8]),
            updated_at=str(row[9]),
        )

    def save(self, state: AffectState) -> None:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        conn.execute(
            "INSERT INTO affect_state ("
            "  user_id, valence, arousal, baseline_valence, baseline_arousal,"
            "  mood_label, mood_intensity, valence_trend_24h, "
            "  arousal_trend_24h, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "  valence = excluded.valence, arousal = excluded.arousal, "
            "  baseline_valence = excluded.baseline_valence, "
            "  baseline_arousal = excluded.baseline_arousal, "
            "  mood_label = excluded.mood_label, "
            "  mood_intensity = excluded.mood_intensity, "
            "  valence_trend_24h = excluded.valence_trend_24h, "
            "  arousal_trend_24h = excluded.arousal_trend_24h, "
            "  updated_at = excluded.updated_at",
            (
                state.user_id,
                float(state.valence),
                float(state.arousal),
                float(state.baseline_valence),
                float(state.baseline_arousal),
                str(state.mood_label),
                float(state.mood_intensity),
                float(state.valence_trend_24h),
                float(state.arousal_trend_24h),
                state.updated_at,
            ),
        )
        conn.commit()


# ── updater ─────────────────────────────────────────────────────────────


class AffectUpdater:
    """Mutates :class:`AffectState` after each turn (post-turn, no LLM).

    Inputs:
      * The reaction Aiko emitted (``[[reaction:X]]``) — primary impulse.
      * The user's text — small keyword hint.
      * Time elapsed since last update — exponential decay toward baseline.

    Outputs:
      * Updated valence/arousal.
      * Re-derived mood_label + intensity.
      * Trend EWMAs nudged.
    """

    # Smoothing factor for new events. Higher = mood reacts faster.
    _ALPHA = 0.35
    # Decay-toward-baseline per minute (exponential half-life ~30 min).
    _DECAY_HALFLIFE_SECONDS = 30 * 60.0
    # Trend EWMA smoothing factor: 0.05 means trend is roughly the average
    # of the last 20 updates.
    _TREND_ALPHA = 0.05

    def __init__(self, store: AffectStore) -> None:
        self._store = store

    def apply_turn(
        self,
        user_id: str,
        *,
        reaction: str | None,
        user_text: str | None,
        user_tone: "VocalTone | None" = None,
        user_affect: tuple[float, float] | None = None,
        contagion_strength: float = 0.0,
        contagion_max_per_turn: float = 0.05,
    ) -> AffectState:
        """Apply one turn's worth of evidence and persist the result.

        ``user_tone`` is the optional vocal-tone signal from
        :func:`app.core.affect.vocal_tone.analyse_wav`. When supplied, its
        ``arousal_hint`` (already capped at ±0.10) nudges Aiko's arousal
        target on top of the reaction-based impulse — the "she catches
        on when you sound tired or excited" signal.

        K37 emotional contagion: when ``user_affect`` (an estimated
        ``(valence, arousal)`` for the user, e.g. from
        :func:`estimate_user_affect`) is supplied and
        ``contagion_strength > 0``, Aiko's post-blend affect is tilted a
        small, capped amount toward it — the residual "I'm picking up on
        him" pull, separate from her own ``[[reaction:...]]`` math.
        """
        state = self._store.get(user_id)
        # 1) decay toward baseline based on elapsed time.
        elapsed_s = self._seconds_since(state.updated_at)
        if elapsed_s > 0:
            weight = math.pow(0.5, elapsed_s / self._DECAY_HALFLIFE_SECONDS)
            state.valence = (
                weight * state.valence + (1 - weight) * state.baseline_valence
            )
            state.arousal = (
                weight * state.arousal + (1 - weight) * state.baseline_arousal
            )

        # 2) compute event impulse.
        rxn = (reaction or "neutral").strip().lower()
        impulse_val, impulse_aro = _REACTION_IMPULSE.get(rxn, (0.0, 0.0))
        hint_val, hint_aro = _user_hint_delta(user_text or "")
        # Vocal-tone arousal nudge: only fires when ``confident=True``.
        # Half-strength (0.5x) so the prompt-reaction impulse stays the
        # primary driver and noisy estimates don't whip the mood.
        tone_aro = 0.0
        if user_tone is not None and getattr(user_tone, "confident", False):
            tone_aro = float(getattr(user_tone, "arousal_hint", 0.0)) * 0.5
        target_val = state.baseline_valence + impulse_val + hint_val
        target_aro = (
            state.baseline_arousal + impulse_aro + hint_aro + tone_aro
        )
        # Clamp targets so a single huge reaction can't push us off-scale.
        target_val = max(-1.0, min(1.0, target_val))
        target_aro = max(0.0, min(1.0, target_aro))

        # 3) blend toward the target.
        new_valence = (
            (1 - self._ALPHA) * state.valence + self._ALPHA * target_val
        )
        new_arousal = (
            (1 - self._ALPHA) * state.arousal + self._ALPHA * target_aro
        )
        new_valence = max(-1.0, min(1.0, new_valence))
        new_arousal = max(0.0, min(1.0, new_arousal))

        # 3b) K37 emotional contagion: tilt toward the user's estimated
        # affect by a small, capped amount. Distinct from the reaction
        # impulse so the strength/cap knobs don't entangle with the
        # ``[[reaction:...]]`` math above.
        contagion_dv = 0.0
        contagion_da = 0.0
        if user_affect is not None and contagion_strength > 0.0:
            before_val, before_aro = new_valence, new_arousal
            new_valence, new_arousal = _apply_user_contagion(
                new_valence,
                new_arousal,
                user_affect,
                strength=contagion_strength,
                cap=contagion_max_per_turn,
            )
            contagion_dv = new_valence - before_val
            contagion_da = new_arousal - before_aro

        # 4) trend EWMAs (compare against baseline, not previous value).
        val_delta = new_valence - state.baseline_valence
        aro_delta = new_arousal - state.baseline_arousal
        new_val_trend = (
            (1 - self._TREND_ALPHA) * state.valence_trend_24h
            + self._TREND_ALPHA * val_delta
        )
        new_aro_trend = (
            (1 - self._TREND_ALPHA) * state.arousal_trend_24h
            + self._TREND_ALPHA * aro_delta
        )

        # 5) re-derive mood label.
        mood_label, mood_intensity = _classify_mood(new_valence, new_arousal)

        state.valence = round(new_valence, 4)
        state.arousal = round(new_arousal, 4)
        state.valence_trend_24h = round(new_val_trend, 4)
        state.arousal_trend_24h = round(new_aro_trend, 4)
        state.mood_label = mood_label
        state.mood_intensity = round(mood_intensity, 4)
        state.updated_at = datetime.now(timezone.utc).isoformat()

        self._store.save(state)
        log.debug(
            "affect: rxn=%s val=%.2f aro=%.2f mood=%s int=%.2f tv=%.2f ta=%.2f "
            "contagion_dv=%.3f contagion_da=%.3f user_affect=%s",
            rxn,
            state.valence, state.arousal,
            state.mood_label, state.mood_intensity,
            state.valence_trend_24h, state.arousal_trend_24h,
            contagion_dv, contagion_da,
            user_affect,
        )
        return state

    @staticmethod
    def _seconds_since(iso_timestamp: str) -> float:
        try:
            ts = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            return 0.0
        delta = datetime.now(timezone.utc) - ts
        return max(0.0, delta.total_seconds())


# ── prompt block ────────────────────────────────────────────────────────


# K44 — felt-language grid. 5 valence bands x 3 arousal bands, mapped to
# direct, forward emotion words (circumplex style) rather than poetic
# texture: "excited" is a word Aiko can naturally echo back in speech
# ("I'm kind of excited today" is a feature); literal floats — and
# purple phrases like "quietly glowing" — are exactly what a model
# parrots badly. The raw valence/arousal floats stay on AffectState for
# MCP / REST / logs; the prompt only ever sees these words.
#
# Rows: valence band (very positive → very negative).
# Columns: arousal band (low / mid / high energy).
_FELT_PHRASES: dict[str, tuple[str, str, str]] = {
    "very_positive": (
        "happy in a quiet, settled way",
        "really good today",
        "genuinely excited, lots of energy",
    ),
    "positive": (
        "mellow and content",
        "in a good mood",
        "upbeat, a little buzzed",
    ),
    "neutral": (
        "calm, unhurried",
        "pretty even",
        "antsy, hard to sit still",
    ),
    "negative": (
        "a bit drained, low energy",
        "a little flat",
        "restless and off",
    ),
    "very_negative": (
        "down and low on energy",
        "heavy-hearted",
        "upset and wound up",
    ),
}


def _valence_band(valence: float) -> str:
    if valence <= -0.5:
        return "very_negative"
    if valence <= -0.15:
        return "negative"
    if valence < 0.15:
        return "neutral"
    if valence < 0.5:
        return "positive"
    return "very_positive"


def _arousal_index(arousal: float) -> int:
    """0 = low energy, 1 = mid, 2 = high."""
    if arousal < 0.35:
        return 0
    if arousal <= 0.65:
        return 1
    return 2


def felt_phrase(valence: float, arousal: float) -> str:
    """Direct felt-language description of a (valence, arousal) point.

    Pure and clamp-tolerant: out-of-range inputs just land in the outer
    bands. Always returns a non-empty phrase.
    """
    try:
        v = float(valence)
        a = float(arousal)
    except (TypeError, ValueError):
        v, a = 0.0, 0.5
    return _FELT_PHRASES[_valence_band(v)][_arousal_index(a)]


def render_ambient_block(
    state: AffectState,
    *,
    trend_threshold: float = 0.15,
) -> str:
    """Format the affect state as a 1-2 line cue for the system prompt.

    Phrased as a private feeling, never as a directive: the persona is
    responsible for tone. The "lately" trend line is suppressed when the
    delta is too small to be meaningful.

    K44: the valence/arousal scalars render as felt-language, never as
    floats. The ``mood_label`` opener is kept alongside the phrase on
    purpose — the label tracks the fast per-turn reaction tags while the
    phrase tracks the smoothed scalars, so a disagreement between them
    ("content" but reading flat) is real signal, not redundancy.
    """
    label = (state.mood_label or "content").replace("_", " ")
    primary = (
        f"You're feeling {label} — "
        f"{felt_phrase(state.valence, state.arousal)}."
    )
    trend_phrase = _trend_phrase(state.valence_trend_24h, trend_threshold)
    if trend_phrase:
        return f"{primary}\n{trend_phrase}"
    return primary


def _trend_phrase(valence_trend: float, threshold: float) -> str:
    """Render the small "lately you've been..." cue, or empty if too weak."""
    if abs(valence_trend) < float(threshold):
        return ""
    if valence_trend > 0:
        return "Lately (over the last day) you've been a touch more upbeat than usual."
    return "Lately (over the last day) you've felt a little flatter than usual."
