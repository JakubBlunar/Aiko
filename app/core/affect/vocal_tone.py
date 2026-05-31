"""Cheap vocal-tone analysis from a captured speech WAV.

Pure-function module that turns a short WAV (the same one Whisper is
about to transcribe) into a small structured signal describing how the
user *sounded*, not what they said. The signal flows three places:

  1. The system prompt — a one-line "User sounds: …" cue so the LLM
     can pick a matching tone (excited reply for excited speech, etc.).
  2. :class:`AffectUpdater` — the ``user_tone`` kwarg nudges Aiko's
     arousal on top of the reaction-based impulse.
  3. The cadence / TTS pipeline — could later modulate Aiko's response
     speed (low-energy user → calmer, slower reply).

Computational cost: a single FFT + zero-crossing-rate + RMS pass over a
mono int16 WAV. Typical phrase is 1-3 seconds; this is well under 5 ms
on a desktop CPU. Safe to call on the live-capture thread right before
``transcribe(wav)`` — Whisper takes 100-500 ms anyway, so we never
become the bottleneck.

The module avoids extra dependencies: it uses ``wave`` from the stdlib
plus ``numpy`` (already a hard dep via Pocket-TTS). No librosa, no
scipy.
"""
from __future__ import annotations

import logging
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy is a hard dep elsewhere
    np = None  # type: ignore[assignment]


log = logging.getLogger("app.vocal_tone")


EnergyBucket = Literal["low", "moderate", "high"]
PitchBucket = Literal["low", "mid", "high"]
PaceBucket = Literal["slow", "normal", "fast"]


# Bucket thresholds, calibrated against typical close-mic speech recorded
# at -20..-6 dBFS. The "moderate" range covers ~70% of casual phrases;
# "low" / "high" only fire on visibly quieter / more energetic speech so
# the prompt cue stays meaningful when present.
_RMS_LOW = 0.018      # ≈ -35 dBFS
_RMS_HIGH = 0.080     # ≈ -22 dBFS

# Pitch buckets in Hz, after a coarse autocorrelation peak pick. These
# brackets are gender-neutral on purpose — we don't try to identify the
# speaker, only whether the current phrase sits below / above their own
# typical band. The "mid" band intentionally covers ~140-220 Hz.
_PITCH_LOW = 140.0
_PITCH_HIGH = 220.0

# Pace buckets, in syllables-per-second proxy: zero-crossing rate per
# voiced frame. Calibrated so neutral conversational pace lands "normal".
_PACE_SLOW = 30.0
_PACE_FAST = 70.0


@dataclass(slots=True, frozen=True)
class VocalTone:
    """A small structured signal describing how the user sounded.

    All fields are coarse-grained so the LLM can latch onto them without
    being misled by a noisy estimate. ``arousal_hint`` is in [-0.10,
    +0.10] and is the recommended delta to feed into
    :class:`AffectUpdater`.
    """

    energy: EnergyBucket
    pitch: PitchBucket
    pace: PaceBucket
    arousal_hint: float = 0.0
    # Raw values kept for tests and future tuning. Not used in the prompt.
    rms: float = 0.0
    pitch_hz: float = 0.0
    zcr: float = 0.0
    # Did we have enough voiced signal to trust the estimate? When False,
    # ``to_prompt_line()`` returns "" so we don't ship a guess.
    confident: bool = False
    samples: int = 0
    # Tags accumulate qualitative descriptors that the prompt builder
    # turns into a comma-joined cue ("low energy, slow pace").
    tags: tuple[str, ...] = field(default_factory=tuple)

    def to_prompt_line(self) -> str:
        """Return a one-line system-prompt cue, or empty when low confidence."""
        if not self.confident or not self.tags:
            return ""
        return f"User sounds: {', '.join(self.tags)}."


def _read_mono_float32(path: Path) -> tuple["np.ndarray | None", int]:
    """Decode a WAV file into a mono float32 array in [-1, 1]."""
    if np is None:
        return None, 0
    try:
        with wave.open(str(path), "rb") as wav:
            rate = wav.getframerate()
            nch = wav.getnchannels()
            width = wav.getsampwidth()
            n_frames = wav.getnframes()
            raw = wav.readframes(n_frames)
    except Exception:
        log.debug("vocal tone: failed to open %s", path, exc_info=True)
        return None, 0

    if not raw:
        return None, rate
    # Map sample width to numpy dtype; Pocket-TTS / mic_capture writes
    # int16, but we tolerate 24-bit and float just in case.
    if width == 2:
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        try:
            arr = np.frombuffer(raw, dtype=np.float32)
        except ValueError:
            arr = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif width == 1:
        arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        return None, rate
    if nch > 1:
        try:
            arr = arr.reshape(-1, nch).mean(axis=1)
        except Exception:
            arr = arr[::nch]
    return arr.astype(np.float32, copy=False), rate


