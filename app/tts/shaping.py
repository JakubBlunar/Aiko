"""What happens to a clip between synthesis and being heard.

Four stages sit between an engine's output and the samples that reach the
client: brightness, level, tempo, and the time-stretch that carries the
first three plus any affect-driven speed. They are the reason a clip does
not sound the way the model made it, and until this module existed they
lived inside ``PcmPlaybackMixin._play_clip``, interleaved with the
emission loop that paces bytes onto a WebSocket.

Which meant the audition lab could not run them. ``tools/tts_lab`` calls
``generate_audio`` and writes the array to a wav, so what it played was the
*engine*, while Aiko plays the engine plus these four stages -- and a voice
tuned by ear in the lab was tuned against a signal the app never produces.
That gap was reported the honest way round: "it sounded good in the tts lab
but it's not that good in real usage."

So the shaping is separated from the shipping. :func:`shape_clip` is pure --
audio in, audio out, no threads, no sockets, no clock -- and both callers
use it, which is the only arrangement in which the lab cannot drift away
from the app.

The targets, and why they come from the reference
-------------------------------------------------
:func:`measure_tilt_target` and :func:`measure_rate_target` derive what a
clip is matched *to* from the voice being cloned, so they belong here for
the same reason: the lab has to derive them identically or it is auditioning
a different voice than the one that will speak. Both are optional by
design. A bare wav somebody dropped in gets no tempo target, and guessing
one for a stranger's clip would be worse than leaving pacing alone.
"""

from __future__ import annotations

import json
import logging
import wave
from dataclasses import dataclass
from pathlib import Path
from statistics import median

import numpy as np

from app.audio.loudness import correction_factor
from app.audio.speech_rate import MAX_CORRECTION as MAX_RATE_CORRECTION
from app.audio.speech_rate import correction_factor as rate_correction_factor
from app.audio.speech_rate import measured_rate
from app.audio.timbre import MAX_CORRECTION_DB, match_tilt, spectral_tilt_db
from app.audio.timestretch import time_stretch

log = logging.getLogger(__name__)

#: Appended to every clip. Gives the client's scheduler somewhere to land
#: and stops the last phoneme being clipped by the stream ending exactly
#: on it.
GUARD_SILENCE_SECONDS = 0.15

#: Fewest measurable parts before a manifest's median is trusted as a
#: tempo target. Two phrases whose syllable estimates are both off would
#: otherwise set her pace for good.
MIN_RATE_PARTS = 3

#: Engines whose default is to ship the level the model produced.
#:
#: pocket-tts is here because it is the voice every other setting was
#: tuned against by ear, and matching its level was heard as "she stopped
#: being lively" -- about a third of its between-sentence spread tracks
#: the intended energy of the line (H48). A cloning engine re-samples her
#: level on every call and the drift dominates that, so it is not here.
RAW_LEVEL_ENGINES = frozenset({"pocket-tts"})


def resolve_loudness_target(
    settings: object, provider: str, *, default: float
) -> float:
    """The level target for one engine, in dBFS. ``0.0`` means off.

    Two tiers, because the right answer is not the same for every engine.
    ``tts.providers.<name>.loudness_target_dbfs`` wins when present;
    otherwise the engine's own ``default``. Most callers want
    :func:`loudness_target_for`, which knows what those defaults are.

    Tolerates a settings object with no ``for_provider`` -- several tests
    build one from a namespace, and an engine that cannot read an optional
    override should fall back rather than fail to construct.
    """
    for_provider = getattr(settings, "for_provider", None)
    if callable(for_provider):
        try:
            override = for_provider(provider).loudness_target_dbfs
        except Exception:
            log.debug("per-provider loudness lookup failed", exc_info=True)
            override = None
        if override is not None:
            return float(override)
    return float(default or 0.0)


def loudness_target_for(provider: str, settings: object) -> float:
    """The level target one engine will actually use.

    The engines apply their own default inline, which was fine while they
    were the only callers and stopped being fine when the audition lab
    needed to predict them: a lab that guesses this wrong previews a
    different voice than the one that speaks.
    """
    key = (provider or "").strip().lower()
    default = (
        0.0
        if key in RAW_LEVEL_ENGINES
        else float(getattr(settings, "loudness_target_dbfs", 0.0) or 0.0)
    )
    return resolve_loudness_target(settings, key, default=default)


