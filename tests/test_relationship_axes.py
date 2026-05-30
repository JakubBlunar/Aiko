"""Tests for ``app.core.relationship_axes`` — store, decay, updater, rendering."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.chat_database import ChatDatabase
from app.core.relationship_axes import (
    RelationshipAxesState,
    RelationshipAxesStore,
    RelationshipAxesUpdater,
    apply_decay,
    render_axes_block,
)


class _TempStore:
    def __enter__(self) -> tuple[ChatDatabase, RelationshipAxesStore]:
        self._dir = tempfile.TemporaryDirectory()
        path = Path(self._dir.name) / "axes.db"
        self.db = ChatDatabase(path)
        return self.db, RelationshipAxesStore(self.db)

    def __exit__(self, *exc):
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            conn.close()
            self.db._local.conn = None
        try:
            self._dir.cleanup()
        except PermissionError:
            pass


class TestStore(unittest.TestCase):
    def test_get_returns_zeroed_state_for_unknown_user(self) -> None:
        with _TempStore() as (_db, store):
            state = store.get_raw("jacob")
            self.assertEqual(state.closeness, 0.0)
            self.assertEqual(state.humor, 0.0)
            self.assertEqual(state.trust, 0.0)
            self.assertEqual(state.comfort, 0.0)

    def test_save_upserts(self) -> None:
        with _TempStore() as (_db, store):
            s = RelationshipAxesState(user_id="jacob", closeness=0.4, humor=0.2)
            store.save(s)
            loaded = store.get_raw("jacob")
            self.assertAlmostEqual(loaded.closeness, 0.4, places=4)
            self.assertAlmostEqual(loaded.humor, 0.2, places=4)

            s.closeness = 0.6
            store.save(s)
            loaded2 = store.get_raw("jacob")
            self.assertAlmostEqual(loaded2.closeness, 0.6, places=4)

    def test_save_clamps_to_unit_interval(self) -> None:
        with _TempStore() as (_db, store):
            s = RelationshipAxesState(user_id="jacob", closeness=1.5, humor=-2.0)
            store.save(s)
            loaded = store.get_raw("jacob")
            self.assertEqual(loaded.closeness, 1.0)
            self.assertEqual(loaded.humor, -1.0)


class TestDecay(unittest.TestCase):
    def test_no_decay_within_min_interval(self) -> None:
        now = datetime.now(timezone.utc)
        state = RelationshipAxesState(
            user_id="jacob",
            closeness=0.5,
            updated_at=now.isoformat(),
        )
        decayed = apply_decay(state, now=now + timedelta(seconds=5))
        # Identity preserved: caller relies on this to skip the save.
        self.assertIs(decayed, state)

    def test_decay_half_after_one_half_life(self) -> None:
        old = datetime.now(timezone.utc) - timedelta(days=30)
        state = RelationshipAxesState(
            user_id="jacob",
            closeness=0.8,
            humor=-0.4,
            updated_at=old.isoformat(),
        )
        decayed = apply_decay(state, now=datetime.now(timezone.utc))
        self.assertIsNot(decayed, state)
        # ~half-life => roughly 0.5x.
        self.assertAlmostEqual(decayed.closeness, 0.4, delta=0.05)
        self.assertAlmostEqual(decayed.humor, -0.2, delta=0.05)

    def test_decay_unparseable_timestamp_returns_state(self) -> None:
        state = RelationshipAxesState(
            user_id="jacob", closeness=0.5, updated_at="not a date"
        )
        decayed = apply_decay(state, now=datetime.now(timezone.utc))
        self.assertIs(decayed, state)


class TestUpdater(unittest.TestCase):
    def test_reaction_tags_drift_axes(self) -> None:
        with _TempStore() as (_db, store):
            updater = RelationshipAxesUpdater(store)
            state = updater.apply_turn(
                "jacob",
                reaction_tags=["laugh", "tender"],
            )
            self.assertGreater(state.humor, 0.0)
            self.assertGreater(state.closeness, 0.0)
            self.assertGreater(state.comfort, 0.0)

    def test_milestone_is_biggest_jump(self) -> None:
        with _TempStore() as (_db, store):
            updater = RelationshipAxesUpdater(store)
            state = updater.apply_turn("jacob", milestone="first_week")
            self.assertAlmostEqual(state.closeness, 0.08, delta=1e-6)
            self.assertAlmostEqual(state.trust, 0.04, delta=1e-6)

    def test_gift_and_promise_kept_drift_separately(self) -> None:
        with _TempStore() as (_db, store):
            updater = RelationshipAxesUpdater(store)
            s1 = updater.apply_turn("jacob", gift_received=True)
            self.assertGreater(s1.closeness, 0.0)
            self.assertGreater(s1.comfort, 0.0)

            s2 = updater.apply_turn("jacob", promise_kept=True)
            self.assertGreater(s2.trust, s1.trust)

    def test_no_signal_returns_state_unchanged(self) -> None:
        with _TempStore() as (_db, store):
            updater = RelationshipAxesUpdater(store)
            s1 = updater.apply_turn("jacob")
            self.assertEqual(s1.closeness, 0.0)
            self.assertEqual(s1.humor, 0.0)
            self.assertEqual(s1.trust, 0.0)
            self.assertEqual(s1.comfort, 0.0)

    def test_user_text_warm_term_nudges_closeness(self) -> None:
        with _TempStore() as (_db, store):
            updater = RelationshipAxesUpdater(store)
            s = updater.apply_turn("jacob", user_text="thank you for listening")
            self.assertGreater(s.closeness, 0.0)

    def test_positive_engagement_delta_nudges_closeness(self) -> None:
        with _TempStore() as (_db, store):
            updater = RelationshipAxesUpdater(store)
            s = updater.apply_turn(
                "jacob", engagement_delta=0.03,
            )
            self.assertGreater(s.closeness, 0.0)
            # Sub-axis cap; ≤ 0.04 (the tracker's own clamp).
            self.assertLessEqual(s.closeness, 0.04 + 1e-9)

    def test_negative_engagement_delta_nudges_closeness_down(
        self,
    ) -> None:
        with _TempStore() as (_db, store):
            updater = RelationshipAxesUpdater(store)
            s = updater.apply_turn(
                "jacob", engagement_delta=-0.03,
            )
            self.assertLess(s.closeness, 0.0)

    def test_engagement_delta_capped_by_max_delta(self) -> None:
        with _TempStore() as (_db, store):
            updater = RelationshipAxesUpdater(store)
            # Combining a milestone + a max engagement_delta should
            # still respect the global per-axis _MAX_DELTA = 0.08 cap.
            s = updater.apply_turn(
                "jacob",
                milestone="first_week",   # +0.08 closeness
                engagement_delta=0.04,    # tracker's own cap
            )
            self.assertLessEqual(s.closeness, 0.08 + 1e-6)

    def test_per_turn_delta_is_capped(self) -> None:
        with _TempStore() as (_db, store):
            updater = RelationshipAxesUpdater(store)
            # Several tags would push closeness past _MAX_DELTA without clamp.
            state = updater.apply_turn(
                "jacob",
                reaction_tags=["love", "love", "love", "love", "love"],
                moment_vibes=["tender", "milestone", "vulnerable"],
            )
            # Single-turn cap is 0.08 per axis.
            self.assertLessEqual(state.closeness, 0.08 + 1e-6)

    def test_clamp_to_unit_interval(self) -> None:
        with _TempStore() as (_db, store):
            existing = RelationshipAxesState(
                user_id="jacob",
                closeness=0.98,
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            store.save(existing)
            updater = RelationshipAxesUpdater(store)
            state = updater.apply_turn("jacob", moment_vibes=["milestone"])
            self.assertLessEqual(state.closeness, 1.0)


class TestRendering(unittest.TestCase):
    def test_below_threshold_renders_empty(self) -> None:
        state = RelationshipAxesState(user_id="jacob", closeness=0.3, humor=0.2)
        self.assertEqual(render_axes_block(state), "")

    def test_high_axis_renders_phrase(self) -> None:
        state = RelationshipAxesState(user_id="jacob", closeness=0.6)
        block = render_axes_block(state)
        self.assertTrue(block)
        self.assertIn("close", block.lower())

    def test_negative_axis_uses_negative_phrase(self) -> None:
        state = RelationshipAxesState(user_id="jacob", comfort=-0.7)
        block = render_axes_block(state)
        self.assertTrue(block)
        self.assertTrue(
            "uneasy" in block.lower() or "on-edge" in block.lower()
        )

    def test_only_top_two_rendered(self) -> None:
        state = RelationshipAxesState(
            user_id="jacob",
            closeness=0.9,
            humor=0.8,
            trust=0.6,
            comfort=0.55,
        )
        block = render_axes_block(state)
        self.assertTrue(block)
        # comfort/trust shouldn't both appear when closeness+humor dominate.
        self.assertEqual(block.count("—"), 1)


if __name__ == "__main__":
    unittest.main()
