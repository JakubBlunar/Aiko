"""Tests for the K32 user-reaction inner-life provider.

The provider is a bound method on :class:`InnerLifeProvidersMixin`.
We exercise it by binding the unbound method onto a tiny harness
object that supplies the handful of attributes it actually reads
(``_settings``, ``_pending_user_reactions``, ``user_display_name``).
This keeps the tests fast (<50ms) and free of the full
SessionController construction cost.

Covers:

  - K32 ``_render_user_reactions_block`` enabled / disabled gating,
    empty-queue silent path, queue drained after a successful
    render, mixed-kind cue shape.

(The K31 ``_render_touch_state_block`` budget cue was removed in B7 —
touch gating is gone, so there is no physical budget to surface.)
"""
from __future__ import annotations

import unittest
from collections import deque
from types import SimpleNamespace

from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin


def _make_harness(
    *,
    user_reactions_enabled: bool = True,
) -> SimpleNamespace:
    """Build an object that satisfies the provider's reads."""
    return SimpleNamespace(
        _settings=SimpleNamespace(
            agent=SimpleNamespace(
                user_reactions_enabled=user_reactions_enabled,
            ),
        ),
        _pending_user_reactions=deque(),
        user_display_name="Jacob",
    )


def _render_reactions(harness: SimpleNamespace) -> str:
    return InnerLifeProvidersMixin._render_user_reactions_block(harness)  # type: ignore[arg-type]


class UserReactionsProviderTests(unittest.TestCase):
    def test_empty_queue_returns_blank(self) -> None:
        harness = _make_harness()
        self.assertEqual(_render_reactions(harness), "")

    def test_single_reaction_renders_cue(self) -> None:
        harness = _make_harness()
        harness._pending_user_reactions.append((42, "heart"))
        block = _render_reactions(harness)
        self.assertIn("Jacob", block)
        self.assertIn("hearted", block)

    def test_queue_drained_after_render(self) -> None:
        harness = _make_harness()
        harness._pending_user_reactions.append((42, "heart"))
        _render_reactions(harness)
        self.assertEqual(len(harness._pending_user_reactions), 0)
        # Second call -- nothing to render.
        self.assertEqual(_render_reactions(harness), "")

    def test_master_switch_off_returns_blank(self) -> None:
        harness = _make_harness(user_reactions_enabled=False)
        harness._pending_user_reactions.append((42, "heart"))
        self.assertEqual(_render_reactions(harness), "")
        # Queue NOT drained when the master switch is off so a later
        # config flip can pick the cue up.
        self.assertEqual(len(harness._pending_user_reactions), 1)

    def test_mixed_kinds_get_summarised(self) -> None:
        harness = _make_harness()
        harness._pending_user_reactions.extend(
            [(1, "heart"), (2, "laugh"), (3, "hug")],
        )
        block = _render_reactions(harness)
        for kind in ("heart", "laugh", "hug"):
            self.assertIn(kind, block)


if __name__ == "__main__":
    unittest.main()