@dataclass(frozen=True)
class Shaping:
    """The per-engine targets every clip is held to.

    All four stages default to inert, so an engine that sets none of them
    gets its output shipped untouched. That is not a theoretical case:
    pocket-tts deliberately sets none of them (H48), and a Chatterbox
    voice with no manifest beside it has no tempo target.
    """

    #: Gated speech level to match, in dBFS. ``0.0`` leaves levels alone.
    loudness_target_dbfs: float = 0.0
    #: Spectral tilt to shelve toward. ``None`` leaves brightness alone.
    tilt_target_db: float | None = None
    tilt_limit_db: float = MAX_CORRECTION_DB
    #: Delivered tempo to stretch toward, in syllables per second.
    #: ``None`` leaves pacing alone.
    rate_target_syl_s: float | None = None
    rate_limit: float = MAX_RATE_CORRECTION
    #: Whether a rate change goes through the stretch or old varispeed.
    pitch_preserving_speed: bool = True

    @classmethod
    def of(cls, host: object) -> Shaping:
        """Read the targets off an engine.

        Every read is defaulted, because these attributes arrived one
        feature at a time and an engine written before any of them still
        has to play.
        """
        return cls(
            loudness_target_dbfs=float(
                getattr(host, "_loudness_target_dbfs", 0.0) or 0.0
            ),
            tilt_target_db=getattr(host, "_tilt_target_db", None),
            tilt_limit_db=float(
                getattr(host, "_tilt_limit_db", MAX_CORRECTION_DB)
            ),
            rate_target_syl_s=getattr(host, "_rate_target_syl_s", None),
            rate_limit=float(
                getattr(host, "_rate_limit", MAX_RATE_CORRECTION)
            ),
            pitch_preserving_speed=bool(
                getattr(host, "_pitch_preserving_speed", True)
            ),
        )

    def is_inert(self) -> bool:
        """True when shaping would return the clip unchanged at speed 1."""
        return (
            self.loudness_target_dbfs >= 0.0
            and self.tilt_target_db is None
            and self.rate_target_syl_s is None
        )


@dataclass(frozen=True)
class Shaped:
    """A shaped clip, plus what each stage decided.

    ``audio`` is pre-gain, because the level correction is folded into
    :attr:`gain_factor` rather than multiplied into the array -- the
    emission path already has exactly one multiply for it, and a second
    pass over a three-second clip buys nothing. Anything that wants the
    samples as they will actually *sound* wants :meth:`rendered`.

    The report fields are the ones that were computed anyway, so carrying
    them is free. Brightness is absent deliberately: ``match_tilt`` does
    not report its shelf gain, and a caller holding both the raw and the
    shaped array can measure the difference itself.
    """

    audio: np.ndarray
    playback_rate: int
    gain_factor: float
    #: Final rate multiplier, after tempo matching folded into it.
    speed: float
    stretched: bool
    tilt_applied: bool
    #: What the level stage asked for, in dB. ``0.0`` when it was off.
    level_gain_db: float
    #: What the tempo stage asked for, as a factor. ``1.0`` when off.
    tempo_factor: float

    def rendered(self) -> np.ndarray:
        """The samples as the client will hear them, gain and all.

        Mirrors the emission path's one multiply and its saturation, so a
        clip written to a wav from here is what came out of the speaker.
        """
        if abs(self.gain_factor - 1.0) <= 1e-3:
            return np.clip(self.audio, -1.0, 1.0)
        return np.clip(self.audio * self.gain_factor, -1.0, 1.0)


def shape_clip(
    audio: np.ndarray,
    sample_rate: int,
    *,
    shaping: Shaping,
    speed: float = 1.0,
    gain_factor: float = 1.0,
    text: str = "",
) -> Shaped:
    """Apply the four stages to one clip.

    ``text`` is what is being spoken and is used only to measure the
    clip's delivered tempo, so a caller without it gets tempo matching
    inactive rather than an error.

    Every stage fails open and says so. A sentence in the wrong shade of
    warm beats no sentence, and the one place that cannot simply skip --
    the stretch, which carries a rate the caller asked for -- falls back
    to varispeed, because wrong pitch is survivable and silence is not.
    """
    # Brightness first, because the shelf changes the level that would
    # otherwise be measured against.
    tilt_applied = False
    if shaping.tilt_target_db is not None:
        try:
            audio = match_tilt(
                audio,
                sample_rate,
                target_tilt_db=float(shaping.tilt_target_db),
                limit_db=float(shaping.tilt_limit_db),
            )
            tilt_applied = True
        except Exception as exc:
            log.warning("timbre match failed, using the clip as-is: %r", exc)

    # Level, measured on the raw speech so the guard silence appended
    # below cannot drag the gate's estimate down.
    level_gain_db = 0.0
    if shaping.loudness_target_dbfs < 0.0:
        correction = correction_factor(
            audio, sample_rate, target_dbfs=shaping.loudness_target_dbfs
        )
        gain_factor = float(gain_factor) * correction
        level_gain_db = 20.0 * float(np.log10(max(correction, 1e-12)))

    # Tempo folds into the speed factor rather than stretching twice, and
    # only when the stretch preserves pitch: correcting a tempo wobble
    # through varispeed would trade it for a pitch wobble, which is worse.
    tempo_factor = 1.0
    if (
        shaping.rate_target_syl_s is not None
        and shaping.pitch_preserving_speed
        and text
    ):
        try:
            tempo_factor = rate_correction_factor(
                audio,
                sample_rate,
                text,
                target_syl_s=float(shaping.rate_target_syl_s),
                intended=float(speed),
                limit=float(shaping.rate_limit),
            )
            speed = float(speed) * tempo_factor
        except Exception as exc:
            log.warning("tempo match failed, using the clip as-is: %r", exc)

    # The rate change lands on the speech only, before the guard silence
    # is appended -- stretching a fixed tail would make the guard itself
    # depend on her mood.
    stretched = False
    if abs(speed - 1.0) > 1e-3 and shaping.pitch_preserving_speed:
        try:
            audio = time_stretch(audio, speed, sample_rate)
            stretched = True
        except Exception as exc:
            log.warning(
                "time-stretch failed, falling back to varispeed: %r", exc,
            )

    silence = np.zeros(
        int(sample_rate * GUARD_SILENCE_SECONDS), dtype=np.float32
    )
    audio = np.concatenate([audio.reshape(-1), silence])

    # After a stretch the duration already lives in the sample count, so
    # the honest native rate goes to the client. Varispeed instead
    # declares a scaled rate and lets the client play the same samples
    # faster, which moves pitch with duration.
    playback_rate = (
        sample_rate
        if stretched or abs(speed - 1.0) <= 1e-3
        else int(sample_rate * speed)
    )
    return Shaped(
        audio=audio,
        playback_rate=playback_rate,
        gain_factor=float(gain_factor),
        speed=float(speed),
        stretched=stretched,
        tilt_applied=tilt_applied,
        level_gain_db=level_gain_db,
        tempo_factor=float(tempo_factor),
    )


