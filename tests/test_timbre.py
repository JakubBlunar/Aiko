"""Per-clip spectral tilt matching.

The behaviour under test is "consecutive sentences share one voice", so
these build clips that differ in brightness the way real generations
differ and assert the *measured* tilt afterwards rather than the shelf
gain that got there.
"""

from __future__ import annotations

import threading
import unittest

import numpy as np

from app.audio import timbre


def _speech(
    seconds: float,
    rate: int = 24000,
    level: float = 0.1,
    *,
    brightness: float = 1.0,
) -> np.ndarray:
    """A voice-like signal whose high harmonics can be scaled.

    ``brightness`` multiplies the harmonics that land in the high band,
    which is how a real generation differs from the one before it: same
    voice, more or less energy up top.
    """
    n = int(seconds * rate)
    t = np.arange(n, dtype=np.float32) / rate
    wave = np.zeros(n, dtype=np.float32)
    f0 = 190.0
    for harmonic, weight in (
        (1, 1.0), (2, 0.5), (3, 0.25), (5, 0.12), (8, 0.08),
        (14, 0.06), (20, 0.04), (26, 0.03),
    ):
        gain = weight
        if f0 * harmonic >= timbre.HIGH_BAND[0]:
            gain = weight * brightness
        wave += gain * np.sin(2 * np.pi * f0 * harmonic * t)
    envelope = 0.55 + 0.45 * np.sin(2 * np.pi * 4.0 * t)
    wave = wave * envelope
    wave = wave / max(1e-9, float(np.abs(wave).max())) * level
    return wave.astype(np.float32)


def _tilt(audio: np.ndarray, rate: int = 24000) -> float:
    return timbre.spectral_tilt_db(audio, rate)


class TiltMeasurementTests(unittest.TestCase):
    def test_a_brighter_clip_measures_a_lower_tilt(self) -> None:
        """Sign convention, pinned: tilt is low-over-high, so more energy
        up top means a smaller number. Getting this backwards would make
        the correction push every clip the wrong way."""
        self.assertLess(_tilt(_speech(1.5, brightness=3.0)),
                        _tilt(_speech(1.5, brightness=1.0)))

    def test_level_does_not_change_the_tilt(self) -> None:
        """Tilt is a ratio between two bands, so it has to survive the
        loudness stage moving the clip up or down."""
        quiet = _speech(1.5, level=0.02)
        loud = _speech(1.5, level=0.4)
        self.assertAlmostEqual(_tilt(quiet), _tilt(loud), delta=0.2)

    def test_silence_and_empty_clips_measure_zero(self) -> None:
        self.assertEqual(_tilt(np.zeros(24000, np.float32)), 0.0)
        self.assertEqual(_tilt(np.zeros(0, np.float32)), 0.0)


class CorrectionTests(unittest.TestCase):
    def test_clips_of_different_brightness_land_together(self) -> None:
        """The whole point, stated as a test.

        The brightness range spans about 3.5 dB of tilt, which is what
        the engine actually does: regenerating one sentence moved 4.2 dB.
        A wider fixture would only be testing the clamp, which has its
        own test below.
        """
        target = _tilt(_speech(1.5, brightness=1.0))
        matched = [
            _tilt(
                timbre.match_tilt(
                    _speech(1.5, brightness=b), 24000, target_tilt_db=target
                )
            )
            for b in (0.8, 0.9, 1.0, 1.1, 1.2)
        ]
        self.assertLess(max(matched) - min(matched), 1.0)

    def test_the_correction_points_the_right_way(self) -> None:
        warm = _speech(1.5, brightness=0.5)
        target = _tilt(_speech(1.5, brightness=1.5))
        self.assertGreater(
            timbre.correction_db(warm, 24000, target_tilt_db=target), 0.0
        )

    def test_the_correction_is_bounded(self) -> None:
        """The limit is what keeps this a correction rather than a rewrite
        of her voice: content genuinely changes brightness, and an
        unbounded shelf would flatten that along with the noise."""
        very_warm = _speech(1.5, brightness=0.05)
        gain = timbre.correction_db(
            very_warm, 24000, target_tilt_db=-40.0, limit_db=4.0
        )
        self.assertLessEqual(abs(gain), 4.0 + 1e-6)

    def test_a_clip_already_on_target_is_returned_untouched(self) -> None:
        """Identity, not a no-op filter: the caller should pay nothing on
        the clips that need nothing."""
        clip = _speech(1.5)
        matched = timbre.match_tilt(clip, 24000, target_tilt_db=_tilt(clip))
        self.assertIs(matched, clip)

    def test_short_clips_are_left_alone(self) -> None:
        """Too few voiced frames for a band ratio to mean anything, and a
        two-word exclamation is exactly the utterance whose brightness is
        legitimately unusual."""
        tiny = _speech(0.1)
        self.assertEqual(
            timbre.correction_db(tiny, 24000, target_tilt_db=0.0), 0.0
        )

    def test_silent_clips_are_left_alone(self) -> None:
        self.assertEqual(
            timbre.correction_db(
                np.zeros(24000, np.float32), 24000, target_tilt_db=0.0
            ),
            0.0,
        )

    def test_peaks_stay_below_full_scale(self) -> None:
        """A boosting shelf can push a loud clip past full scale, and the
        emission path clips rather than complaining."""
        loud = _speech(2.0, level=0.98, brightness=0.2)
        matched = timbre.match_tilt(loud, 24000, target_tilt_db=-30.0)
        self.assertLessEqual(float(np.abs(matched).max()), 1.0)


