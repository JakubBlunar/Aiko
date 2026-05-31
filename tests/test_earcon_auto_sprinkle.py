"""Layer 4b tests: cadence auto-sprinkles breath / soft_sigh on sad openers.

Covers:
  * Sad / wistful / melancholy / cry / concerned reactions get a
    breath or soft_sigh prepended (probability + cooldown gate).
  * Other reactions never auto-sprinkle.
  * Cooldown suppresses repeats inside the window.
  * Disabling ``earcon_auto_sprinkle`` silences the cue entirely.
  * A sentence that already has a prefix or prosody label is skipped
    (don't pile cues onto cues).
"""
from __future__ import annotations

import random
import unittest
from typing import Any

from app.core.voice.cadence import (
    CadenceContext,
    ProsodyDispatcher,
    ProsodyParams,
    analyze_sentence,
)


class _FixedRandom(random.Random):
    """A random.Random subclass whose ``random()`` returns 0.0 forever."""

    def random(self) -> float:  # type: ignore[override]
        return 0.0


class _RecordingProvider:
    """Stand-in for ``TtsQueue.enqueue_earcon`` used by the dispatcher."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, kind: str) -> None:
        self.calls.append(kind)


class _RecordingEnqueue:
    """Minimal recorder for ``ProsodyDispatcher`` text output."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, float | None]] = []

    def __call__(self, text, reaction, *, speed=None, gain_db=0.0):
        self.calls.append((text, reaction, speed))


def _make_dispatcher(
    *,
    enabled: bool = True,
    sprinkle: bool = True,
    rng: random.Random | None = None,
) -> tuple[ProsodyDispatcher, _RecordingEnqueue, _RecordingProvider]:
    enqueue = _RecordingEnqueue()
    earcon = _RecordingProvider()
    dispatcher = ProsodyDispatcher(
        enqueue,
        rng=rng or _FixedRandom(0),
        enabled=enabled,
        earcon_auto_sprinkle=sprinkle,
    )
    dispatcher.set_earcon_provider(earcon)
    # Quiet ambient context so the analyzer doesn't add gain noise to
    # the assertions below.
    dispatcher.set_context_provider(
        lambda: CadenceContext(
            base_reaction="neutral",
            rng=_FixedRandom(0),
        )
    )
    return dispatcher, enqueue, earcon


class AutoSprinkleHappyPathTests(unittest.TestCase):
    def test_sad_opener_fires_breath_or_soft_sigh(self) -> None:
        dispatcher, enqueue, earcon = _make_dispatcher()
        # ``derive_sentence_reaction`` won't pick "sad" on its own from
        # this text -- we pass the carrier reaction through dispatch
        # so the sentence reaction lands on "sad".
        dispatcher.dispatch(
            "I miss them so much.", reaction="sad",
        )
        self.assertEqual(len(earcon.calls), 1)
        self.assertIn(earcon.calls[0], {"breath", "soft_sigh"})
        self.assertEqual(len(enqueue.calls), 1)

    def test_cry_reaction_also_fires(self) -> None:
        dispatcher, _enqueue, earcon = _make_dispatcher()
        dispatcher.dispatch(
            "I just miss them so much it hurts to think about.",
            reaction="cry",
        )
        self.assertEqual(len(earcon.calls), 1)


class AutoSprinkleGateTests(unittest.TestCase):
    def test_cheerful_reaction_never_fires(self) -> None:
        dispatcher, _enqueue, earcon = _make_dispatcher()
        dispatcher.dispatch(
            "I had the best day! Everything went great.",
            reaction="cheerful",
        )
        self.assertEqual(earcon.calls, [])

    def test_neutral_reaction_never_fires(self) -> None:
        dispatcher, _enqueue, earcon = _make_dispatcher()
        dispatcher.dispatch(
            "Yeah, that makes sense.", reaction="neutral",
        )
        self.assertEqual(earcon.calls, [])

    def test_disabled_setting_silences_sprinkle(self) -> None:
        dispatcher, _enqueue, earcon = _make_dispatcher(sprinkle=False)
        dispatcher.dispatch(
            "I miss them so much.", reaction="sad",
        )
        self.assertEqual(earcon.calls, [])

    def test_no_provider_drops_silently(self) -> None:
        # Build a dispatcher *without* installing an earcon provider --
        # the auto-sprinkle path noops cleanly.
        enqueue = _RecordingEnqueue()
        dispatcher = ProsodyDispatcher(
            enqueue,
            rng=_FixedRandom(0),
            enabled=True,
            earcon_auto_sprinkle=True,
        )
        dispatcher.dispatch("I miss them.", reaction="sad")
        self.assertEqual(len(enqueue.calls), 1)

    def test_cooldown_suppresses_repeats(self) -> None:
        dispatcher, _enqueue, earcon = _make_dispatcher()
        dispatcher.dispatch("I miss them.", reaction="sad")
        dispatcher.dispatch("It hurts.", reaction="sad")
        # Cooldown is 25 s; second dispatch must not fire.
        self.assertEqual(len(earcon.calls), 1)


class AutoSprinkleDoesNotPileTests(unittest.TestCase):
    """Sentences with a prosody label or a prefix interjection are
    skipped by auto-sprinkle so the cue doesn't stack on a cue."""

    def test_prosody_label_skips_sprinkle(self) -> None:
        dispatcher, _enqueue, earcon = _make_dispatcher()
        dispatcher.dispatch(
            "[[prosody:whisper]] I miss them.", reaction="sad",
        )
        self.assertEqual(earcon.calls, [])

    def test_prefix_interjection_skips_sprinkle(self) -> None:
        # Synthetic: build a ``ProsodyParams`` with a prefix and feed
        # it directly into the maybe-sprinkle helper. analyze_sentence
        # sometimes produces a prefix on its own, but the deterministic
        # _FixedRandom we use for the dispatcher tests shouldn't.
        dispatcher, _enqueue, earcon = _make_dispatcher()
        params = ProsodyParams(
            reaction="sad", prefix_text="Yeah,", prefix_reaction="concerned",
        )
        dispatcher._maybe_auto_sprinkle(params)
        self.assertEqual(earcon.calls, [])


class AutoSprinkleProbabilityTests(unittest.TestCase):
    """The 30% fire-rate gate: an RNG that always returns 1.0 should
    never auto-sprinkle even when every other gate is open."""

    def test_unlucky_rng_skips(self) -> None:
        class _AlwaysHigh(random.Random):
            def random(self) -> float:  # type: ignore[override]
                return 0.99

        dispatcher, _enqueue, earcon = _make_dispatcher(rng=_AlwaysHigh(0))
        dispatcher.dispatch("I miss them.", reaction="sad")
        self.assertEqual(earcon.calls, [])


if __name__ == "__main__":
    unittest.main()