# ── deriving the targets from a reference clip ────────────────────────


def read_wav_mono(path: Path) -> tuple[np.ndarray, int]:
    """Int16 WAV as float32 in [-1, 1], extra channels averaged.

    Stdlib only, and deliberately: this reads sidecar output on the hot
    path, where a few hundred KB on an NVMe costs about a millisecond
    against a synthesis measured in hundreds, and a decoder in the loop
    would cost more than the read.
    """
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        frames = handle.readframes(handle.getnframes())
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio, int(rate)


def measure_tilt_target(reference: Path) -> float | None:
    """Brightness target from the clip being cloned, or ``None``.

    The reference is what her voice is supposed to sound like, which
    makes it a better target than any constant: it flattens the
    per-sentence timbre drift *and* closes the 1.2-1.5 dB high-frequency
    deficit that reads as "a little muffled".
    """
    try:
        audio, rate = read_wav_mono(reference)
        target = spectral_tilt_db(audio, rate)
    except Exception as exc:
        log.warning(
            "could not measure %s's timbre, brightness matching is off: %r",
            reference.name,
            exc,
        )
        return None
    return float(target) if target else None


@dataclass(frozen=True)
class RateTarget:
    """A tempo target and how it was arrived at.

    The provenance is not decoration. "Declared" and "measured over 9
    parts" fail in opposite directions, and a target that turns out to be
    wrong is diagnosed entirely differently depending on which it was.
    """

    syl_s: float
    #: ``"declared"`` or ``"measured"``.
    source: str
    #: Parts that yielded a rate. ``0`` for a declared target.
    parts: int


def measure_rate_target(reference: Path) -> RateTarget | None:
    """Tempo target from the reference's own manifest, or ``None``.

    Measuring a rate needs text as well as audio, so unlike brightness
    this cannot be read off the wav. What it can be read off is the
    ``manifest.json`` the reference was built with, which lists each
    part's phrase beside its file: measure every part and take the
    median, which is robust to one phrase's syllable estimate being off.

    A manifest may also **declare** ``target_syl_s`` outright, which the
    measured route cannot always reach. Her recovered source pack is
    Japanese game audio -- unquestionably her voice, with no honest
    English transcript to measure a rate from -- so a reference built out
    of it gets no target and delivers about 8% slow with nothing able to
    correct it. Declaring the target says "this is her, hold her to her
    own pace", and is the one case where a constant beats a measurement.
    """
    manifest = reference.parent / "manifest.json"
    if not manifest.is_file():
        return None
    try:
        body = json.loads(manifest.read_text(encoding="utf-8"))
        declared = float(body.get("target_syl_s") or 0.0)
        if declared > 0.0:
            return RateTarget(syl_s=declared, source="declared", parts=0)
        rates: list[float] = []
        for part in body.get("parts") or []:
            phrase = str(part.get("phrase") or "")
            name = str(part.get("file") or "")
            if not phrase or not name:
                continue
            clip = manifest.parent / "parts" / name
            if not clip.is_file():
                continue
            audio, rate = read_wav_mono(clip)
            measured = measured_rate(audio, rate, phrase)
            if measured > 0.0:
                rates.append(measured)
    except Exception as exc:
        log.warning(
            "could not measure %s's tempo, matching is off: %r",
            reference.name,
            exc,
        )
        return None
    if len(rates) < MIN_RATE_PARTS:
        return None
    return RateTarget(
        syl_s=float(median(rates)), source="measured", parts=len(rates)
    )
