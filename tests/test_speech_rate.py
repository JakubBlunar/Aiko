"""Per-clip tempo matching.

The behaviour under test is "consecutive sentences keep one pace", so
these build clips whose *delivered* tempo differs the way real generations
differ and assert on the tempo measured afterwards, not on the stretch
factor that got there.
"""

from __future__ import annotations

import threading
import unittest

import numpy as np

from app.audio import speech_rate

RATE = 24000


def _speech(syllable_count: int, syllables_per_second: float) -> np.ndarray:
    """A voice-like signal with countable syllable pulses.

    Amplitude-modulated at the wanted syllable rate and gated to silence
    between pulses, so ``speech_seconds`` sees the same structure it sees
    in real speech: voiced nuclei separated by short gaps.
    """
    duration = syllable_count / float(syllables_per_second)
    n = int(duration * RATE)
    t = np.arange(n, dtype=np.float64) / RATE
    carrier = np.zeros(n, dtype=np.float64)
    for harmonic, weight in ((1, 1.0), (2, 0.5), (3, 0.25), (5, 0.12)):
        carrier += weight * np.sin(2 * np.pi * 190.0 * harmonic * t)
    # Raised-cosine pulse train, one pulse per syllable.
    envelope = 0.5 - 0.5 * np.cos(
        2 * np.pi * float(syllables_per_second) * t
    )
    wave = carrier * envelope
    wave = wave / max(1e-9, float(np.abs(wave).max())) * 0.2
    return wave.astype(np.float32)


TEXT = "The tweaks can wait if your brain is turning into soup."

#: Kept in step with the text rather than hardcoded twice. A clip built
#: with a different pulse count than the text claims would measure at the
#: wrong tempo, and every correction assertion below would then be testing
#: the fixture instead of the code -- which is exactly what happened on the
#: first run of this file.
SYLLABLES = speech_rate.syllables(TEXT)


class SyllableCountTests(unittest.TestCase):
    def test_it_counts_vowel_groups(self) -> None:
        self.assertEqual(speech_rate.syllables("cat"), 1)
        self.assertEqual(speech_rate.syllables("water"), 2)
        self.assertEqual(speech_rate.syllables("computer"), 3)

    def test_a_silent_trailing_e_is_not_a_syllable(self) -> None:
        self.assertEqual(speech_rate.syllables("time"), 1)
        self.assertEqual(speech_rate.syllables("came"), 1)

    def test_every_word_counts_at_least_one(self) -> None:
        """Otherwise a vowelless word makes a sentence look faster than it
        is, and the correction slows her down for no reason."""
        self.assertEqual(speech_rate.syllables("hmm"), 1)
        self.assertGreaterEqual(speech_rate.syllables("rhythm shh"), 2)

    def test_the_test_sentence_is_what_we_think(self) -> None:
        """The + tweaks + can + wait + if + your + brain + is + turn-ing +
        in-to + soup. Pinned so a change to the counter shows up here
        rather than as a mysterious tempo error elsewhere."""
        self.assertEqual(speech_rate.syllables(TEXT), 13)

    def test_empty_text_counts_nothing(self) -> None:
        self.assertEqual(speech_rate.syllables(""), 0)
        self.assertEqual(speech_rate.syllables("!!! ??? 123"), 0)


class MeasurabilityTests(unittest.TestCase):
    def test_ordinary_prose_is_measurable(self) -> None:
        self.assertTrue(speech_rate.is_measurable(TEXT))

    def test_a_url_is_not(self) -> None:
        """The counter is a letter-pattern heuristic, so text it cannot
        read should get no correction rather than a guessed one."""
        self.assertFalse(
            speech_rate.is_measurable("https://x.co/a?b=1&c=2#dd_e-f")
        )

    def test_a_number_heavy_line_is_not(self) -> None:
        self.assertFalse(speech_rate.is_measurable("3.14159 * 2.71828 = ?"))

    def test_blank_text_is_not(self) -> None:
        self.assertFalse(speech_rate.is_measurable("   "))


class SpeechSecondsTests(unittest.TestCase):
    def test_it_measures_the_voiced_part_not_the_clip(self) -> None:
        """A sentence with silence around it is not spoken more slowly
        than the same sentence without."""
        clip = _speech(SYLLABLES, 6.5)
        padded = np.concatenate(
            [np.zeros(RATE, np.float32), clip, np.zeros(RATE, np.float32)]
        )
        self.assertAlmostEqual(
            speech_rate.speech_seconds(clip, RATE),
            speech_rate.speech_seconds(padded, RATE),
            delta=0.12,
        )

    def test_silence_measures_zero(self) -> None:
        self.assertEqual(
            speech_rate.speech_seconds(np.zeros(RATE, np.float32), RATE), 0.0
        )

    def test_an_empty_clip_measures_zero(self) -> None:
        self.assertEqual(
            speech_rate.speech_seconds(np.zeros(0, np.float32), RATE), 0.0
        )


