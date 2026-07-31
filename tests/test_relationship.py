"""Tests for the relationship tracker (Phase 3b)."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.infra.chat_database import ChatDatabase
from app.core.relationship.relationship import (
    RelationshipState,
    RelationshipStore,
    RelationshipTracker,
    phase_for,
    render_ambient,
)


def _state(
    *,
    turns: int = 0,
    sessions: int = 0,
    first_seen_days_ago: int = 0,
    milestone: str | None = None,
) -> RelationshipState:
    first = datetime.now(timezone.utc) - timedelta(days=first_seen_days_ago)
    return RelationshipState(
        user_id="u",
        first_seen_at=first.isoformat(timespec="seconds"),
        total_turns=turns,
        total_sessions=sessions,
        last_milestone_at=None,
        milestone_label=milestone,
    )


class _Fixture:
    def __init__(self):
        self.tmp = TemporaryDirectory()
        self.db = ChatDatabase(Path(self.tmp.name) / "chat.db")
        self.store = RelationshipStore(self.db)
        self.tracker = RelationshipTracker(self.store)

    def close(self):
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self.db._local.conn = None
        try:
            self.tmp.cleanup()
        except PermissionError:
            pass


class PhaseForTests(unittest.TestCase):
    def test_new_user_is_new(self):
        s = _state(turns=0)
        self.assertEqual(phase_for(s, now=datetime.now(timezone.utc)), "new")

    def test_warming_up_after_5_turns(self):
        s = _state(turns=6)
        self.assertEqual(phase_for(s, now=datetime.now(timezone.utc)), "warming_up")

    def test_familiar_after_50_turns_and_week(self):
        s = _state(turns=60, first_seen_days_ago=8)
        self.assertEqual(phase_for(s, now=datetime.now(timezone.utc)), "familiar")

    def test_familiar_requires_age_too(self):
        s = _state(turns=60, first_seen_days_ago=2)
        # Even with 60 turns, only 2 days in -> still warming_up.
        self.assertEqual(phase_for(s, now=datetime.now(timezone.utc)), "warming_up")

    def test_close_phase(self):
        s = _state(turns=600, first_seen_days_ago=70)
        self.assertEqual(phase_for(s, now=datetime.now(timezone.utc)), "close")


class RenderAmbientTests(unittest.TestCase):
    def test_new_phase_message(self):
        s = _state(turns=0)
        line = render_ambient(s, now=datetime.now(timezone.utc))
        self.assertIn("just met", line)

    def test_age_suffix_when_old_enough(self):
        s = _state(turns=10, first_seen_days_ago=4)
        line = render_ambient(s, now=datetime.now(timezone.utc))
        self.assertIn("4 days", line)
        self.assertIn("10 turns", line)

    def test_milestone_overrides_age_suffix(self):
        s = _state(turns=120, first_seen_days_ago=15, milestone="first_hundred_turns")
        line = render_ambient(s, now=datetime.now(timezone.utc))
        self.assertIn("first hundred turns", line)

    def test_stale_milestone_falls_back_to_age_line(self):
        # A milestone crossed long ago must not pin the ambient suffix to
        # "Recent milestone: …" forever (it read as freshly-reached to the
        # LLM and starved the informative age/turns line).
        now = datetime.now(timezone.utc)
        s = RelationshipState(
            user_id="u",
            first_seen_at=(now - timedelta(days=40)).isoformat(timespec="seconds"),
            total_turns=1490,
            total_sessions=30,
            last_milestone_at=(now - timedelta(days=10)).isoformat(timespec="seconds"),
            milestone_label="first_hundred_turns",
            milestones_surfaced=("first_hundred_turns",),
        )
        line = render_ambient(s, now=now)
        self.assertNotIn("Recent milestone", line)
        self.assertIn("1490 turns", line)


class RelationshipTrackerTests(unittest.TestCase):
    def test_record_turn_increments(self):
        f = _Fixture()
        try:
            for i in range(3):
                state, _ = f.tracker.record_turn("u1")
            self.assertEqual(state.total_turns, 3)
        finally:
            f.close()

    def test_record_turn_emits_first_hundred_milestone(self):
        f = _Fixture()
        try:
            milestones: list[str] = []
            for _ in range(101):
                _, m = f.tracker.record_turn("u2")
                if m:
                    milestones.append(m)
            self.assertIn("first_hundred_turns", milestones)
        finally:
            f.close()

    def test_milestones_are_not_repeated(self):
        f = _Fixture()
        try:
            for _ in range(101):
                f.tracker.record_turn("u3")
            # Cross another 50 turns; first_hundred shouldn't fire again.
            seen: list[str] = []
            for _ in range(50):
                _, m = f.tracker.record_turn("u3")
                if m:
                    seen.append(m)
            self.assertNotIn("first_hundred_turns", seen)
        finally:
            f.close()

    def test_milestone_does_not_pingpong_across_a_week(self):
        # Regression: the old single-``milestone_label`` logic re-announced
        # a crossed milestone the moment a *different* one was also crossed,
        # so an established relationship ping-ponged "first hundred turns"
        # and "first week together" on alternating turns forever.
        f = _Fixture()
        try:
            base = datetime.now(timezone.utc)
            fired: list[str] = []
            for _ in range(100):
                _, m = f.tracker.record_turn("u", now=base)
                if m:
                    fired.append(m)
            self.assertEqual(fired.count("first_hundred_turns"), 1)

            later = base + timedelta(days=8)
            fired2: list[str] = []
            for _ in range(40):
                _, m = f.tracker.record_turn("u", now=later)
                if m:
                    fired2.append(m)
            # first_hundred_turns was already surfaced -> never again.
            self.assertNotIn("first_hundred_turns", fired2)
            # first_week_together crosses now, but only once.
            self.assertEqual(fired2.count("first_week_together"), 1)
        finally:
            f.close()

    def test_backfill_suppresses_stale_milestones(self):
        # A pre-v25 row (milestones_surfaced NULL) with a long history must
        # not spray a burst of stale milestones; the first tick seeds the
        # surfaced set with everything already crossed and announces nothing.
        f = _Fixture()
        try:
            f.tracker.get("old")  # create the row
            old_seen = (
                datetime.now(timezone.utc) - timedelta(days=60)
            ).isoformat(timespec="seconds")
            f.db.execute_commit(
                "UPDATE user_relationship SET total_turns = ?, "
                "first_seen_at = ?, milestones_surfaced = NULL "
                "WHERE user_id = ?",
                (500, old_seen, "old"),
            )
            state, m = f.tracker.record_turn("old")
            self.assertIsNone(m)
            assert state.milestones_surfaced is not None
            self.assertIn("first_hundred_turns", state.milestones_surfaced)
            self.assertIn("first_week_together", state.milestones_surfaced)
            self.assertIn("first_month_together", state.milestones_surfaced)
            self.assertNotIn("hundred_days_together", state.milestones_surfaced)

            # A genuinely-new milestone still fires once after backfill.
            later = datetime.now(timezone.utc) + timedelta(days=110)
            fired: list[str] = []
            for _ in range(3):
                _, m2 = f.tracker.record_turn("old", now=later)
                if m2:
                    fired.append(m2)
            self.assertEqual(fired.count("hundred_days_together"), 1)
        finally:
            f.close()

    def test_surfaced_set_persists(self):
        f = _Fixture()
        try:
            now = datetime.now(timezone.utc)
            for _ in range(100):
                f.tracker.record_turn("p", now=now)
            reloaded = f.store.get("p")
            assert reloaded is not None and reloaded.milestones_surfaced is not None
            self.assertIn("first_hundred_turns", reloaded.milestones_surfaced)
        finally:
            f.close()

    def test_session_counter(self):
        f = _Fixture()
        try:
            f.tracker.register_session_start("u4")
            f.tracker.register_session_start("u4")
            state = f.tracker.get("u4")
            self.assertEqual(state.total_sessions, 2)
        finally:
            f.close()

    def test_get_or_create_initializes(self):
        f = _Fixture()
        try:
            state = f.tracker.get("never_seen_before")
            self.assertEqual(state.total_turns, 0)
            self.assertEqual(state.total_sessions, 0)
        finally:
            f.close()

    def test_phase_changes_after_enough_turns(self):
        f = _Fixture()
        try:
            for _ in range(6):
                f.tracker.record_turn("u5")
            self.assertEqual(f.tracker.current_phase("u5"), "warming_up")
        finally:
            f.close()


if __name__ == "__main__":
    unittest.main()
