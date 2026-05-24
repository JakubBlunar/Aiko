"""Tests for the relationship tracker (Phase 3b)."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.chat_database import ChatDatabase
from app.core.relationship import (
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
