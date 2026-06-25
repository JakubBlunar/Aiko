"""J6 — conflict-repair memory: pure helpers + post-turn tracker."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.core.relationship import conflict_repair as cr
from app.core.session.post_turn_mixin import PostTurnMixin
# ``_KV_CONFLICT_REPAIR_AT`` + ``_maybe_track_conflict_repair`` live on the
# helpers mixin (the file-size split); ``PostTurnMixin`` inherits the
# method via ``PostTurnMixin(PostTurnHelpersMixin)``. Import the constant
# from the module that actually defines it.
from app.core.session.post_turn_helpers_mixin import _KV_CONFLICT_REPAIR_AT


class CleanTopicTests(unittest.TestCase):
    def test_collapses_whitespace(self) -> None:
        self.assertEqual(cr.clean_topic("  hello   world\n\t!"), "hello world !")

    def test_clips_long(self) -> None:
        out = cr.clean_topic("x" * 200, max_len=10)
        self.assertTrue(out.endswith("\u2026"))
        self.assertLessEqual(len(out), 11)

    def test_empty(self) -> None:
        self.assertEqual(cr.clean_topic(None), "")


class HasRecoveredTests(unittest.TestCase):
    def _watch(self, target=0.3, floor=-0.2):
        return cr.RepairWatch(
            recovery_target=target, dip_floor=floor, topic="t", turns_left=5,
        )

    def test_back_to_baseline(self) -> None:
        self.assertTrue(cr.has_recovered(0.28, self._watch(), epsilon=0.05))

    def test_not_recovered(self) -> None:
        # Still well below baseline and only a tiny rise off the floor.
        w = self._watch(target=0.3, floor=-0.2)
        self.assertFalse(
            cr.has_recovered(-0.15, w, epsilon=0.05, min_rise=0.10)
        )

    def test_rose_from_floor(self) -> None:
        # Baseline was itself low; a solid rise off the floor qualifies.
        w = self._watch(target=-0.1, floor=-0.5)
        self.assertTrue(
            cr.has_recovered(-0.35, w, epsilon=0.05, min_rise=0.10)
        )


class SummaryTests(unittest.TestCase):
    def test_with_topic(self) -> None:
        s = cr.build_repair_summary("Jacob", "the schedule")
        self.assertIn("Jacob", s)
        self.assertIn("the schedule", s)
        self.assertIn("worked through it", s)

    def test_without_topic(self) -> None:
        s = cr.build_repair_summary("Jacob", "")
        self.assertIn("Jacob", s)
        self.assertNotIn('""', s)

    def test_name_fallback(self) -> None:
        self.assertIn("they", cr.build_repair_summary("", ""))


# ── Tracker plumbing ────────────────────────────────────────────────────

class _Store:
    def __init__(self, row=True) -> None:
        self.calls: list[dict] = []
        self._row = SimpleNamespace(id=42, vibe="repair") if row else None

    def add(self, **kw):
        self.calls.append(kw)
        return self._row


class _KvDb:
    def __init__(self, seed=None) -> None:
        self._kv = dict(seed or {})

    def kv_get(self, k):
        return self._kv.get(k)

    def kv_set(self, k, v):
        self._kv[k] = v


def _agent(**over):
    base = dict(
        conflict_repair_enabled=True,
        conflict_repair_watch_turns=3,
        conflict_repair_recovery_epsilon=0.05,
        conflict_repair_min_recovery_rise=0.10,
        conflict_repair_cooldown_hours=12.0,
    )
    base.update(over)
    return SimpleNamespace(**base)


class _Host(PostTurnMixin):
    def __init__(self, *, agent=None, store=True, kv=None, chat_db=True):
        self._settings = SimpleNamespace(agent=agent or _agent())
        self._repair_watch = None
        self._shared_moments_store = _Store(store) if store is not None else None
        self._chat_db = _KvDb(kv) if chat_db else None
        self.user_display_name = "Jacob"
        self.session_key = "user:sess"
        self.notified: list = []

    def _notify_shared_moment_added(self, row):
        self.notified.append(row)


def _rupture(prior=0.3, current=-0.1):
    return SimpleNamespace(prior_valence=prior, current_valence=current)


class TrackerTests(unittest.TestCase):
    def test_rupture_arms_watch_no_record(self) -> None:
        host = _Host()
        host._maybe_track_conflict_repair(
            rupture_result=_rupture(0.3, -0.1),
            current_valence=-0.1,
            user_text="this is wrong",
            user_message_id=1,
            assistant_message_id=2,
        )
        self.assertIsNotNone(host._repair_watch)
        self.assertEqual(host._repair_watch.recovery_target, 0.3)
        self.assertEqual(host._repair_watch.dip_floor, -0.1)
        self.assertEqual(host._repair_watch.topic, "this is wrong")
        self.assertEqual(host._shared_moments_store.calls, [])

    def test_recovery_records_and_clears(self) -> None:
        host = _Host()
        host._repair_watch = cr.RepairWatch(0.3, -0.1, "the plan", 3)
        host._maybe_track_conflict_repair(
            rupture_result=None,
            current_valence=0.3,  # back to baseline
            user_text="ok we're good",
            user_message_id=5,
            assistant_message_id=6,
        )
        self.assertIsNone(host._repair_watch)
        self.assertEqual(len(host._shared_moments_store.calls), 1)
        call = host._shared_moments_store.calls[0]
        self.assertEqual(call["vibe"], "repair")
        self.assertEqual(call["source"], "repair")
        self.assertIn("the plan", call["summary"])
        self.assertEqual(call["source_message_ids"], [5, 6])
        self.assertTrue(host._chat_db.kv_get(_KV_CONFLICT_REPAIR_AT))
        self.assertEqual(len(host.notified), 1)

    def test_no_recovery_decrements_then_drops(self) -> None:
        host = _Host()
        host._repair_watch = cr.RepairWatch(0.5, -0.3, "topic", 2)
        host._maybe_track_conflict_repair(
            rupture_result=None, current_valence=-0.25,
            user_text="still annoyed", user_message_id=1,
            assistant_message_id=2,
        )
        self.assertIsNotNone(host._repair_watch)
        self.assertEqual(host._repair_watch.turns_left, 1)
        host._maybe_track_conflict_repair(
            rupture_result=None, current_valence=-0.28,
            user_text="meh", user_message_id=3, assistant_message_id=4,
        )
        self.assertIsNone(host._repair_watch)
        self.assertEqual(host._shared_moments_store.calls, [])

    def test_new_rupture_refreshes_and_keeps_topic(self) -> None:
        host = _Host()
        host._repair_watch = cr.RepairWatch(0.4, -0.2, "old topic", 1)
        # New rupture with empty user_text -> keep prior topic, reset window.
        host._maybe_track_conflict_repair(
            rupture_result=_rupture(0.2, -0.3),
            current_valence=-0.3, user_text="   ",
            user_message_id=1, assistant_message_id=2,
        )
        self.assertEqual(host._repair_watch.topic, "old topic")
        self.assertEqual(host._repair_watch.turns_left, 3)
        self.assertEqual(host._repair_watch.dip_floor, -0.3)

    def test_disabled_clears_watch(self) -> None:
        host = _Host(agent=_agent(conflict_repair_enabled=False))
        host._repair_watch = cr.RepairWatch(0.3, -0.1, "t", 3)
        host._maybe_track_conflict_repair(
            rupture_result=None, current_valence=0.3, user_text="hi",
            user_message_id=1, assistant_message_id=2,
        )
        self.assertIsNone(host._repair_watch)
        self.assertEqual(host._shared_moments_store.calls, [])

    def test_cooldown_suppresses_record(self) -> None:
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        host = _Host(kv={_KV_CONFLICT_REPAIR_AT: recent})
        host._repair_watch = cr.RepairWatch(0.3, -0.1, "t", 3)
        host._maybe_track_conflict_repair(
            rupture_result=None, current_valence=0.3, user_text="ok",
            user_message_id=1, assistant_message_id=2,
        )
        # Watch cleared (recovery happened) but no row written (cooldown).
        self.assertIsNone(host._repair_watch)
        self.assertEqual(host._shared_moments_store.calls, [])

    def test_no_store_safe(self) -> None:
        host = _Host(store=None)
        host._repair_watch = cr.RepairWatch(0.3, -0.1, "t", 3)
        host._maybe_track_conflict_repair(
            rupture_result=None, current_valence=0.3, user_text="ok",
            user_message_id=1, assistant_message_id=2,
        )
        self.assertIsNone(host._repair_watch)


if __name__ == "__main__":
    unittest.main()
