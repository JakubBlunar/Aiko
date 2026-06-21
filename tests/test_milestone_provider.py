"""J8 — milestone-celebration provider plumbing.

Exercises ``InnerLifeProvidersMixin._render_milestone_block`` via a minimal
stub host (the same pattern as ``test_day_color_provider.py``): master
switch, one-shot consumption, stage-aware tone, and the humanised
fallback for an unknown label. The bond-stage resolution itself is
covered in ``test_relationship_axes.py``; here we stub
``relationship_stage_now`` so the test stays focused on the block.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin


class _Host(InnerLifeProvidersMixin):
    def __init__(self, *, enabled: bool = True, stage: str = "new") -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(milestone_celebration_enabled=enabled)
        )
        self._pending_milestone_celebration: str | None = None
        self.user_display_name = "Jacob"
        # Shadow the mixin method so the block test doesn't need stores.
        self.relationship_stage_now = lambda: stage  # type: ignore[assignment]


class MilestoneProviderTests(unittest.TestCase):
    def test_disabled_returns_empty(self) -> None:
        host = _Host(enabled=False)
        host._pending_milestone_celebration = "first_week_together"
        self.assertEqual(host._render_milestone_block(), "")
        # Disabled must NOT consume the slot.
        self.assertEqual(
            host._pending_milestone_celebration, "first_week_together"
        )

    def test_no_pending_returns_empty(self) -> None:
        host = _Host()
        self.assertEqual(host._render_milestone_block(), "")

    def test_renders_friendly_phrase_and_consumes(self) -> None:
        host = _Host()
        host._pending_milestone_celebration = "first_month_together"
        block = host._render_milestone_block()
        self.assertIn("a month", block.lower())
        self.assertIn("Jacob", block)
        # One-shot: slot consumed, second call empty.
        self.assertIsNone(host._pending_milestone_celebration)
        self.assertEqual(host._render_milestone_block(), "")

    def test_shallow_stage_understated_tone(self) -> None:
        host = _Host(stage="new")
        host._pending_milestone_celebration = "first_hundred_turns"
        block = host._render_milestone_block().lower()
        self.assertIn("small, genuine note", block)
        self.assertNotIn("warmth show", block)

    def test_deep_stage_warmer_tone(self) -> None:
        host = _Host(stage="close")
        host._pending_milestone_celebration = "first_year_together"
        block = host._render_milestone_block().lower()
        self.assertIn("warmth show", block)

    def test_unknown_label_humanised_fallback(self) -> None:
        host = _Host()
        host._pending_milestone_celebration = "five_years_together"
        block = host._render_milestone_block()
        self.assertIn("five years together", block)
        self.assertIsNone(host._pending_milestone_celebration)

    def test_never_performs(self) -> None:
        host = _Host()
        host._pending_milestone_celebration = "first_week_together"
        block = host._render_milestone_block().lower()
        self.assertIn("don't make a production", block)


if __name__ == "__main__":
    unittest.main()