class MeasuredRateTests(unittest.TestCase):
    def test_a_faster_clip_measures_a_higher_rate(self) -> None:
        slow = speech_rate.measured_rate(_speech(SYLLABLES, 5.0), RATE, TEXT)
        fast = speech_rate.measured_rate(_speech(SYLLABLES, 8.0), RATE, TEXT)
        self.assertGreater(fast, slow)

    def test_a_very_short_clip_is_not_measured(self) -> None:
        """Too few syllables for a rate to mean anything, and short
        interjections are where tempo is legitimately unusual."""
        self.assertEqual(
            speech_rate.measured_rate(_speech(2, 6.5), RATE, "Oh no"), 0.0
        )

    def test_unreadable_text_is_not_measured(self) -> None:
        self.assertEqual(
            speech_rate.measured_rate(_speech(SYLLABLES, 6.5), RATE, "1 2 3 4 5"), 0.0
        )


class CorrectionTests(unittest.TestCase):
    def _factor(self, delivered: float, **kwargs: float) -> float:
        return speech_rate.correction_factor(
            _speech(SYLLABLES, delivered), RATE, TEXT, **kwargs
        )

    def test_a_slow_clip_is_sped_up(self) -> None:
        self.assertGreater(self._factor(5.5, target_syl_s=6.55), 1.0)

    def test_a_fast_clip_is_slowed_down(self) -> None:
        self.assertLess(self._factor(7.6, target_syl_s=6.55), 1.0)

    def test_a_clip_already_on_pace_is_left_alone(self) -> None:
        self.assertAlmostEqual(
            self._factor(6.55, target_syl_s=6.55), 1.0, delta=0.06
        )

    def test_the_correction_is_capped(self) -> None:
        """A wildly off draw is pulled toward her pace, not all the way to
        it: an unbounded stretch would flatten real variation and start
        producing audible WSOLA artefacts."""
        self.assertAlmostEqual(
            self._factor(3.0, target_syl_s=6.55, limit=0.15), 1.15, delta=1e-6
        )
        self.assertAlmostEqual(
            self._factor(20.0, target_syl_s=6.55, limit=0.15), 0.85, delta=1e-6
        )

    def test_an_intended_speed_moves_the_target_with_it(self) -> None:
        """The correction composes with the pacing it is given rather than
        overriding it -- otherwise switching the affect channel on would
        have every deliberate speed change quietly cancelled here.

        A clip delivered at 5.5 syl/s is 16% under her pace, so at face
        value it wants a big speed-up. Told that 0.85x was *asked for*, the
        same clip is already where it should be and must be left alone.
        """
        at_face_value = self._factor(5.5, target_syl_s=6.55, intended=1.0)
        deliberately_slow = self._factor(5.5, target_syl_s=6.55, intended=0.85)
        self.assertGreater(at_face_value, 1.10)
        self.assertAlmostEqual(deliberately_slow, 1.0, delta=0.03)

    def test_a_disabled_target_is_a_no_op(self) -> None:
        self.assertEqual(self._factor(5.0, target_syl_s=0.0), 1.0)

    def test_an_unmeasurable_clip_is_a_no_op(self) -> None:
        self.assertEqual(
            speech_rate.correction_factor(
                np.zeros(RATE, np.float32), RATE, TEXT, target_syl_s=6.55
            ),
            1.0,
        )


