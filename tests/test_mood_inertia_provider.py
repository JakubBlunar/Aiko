"""Controller-plumbing tests for the K45 mood-inertia feature.

Two halves, both exercised via minimal stub hosts (no full
:class:`SessionController`):

* ``PostTurnMixin._maybe_arm_mood_inertia`` — ring append, master
  switch, cooldown arm + decrement, strong-band arming, pre-impulse
  state usage.
* ``InnerLifeProvidersMixin._render_mood_inertia_block`` — one-shot
  consumption, master switch, force-flag bypass.

The pure math (targets / mismatch / bands / cue copy) is covered in
``tests/test_mood_inertia.py``.
"""
from __future__ import annotations

import unittest
from collections import deque
from types import SimpleNamespace
from typing import Any

from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin
from app.core.session.post_turn_mixin import PostTurnMixin


def _settings(
    *,
    enabled: bool = True,
    threshold: float = 0.45,
    cooldown: int = 3,
) -> SimpleNamespace:
    return SimpleNamespace(
        agent=SimpleNamespace(mood_inertia_enabled=enabled),
        memory=SimpleNamespace(
            mood_inertia_mismatch_threshold=threshold,
            mood_inertia_cooldown_turns=cooldown,
        ),
    )


class _ArmHost(PostTurnMixin):
    """Minimal host for ``_maybe_arm_mood_inertia``."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        threshold: float = 0.45,
        cooldown: int = 3,
        cooldown_remaining: int = 0,
    ) -> None:
        settings = _settings(
            enabled=enabled, threshold=threshold, cooldown=cooldown,
        )
        self._settings = settings
        self._memory_settings = settings.memory
        self._mood_inertia_reactions: deque[str] = deque(maxlen=3)
        self._pending_mood_inertia: Any = None
        self._mood_inertia_cooldown_remaining = cooldown_remaining
        self._mood_inertia_last: dict[str, Any] | None = None


def _affect(valence: float, arousal: float) -> SimpleNamespace:
    return SimpleNamespace(valence=valence, arousal=arousal)


class MaybeArmMoodInertiaTests(unittest.TestCase):
    def test_strong_mismatch_arms_pending_cue(self) -> None:
        host = _ArmHost()
        host._maybe_arm_mood_inertia(
            reaction="excited", affect_before=_affect(-0.7, 0.2),
        )
        self.assertIsNotNone(host._pending_mood_inertia)
        self.assertIn("excited", str(host._pending_mood_inertia))
        self.assertEqual(host._mood_inertia_cooldown_remaining, 3)
        assert host._mood_inertia_last is not None
        self.assertEqual(host._mood_inertia_last["band"], "strong")

    def test_aligned_reaction_does_not_arm(self) -> None:
        host = _ArmHost()
        host._maybe_arm_mood_inertia(
            reaction="excited", affect_before=_affect(0.8, 0.9),
        )
        self.assertIsNone(host._pending_mood_inertia)
        assert host._mood_inertia_last is not None
        self.assertEqual(host._mood_inertia_last["band"], "none")

    def test_master_switch_off_skips_assessment(self) -> None:
        host = _ArmHost(enabled=False)
        host._maybe_arm_mood_inertia(
            reaction="excited", affect_before=_affect(-0.9, 0.1),
        )
        self.assertIsNone(host._pending_mood_inertia)
        self.assertIsNone(host._mood_inertia_last)
        # The ring still advances so whiplash context survives a
        # toggle flip.
        self.assertEqual(list(host._mood_inertia_reactions), ["excited"])

    def test_cooldown_blocks_and_decrements(self) -> None:
        host = _ArmHost(cooldown_remaining=2)
        host._maybe_arm_mood_inertia(
            reaction="excited", affect_before=_affect(-0.9, 0.1),
        )
        self.assertIsNone(host._pending_mood_inertia)
        self.assertEqual(host._mood_inertia_cooldown_remaining, 1)

    def test_ring_advances_even_on_gated_turns(self) -> None:
        host = _ArmHost(cooldown_remaining=5)
        for reaction in ("excited", "sad", "cheerful"):
            host._maybe_arm_mood_inertia(
                reaction=reaction, affect_before=_affect(0.0, 0.4),
            )
        self.assertEqual(
            list(host._mood_inertia_reactions),
            ["excited", "sad", "cheerful"],
        )

    def test_missing_affect_or_reaction_is_silent(self) -> None:
        host = _ArmHost()
        host._maybe_arm_mood_inertia(reaction="", affect_before=_affect(0, 0.4))
        host._maybe_arm_mood_inertia(reaction="excited", affect_before=None)
        self.assertIsNone(host._pending_mood_inertia)

    def test_whiplash_history_feeds_assessment(self) -> None:
        # The same borderline mismatch fires only once whiplash from
        # the ring bumps it over the threshold.
        host = _ArmHost(threshold=0.5)
        host._maybe_arm_mood_inertia(
            reaction="sad", affect_before=_affect(0.0, 0.4),
        )
        host._pending_mood_inertia = None
        host._mood_inertia_cooldown_remaining = 0
        host._maybe_arm_mood_inertia(
            reaction="cheerful", affect_before=_affect(-0.1, 0.4),
        )
        assert host._mood_inertia_last is not None
        self.assertTrue(host._mood_inertia_last["whiplash"])


class _FakeAffectStore:
    def __init__(self, valence: float = -0.6, arousal: float = 0.2) -> None:
        self._state = SimpleNamespace(valence=valence, arousal=arousal)

    def get(self, user_id: str) -> SimpleNamespace:  # noqa: ARG002
        return self._state


class _RenderHost(InnerLifeProvidersMixin):
    """Minimal host for ``_render_mood_inertia_block``."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        pending: str | None = None,
        force: bool = False,
    ) -> None:
        self._settings = _settings(enabled=enabled)
        self._user_id = "u1"
        self._affect_store = _FakeAffectStore()
        self._mood_inertia_reactions: deque[str] = deque(
            ["excited"], maxlen=3,
        )
        self._pending_mood_inertia: Any = pending
        self._mood_inertia_force = force


class RenderMoodInertiaBlockTests(unittest.TestCase):
    def test_pending_cue_renders_once_and_clears(self) -> None:
        host = _RenderHost(pending="Heads-up: your face just jumped to excited")
        first = host._render_mood_inertia_block()
        second = host._render_mood_inertia_block()
        self.assertIn("excited", first)
        self.assertEqual(second, "")
        self.assertIsNone(host._pending_mood_inertia)

    def test_empty_slot_renders_nothing(self) -> None:
        host = _RenderHost(pending=None)
        self.assertEqual(host._render_mood_inertia_block(), "")

    def test_master_switch_off_suppresses_pending(self) -> None:
        host = _RenderHost(enabled=False, pending="Heads-up: cue")
        self.assertEqual(host._render_mood_inertia_block(), "")

    def test_force_flag_renders_synthetic_cue_once(self) -> None:
        host = _RenderHost(force=True)
        first = host._render_mood_inertia_block()
        self.assertIn("your face just jumped to excited", first)
        self.assertFalse(host._mood_inertia_force)
        # Consumed: the next call has neither force nor pending.
        self.assertEqual(host._render_mood_inertia_block(), "")

    def test_force_flag_prefers_latest_ring_reaction(self) -> None:
        host = _RenderHost(force=True)
        host._mood_inertia_reactions = deque(["sad", "cheerful"], maxlen=3)
        cue = host._render_mood_inertia_block()
        self.assertIn("cheerful", cue)


if __name__ == "__main__":
    unittest.main()
