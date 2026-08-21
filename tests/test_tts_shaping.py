"""The lab and the app must shape a clip identically.

That is the whole reason :mod:`app.tts.shaping` exists. The lab played
``generate_audio``'s array while the app played that array plus four
stages, so a voice tuned by ear in the lab was tuned against a signal the
app never produces -- reported as "it sounded good in the tts lab but it's
not that good in real usage".

Two things are worth testing and they are different. That shaping does what
each stage claims, and that the *engine's* playback path and the *lab's*
preview reach the same samples. The second is the one that rots: both
callers work, nobody notices they have drifted, and the lab quietly goes
back to being a preview of something else.
"""
from __future__ import annotations

import unittest

import numpy as np

from app.audio.loudness import gated_rms
from app.tts.shaping import (
    GUARD_SILENCE_SECONDS,
    RAW_LEVEL_ENGINES,
    Shaping,
    loudness_target_for,
    shape_clip,
)

RATE = 24000


def _speech(seconds: float = 1.2, level: float = 0.25, hz: float = 180.0):
    """A voiced tone at a known level. Not speech, but gated like it.

    Levels used below are chosen against the +/-12 dB correction bound:
    ``0.05`` measures -28.5 dBFS and ``0.15`` measures -19.0, so both are
    reachable, while ``0.6`` at -7.0 deliberately is not.
    """
    t = np.arange(int(RATE * seconds), dtype=np.float32) / RATE
    tone = np.sin(2.0 * np.pi * hz * t).astype(np.float32)
    tone += 0.3 * np.sin(2.0 * np.pi * hz * 3 * t).astype(np.float32)
    tone += 0.15 * np.sin(2.0 * np.pi * hz * 7 * t).astype(np.float32)
    tone /= float(np.max(np.abs(tone)))
    return (tone * level).astype(np.float32)


def _broadband(seconds: float = 1.2, level: float = 0.2, seed: int = 7):
    """Noise, for the brightness stage only.

    The tone above is useless here and misleadingly so: with all its
    energy in three low partials its measured tilt is +93 dB, a ratio
    against an empty high band, and a shelf moves that number wildly.
    Brightness has to be tested on something with energy in both bands,
    where the same signal measures a sane -6.2 dB.
    """
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(int(RATE * seconds)).astype(np.float32)
    noise /= float(np.max(np.abs(noise)))
    return (noise * level).astype(np.float32)


def _dbfs(audio: np.ndarray, rate: int = RATE) -> float:
    return 20.0 * float(np.log10(max(gated_rms(audio, rate), 1e-12)))


def _speech_only(shaped) -> np.ndarray:
    """The clip without the guard silence appended after the stretch."""
    return shaped.audio[: shaped.audio.size - int(RATE * GUARD_SILENCE_SECONDS)]


class InertTests(unittest.TestCase):
    """An engine that sets no targets gets its output back."""

    def test_nothing_configured_changes_nothing_but_the_guard(self) -> None:
        audio = _speech()
        out = shape_clip(audio, RATE, shaping=Shaping())
        guard = int(RATE * GUARD_SILENCE_SECONDS)
        self.assertEqual(out.audio.size, audio.size + guard)
        np.testing.assert_allclose(out.audio[: audio.size], audio, atol=1e-6)
        self.assertEqual(out.gain_factor, 1.0)
        self.assertEqual(out.playback_rate, RATE)
        self.assertFalse(out.stretched)
        self.assertTrue(Shaping().is_inert())

    def test_the_guard_silence_is_actually_silent(self) -> None:
        out = shape_clip(_speech(), RATE, shaping=Shaping())
        tail = out.audio[-int(RATE * GUARD_SILENCE_SECONDS) :]
        self.assertEqual(float(np.max(np.abs(tail))), 0.0)