class PlaybackIntegrationTests(unittest.TestCase):
    """Through the shipped ``_play_clip``, since the wiring is where this
    can silently do nothing."""

    def _emit(
        self,
        clip: np.ndarray,
        target: float | None,
        *,
        text: str = TEXT,
        speed: float = 1.0,
    ) -> np.ndarray:
        from app.tts.pcm_playback import PcmPlaybackMixin

        class Host(PcmPlaybackMixin):
            pass

        host = Host()
        host._stop_requested = threading.Event()
        host._pitch_preserving_speed = True
        host._rate_target_syl_s = target
        host._rate_limit = 0.15
        host._clip_end_listener = None
        # One burst: the pacer would otherwise sleep through real time.
        host._PRE_ROLL_CHUNKS = 10**9
        chunks: list[bytes] = []
        host._pcm_listener = lambda rate, ch, pcm: chunks.append(pcm)
        host._play_clip(clip, RATE, speed=speed, text=text)
        return (
            np.frombuffer(b"".join(chunks), dtype=np.int16).astype(np.float32)
            / 32768.0
        )

    def test_a_slow_sentence_comes_out_closer_to_her_pace(self) -> None:
        clip = _speech(SYLLABLES, 5.7)
        before = speech_rate.measured_rate(clip, RATE, TEXT)
        after = speech_rate.measured_rate(self._emit(clip, 6.55), RATE, TEXT)
        self.assertGreater(after, before)
        self.assertLess(abs(after - 6.55), abs(before - 6.55))

    def test_two_different_draws_land_closer_together(self) -> None:
        """The actual complaint: one sentence fine, the next much slower."""
        slow, fast = _speech(SYLLABLES, 5.8), _speech(SYLLABLES, 7.5)
        raw = abs(
            speech_rate.measured_rate(slow, RATE, TEXT)
            - speech_rate.measured_rate(fast, RATE, TEXT)
        )
        matched = abs(
            speech_rate.measured_rate(self._emit(slow, 6.55), RATE, TEXT)
            - speech_rate.measured_rate(self._emit(fast, 6.55), RATE, TEXT)
        )
        self.assertLess(matched, raw / 2.0)

    def test_no_target_leaves_the_tempo_alone(self) -> None:
        """pocket-tts has no reference manifest to measure, so it must
        come through untouched."""
        clip = _speech(SYLLABLES, 5.7)
        emitted = self._emit(clip, None)
        self.assertAlmostEqual(
            speech_rate.measured_rate(emitted, RATE, TEXT),
            speech_rate.measured_rate(clip, RATE, TEXT),
            delta=0.15,
        )

    def test_no_text_leaves_the_tempo_alone(self) -> None:
        """A caller that never passed text must not get a correction
        computed from an empty syllable count."""
        clip = _speech(SYLLABLES, 5.7)
        emitted = self._emit(clip, 6.55, text="")
        self.assertAlmostEqual(
            speech_rate.measured_rate(emitted, RATE, TEXT),
            speech_rate.measured_rate(clip, RATE, TEXT),
            delta=0.15,
        )

    def test_varispeed_engines_are_left_alone(self) -> None:
        """Correcting tempo through varispeed would trade a tempo wobble
        for a pitch wobble, which is the worse of the two."""
        from app.tts.pcm_playback import PcmPlaybackMixin

        class Host(PcmPlaybackMixin):
            pass

        host = Host()
        host._stop_requested = threading.Event()
        host._pitch_preserving_speed = False
        host._rate_target_syl_s = 6.55
        host._clip_end_listener = None
        host._PRE_ROLL_CHUNKS = 10**9
        rates: list[int] = []
        host._pcm_listener = lambda rate, ch, pcm: rates.append(rate)
        host._play_clip(_speech(SYLLABLES, 5.7), RATE, text=TEXT)
        # An untouched clip is shipped at its native rate; a corrected one
        # would have been declared at a scaled rate instead.
        self.assertEqual(set(rates), {RATE})

    def test_an_engine_without_the_attributes_is_unaffected(self) -> None:
        """Back-compat: the mixin defaults the target off, so an engine
        written before this existed keeps its own pacing."""
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
        clip = _speech(SYLLABLES, 5.7)
        host._play_clip(clip, RATE, text=TEXT)
        emitted = (
            np.frombuffer(b"".join(chunks), dtype=np.int16).astype(np.float32)
            / 32768.0
        )
        self.assertAlmostEqual(
            speech_rate.measured_rate(emitted, RATE, TEXT),
            speech_rate.measured_rate(clip, RATE, TEXT),
            delta=0.15,
        )


class SettingsTests(unittest.TestCase):
    def test_zero_survives_as_the_off_switch(self) -> None:
        from app.core.infra.settings import _parse_speech_rate_limit

        self.assertEqual(_parse_speech_rate_limit(0.0), 0.0)

    def test_a_sane_limit_is_honoured(self) -> None:
        from app.core.infra.settings import _parse_speech_rate_limit

        self.assertEqual(_parse_speech_rate_limit(0.08), 0.08)

    def test_an_absurd_limit_falls_back(self) -> None:
        from app.core.infra.settings import _parse_speech_rate_limit

        self.assertEqual(_parse_speech_rate_limit(0.9), 0.15)

    def test_nonsense_falls_back(self) -> None:
        from app.core.infra.settings import _parse_speech_rate_limit

        self.assertEqual(_parse_speech_rate_limit("brisk"), 0.15)
        self.assertEqual(_parse_speech_rate_limit(None), 0.15)

    def test_the_default_is_the_measured_knee(self) -> None:
        """0.10 left the tails uncorrected; 0.20 bought two points of
        spread for more flattening. If this changes, re-measure."""
        from app.core.infra.settings import load_settings

        self.assertEqual(load_settings().tts.speech_rate_match_limit, 0.15)
        self.assertEqual(speech_rate.MAX_CORRECTION, 0.15)


if __name__ == "__main__":
    unittest.main()
