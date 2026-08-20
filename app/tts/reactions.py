"""How each reaction changes her speaking rate.

Lifted out of ``pocket_tts_service`` when Chatterbox arrived. These
tables describe *Aiko*, not an engine: how much faster she gets when
excited, how far she is allowed to drag when she is crying. Which model
renders that is irrelevant, so a provider swap must not change it -- and
would have, had the second engine grown its own copy.

Importing them had to stop costing PyTorch, too. ``pocket_tts_service``
does ``from pocket_tts import TTSModel`` at module scope, so reading one
dict out of it pulled the entire torch runtime into the process -- which
is precisely what the lazy provider registry exists to avoid.

Comments and values are unchanged from the original; the speed bands were
tuned by ear against the varispeed pitch shift that used to be
unavoidable, and are conservative now that
:mod:`app.audio.timestretch` holds pitch steady.
"""

from __future__ import annotations

# Reaction-to-speed multipliers. Capped to ±8% so the samplerate-only
# pitch shift didn't fall into chipmunk territory at the high end or
# "underwater" at the low end. These are the *baseline* per-reaction
# speeds; the cadence layer can further nudge per-sentence via the
# ``speed`` kwarg on ``speak_async``. Must cover every name in
# ``app.core.affect.reactions.REACTIONS`` (locked by
# ``tests/test_reaction_table_coverage.py``): the lookup falls back to
# 1.0, so a canonical reaction missing here is not an error, just a
# permanently flat delivery for that shade.
REACTION_SPEED: dict[str, float] = {
    "excited":      1.08,
    "enthusiastic": 1.07,
    "cheerful":     1.06,
    "amused":       1.05,
    "playful":      1.05,
    "surprised":    1.06,
    "curious":      1.04,
    "friendly":     1.02,
    "warm":         1.00,
    "tender":       0.97,
    "neutral":      1.00,
    "thoughtful":   0.96,
    "wistful":      0.95,
    "calm":         0.95,
    "serious":      0.95,
    "concerned":    0.94,
    "sad":          0.93,
    "melancholy":   0.93,
    # ``cry`` is the slowest reaction — choked / strained delivery
    # right at the safe-range floor (any lower would cross into
    # underwater-pitch territory after the samplerate-only shift).
    "cry":          0.92,
    "tired":        0.93,
    "gentle":       0.94,
    "angry":        1.04,
    "frustrated":   1.03,
    "confused":     0.97,
    "embarrassed":  1.01,
    "nervous":      1.04,
    "defiant":      1.02,
    "smug":         1.01,
    "pouty":        1.01,
    "sulky":        0.95,
    "mischievous":  1.04,
}

# Hard caps applied AFTER any caller-supplied speed, so a runaway
# cadence multiplier can't push us into uncanny territory. The base
# floor and ceiling are widened slightly from the historic ±8% to
# ±12% so the loudest / quietest reactions can stretch further; the
# per-reaction sub-cap table below pins each reaction back to a
# safe band so only the ones that actually want the extra room
# (cry, tired, sad, excited, surprised) get to use it.
SPEED_MIN = 0.88
SPEED_MAX = 1.12

# Per-reaction sub-caps. A reaction that isn't listed falls back to
# the historic ±8% band ``[0.92, 1.08]`` -- the same envelope the
# samplerate-only pitch shift was originally tuned against. Only the
# entries below get to use the wider outer band.
REACTION_SPEED_CAPS: dict[str, tuple[float, float]] = {
    # Lower-end stretch: sob / strained / drained delivery. ``cry``
    # already sat at the old floor, ``tired`` and ``sad`` /
    # ``melancholy`` had no headroom to drop further when the
    # context piled on (drowsy circadian, noisy room).
    "cry":        (0.88, 1.00),
    "tired":      (0.90, 1.00),
    "sad":        (0.91, 1.00),
    "melancholy": (0.91, 1.00),
    # Upper-end stretch: a genuine "!" beat and surprise reaction
    # both want to outrun the regular cheerful band by a hair.
    "excited":    (1.00, 1.12),
    "surprised":  (1.00, 1.10),
}