class LevelTests(unittest.TestCase):
    def test_a_quiet_clip_is_brought_up_to_the_target(self) -> None:
        quiet = _speech(level=0.05)
        out = shape_clip(
            quiet, RATE, shaping=Shaping(loudness_target_dbfs=-26.0)
        )
        self.assertAlmostEqual(_dbfs(out.rendered()), -26.0, delta=0.6)

    def test_a_loud_clip_is_brought_down_to_the_same_target(self) -> None:
        loud = _speech(level=0.15)
        out = shape_clip(
            loud, RATE, shaping=Shaping(loudness_target_dbfs=-26.0)
        )
        self.assertAlmostEqual(_dbfs(out.rendered()), -26.0, delta=0.6)

    def test_a_clip_needing_more_than_the_bound_does_not_get_it(self) -> None:
        """A clip 19 dB out is broken, and a 19 dB move would say so loudly."""
        from app.audio.loudness import MAX_CORRECTION_DB

        out = shape_clip(
            _speech(level=0.6),
            RATE,
            shaping=Shaping(loudness_target_dbfs=-26.0),
        )
        self.assertAlmostEqual(
            out.level_gain_db, -MAX_CORRECTION_DB, delta=0.1
        )
        self.assertGreater(_dbfs(out.rendered()), -26.0)

    def test_the_correction_is_reported_in_db(self) -> None:
        out = shape_clip(
            _speech(level=0.05),
            RATE,
            shaping=Shaping(loudness_target_dbfs=-26.0),
        )
        self.assertGreater(out.level_gain_db, 0.0)
        self.assertAlmostEqual(
            out.level_gain_db,
            20.0 * float(np.log10(out.gain_factor)),
            places=5,
        )

    def test_it_is_folded_into_gain_not_into_the_array(self) -> None:
        """The emission path has exactly one multiply for this."""
        audio = _speech(level=0.05)
        out = shape_clip(
            audio, RATE, shaping=Shaping(loudness_target_dbfs=-26.0)
        )
        np.testing.assert_allclose(
            out.audio[: audio.size], audio, atol=1e-6
        )
        self.assertGreater(out.gain_factor, 1.0)

    def test_off_leaves_the_level_alone(self) -> None:
        audio = _speech(level=0.05)
        out = shape_clip(audio, RATE, shaping=Shaping())
        self.assertAlmostEqual(_dbfs(out.rendered()), _dbfs(audio), delta=0.1)
        self.assertEqual(out.level_gain_db, 0.0)

    def test_rendered_never_clips(self) -> None:
        out = shape_clip(
            _speech(level=0.9),
            RATE,
            shaping=Shaping(loudness_target_dbfs=-6.0),
        )
        self.assertLessEqual(float(np.max(np.abs(out.rendered()))), 1.0)


class SpeedTests(unittest.TestCase):
    def test_a_stretch_changes_duration_and_keeps_the_native_rate(
        self,
    ) -> None:
        audio = _speech(seconds=1.0)
        faster = shape_clip(audio, RATE, shaping=Shaping(), speed=1.1)
        slower = shape_clip(audio, RATE, shaping=Shaping(), speed=0.9)
        self.assertTrue(faster.stretched)
        self.assertTrue(slower.stretched)
        # The native rate is the point: duration now lives in the sample
        # count, so nothing has to declare a scaled rate to the client.
        self.assertEqual(faster.playback_rate, RATE)
        self.assertEqual(slower.playback_rate, RATE)
        # Compared on the speech, since the guard is appended afterwards.
        self.assertLess(_speech_only(faster).size, audio.size)
        self.assertGreater(_speech_only(slower).size, audio.size)

    def test_varispeed_declares_a_scaled_rate_instead(self) -> None:
        """The old path, kept for A/B; duration lives in the rate."""
        audio = _speech(seconds=1.0)
        out = shape_clip(
            audio,
            RATE,
            shaping=Shaping(pitch_preserving_speed=False),
            speed=1.1,
        )
        self.assertFalse(out.stretched)
        self.assertEqual(out.playback_rate, int(RATE * 1.1))
        self.assertEqual(out.audio.size, audio.size + int(RATE * 0.15))

    def test_the_guard_is_not_stretched_with_her_mood(self) -> None:
        """The tail is appended after the stretch, so it stays fixed."""
        for speed in (0.9, 1.0, 1.1):
            out = shape_clip(_speech(), RATE, shaping=Shaping(), speed=speed)
            tail = out.audio[-int(RATE * GUARD_SILENCE_SECONDS) :]
            self.assertEqual(tail.size, int(RATE * GUARD_SILENCE_SECONDS))
            self.assertEqual(float(np.max(np.abs(tail))), 0.0)


class BrightnessTests(unittest.TestCase):
    def test_a_target_moves_the_tilt_toward_it(self) -> None:
        from app.audio.timbre import spectral_tilt_db

        audio = _broadband()
        before = spectral_tilt_db(audio, RATE)
        target = before - 6.0
        out = shape_clip(
            audio,
            RATE,
            shaping=Shaping(tilt_target_db=target, tilt_limit_db=4.0),
        )
        after = spectral_tilt_db(_speech_only(out), RATE)
        self.assertTrue(out.tilt_applied)
        self.assertLess(abs(after - target), abs(before - target))

    def test_the_limit_bounds_how_far_it_goes(self) -> None:
        """An absurd ask lands in the same place as a reachable one."""
        from app.audio.timbre import spectral_tilt_db

        audio = _broadband()
        before = spectral_tilt_db(audio, RATE)
        moved = []
        for delta in (-6.0, -40.0):
            out = shape_clip(
                audio,
                RATE,
                shaping=Shaping(
                    tilt_target_db=before + delta, tilt_limit_db=3.0
                ),
            )
            moved.append(spectral_tilt_db(_speech_only(out), RATE) - before)
        self.assertAlmostEqual(moved[0], moved[1], places=6)
        self.assertLess(abs(moved[0]), 3.0 + 0.5)

    def test_a_tighter_limit_moves_it_less(self) -> None:
        from app.audio.timbre import spectral_tilt_db

        audio = _broadband()
        before = spectral_tilt_db(audio, RATE)
        far = shape_clip(
            audio,
            RATE,
            shaping=Shaping(tilt_target_db=before - 20.0, tilt_limit_db=4.0),
        )
        near = shape_clip(
            audio,
            RATE,
            shaping=Shaping(tilt_target_db=before - 20.0, tilt_limit_db=3.0),
        )
        self.assertGreater(
            abs(spectral_tilt_db(_speech_only(far), RATE) - before),
            abs(spectral_tilt_db(_speech_only(near), RATE) - before),
        )

    def test_no_target_leaves_brightness_alone(self) -> None:
        audio = _broadband()
        out = shape_clip(audio, RATE, shaping=Shaping())
        self.assertFalse(out.tilt_applied)
        np.testing.assert_allclose(
            out.audio[: audio.size], audio, atol=1e-6
        )


