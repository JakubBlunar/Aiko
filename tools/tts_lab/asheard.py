"""Audition through the app's playback path instead of around it.

The lab used to hand ``generate_audio``'s array straight to a wav, which
meant it played the *engine* while Aiko plays the engine plus four shaping
stages -- brightness, level, tempo and the stretch. Reported the honest way
round: "it sounded good in the tts lab but it's not that good in real
usage." A voice tuned by ear against a signal the app never produces is
tuned against nothing.

So this module answers one question -- *what will the app do to this clip*
-- by building the same :class:`~app.tts.shaping.Shaping` the engine will
build and calling the same :func:`~app.tts.shaping.shape_clip`. Nothing is
reimplemented here; if it were, the lab would drift from the app again and
the drift is exactly what this is for.

What the lab adds is the report. The app has no reason to know how many dB
of shelf a clip took, but somebody deciding between two references does,
so the before/after measurements that production would be wasting time on
are taken here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.audio.timbre import spectral_tilt_db
from app.core.infra.settings import load_settings
from app.tts.reactions import resolve_playback_speed
from app.tts.shaping import (
    Shaping,
    loudness_target_for,
    measure_rate_target,
    measure_tilt_target,
    shape_clip,
)

#: Engines whose service derives brightness and tempo targets from the
#: reference clip when it clones. Mirrors ``ChatterboxTtsService._clone``.
#:
#: Deliberately a separate list from ``shaping.RAW_LEVEL_ENGINES`` even
#: though the two currently partition the same engines. They answer
#: different questions -- "does this engine match its level" and "does this
#: engine have a reference to measure targets from" -- and folding them
#: into one set would make a future engine that clones but ships its own
#: level impossible to express.
CLONES_FROM_REFERENCE = frozenset(
    {"chatterbox-nano", "chatterbox-turbo", "chatterbox-multilingual"}
)


@dataclass
class Preview:
    """A shaped clip and what each stage did to it."""

    audio: np.ndarray
    sample_rate: int
    #: Stage-by-stage, for display. Values are already rounded for the UI.
    report: dict[str, object] = field(default_factory=dict)


def shaping_for(engine: str, reference: Path | None) -> Shaping:
    """The shaping the app will apply for this engine and voice.

    Targets come from the reference exactly as the service derives them on
    clone, so a reference with no manifest gets no tempo target here for
    the same reason it gets none in production.
    """
    settings = load_settings().tts
    key = (engine or "").strip().lower()
    tilt: float | None = None
    rate: float | None = None
    if key in CLONES_FROM_REFERENCE and reference is not None:
        limit = float(getattr(settings, "timbre_match_limit_db", 0.0) or 0.0)
        if limit > 0.0:
            tilt = measure_tilt_target(reference)
        if float(getattr(settings, "speech_rate_match_limit", 0.0) or 0.0) > 0:
            target = measure_rate_target(reference)
            rate = target.syl_s if target else None
    return Shaping(
        loudness_target_dbfs=loudness_target_for(key, settings),
        tilt_target_db=tilt,
        tilt_limit_db=float(
            getattr(settings, "timbre_match_limit_db", 0.0) or 0.0
        ),
        rate_target_syl_s=rate,
        rate_limit=float(
            getattr(settings, "speech_rate_match_limit", 0.0) or 0.0
        ),
        pitch_preserving_speed=bool(
            getattr(settings, "pitch_preserving_speed", True)
        ),
    )


def resting_speed() -> float:
    """The rate a neutral sentence gets, with no reaction in play.

    Not hardcoded to 1.0: the pacing slider applies whether or not the
    affect channel is gated, so a user who has moved it should hear that
    in an audition. With the shipped defaults this *is* 1.0.
    """
    settings = load_settings()
    return resolve_playback_speed(
        None,
        None,
        runtime_speed_enabled=bool(
            getattr(settings.agent, "tts_runtime_speed_enabled", False)
        ),
        length_scale=float(
            getattr(settings.assistant, "tts_length_scale", 1.0) or 1.0
        ),
    )


def apply(
    audio: np.ndarray,
    sample_rate: int,
    *,
    engine: str,
    reference: Path | None,
    text: str,
) -> Preview:
    """Shape one clip the way the app will, and say what happened."""
    shaping = shaping_for(engine, reference)
    speed = resting_speed()
    before_tilt = _tilt(audio, sample_rate)

    shaped = shape_clip(
        audio,
        sample_rate,
        shaping=shaping,
        speed=speed,
        gain_factor=1.0,
        text=text,
    )
    rendered = shaped.rendered()
    after_tilt = _tilt(rendered, shaped.playback_rate)

    stages: list[str] = []
    if shaped.tilt_applied:
        stages.append("brightness")
    if abs(shaped.level_gain_db) > 0.05:
        stages.append("level")
    if abs(shaped.tempo_factor - 1.0) > 0.005:
        stages.append("tempo")
    if shaped.stretched:
        stages.append("stretch")

    return Preview(
        audio=rendered,
        sample_rate=shaped.playback_rate,
        report={
            "stages": stages,
            "inert": shaping.is_inert() and abs(speed - 1.0) <= 1e-3,
            "level_target_dbfs": (
                round(shaping.loudness_target_dbfs, 1)
                if shaping.loudness_target_dbfs < 0.0
                else None
            ),
            "level_gain_db": round(shaped.level_gain_db, 2),
            "tilt_target_db": (
                round(shaping.tilt_target_db, 2)
                if shaping.tilt_target_db is not None
                else None
            ),
            "tilt_before_db": _rounded(before_tilt),
            "tilt_after_db": _rounded(after_tilt),
            "rate_target_syl_s": (
                round(shaping.rate_target_syl_s, 2)
                if shaping.rate_target_syl_s is not None
                else None
            ),
            "tempo_factor": round(shaped.tempo_factor, 3),
            "speed": round(shaped.speed, 3),
        },
    )


def _tilt(audio: np.ndarray, sample_rate: int) -> float | None:
    """Spectral tilt, or ``None`` when it cannot be measured.

    Only ever used for the report, so a failure here must not cost the
    audition -- the clip is fine, the number beside it is missing.
    """
    try:
        value = spectral_tilt_db(audio, sample_rate)
    except Exception:
        return None
    return float(value) if value else None


def _rounded(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None
