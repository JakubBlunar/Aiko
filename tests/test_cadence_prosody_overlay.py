"""Layer 3b tests: cadence consumes the prosody tag and overlays params.

Covers:
  * ``analyze_sentence`` consumes a leading tag and returns the
    overlaid ``ProsodyParams`` (speed / gain / pause / label).
  * Each of the five v1 values produces the documented overlay.
  * The overlay dies with the sentence -- the next call gets a clean
    ``ProsodyParams`` again.
  * Ambient gain stacks additively on top of a prosody overlay.
  * ``ProsodyDispatcher.dispatch`` strips the leading tag before
    forwarding to ``enqueue`` so the engine never sees it.
"""
from __future__ import annotations

import random
import unittest

from app.core.voice.cadence import (
    CadenceContext,
    ProsodyDispatcher,
    ProsodyParams,
    _PROSODY_OVERLAYS,
    _apply_prosody_overlay,
    analyze_sentence,
)


def _ctx(reaction: str = "neutral", **kwargs) -> CadenceContext:
    """Helper -- deterministic RNG so the prefix lottery never fires."""
    return CadenceContext(
        base_reaction=reaction,
        rng=random.Random(0),
        **kwargs,
    )


class AnalyzeSentenceProsodyTests(unittest.TestCase):
    def test_whisper_overlay_lowers_speed_and_gain(self) -> None:
        params = analyze_sentence(
            "[[prosody:whisper]] I missed you",
            _ctx("warm"),
        )
        self.assertEqual(params.prosody_label, "whisper")
        # Speed multiplied by the whisper overlay (0.97).
        speed_mult, gain_delta, _ = _PROSODY_OVERLAYS["whisper"]
        # Without the prosody tag the warm reaction sentence would
        # produce speed=1.0; multiplied by 0.97 = 0.97.
        self.assertAlmostEqual(params.speed_hint, 1.0 * speed_mult, places=3)
        # Gain offset matches the overlay.
        self.assertAlmostEqual(params.gain_db, gain_delta, places=3)

    def test_firm_overlay_pause_and_gain(self) -> None:
        params = analyze_sentence(
            "[[prosody:firm]] no, that's not right",
            _ctx("serious"),
        )
        self.assertEqual(params.prosody_label, "firm")
        speed_mult, gain_delta, pause_before = _PROSODY_OVERLAYS["firm"]
        self.assertAlmostEqual(params.gain_db, gain_delta, places=3)
        self.assertGreaterEqual(params.pause_before_ms, pause_before)

    def test_slow_overlay_slows_speed(self) -> None:
        params = analyze_sentence(
            "[[prosody:slow]] I really mean that",
            _ctx("warm"),
        )
        self.assertEqual(params.prosody_label, "slow")
        speed_mult, _, _ = _PROSODY_OVERLAYS["slow"]
        self.assertAlmostEqual(params.speed_hint, 1.0 * speed_mult, places=3)

    def test_fast_overlay_speeds_up(self) -> None:
        params = analyze_sentence(
            "[[prosody:fast]] wait wait wait",
            _ctx("warm"),
        )
        self.assertEqual(params.prosody_label, "fast")
        speed_mult, _, _ = _PROSODY_OVERLAYS["fast"]
        self.assertAlmostEqual(params.speed_hint, 1.0 * speed_mult, places=3)

    def test_unknown_label_no_overlay(self) -> None:
        # Unknown values never reach analyze_sentence's overlay
        # branch -- ``consume_leading_prosody_tag`` rejects them and
        # the catch-all strip drops the tag downstream.
        params = analyze_sentence(
            "[[prosody:bogus]] hi there",
            _ctx("warm"),
        )
        self.assertEqual(params.prosody_label, "")
        self.assertAlmostEqual(params.gain_db, 0.0)


class ProsodyOverlayDeathTests(unittest.TestCase):
    """The overlay applies to a single sentence and doesn't bleed forward."""

    def test_no_state_carried_across_calls(self) -> None:
        ctx = _ctx("warm")
        first = analyze_sentence(
            "[[prosody:whisper]] secret thing",
            ctx,
        )
        self.assertEqual(first.prosody_label, "whisper")
        # Second sentence has no tag; should land back at default speed
        # / gain (0.0) without the previous overlay's residue.
        second = analyze_sentence("normal sentence here", ctx)
        self.assertEqual(second.prosody_label, "")
        self.assertAlmostEqual(second.gain_db, 0.0)


