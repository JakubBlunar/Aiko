"""Tests for the Phase 2d address-style block."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.core.relationship.relationship import (
    RelationshipState,
    render_petname_block,
)


def _state(*, total_turns: int, age_days: float) -> RelationshipState:
    """Build a minimal RelationshipState at the requested age/turn count."""
    now = datetime.now(timezone.utc)
    first_seen = (now - timedelta(days=age_days)).isoformat()
    return RelationshipState(
        user_id="u1",
        first_seen_at=first_seen,
        total_turns=total_turns,
        total_sessions=1,
        last_milestone_at=None,
        milestone_label=None,
    )


class RenderPetnameBlockTests(unittest.TestCase):
    def test_new_phase_returns_empty(self) -> None:
        out = render_petname_block(
            _state(total_turns=2, age_days=0.1),
            now=datetime.now(timezone.utc),
        )
        self.assertEqual(out, "")

    def test_warming_up_mentions_softening(self) -> None:
        out = render_petname_block(
            _state(total_turns=20, age_days=2.0),
            now=datetime.now(timezone.utc),
        )
        self.assertNotEqual(out, "")
        self.assertIn("Address style", out)

    def test_close_phase_mentions_pet_names(self) -> None:
        out = render_petname_block(
            _state(total_turns=2000, age_days=200.0),
            now=datetime.now(timezone.utc),
        )
        self.assertNotEqual(out, "")
        self.assertIn("pet names", out.lower())


if __name__ == "__main__":
    unittest.main()
