"""Pacing is hers, not the engine's.

The bug these pin: ``set_length_scale`` and ``set_runtime_speed_enabled``
existed only on pocket-tts, and ``_apply_assistant_preferences`` reaches
both through ``getattr``, so on Chatterbox they were *absent* rather than
broken. The pacing slider silently did nothing, and the affect-speed gate
being off did not stop the cadence layer's per-sentence hints from being
applied in full -- she simply spoke faster on one engine than the other.
"""

from __future__ import annotations

import inspect
import unittest

from app.tts import reactions
from app.tts.reactions import (
    LENGTH_SCALE_MAX,
    LENGTH_SCALE_MIN,
    SPEED_MAX,
    SPEED_MIN,
    clamp_length_scale,
    resolve_playback_speed,
)


class GateTests(unittest.TestCase):
    def test_the_gate_off_pins_every_sentence_flat(self) -> None:
        """Off is the default, and it has to ignore both the reaction
        baseline and whatever the cadence layer asked for -- otherwise
        the affect channel is live while the setting says it is not."""
        for reaction, hint in (
            ("excited", 1.12), ("cheerful", None), ("cry", 0.88), (None, 1.05),
        ):
            self.assertEqual(
                resolve_playback_speed(
                    reaction, hint, runtime_speed_enabled=False
                ),
                1.0,
            )

    def test_the_gate_on_uses_the_reaction_baseline(self) -> None:
        self.assertAlmostEqual(
            resolve_playback_speed(
                "excited", None, runtime_speed_enabled=True
            ),
            reactions.REACTION_SPEED["excited"],
            places=4,
        )

    def test_the_gate_on_honours_the_cadence_hint(self) -> None:
        self.assertAlmostEqual(
            resolve_playback_speed(
                "neutral", 1.04, runtime_speed_enabled=True
            ),
            1.04,
            places=4,
        )

    def test_a_runaway_hint_is_capped(self) -> None:
        """The uncapped path was the audible half of the Chatterbox bug."""
        speed = resolve_playback_speed(
            "cheerful", 4.0, runtime_speed_enabled=True
        )
        self.assertLessEqual(speed, SPEED_MAX)
        self.assertLessEqual(speed, reactions.resolve_speed_caps("cheerful")[1])

    def test_a_nonsense_hint_falls_back_to_the_reaction(self) -> None:
        self.assertAlmostEqual(
            resolve_playback_speed(
                "tender", "quickly", runtime_speed_enabled=True
            ),
            reactions.REACTION_SPEED["tender"],
            places=4,
        )


class LengthScaleTests(unittest.TestCase):
    def test_the_slider_applies_even_with_the_gate_off(self) -> None:
        """A deliberate global preference, not affect drift, so the gate
        must not switch it off."""
        speed = resolve_playback_speed(
            "neutral", None, runtime_speed_enabled=False, length_scale=1.08
        )
        self.assertAlmostEqual(speed, 1.0 / 1.08, places=4)

    def test_above_one_is_slower(self) -> None:
        slower = resolve_playback_speed(
            "neutral", None, runtime_speed_enabled=False, length_scale=1.10
        )
        faster = resolve_playback_speed(
            "neutral", None, runtime_speed_enabled=False, length_scale=0.90
        )
        self.assertLess(slower, faster)

    def test_the_slider_is_clamped(self) -> None:
        self.assertEqual(clamp_length_scale(5.0), LENGTH_SCALE_MAX)
        self.assertEqual(clamp_length_scale(0.1), LENGTH_SCALE_MIN)

    def test_nonsense_and_zero_read_as_no_change(self) -> None:
        for value in ("slow", None, 0.0, -1.0):
            self.assertEqual(clamp_length_scale(value), 1.0)

    def test_the_result_stays_inside_the_safe_envelope(self) -> None:
        """A slow slider stacked on the slowest reaction lands under
        SPEED_MIN, and the final clamp is what stops that reaching the
        stretch."""
        speed = resolve_playback_speed(
            "cry", None, runtime_speed_enabled=True, length_scale=1.15
        )
        self.assertGreaterEqual(speed, SPEED_MIN)
        self.assertLessEqual(speed, SPEED_MAX)


class EngineParityTests(unittest.TestCase):
    """Both engines have to expose the knobs, or the wiring skips them."""

    def _engines(self):
        from app.core.infra.settings import load_settings
        from app.tts.chatterbox_service import ChatterboxTtsService
        from app.tts.pocket_tts_service import PocketTtsService

        settings = load_settings().tts
        settings.enabled = False
        return (
            PocketTtsService.__new__(PocketTtsService),
            ChatterboxTtsService.__new__(ChatterboxTtsService),
        )

    def test_both_engines_expose_the_pacing_knobs(self) -> None:
        """``_apply_assistant_preferences`` looks these up with
        ``getattr`` and skips silently when absent, which is how the
        Chatterbox gap went unnoticed."""
        for engine in self._engines():
            for name in (
                "set_length_scale",
                "get_length_scale",
                "set_runtime_speed_enabled",
                "get_runtime_speed_enabled",
            ):
                self.assertTrue(
                    callable(getattr(engine, name, None)),
                    f"{type(engine).__name__} is missing {name}",
                )

    def test_both_engines_round_trip_the_slider_identically(self) -> None:
        for engine in self._engines():
            engine.set_length_scale(1.08)
            self.assertAlmostEqual(engine.get_length_scale(), 1.08, places=4)
            engine.set_length_scale(99.0)
            self.assertEqual(engine.get_length_scale(), LENGTH_SCALE_MAX)

    def test_both_engines_round_trip_the_gate_identically(self) -> None:
        for engine in self._engines():
            engine.set_runtime_speed_enabled(True)
            self.assertTrue(engine.get_runtime_speed_enabled())
            engine.set_runtime_speed_enabled(False)
            self.assertFalse(engine.get_runtime_speed_enabled())

    def test_a_fresh_chatterbox_defaults_to_the_incumbent_behaviour(self) -> None:
        """A provider swap must not also be a pacing change: a new engine
        starts flat and ungated, exactly as pocket-tts does."""
        from app.tts.chatterbox_service import ChatterboxTtsService

        engine = ChatterboxTtsService.__new__(ChatterboxTtsService)
        ChatterboxTtsService.set_length_scale(engine, 1.0)
        ChatterboxTtsService.set_runtime_speed_enabled(engine, False)
        self.assertEqual(engine.get_length_scale(), 1.0)
        self.assertFalse(engine.get_runtime_speed_enabled())
        # And the constructor's own defaults, read off the source rather
        # than by starting a subprocess.
        source = inspect.getsource(ChatterboxTtsService.__init__)
        self.assertIn("self._length_scale: float = 1.0", source)
        self.assertIn("self._runtime_speed_enabled: bool = False", source)


if __name__ == "__main__":
    unittest.main()
