"""Per-clip loudness matching.

The behaviour under test is "consecutive sentences come out at the same
level", so most of these build clips that differ in the ways real
synthesis differs -- length, silence padding, crest factor -- and assert
the *measured* result rather than the multiplier.
"""

from __future__ import annotations

import threading
import unittest

import numpy as np

from app.audio import loudness


def _speech(
    seconds: float,
    rate: int = 24000,
    level: float = 0.1,
    *,
    pad: float = 0.0,
) -> np.ndarray:
    """A voice-like signal: harmonic stack, syllable-rate envelope.

    Not noise, because the gate keys off frame energy and a stationary
    signal would not exercise it.
    """
    n = int(seconds * rate)
    t = np.arange(n, dtype=np.float32) / rate
    wave = np.zeros(n, dtype=np.float32)
    for harmonic, weight in ((1, 1.0), (2, 0.5), (3, 0.25), (5, 0.1)):
        wave += weight * np.sin(2 * np.pi * 190.0 * harmonic * t)
    envelope = 0.55 + 0.45 * np.sin(2 * np.pi * 4.0 * t)
    wave = wave * envelope
    wave = wave / max(1e-9, float(np.abs(wave).max())) * level
    if pad > 0.0:
        silence = np.zeros(int(pad * rate), dtype=np.float32)
        wave = np.concatenate([silence, wave, silence])
    return wave.astype(np.float32)


def _dbfs(audio: np.ndarray, rate: int = 24000) -> float:
    return 20.0 * float(np.log10(max(loudness.gated_rms(audio, rate), 1e-12)))


class GatedRmsTests(unittest.TestCase):
    def test_silence_padding_does_not_change_the_measurement(self) -> None:
        """The reason not to use whole-clip RMS. A short exclamation is
        mostly silence, and a level that fell with the silence fraction
        would boost exactly the clips that need it least."""
        bare = _speech(1.5)
        padded = _speech(1.5, pad=1.0)
        self.assertAlmostEqual(
            _dbfs(bare), _dbfs(padded), delta=0.5,
        )

    def test_whole_clip_rms_would_have_been_fooled(self) -> None:
        """Guards the choice above: the naive measure really does drift
        on the same speech, so the gate is earning its place."""
        bare = _speech(1.5)
        padded = _speech(1.5, pad=1.0)
        naive = [
            20.0 * np.log10(float(np.sqrt(np.mean(clip**2))))
            for clip in (bare, padded)
        ]
        self.assertGreater(abs(naive[0] - naive[1]), 3.0)

    def test_a_silent_clip_measures_zero(self) -> None:
        self.assertEqual(loudness.gated_rms(np.zeros(24000, np.float32), 24000), 0.0)

    def test_an_empty_clip_measures_zero(self) -> None:
        self.assertEqual(loudness.gated_rms(np.zeros(0, np.float32), 24000), 0.0)

    def test_a_clip_shorter_than_one_frame_still_measures(self) -> None:
        tiny = _speech(0.005)
        self.assertGreater(loudness.gated_rms(tiny, 24000), 0.0)


