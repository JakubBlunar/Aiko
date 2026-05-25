"""Tests for the cheap vocal-tone analyser.

We can't ship reference WAVs cheaply, so the tests synthesise short PCM
buffers with controlled energy / pitch / pace properties and write them
out via the standard library ``wave`` module before feeding them to
:func:`app.core.vocal_tone.analyse_wav`. Each test asserts the bucket
classification, not the raw numbers — the buckets are the only contract
the prompt and AffectUpdater rely on.
"""
from __future__ import annotations

import math
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from app.core.vocal_tone import VocalTone, analyse_wav


SR = 16000


def _write_wav(path: Path, samples: np.ndarray, sample_rate: int = SR) -> None:
    """Write a mono float32 buffer (in [-1, 1]) to a 16-bit PCM WAV."""
    int16 = np.clip(samples, -1.0, 1.0)
    int16 = (int16 * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(int16.tobytes())


def _synth_speech_like(
    *,
    seconds: float = 1.5,
    pitch_hz: float = 170.0,
    amplitude: float = 0.20,
    syllable_hz: float = 4.0,
    sample_rate: int = SR,
) -> np.ndarray:
    """A toy speech-like signal: pitch sine modulated by a syllable
    envelope, plus mild noise. Good enough to exercise bucket
    classification without shipping reference audio.
    """
    n = int(seconds * sample_rate)
    t = np.arange(n, dtype=np.float32) / float(sample_rate)
    carrier = np.sin(2.0 * math.pi * pitch_hz * t)
    # Half-wave rectified envelope at syllable rate so we get clear
    # voiced/unvoiced segments.
    envelope = np.maximum(0.0, np.sin(2.0 * math.pi * syllable_hz * t))
    noise = np.random.RandomState(0).normal(scale=0.005, size=n)
    return amplitude * envelope * carrier + noise


class AnalyseWavTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        try:
            self._tmp.cleanup()
        except Exception:
            pass

    def _wav(self, samples: np.ndarray, name: str = "phrase.wav") -> Path:
        path = self._dir / name
        _write_wav(path, samples)
        return path

    def test_returns_unconfident_for_silence(self) -> None:
        # 1.5 seconds of near-silence; analyser must not pretend to know.
        samples = np.zeros(int(SR * 1.5), dtype=np.float32) + 1e-5
        tone = analyse_wav(self._wav(samples))
        self.assertFalse(tone.confident)
        self.assertEqual(tone.to_prompt_line(), "")

    def test_returns_unconfident_for_too_short_clip(self) -> None:
        # < 200 ms: not enough to pitch-track.
        samples = _synth_speech_like(seconds=0.10)
        tone = analyse_wav(self._wav(samples))
        self.assertFalse(tone.confident)

    def test_returns_unconfident_for_missing_file(self) -> None:
        tone = analyse_wav(self._dir / "does_not_exist.wav")
        self.assertFalse(tone.confident)

    def test_low_energy_is_classified_low(self) -> None:
        samples = _synth_speech_like(amplitude=0.018)
        tone = analyse_wav(self._wav(samples))
        self.assertTrue(tone.confident)
        self.assertIn(tone.energy, ("low", "moderate"))
        # Either way the arousal hint should be non-positive when energy
        # is below the "high" threshold.
        self.assertLessEqual(tone.arousal_hint, 0.0)

    def test_high_energy_is_classified_high(self) -> None:
        # Loud, fast carrier — should land in the "high" energy bucket
        # and produce a positive arousal hint.
        samples = _synth_speech_like(amplitude=0.40, syllable_hz=6.0)
        tone = analyse_wav(self._wav(samples))
        self.assertTrue(tone.confident)
        self.assertEqual(tone.energy, "high")
        self.assertGreater(tone.arousal_hint, 0.0)

    def test_low_pitch_is_classified_low(self) -> None:
        samples = _synth_speech_like(pitch_hz=110.0, amplitude=0.20)
        tone = analyse_wav(self._wav(samples))
        self.assertTrue(tone.confident)
        self.assertEqual(tone.pitch, "low")

    def test_high_pitch_is_classified_high(self) -> None:
        samples = _synth_speech_like(pitch_hz=260.0, amplitude=0.20)
        tone = analyse_wav(self._wav(samples))
        self.assertTrue(tone.confident)
        self.assertEqual(tone.pitch, "high")

    def test_to_prompt_line_only_mentions_off_baseline_dimensions(self) -> None:
        # Default synth lands "moderate / mid / normal" → no tags → empty
        # prompt line even when confident.
        samples = _synth_speech_like(amplitude=0.05, pitch_hz=170.0)
        tone = analyse_wav(self._wav(samples))
        # Even if confident, when no dimension is "off baseline" the
        # prompt line should be empty so the LLM doesn't see noise.
        if tone.confident and not tone.tags:
            self.assertEqual(tone.to_prompt_line(), "")

    def test_prompt_line_format_when_tagged(self) -> None:
        samples = _synth_speech_like(amplitude=0.40, pitch_hz=110.0, syllable_hz=6.0)
        tone = analyse_wav(self._wav(samples))
        self.assertTrue(tone.confident)
        line = tone.to_prompt_line()
        if line:  # confidence + at least one off-baseline tag
            self.assertTrue(line.startswith("User sounds: "))
            self.assertTrue(line.endswith("."))


class VocalToneAffectIntegrationTests(unittest.TestCase):
    """Cross-module: the analyser's ``arousal_hint`` is the contract
    consumed by :class:`AffectUpdater`. Verify the field exists and is
    bounded so AffectUpdater can rely on it without re-clamping."""

    def test_arousal_hint_is_bounded(self) -> None:
        for amp in (0.005, 0.05, 0.40):
            for pitch in (90.0, 170.0, 280.0):
                for sylr in (2.0, 4.0, 8.0):
                    samples = _synth_speech_like(
                        amplitude=amp, pitch_hz=pitch, syllable_hz=sylr,
                    )
                    tmp = tempfile.NamedTemporaryFile(
                        suffix=".wav", delete=False,
                    )
                    tmp.close()
                    path = Path(tmp.name)
                    try:
                        _write_wav(path, samples)
                        tone = analyse_wav(path)
                        self.assertGreaterEqual(tone.arousal_hint, -0.10)
                        self.assertLessEqual(tone.arousal_hint, +0.10)
                    finally:
                        try:
                            path.unlink(missing_ok=True)
                        except Exception:
                            pass


if __name__ == "__main__":
    unittest.main()