class AmbientPlusProsodyTests(unittest.TestCase):
    """Ambient noise gain (dB) stacks additively with a prosody overlay."""

    def test_whisper_in_noisy_room(self) -> None:
        ctx = _ctx("warm", ambient_volume_db_offset=1.5)
        params = analyze_sentence(
            "[[prosody:whisper]] I missed you", ctx,
        )
        # Whisper overlay is -6 dB; ambient adds +1.5 dB. Combined gain
        # should be -4.5 dB (within rounding).
        _, gain_delta, _ = _PROSODY_OVERLAYS["whisper"]
        self.assertAlmostEqual(
            params.gain_db, gain_delta + 1.5, places=3,
        )


class _RecordingEnqueue:
    """Minimal recorder used by the dispatcher tests below."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, float | None, float]] = []

    def __call__(self, text, reaction, *, speed=None, gain_db=0.0):
        self.calls.append((text, reaction, speed, float(gain_db)))


class DispatchTagStripTests(unittest.TestCase):
    """``ProsodyDispatcher.dispatch`` removes the leading tag before
    forwarding to ``enqueue`` so the synth backend never sees it."""

    def test_dispatch_strips_leading_tag(self) -> None:
        recorder = _RecordingEnqueue()
        dispatcher = ProsodyDispatcher(recorder, enabled=True)
        dispatcher.dispatch(
            "[[prosody:whisper]] confessions hour",
            reaction="warm",
        )
        self.assertEqual(len(recorder.calls), 1)
        text, reaction, speed, gain_db = recorder.calls[0]
        self.assertNotIn("[[prosody:", text)
        self.assertIn("confessions hour", text)
        # Gain offset reaches the engine via the kwarg.
        _, expected_gain, _ = _PROSODY_OVERLAYS["whisper"]
        self.assertAlmostEqual(gain_db, expected_gain, places=3)

    def test_dispatch_passes_through_when_disabled(self) -> None:
        # When the dispatcher is disabled the tag should still NOT
        # leak to the engine because the queue's ``prepare_tts_text``
        # strips it. We just confirm the dispatcher forwards the raw
        # sentence and let the queue tests cover the strip path.
        recorder = _RecordingEnqueue()
        dispatcher = ProsodyDispatcher(recorder, enabled=False)
        dispatcher.dispatch(
            "[[prosody:whisper]] confessions hour",
            reaction="warm",
        )
        self.assertEqual(len(recorder.calls), 1)
        text, _, _, _ = recorder.calls[0]
        # In disabled mode the raw cleaned text is forwarded; the
        # synth-side prepare_tts_text catch-all takes care of the tag.
        self.assertIn("[[prosody:whisper]]", text)


class ApplyProsodyOverlayUnitTests(unittest.TestCase):
    """Direct unit tests for the overlay helper -- no analyzer in the way."""

    def test_unknown_label_returns_base(self) -> None:
        base = ProsodyParams(reaction="neutral", speed_hint=1.0)
        out = _apply_prosody_overlay(base, "scream")
        # Unknown -> identical params.
        self.assertEqual(out.reaction, base.reaction)
        self.assertAlmostEqual(out.speed_hint, base.speed_hint)
        self.assertAlmostEqual(out.gain_db, base.gain_db)

    def test_pause_max_not_lowered(self) -> None:
        base = ProsodyParams(
            reaction="neutral", speed_hint=1.0, pause_before_ms=300,
        )
        out = _apply_prosody_overlay(base, "firm")
        # Firm requests 80 ms pause_before but base already has 300; max wins.
        self.assertEqual(out.pause_before_ms, 300)

    def test_label_carried_through(self) -> None:
        base = ProsodyParams(reaction="warm", speed_hint=1.0)
        out = _apply_prosody_overlay(base, "soft")
        self.assertEqual(out.prosody_label, "soft")


if __name__ == "__main__":
    unittest.main()