class FailOpenTests(unittest.TestCase):
    """Every stage must prefer a worse sentence to no sentence."""

    def test_a_failing_stretch_falls_back_to_varispeed(self) -> None:
        import app.tts.shaping as mod

        original = mod.time_stretch
        mod.time_stretch = lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("no")
        )
        try:
            out = shape_clip(_speech(), RATE, shaping=Shaping(), speed=1.1)
        finally:
            mod.time_stretch = original
        self.assertFalse(out.stretched)
        self.assertEqual(out.playback_rate, int(RATE * 1.1))

    def test_a_failing_tilt_ships_the_clip_as_is(self) -> None:
        import app.tts.shaping as mod

        original = mod.match_tilt
        mod.match_tilt = lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("no")
        )
        audio = _speech()
        try:
            out = shape_clip(
                audio, RATE, shaping=Shaping(tilt_target_db=-3.0)
            )
        finally:
            mod.match_tilt = original
        self.assertFalse(out.tilt_applied)
        np.testing.assert_allclose(
            out.audio[: audio.size], audio, atol=1e-6
        )


class EngineDefaultTests(unittest.TestCase):
    """The lab has to predict the level target, not guess it."""

    def test_pocket_tts_is_raw_and_a_cloning_engine_is_not(self) -> None:
        from app.core.infra.settings import TtsSettings

        settings = TtsSettings(
            provider="pocket-tts",
            voice="",
            enabled=True,
            loudness_target_dbfs=-26.0,
        )
        self.assertEqual(loudness_target_for("pocket-tts", settings), 0.0)
        self.assertEqual(
            loudness_target_for("chatterbox-nano", settings), -26.0
        )

    def test_the_raw_set_names_the_engine_it_is_about(self) -> None:
        self.assertIn("pocket-tts", RAW_LEVEL_ENGINES)


class LabMatchesAppTests(unittest.TestCase):
    """The contract that rots if nobody pins it."""

    def test_the_lab_preview_is_sample_identical_to_playback(self) -> None:
        from tools.tts_lab import asheard

        audio = _speech(level=0.09)
        shaping = Shaping(
            loudness_target_dbfs=-26.0,
            tilt_target_db=-8.0,
            tilt_limit_db=4.0,
        )

        # What the engine's playback path produces, via the same call
        # ``_play_clip`` makes.
        played = shape_clip(
            audio, RATE, shaping=shaping, speed=1.0, gain_factor=1.0,
            text="hey, how did the build go",
        )

        # What the lab plays, given the same shaping.
        original = asheard.shaping_for
        asheard.shaping_for = lambda *_a, **_k: shaping
        try:
            preview = asheard.apply(
                audio,
                RATE,
                engine="chatterbox-nano",
                reference=None,
                text="hey, how did the build go",
            )
        finally:
            asheard.shaping_for = original

        np.testing.assert_allclose(
            preview.audio, played.rendered(), atol=1e-6
        )
        self.assertEqual(preview.sample_rate, played.playback_rate)

    def test_the_report_names_the_stages_that_ran(self) -> None:
        from tools.tts_lab import asheard

        shaping = Shaping(loudness_target_dbfs=-26.0)
        original = asheard.shaping_for
        asheard.shaping_for = lambda *_a, **_k: shaping
        try:
            preview = asheard.apply(
                _speech(level=0.05),
                RATE,
                engine="chatterbox-nano",
                reference=None,
                text="hey",
            )
        finally:
            asheard.shaping_for = original
        self.assertIn("level", preview.report["stages"])
        self.assertFalse(preview.report["inert"])

    def test_an_inert_engine_says_so_rather_than_listing_nothing(
        self,
    ) -> None:
        from tools.tts_lab import asheard

        original = asheard.shaping_for
        asheard.shaping_for = lambda *_a, **_k: Shaping()
        try:
            preview = asheard.apply(
                _speech(),
                RATE,
                engine="pocket-tts",
                reference=None,
                text="hey",
            )
        finally:
            asheard.shaping_for = original
        self.assertTrue(preview.report["inert"])
        self.assertEqual(preview.report["stages"], [])


if __name__ == "__main__":
    unittest.main()
