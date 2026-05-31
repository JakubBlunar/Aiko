"""Tests for the slow-first-token FillerInjector (Phase 1c)."""
from __future__ import annotations

import threading
import time
import unittest

from app.core.voice.filler_injector import FillerInjector, pick_filler


class PickFillerTests(unittest.TestCase):
    def test_returns_phrase_and_reaction(self) -> None:
        phrase, reaction = pick_filler("thoughtful")
        self.assertTrue(isinstance(phrase, str) and len(phrase) > 0)
        self.assertEqual(reaction, "thoughtful")

    def test_unknown_reaction_falls_back_to_neutral(self) -> None:
        phrase, reaction = pick_filler("xenomorphic")
        self.assertTrue(isinstance(phrase, str) and len(phrase) > 0)
        self.assertEqual(reaction, "xenomorphic")

    def test_none_reaction_uses_neutral_bucket(self) -> None:
        phrase, reaction = pick_filler(None)
        self.assertTrue(len(phrase) > 0)
        self.assertEqual(reaction, "thoughtful")


class FillerInjectorTests(unittest.TestCase):
    def test_disabled_does_not_fire(self) -> None:
        events: list[tuple[str, str]] = []
        inj = FillerInjector(threshold_ms=50, enabled=False)
        inj.arm(lambda phrase, reaction: events.append((phrase, reaction)),
                carry_over_reaction="thoughtful")
        time.sleep(0.15)
        self.assertEqual(events, [])
        self.assertFalse(inj.fired)

    def test_fires_after_threshold(self) -> None:
        events: list[tuple[str, str]] = []
        inj = FillerInjector(threshold_ms=80, enabled=True)
        inj.arm(lambda phrase, reaction: events.append((phrase, reaction)),
                carry_over_reaction="warm")
        time.sleep(0.20)
        self.assertEqual(len(events), 1)
        phrase, reaction = events[0]
        self.assertTrue(len(phrase) > 0)
        # warm carry-over should produce a "warm" reaction tag for TTS.
        self.assertEqual(reaction, "warm")
        self.assertTrue(inj.fired)

    def test_disarm_before_fire_prevents_emit(self) -> None:
        events: list[tuple[str, str]] = []
        inj = FillerInjector(threshold_ms=200, enabled=True)
        inj.arm(lambda phrase, reaction: events.append((phrase, reaction)),
                carry_over_reaction="thoughtful")
        # Disarm before timer fires.
        already_fired = inj.disarm()
        self.assertFalse(already_fired)
        time.sleep(0.30)
        self.assertEqual(events, [])

    def test_disarm_after_fire_returns_true(self) -> None:
        events: list[tuple[str, str]] = []
        inj = FillerInjector(threshold_ms=50, enabled=True)
        inj.arm(lambda phrase, reaction: events.append((phrase, reaction)),
                carry_over_reaction="cheerful")
        time.sleep(0.15)
        self.assertEqual(len(events), 1)
        self.assertTrue(inj.disarm())

    def test_arm_again_resets_fired_flag(self) -> None:
        events: list[tuple[str, str]] = []
        inj = FillerInjector(threshold_ms=50, enabled=True)
        inj.arm(lambda p, r: events.append((p, r)), carry_over_reaction=None)
        time.sleep(0.15)
        self.assertTrue(inj.fired)
        inj.arm(lambda p, r: events.append((p, r)), carry_over_reaction=None)
        self.assertFalse(inj.fired)
        inj.disarm()

    def test_no_callback_does_not_crash(self) -> None:
        inj = FillerInjector(threshold_ms=50, enabled=True)
        # Passing None for the callback should be a clean no-op.
        inj.arm(None, carry_over_reaction="neutral")
        time.sleep(0.15)
        self.assertFalse(inj.fired)
        inj.disarm()

    def test_concurrent_disarm_during_fire(self) -> None:
        # Stress: many threads disarm while the fire is racing.
        events: list[tuple[str, str]] = []
        inj = FillerInjector(threshold_ms=50, enabled=True)
        inj.arm(lambda p, r: events.append((p, r)), carry_over_reaction=None)
        threads = [threading.Thread(target=inj.disarm) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        time.sleep(0.15)
        # Either fired exactly once (race) or not at all (disarm won).
        self.assertLessEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