def _voiced_mask(samples: "np.ndarray", sample_rate: int) -> "np.ndarray":
    """Return a boolean mask of frames whose RMS exceeds a small floor.

    The floor is set well below conversational speech (-50 dBFS) so we
    only cut out true silence between words, not quiet syllables.
    """
    frame = max(1, int(sample_rate * 0.020))  # 20 ms frames
    if samples.size < frame:
        return np.zeros(0, dtype=bool)
    n = (samples.size // frame) * frame
    blocks = samples[:n].reshape(-1, frame)
    rms = np.sqrt(np.mean(blocks * blocks, axis=1) + 1e-12)
    floor = max(1e-4, float(rms.max() * 0.10))
    return rms > floor


def _autocorr_pitch(voiced: "np.ndarray", sample_rate: int) -> float:
    """Estimate fundamental frequency via autocorrelation peak pick.

    Restricted to the human speech range [70, 400] Hz. Returns 0.0 when
    the autocorrelation lacks a confident peak (unvoiced / noisy frame).
    """
    if voiced.size < sample_rate // 8:
        return 0.0
    # Center, window, and de-mean to suppress DC.
    seg = voiced[: sample_rate // 4]  # at most 250 ms; cheap.
    seg = seg - float(seg.mean())
    win = np.hanning(seg.size).astype(np.float32)
    seg = seg * win
    corr = np.correlate(seg, seg, mode="full")
    corr = corr[corr.size // 2:]
    # Search lags corresponding to 70..400 Hz.
    lag_min = max(2, int(sample_rate / 400.0))
    lag_max = int(sample_rate / 70.0)
    if lag_max <= lag_min or lag_max >= corr.size:
        return 0.0
    region = corr[lag_min:lag_max]
    if region.size == 0:
        return 0.0
    peak = int(np.argmax(region)) + lag_min
    if corr[peak] <= 0.10 * float(corr[0] + 1e-9):
        return 0.0
    return float(sample_rate) / float(peak)


def _zero_crossing_rate(voiced: "np.ndarray", sample_rate: int) -> float:
    """Zero crossings per second for the voiced portion. Loose pace proxy."""
    if voiced.size < 2:
        return 0.0
    signs = np.signbit(voiced)
    crossings = int(np.sum(signs[:-1] ^ signs[1:]))
    seconds = voiced.size / float(sample_rate)
    if seconds <= 0:
        return 0.0
    return crossings / seconds


def analyse_wav(path: Path | str) -> VocalTone:
    """Run the full pipeline on a WAV path. Always returns a ``VocalTone``;
    sets ``confident=False`` when the file was too short or unreadable so
    callers can choose to skip the prompt cue."""
    if np is None:
        return VocalTone(
            energy="moderate", pitch="mid", pace="normal", confident=False,
        )
    samples, rate = _read_mono_float32(Path(path))
    if samples is None or rate <= 0 or samples.size < int(rate * 0.20):
        return VocalTone(
            energy="moderate", pitch="mid", pace="normal", confident=False,
        )

    mask = _voiced_mask(samples, rate)
    # Re-expand the frame mask back to a sample-level slice. Approximate
    # is fine because the analysis is statistical.
    frame = max(1, int(rate * 0.020))
    if mask.size == 0:
        return VocalTone(
            energy="moderate", pitch="mid", pace="normal", confident=False,
            samples=int(samples.size),
        )
    voiced = samples[: mask.size * frame].reshape(-1, frame)[mask].reshape(-1)
    if voiced.size < int(rate * 0.15):
        return VocalTone(
            energy="moderate", pitch="mid", pace="normal", confident=False,
            samples=int(samples.size),
        )

    rms = float(np.sqrt(np.mean(voiced * voiced) + 1e-12))
    pitch_hz = _autocorr_pitch(voiced, rate)
    zcr = _zero_crossing_rate(voiced, rate)

    energy: EnergyBucket = (
        "low" if rms < _RMS_LOW
        else "high" if rms > _RMS_HIGH
        else "moderate"
    )
    pitch: PitchBucket = (
        "low" if 0 < pitch_hz < _PITCH_LOW
        else "high" if pitch_hz > _PITCH_HIGH
        else "mid"
    )
    pace: PaceBucket = (
        "slow" if zcr < _PACE_SLOW
        else "fast" if zcr > _PACE_FAST
        else "normal"
    )

    # Build the qualitative tag list. We only mention dimensions that
    # depart from "moderate / mid / normal" so the cue stays terse.
    tags: list[str] = []
    if energy == "low":
        tags.append("low energy")
    elif energy == "high":
        tags.append("high energy")
    if pitch == "low":
        tags.append("lower pitch")
    elif pitch == "high":
        tags.append("higher pitch")
    if pace == "slow":
        tags.append("slow pace")
    elif pace == "fast":
        tags.append("quick pace")

    # Map the buckets to a small arousal nudge. High energy / fast / high
    # pitch = +arousal; low-everything = -arousal. Capped at ±0.10.
    arousal_score = 0.0
    arousal_score += {"low": -0.05, "moderate": 0.0, "high": +0.05}[energy]
    arousal_score += {"slow": -0.03, "normal": 0.0, "fast": +0.03}[pace]
    arousal_score += {"low": -0.02, "mid": 0.0, "high": +0.02}[pitch]
    arousal_score = max(-0.10, min(0.10, arousal_score))

    return VocalTone(
        energy=energy,
        pitch=pitch,
        pace=pace,
        arousal_hint=round(arousal_score, 3),
        rms=round(rms, 5),
        pitch_hz=round(pitch_hz, 1),
        zcr=round(zcr, 1),
        confident=True,
        samples=int(samples.size),
        tags=tuple(tags),
    )


__all__ = [
    "VocalTone",
    "EnergyBucket",
    "PitchBucket",
    "PaceBucket",
    "analyse_wav",
]
