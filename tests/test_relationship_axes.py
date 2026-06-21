"""Tests for ``app.core.relationship.relationship_axes`` — store, decay, updater, rendering."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.core.infra.chat_database import ChatDatabase
from app.core.relationship.relationship_axes import (
    STAGE_CLOSE,
    STAGE_FAMILIAR,
    STAGE_INTIMATE,
    STAGE_NEW,
    RelationshipAxesState,
    RelationshipAxesStore,
    RelationshipAxesUpdater,
    apply_decay,
    relationship_bond,
    relationship_stage,
    render_axes_block,
    stage_rank,
    stage_register_hint,
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


class TestRelationshipStage(unittest.TestCase):
    """J4 — bond stage derived from axes + tenure with hysteresis."""

    def _state(self, **axes) -> RelationshipAxesState:
        return RelationshipAxesState(user_id="jacob", **axes)

    def test_bond_excludes_humor(self) -> None:
        # Humor maxed but depth axes zero -> bond stays ~0.
        bond = relationship_bond(self._state(humor=1.0))
        self.assertAlmostEqual(bond, 0.0, places=6)

    def test_bond_is_weighted_blend(self) -> None:
        bond = relationship_bond(
            self._state(closeness=1.0, trust=1.0, comfort=1.0)
        )
        self.assertAlmostEqual(bond, 1.0, places=6)

    def test_stage_rank_known_and_unknown(self) -> None:
        self.assertEqual(stage_rank(STAGE_NEW), 0)
        self.assertEqual(stage_rank(STAGE_INTIMATE), 3)
        self.assertEqual(stage_rank(None), 0)
        self.assertEqual(stage_rank("nonsense"), 0)

    def test_new_user_neutral_axes_is_new(self) -> None:
        stage = relationship_stage(self._state(), tenure_days=0.0)
        self.assertEqual(stage, STAGE_NEW)

    def test_cannot_be_intimate_on_day_one(self) -> None:
        # Max axes but zero tenure -> ceiling pins to familiar.
        stage = relationship_stage(
            self._state(closeness=1.0, trust=1.0, comfort=1.0),
            tenure_days=0.0,
        )
        self.assertEqual(stage, STAGE_FAMILIAR)

    def test_high_bond_long_tenure_is_intimate(self) -> None:
        stage = relationship_stage(
            self._state(closeness=0.8, trust=0.8, comfort=0.7),
            tenure_days=60.0,
        )
        self.assertEqual(stage, STAGE_INTIMATE)

    def test_mid_bond_resolves_close(self) -> None:
        stage = relationship_stage(
            self._state(closeness=0.45, trust=0.45, comfort=0.4),
            tenure_days=30.0,
        )
        self.assertEqual(stage, STAGE_CLOSE)

    def test_tenure_floor_lifts_cold_long_relationship(self) -> None:
        # Neutral axes but 20 days known -> at least familiar.
        stage = relationship_stage(self._state(), tenure_days=20.0)
        self.assertEqual(stage, STAGE_FAMILIAR)

    def test_ceiling_caps_below_one_week(self) -> None:
        # Strong bond but only 5 days -> capped at close (not intimate).
        stage = relationship_stage(
            self._state(closeness=0.9, trust=0.9, comfort=0.9),
            tenure_days=5.0,
        )
        self.assertEqual(stage, STAGE_CLOSE)

    def test_hysteresis_prevents_flap_at_boundary(self) -> None:
        # Bond sits just below the close threshold (0.35). With current
        # stage already CLOSE it stays close (sticky); with current NEW it
        # does not promote to close.
        state = self._state(closeness=0.34, trust=0.34, comfort=0.34)
        sticky = relationship_stage(
            state, tenure_days=30.0, current_stage=STAGE_CLOSE
        )
        cold = relationship_stage(
            state, tenure_days=30.0, current_stage=STAGE_NEW
        )
        self.assertEqual(sticky, STAGE_CLOSE)
        self.assertEqual(cold, STAGE_FAMILIAR)

    def test_register_hint_silent_for_shallow_stages(self) -> None:
        self.assertEqual(stage_register_hint(STAGE_NEW), "")
        self.assertEqual(stage_register_hint(STAGE_FAMILIAR), "")

    def test_register_hint_present_for_deep_stages(self) -> None:
        close = stage_register_hint(STAGE_CLOSE, user_display_name="Jacob")
        intimate = stage_register_hint(STAGE_INTIMATE, user_display_name="Jacob")
        self.assertIn("Jacob", close)
        self.assertIn("close", close.lower())
        self.assertIn("Jacob", intimate)
        # The hint never names the stage mechanically at the user.
        self.assertNotIn("level", close.lower())
        self.assertNotIn("stage", intimate.lower())


if __name__ == "__main__":
    unittest.main()