_DEFAULT_BAND = (0.92, 1.08)


def resolve_speed_caps(reaction: str | None) -> tuple[float, float]:
    """Return the ``(min, max)`` clamp for ``reaction``.

    Falls back to the legacy ±8% envelope when the reaction has no
    explicit override.
    """
    if not (reaction or "").strip():
        return _DEFAULT_BAND
    return REACTION_SPEED_CAPS.get(
        (reaction or "").strip().lower(), _DEFAULT_BAND
    )


def reaction_to_speed(reaction: str | None) -> float:
    """Baseline rate multiplier for a reaction; 1.0 for anything unknown."""
    key = (reaction or "").strip().lower()
    if not key:
        return 1.0
    return float(REACTION_SPEED.get(key, 1.0))


# Hard caps on the user-facing pacing slider, which feeds
# ``set_length_scale``. Values outside this band are clamped silently.
# Narrower than the ``[0.65, 1.35]`` of :class:`AssistantSettings`
# because the slider stacks multiplicatively with reaction speed AND the
# cadence layer's per-sentence ``speed_hint``, so a 0.65 slider would
# routinely blow past the per-reaction floor.
LENGTH_SCALE_MIN = 0.85
LENGTH_SCALE_MAX = 1.15


def clamp_length_scale(scale: float) -> float:
    """The pacing slider, bounded. Non-numeric and zero read as 1.0."""
    try:
        value = float(scale)
    except (TypeError, ValueError):
        return 1.0
    if value <= 0.0:
        return 1.0
    return max(LENGTH_SCALE_MIN, min(LENGTH_SCALE_MAX, value))


def resolve_playback_speed(
    reaction: str | None,
    requested: float | None,
    *,
    runtime_speed_enabled: bool,
    length_scale: float = 1.0,
) -> float:
    """The final rate multiplier for one sentence.

    Shared rather than per-engine for the same reason as the tables
    above: this is how *Aiko* paces herself, so a provider swap must not
    change it. It did. Only pocket-tts implemented the gate and the
    slider, so on Chatterbox the affect-driven speed channel ran
    unrestrained and uncapped while the incumbent pinned it flat, and the
    user's pacing slider silently did nothing at all — she simply spoke
    faster on one engine than the other, for no stated reason.

    Two independent knobs, deliberately:

    * ``runtime_speed_enabled`` gates the *affect* channel. Off (the
      default) pins every sentence to 1.0 before the slider, ignoring
      both the per-reaction baseline and any cadence ``speed_hint``, so
      delivery stays flat across a reply.
    * ``length_scale`` is the user's static pacing preference and applies
      either way — a deliberate global knob, not affect drift. Above 1.0
      is slower.
    """
    if not runtime_speed_enabled:
        speed = 1.0
    else:
        if requested is None:
            speed = reaction_to_speed(reaction)
        else:
            try:
                speed = float(requested)
            except (TypeError, ValueError):
                speed = reaction_to_speed(reaction)
        # Per-reaction sub-cap first, then the global outer envelope, so
        # a runaway cadence multiplier cannot reach uncanny territory.
        sub_min, sub_max = resolve_speed_caps(reaction)
        speed = max(sub_min, min(sub_max, speed))
        speed = max(SPEED_MIN, min(SPEED_MAX, speed))
    # Applied after the reaction clamp so a slow pacing preference does
    # not fight the per-reaction floor (cry sits near 0.92; dividing by
    # 1.10 lands at ~0.84, below SPEED_MIN — the final clamp catches it).
    scale = clamp_length_scale(length_scale)
    if abs(scale - 1.0) > 1e-3:
        speed = speed / scale
    return max(SPEED_MIN, min(SPEED_MAX, speed))
