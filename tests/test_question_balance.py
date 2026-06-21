"""K47 — question/share balance (stop interviewing).

Three layers:

1. The pure helpers in ``app.core.conversation.question_balance``
   (``is_question_turn`` / ``compute_ratio`` / ``should_suppress`` /
   ``render_share_first_cue``).
2. The post-turn gate logic ``PostTurnMixin._update_question_balance``
   (ring append + suppress-countdown arm/decay) via a minimal host.
3. The provider plumbing: ``_render_question_balance_block`` surfaces
   the share-first cue while armed, and the question-pushing providers
   early-return ``""`` while armed.
"""
from __future__ import annotations

import unittest
from collections import deque
from types import SimpleNamespace
from typing import Any

from app.core.conversation.question_balance import (
    compute_ratio,
    is_question_turn,
    render_share_first_cue,
    should_suppress,
)
from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin
from app.core.session.post_turn_mixin import PostTurnMixin


# ── 1. pure helpers ────────────────────────────────────────────────


class PureHelperTests(unittest.TestCase):
    def test_is_question_turn_any_question_mark(self) -> None:
        self.assertTrue(is_question_turn("how are you?"))
        self.assertTrue(is_question_turn("wait, really? cool."))
        self.assertFalse(is_question_turn("that's wild."))
        self.assertFalse(is_question_turn(""))
        self.assertFalse(is_question_turn(None))  # type: ignore[arg-type]

    def test_compute_ratio(self) -> None:
        self.assertEqual(compute_ratio([]), 0.0)
        self.assertEqual(compute_ratio([True, True]), 1.0)
        self.assertAlmostEqual(compute_ratio([True, False, True]), 2 / 3)

    def test_should_suppress_requires_min_samples(self) -> None:
        # 100% questions but only 3 samples < min_samples=5 -> no.
        self.assertFalse(
            should_suppress([True, True, True], threshold=0.55, min_samples=5)
        )

    def test_should_suppress_strictly_above_threshold(self) -> None:
        flags = [True] * 6 + [False] * 4  # ratio 0.6
        self.assertTrue(
            should_suppress(flags, threshold=0.55, min_samples=5)
        )
        # exactly at threshold does NOT fire (strict >).
        half = [True] * 5 + [False] * 5  # ratio 0.5
        self.assertFalse(
            should_suppress(half, threshold=0.5, min_samples=5)
        )

    def test_render_share_first_cue_name(self) -> None:
        cue = render_share_first_cue("Jacob")
        self.assertIn("Jacob", cue)
        self.assertIn("share first", cue.lower())
        # fallback name
        self.assertIn("them", render_share_first_cue(None))


# ── 2. post-turn gate logic ────────────────────────────────────────


def _agent(**overrides: Any) -> SimpleNamespace:
    base = dict(
        question_balance_enabled=True,
        question_balance_ratio_threshold=0.55,
        question_balance_window=10,
        question_balance_suppress_turns=2,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _GateHost(PostTurnMixin):
    def __init__(self, agent: SimpleNamespace | None = None) -> None:
        self._settings = SimpleNamespace(agent=agent or _agent())
        window = self._settings.agent.question_balance_window
        self._question_turn_flags: deque[bool] = deque(maxlen=window)
        self._question_balance_suppress_remaining = 0


class PostTurnGateTests(unittest.TestCase):
    def test_arms_after_question_streak(self) -> None:
        host = _GateHost()
        # 6 question turns out of 6 -> ratio 1.0 > 0.55, samples >= 5.
        for _ in range(6):
            host._update_question_balance("how's that going?")
        self.assertEqual(host._question_balance_suppress_remaining, 2)

    def test_stays_silent_below_threshold(self) -> None:
        host = _GateHost()
        # mostly statements, one question -> ratio 1/6 < 0.55.
        host._update_question_balance("anyway?")
        for _ in range(5):
            host._update_question_balance("that's wild.")
        self.assertEqual(host._question_balance_suppress_remaining, 0)

    def test_decays_when_ratio_drops(self) -> None:
        host = _GateHost()
        for _ in range(6):
            host._update_question_balance("really?")
        self.assertEqual(host._question_balance_suppress_remaining, 2)
        # Now flood the window with statements so ratio falls under
        # threshold; the countdown should decay 2 -> 1 -> 0.
        host._update_question_balance("here's a thought.")  # decay, recompute
        first = host._question_balance_suppress_remaining
        for _ in range(20):
            host._update_question_balance("statement.")
        self.assertEqual(host._question_balance_suppress_remaining, 0)
        self.assertLessEqual(first, 2)

    def test_disabled_when_suppress_turns_zero(self) -> None:
        host = _GateHost(_agent(question_balance_suppress_turns=0))
        for _ in range(8):
            host._update_question_balance("why?")
        self.assertEqual(host._question_balance_suppress_remaining, 0)


# ── 3. provider plumbing ───────────────────────────────────────────


class _ProviderHost(InnerLifeProvidersMixin):
    def __init__(
        self,
        *,
        suppress_remaining: int = 0,
        agent: SimpleNamespace | None = None,
    ) -> None:
        agent = agent or _agent()
        # Enable the question-pushing providers' own master switches so
        # the K47 guard is the thing under test (not their own gates).
        for flag in (
            "curiosity_seed_enabled",
            "forward_curiosity_enabled",
            "follow_up_enabled",
        ):
            setattr(agent, flag, True)
        self._settings = SimpleNamespace(agent=agent)
        self._question_balance_suppress_remaining = suppress_remaining
        self.user_display_name = "Jacob"
        # Stores the guarded providers would reach for *after* the guard;
        # they should never be touched while suppressed.
        self._knowledge_gap_store = None
        self._memory_store = None


class ProviderTests(unittest.TestCase):
    def test_share_first_cue_only_when_armed(self) -> None:
        self.assertEqual(
            _ProviderHost(suppress_remaining=0)._render_question_balance_block(),
            "",
        )
        cue = _ProviderHost(
            suppress_remaining=2
        )._render_question_balance_block()
        self.assertIn("share first", cue.lower())
        self.assertIn("Jacob", cue)

    def test_master_switch_off_never_suppresses(self) -> None:
        host = _ProviderHost(
            suppress_remaining=2,
            agent=_agent(question_balance_enabled=False),
        )
        self.assertFalse(host._question_balance_suppressed())
        self.assertEqual(host._render_question_balance_block(), "")

    def test_question_pushers_muted_while_armed(self) -> None:
        host = _ProviderHost(suppress_remaining=2)
        self.assertTrue(host._question_balance_suppressed())
        self.assertEqual(host._render_knowledge_gaps_block("anything"), "")
        self.assertEqual(host._render_curiosity_seeds_block(), "")
        self.assertEqual(host._render_forward_curiosity_block(), "")
        self.assertEqual(host._render_follow_up_block(), "")

    def test_no_double_decrement_on_repeated_render(self) -> None:
        # The provider must NOT mutate the countdown (decay is post-turn),
        # so two renders in the same assembly stay consistent.
        host = _ProviderHost(suppress_remaining=2)
        host._render_question_balance_block()
        host._render_question_balance_block()
        self.assertEqual(host._question_balance_suppress_remaining, 2)


if __name__ == "__main__":
    unittest.main()