class PlaybackIntegrationTests(unittest.TestCase):
    """Through ``PcmPlaybackMixin``, which is where it actually runs."""

    def _emit(self, clip: np.ndarray, target: float | None, limit: float = 4.0):
        from app.tts.pcm_playback import PcmPlaybackMixin

        class Host(PcmPlaybackMixin):
            pass

        host = Host()
        host._stop_requested = threading.Event()
        host._pitch_preserving_speed = True
        host._loudness_target_dbfs = 0.0
        host._tilt_target_db = target
        host._tilt_limit_db = limit
        host._clip_end_listener = None
        host._PRE_ROLL_CHUNKS = 10**9
        chunks: list[bytes] = []
        host._pcm_listener = lambda rate, ch, pcm: chunks.append(pcm)
        host._play_clip(clip, 24000)
        raw = b"".join(chunks)
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    def test_the_emitted_pcm_lands_on_the_target(self) -> None:
        target = _tilt(_speech(1.5, brightness=1.0))
        for brightness in (0.8, 1.0, 1.2):
            emitted = self._emit(_speech(1.5, brightness=brightness), target)
            self.assertAlmostEqual(_tilt(emitted), target, delta=0.6)

    def test_no_target_leaves_brightness_alone(self) -> None:
        """pocket-tts has no reference clip to aim at, so it must come
        through untouched."""
        clip = _speech(1.5, brightness=0.5)
        emitted = self._emit(clip, None)
        self.assertAlmostEqual(_tilt(emitted), _tilt(clip), delta=0.3)

    def test_an_engine_without_the_attributes_is_unaffected(self) -> None:
        """Back-compat: the mixin defaults the target off, so an engine
        written before this existed keeps its own timbre."""
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
        clip = _speech(1.0, brightness=0.4)
        host._play_clip(clip, 24000)
        emitted = (
            np.frombuffer(b"".join(chunks), dtype=np.int16).astype(np.float32)
            / 32768.0
        )
        self.assertAlmostEqual(_tilt(emitted), _tilt(clip), delta=0.3)

    def test_loudness_matching_still_lands_on_its_target(self) -> None:
        """The two stages compose. The shelf changes the level it would
        otherwise be measured against, which is why brightness runs
        first -- if that order flips, this drifts."""
        from app.audio.loudness import gated_rms
        from app.tts.pcm_playback import PcmPlaybackMixin

        class Host(PcmPlaybackMixin):
            pass

        host = Host()
        host._stop_requested = threading.Event()
        host._pitch_preserving_speed = True
        host._loudness_target_dbfs = -26.0
        host._tilt_target_db = _tilt(_speech(1.5, brightness=1.0))
        host._tilt_limit_db = 4.0
        host._clip_end_listener = None
        host._PRE_ROLL_CHUNKS = 10**9
        chunks: list[bytes] = []
        host._pcm_listener = lambda rate, ch, pcm: chunks.append(pcm)
        host._play_clip(_speech(1.5, level=0.05, brightness=0.4), 24000)
        emitted = (
            np.frombuffer(b"".join(chunks), dtype=np.int16).astype(np.float32)
            / 32768.0
        )
        measured = 20.0 * np.log10(max(gated_rms(emitted, 24000), 1e-12))
        self.assertAlmostEqual(measured, -26.0, delta=0.6)


class SettingsTests(unittest.TestCase):
    def test_zero_survives_as_the_off_switch(self) -> None:
        from app.core.infra.settings import _parse_timbre_limit

        self.assertEqual(_parse_timbre_limit(0.0), 0.0)

    def test_a_sane_limit_is_honoured(self) -> None:
        from app.core.infra.settings import _parse_timbre_limit

        self.assertEqual(_parse_timbre_limit(2.5), 2.5)

    def test_an_absurd_limit_falls_back(self) -> None:
        from app.core.infra.settings import _parse_timbre_limit

        self.assertEqual(_parse_timbre_limit(40.0), 4.0)

    def test_nonsense_falls_back(self) -> None:
        from app.core.infra.settings import _parse_timbre_limit

        self.assertEqual(_parse_timbre_limit("bright"), 4.0)
        self.assertEqual(_parse_timbre_limit(None), 4.0)

    def test_a_negative_limit_is_read_as_its_magnitude(self) -> None:
        from app.core.infra.settings import _parse_timbre_limit

        self.assertEqual(_parse_timbre_limit(-3.0), 3.0)


if __name__ == "__main__":
    unittest.main()
