"""Ambient-noise floor tracker (Phase 4b — "Aiko human-like upgrades").

Maintains a 30-second exponentially-weighted moving average of the
microphone RMS during *silence-only* portions of capture. The session
controller feeds samples in via :meth:`AmbientNoiseTracker.observe`;
the prompt assembler reads :meth:`prompt_block` and Pocket-TTS reads
:meth:`tts_volume_db_offset` / :meth:`tts_speed_multiplier`.

Design notes:
    - 30-second EMA: alpha is computed from a target half-life; we
      assume capture chunks arrive at ~10 Hz (100 ms each) and the
      EMA spans about 30 s of those samples. Both the half-life and
      the assumed chunk period are configurable in case the capture
      cadence changes.
    - Pure data, no I/O. Thread-safety: a small ``threading.Lock``
      guards the EMA so the audio thread and prompt thread can poke
      at it without races.
    - The "noisy" classification is a hard threshold on the EMA; we
      only emit the prompt cue and TTS nudges when the floor sits
      above ``loud_threshold`` for the current sample. The threshold
      is calibrated on a typical condenser mic at -22..-18 dBFS.
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass


# Calibration constants. ``_LOUD_THRESHOLD`` is intentionally
# conservative — we only want to fire on rooms that are *definitely*
# noisy (HVAC running, kid in the next room, traffic outside).
# Equivalent to roughly -28 dBFS RMS during quiet periods. The
# "very noisy" threshold ~-18 dBFS lifts TTS nudges further still.
_LOUD_THRESHOLD = 0.012
_VERY_LOUD_THRESHOLD = 0.030


@dataclass(slots=True)
class AmbientNoiseSnapshot:
    """Cheap-to-copy snapshot of the current ambient noise state."""

    floor: float
    is_noisy: bool
    is_very_noisy: bool
    samples: int


class AmbientNoiseTracker:
    """Tracks an EMA of mic-floor RMS during silence periods.

    Use::

        tracker = AmbientNoiseTracker()
        # In the audio thread:
        tracker.observe(rms_level)   # rms is float32 in [0, ~1]

        # In the prompt thread:
        if tracker.snapshot().is_noisy:
            prompt += tracker.prompt_block()

        # In the TTS thread:
        speed = tracker.tts_speed_multiplier()
    """

    def __init__(
        self,
        *,
        ema_seconds: float = 30.0,
        chunk_period_seconds: float = 0.1,
    ) -> None:
        self._lock = threading.Lock()
        self._floor = 0.0
        self._samples = 0
        # Convert "want a 30-second half-life" into an EMA alpha. With a
        # chunk arriving every ``chunk_period_seconds`` and a half-life
        # of ``ema_seconds`` we want ``(1-α) ** N == 0.5`` where
        # ``N = ema_seconds / chunk_period_seconds``. Solving for α:
        chunks = max(1.0, float(ema_seconds) / max(0.001, float(chunk_period_seconds)))
        # The half-life formula is: alpha = 1 - 0.5 ** (1 / chunks)
        self._alpha = 1.0 - math.pow(0.5, 1.0 / chunks)
        # Clamp: alpha must stay well below 1 so a single noisy sample
        # can't dominate the EMA, and well above 0 so the EMA still
        # tracks a moving floor.
        self._alpha = max(1e-4, min(0.5, self._alpha))

    # ── observation ────────────────────────────────────────────────────

    def observe(self, level: float) -> None:
        """Fold a single silence-period RMS sample into the EMA.

        Levels are expected to be float RMS in [0, ~1]; non-finite
        values are silently ignored.
        """
        if not math.isfinite(level):
            return
        if level < 0.0:
            level = 0.0
        with self._lock:
            self._samples += 1
            if self._samples == 1:
                # Seed the EMA with the first sample so we converge
                # quickly even with a very small alpha.
                self._floor = float(level)
            else:
                self._floor += (float(level) - self._floor) * self._alpha

    def reset(self) -> None:
        with self._lock:
            self._floor = 0.0
            self._samples = 0

    # ── reads ──────────────────────────────────────────────────────────

    def snapshot(self) -> AmbientNoiseSnapshot:
        with self._lock:
            floor = self._floor
            samples = self._samples
        return AmbientNoiseSnapshot(
            floor=round(floor, 5),
            is_noisy=samples > 0 and floor > _LOUD_THRESHOLD,
            is_very_noisy=samples > 0 and floor > _VERY_LOUD_THRESHOLD,
            samples=samples,
        )

    def prompt_block(self) -> str:
        """One-line system-prompt cue, or empty when the room is quiet."""
        snap = self.snapshot()
        if not snap.is_noisy:
            return ""
        if snap.is_very_noisy:
            return (
                "Background is noticeably noisy — keep replies short and "
                "speak clearly. Skip soft asides he might miss."
            )
        return (
            "Background has a soft hum to it — speak clearly and don't "
            "trail off."
        )

    # ── TTS nudges ─────────────────────────────────────────────────────

    def tts_volume_db_offset(self) -> float:
        """Phase 4b: small positive volume nudge when the room is loud.

        Returns 0.0 in quiet rooms; up to +1.5 dB in very noisy rooms.
        """
        snap = self.snapshot()
        if snap.is_very_noisy:
            return 1.5
        if snap.is_noisy:
            return 0.8
        return 0.0

    def tts_speed_multiplier(self) -> float:
        """Phase 4b: slow down a hair in noisy rooms so the listener has
        more time to parse each word against the background. Clamped to
        the same ±8% band the cadence layer uses, so the TTS never
        produces audible chipmunk / dopey artefacts.
        """
        snap = self.snapshot()
        if snap.is_very_noisy:
            return 0.96
        if snap.is_noisy:
            return 0.98
        return 1.0


__all__ = ["AmbientNoiseTracker", "AmbientNoiseSnapshot"]