class CorrectionTests(unittest.TestCase):
    def test_clips_at_different_levels_land_together(self) -> None:
        """The whole point, stated as a test.

        The levels span roughly -32 to -20 dBFS gated, which is the range
        real synthesis produces: measured over a twelve-sentence turn,
        both engines stayed inside -18 to -30. Anything further out is
        the clamp's business, tested separately below.
        """
        target = -26.0
        landed = []
        for level in (0.06, 0.1, 0.18, 0.3):
            clip = _speech(2.0, level=level)
            factor = loudness.correction_factor(clip, 24000, target_dbfs=target)
            landed.append(_dbfs(clip * factor))
        for value in landed:
            self.assertAlmostEqual(value, target, delta=0.3)
        self.assertLess(max(landed) - min(landed), 0.5)

    def test_length_does_not_affect_where_a_clip_lands(self) -> None:
        """A one-word reply and a long sentence should sit at the same
        level -- the complaint was about consecutive sentences, and
        sentence length is what varies most between them."""
        target = -26.0
        for seconds in (0.4, 1.0, 3.0, 6.0):
            clip = _speech(seconds, level=0.15)
            factor = loudness.correction_factor(clip, 24000, target_dbfs=target)
            self.assertAlmostEqual(_dbfs(clip * factor), target, delta=0.4)

    def test_a_silent_clip_is_left_alone(self) -> None:
        """Rather than boosted by the maximum correction, which would
        turn a failed generation into a burst of amplified noise."""
        self.assertEqual(
            loudness.correction_factor(np.zeros(24000, np.float32), 24000), 1.0
        )

    def test_an_empty_clip_is_left_alone(self) -> None:
        self.assertEqual(
            loudness.correction_factor(np.zeros(0, np.float32), 24000), 1.0
        )

    def test_the_correction_is_bounded(self) -> None:
        """A clip 40 dB down is broken, not quiet."""
        clip = _speech(2.0, level=0.0002)
        factor = loudness.correction_factor(clip, 24000, target_dbfs=-26.0)
        ceiling = 10.0 ** (loudness.MAX_CORRECTION_DB / 20.0)
        self.assertLessEqual(factor, ceiling + 1e-6)

    def test_peaks_are_kept_below_full_scale(self) -> None:
        """Speech has a high crest factor, so reaching an RMS target can
        ask for a peak above 1.0. Clipped speech is a worse artefact than
        landing under target, so the correction backs off instead."""
        clip = _speech(2.0, level=0.9)
        # A deliberately peaky clip: one plosive-like spike well above
        # the body of the speech.
        clip[len(clip) // 2] = 0.999
        factor = loudness.correction_factor(clip, 24000, target_dbfs=-3.0)
        self.assertLessEqual(float(np.abs(clip * factor).max()), 1.0)

    def test_the_default_target_matches_the_incumbent(self) -> None:
        """-26 dBFS is where pocket-tts already averages. If this moves,
        turning normalisation on becomes a volume change and every user
        notices."""
        self.assertAlmostEqual(loudness.DEFAULT_TARGET_DBFS, -26.0, places=1)


class PlaybackIntegrationTests(unittest.TestCase):
    """Through ``PcmPlaybackMixin``, which is where it actually runs."""

    def _emit(self, clip: np.ndarray, target: float, gain: float = 1.0):
        from app.tts.pcm_playback import PcmPlaybackMixin

        class Host(PcmPlaybackMixin):
            pass

        host = Host()
        host._stop_requested = threading.Event()
        host._pitch_preserving_speed = True
        host._loudness_target_dbfs = target
        host._clip_end_listener = None
        # Ship everything in the pre-roll so the test does not sit
        # through real-time pacing.
        host._PRE_ROLL_CHUNKS = 10**9
        chunks: list[bytes] = []
        host._pcm_listener = lambda rate, ch, pcm: chunks.append(pcm)
        host._play_clip(clip, 24000, gain_factor=gain)
        raw = b"".join(chunks)
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    def test_the_emitted_pcm_is_at_the_target(self) -> None:
        for level in (0.06, 0.15, 0.3):
            emitted = self._emit(_speech(1.5, level=level), -26.0)
            self.assertAlmostEqual(_dbfs(emitted), -26.0, delta=0.5)

    def test_zero_disables_it(self) -> None:
        """The escape hatch has to actually leave the audio alone, or
        an A/B listen compares two normalised clips."""
        clip = _speech(1.5, level=0.05)
        emitted = self._emit(clip, 0.0)
        self.assertAlmostEqual(_dbfs(emitted), _dbfs(clip), delta=0.3)

    def test_affect_gain_still_stacks_on_top(self) -> None:
        """Normalisation pins the base level; it must not swallow the
        deliberate offsets, which are the reason the base needed pinning."""
        clip = _speech(1.5, level=0.1)
        plain = self._emit(clip, -26.0, gain=1.0)
        quiet = self._emit(clip, -26.0, gain=0.5)
        self.assertAlmostEqual(_dbfs(quiet), _dbfs(plain) - 6.0, delta=0.5)

    def test_an_engine_without_the_attribute_is_unaffected(self) -> None:
        """Back-compat: the mixin defaults the target off, so an engine
        written before this existed keeps its own levels."""
        from app.tts.pcm_playback import PcmPlaybackMixin

        class Old(PcmPlaybackMixin):
            pass

        host = Old()
        host._stop_requested = threading.Event()
        host._pitch_preserving_speed = True
        host._clip_end_listener = None
        host._PRE_ROLL_CHUNKS = 10**9
        chunks: list[bytes] = []
        host._pcm_listener = lambda rate, ch, pcm: chunks.append(pcm)
        clip = _speech(1.0, level=0.05)
        host._play_clip(clip, 24000)
        emitted = (
            np.frombuffer(b"".join(chunks), dtype=np.int16).astype(np.float32)
            / 32768.0
        )
        self.assertAlmostEqual(_dbfs(emitted), _dbfs(clip), delta=0.3)


class SettingsTests(unittest.TestCase):
    def test_a_positive_target_is_refused(self) -> None:
        """dBFS is a negative scale. A positive figure would ask for gain
        above full scale on every clip."""
        from app.core.infra.settings import _parse_loudness_target

        self.assertEqual(_parse_loudness_target(6.0), -26.0)

    def test_zero_survives_as_the_off_switch(self) -> None:
        from app.core.infra.settings import _parse_loudness_target

        self.assertEqual(_parse_loudness_target(0.0), 0.0)

    def test_a_sane_target_is_honoured(self) -> None:
        from app.core.infra.settings import _parse_loudness_target

        self.assertEqual(_parse_loudness_target(-20.0), -20.0)

    def test_nonsense_falls_back(self) -> None:
        from app.core.infra.settings import _parse_loudness_target

        self.assertEqual(_parse_loudness_target("loud"), -26.0)
        self.assertEqual(_parse_loudness_target(None), -26.0)


if __name__ == "__main__":
    unittest.main()
