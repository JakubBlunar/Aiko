"""Tests for the world-related slice of :class:`SessionController`.

Bypasses ``__init__`` and wires only the world store + listeners so we
can exercise:
  - ``add_world_listener`` / ``_notify_world`` fan-out
  - ``update_world_state`` / ``add_world_item`` / ``consume_world_item``
    snapshot shapes and listener triggers
  - ``give_item`` defaults (kitchenette + given_by="user")
  - ``_render_world_block`` graceful fallback when the store is missing
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from dataclasses import dataclass

from app.core.infra.chat_database import ChatDatabase
from app.core.session.session_controller import SessionController
from app.core.world.world_store import WorldStore


@dataclass
class _AssistantStub:
    user_display_name: str = "Jacob"


@dataclass
class _SettingsStub:
    assistant: _AssistantStub


def _make_controller(
    *, seed: bool = True,
) -> tuple[SessionController, Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    db_path = Path(tmp.name) / "session_world.db"
    ChatDatabase(db_path)
    store = WorldStore(db_path)
    if seed:
        store.seed_default()
    controller = SessionController.__new__(SessionController)
    controller._world_store = store
    controller._world_listeners = []
    # ``user_display_name`` (and therefore ``reseed_world`` /
    # ``_render_world_block``) reads ``self._settings.assistant``.
    controller._settings = _SettingsStub(assistant=_AssistantStub())  # type: ignore[attr-defined]
    return controller, db_path, tmp


def _cleanup(tmp: tempfile.TemporaryDirectory, controller: SessionController) -> None:
    try:
        if controller._world_store is not None:
            controller._world_store.close()
    except Exception:
        pass
    try:
        tmp.cleanup()
    except PermissionError:
        pass


class ListenerTests(unittest.TestCase):
    def test_listener_fires_on_state_update(self) -> None:
        controller, _, tmp = _make_controller()
        captured: list[dict[str, Any]] = []
        controller.add_world_listener(lambda patch: captured.append(dict(patch)))
        snap = controller.update_world_state(posture="lying")
        self.assertIsNotNone(snap)
        self.assertEqual(len(captured), 1)
        self.assertIn("state", captured[0])
        self.assertEqual(captured[0]["state"]["posture"], "lying")
        _cleanup(tmp, controller)

    def test_listener_fires_on_item_add(self) -> None:
        controller, _, tmp = _make_controller(seed=False)
        controller._world_store.seed_default()
        captured: list[dict[str, Any]] = []
        controller.add_world_listener(lambda patch: captured.append(dict(patch)))
        snap = controller.add_world_item(
            name="extra cookie", kind="food", consumable=True, quantity=1,
        )
        self.assertIsNotNone(snap)
        self.assertEqual(len(captured), 1)
        self.assertIn("item", captured[0])
        _cleanup(tmp, controller)

    def test_consume_to_zero_emits_deletion(self) -> None:
        controller, _, tmp = _make_controller(seed=False)
        controller._world_store.seed_default()
        result = controller.add_world_item(
            name="last_cookie", kind="food", consumable=True, quantity=1,
        )
        captured: list[dict[str, Any]] = []
        controller.add_world_listener(lambda patch: captured.append(dict(patch)))
        outcome = controller.consume_world_item(result["id"], amount=1)
        self.assertIsNotNone(outcome)
        self.assertIn("deleted_item_id", outcome)
        deletion_events = [p for p in captured if "deleted_item_id" in p]
        self.assertEqual(len(deletion_events), 1)
        _cleanup(tmp, controller)


class GiveItemTests(unittest.TestCase):
    def test_give_item_default_lands_in_kitchenette(self) -> None:
        controller, _, tmp = _make_controller()
        snap = controller.give_item(
            name="cookies", kind="food", quantity=2,
        )
        self.assertIsNotNone(snap)
        self.assertEqual(snap["given_by"], "user")
        kitchen = controller._world_store.get_location("kitchenette")
        # The seeded default already has a "cookie_jar" stack — the give
        # path should merge into it (since it's the same slug). Confirm
        # the resulting row is in the kitchenette regardless.
        self.assertEqual(snap["location_id"], kitchen.id)
        _cleanup(tmp, controller)

    def test_give_item_with_explicit_location(self) -> None:
        controller, _, tmp = _make_controller()
        bed = controller._world_store.get_location("bed")
        self.assertIsNotNone(bed)
        snap = controller.give_item(
            name="teddy",
            kind="toy",
            location_slug="bed",
        )
        self.assertEqual(snap["location_id"], bed.id)
        self.assertFalse(snap["consumable"])  # toys aren't consumable by default
        _cleanup(tmp, controller)

    def test_give_food_is_consumable_by_default(self) -> None:
        controller, _, tmp = _make_controller()
        snap = controller.give_item(name="apple", kind="food")
        self.assertTrue(snap["consumable"])
        _cleanup(tmp, controller)

    def test_give_with_unknown_location_falls_back_to_first(self) -> None:
        controller, _, tmp = _make_controller()
        snap = controller.give_item(
            name="wandering gift", kind="other", location_slug="dungeon",
        )
        self.assertIsNotNone(snap)
        # Should have landed in *some* real location.
        self.assertIsNotNone(snap["location_id"])
        _cleanup(tmp, controller)


class RenderBlockTests(unittest.TestCase):
    def test_renders_when_store_present(self) -> None:
        controller, _, tmp = _make_controller()
        block = controller._render_world_block()
        self.assertNotEqual(block, "")
        self.assertIn("desk", block.lower())
        _cleanup(tmp, controller)

    def test_returns_empty_when_store_missing(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._world_store = None
        self.assertEqual(controller._render_world_block(), "")

    def test_world_snapshot_disabled_when_store_missing(self) -> None:
        controller = SessionController.__new__(SessionController)
        controller._world_store = None
        snap = controller.world_snapshot()
        self.assertFalse(snap["enabled"])
        self.assertEqual(snap["locations"], [])
        self.assertEqual(snap["items"], [])
        self.assertEqual(snap["scenes"], [])


class GiftSignalTests(unittest.TestCase):
    """add_world_item must arm the gift signal + watermark for user gifts.

    The UI's "give" surface (POST /api/world/items) calls add_world_item
    directly, bypassing give_item — so the signal has to live here.
    """

    def _make_with_db(self):
        tmp = tempfile.TemporaryDirectory()
        db_path = Path(tmp.name) / "gift.db"
        db = ChatDatabase(db_path)
        store = WorldStore(db_path)
        store.seed_default()
        controller = SessionController.__new__(SessionController)
        controller._world_store = store
        controller._world_listeners = []
        controller._chat_db = db
        controller._last_turn_gift_received = False
        controller._settings = _SettingsStub(assistant=_AssistantStub())  # type: ignore[attr-defined]
        return controller, db, tmp

    def test_user_gift_sets_signal_and_watermark(self) -> None:
        import json as _json

        from app.core.session.world_mixin import WORLD_LAST_USER_GIFT_KEY

        controller, db, tmp = self._make_with_db()
        snap = controller.add_world_item(
            name="green tea", kind="food", consumable=True, given_by="user",
        )
        self.assertIsNotNone(snap)
        self.assertTrue(controller._last_turn_gift_received)
        raw = db.kv_get(WORLD_LAST_USER_GIFT_KEY)
        self.assertIsNotNone(raw)
        blob = _json.loads(raw)
        self.assertEqual(blob["name"], "green tea")
        self.assertTrue(blob.get("at"))
        _cleanup(tmp, controller)

    def test_non_user_item_does_not_arm_signal(self) -> None:
        from app.core.session.world_mixin import WORLD_LAST_USER_GIFT_KEY

        controller, db, tmp = self._make_with_db()
        controller.add_world_item(name="loose rock", kind="other")
        self.assertFalse(controller._last_turn_gift_received)
        self.assertIsNone(db.kv_get(WORLD_LAST_USER_GIFT_KEY))
        _cleanup(tmp, controller)

    def test_new_gift_render_is_one_shot_strong_cue(self) -> None:
        controller, _db, tmp = self._make_with_db()
        controller.add_world_item(
            name="paper crane", kind="other", given_by="user",
        )
        strong = controller._world_store.render_block(new_gift=True)
        self.assertIn("just set", strong.lower())
        self.assertIn("first time", strong.lower())
        calm = controller._world_store.render_block(new_gift=False)
        self.assertIn("gave you", calm.lower())
        self.assertIn("never force a room mention", calm.lower())
        _cleanup(tmp, controller)


class TogetherSummaryTests(unittest.TestCase):
    """``get_together_summary`` against a real tracker, not a stand-in.

    The route test (``test_web_server_together``) replaces the whole method
    with a fake, so nothing exercised the real one -- which is how it went
    unnoticed that it reached for ``tracker.store`` (a private ``_store``
    on :class:`RelationshipTracker`). The ``AttributeError`` was swallowed
    by a bare ``except``, so the tab silently reported a brand-new
    relationship: phase "new", zero turns, zero days, and every milestone
    unmarked no matter how long the two had been talking.
    """

    @staticmethod
    def _drop(db, tmp: tempfile.TemporaryDirectory) -> None:
        try:
            db.close()
        except Exception:
            pass
        try:
            tmp.cleanup()
        except PermissionError:
            # Windows keeps the SQLite handle a moment longer; the temp dir
            # is the OS's problem, not the test's.
            pass

    def _make(
        self,
        *,
        turns: int,
        days_ago: float,
        last_milestone: tuple[str, str] | None = None,
    ):
        from datetime import timedelta

        from app.core.infra import timephrase
        from app.core.relationship.relationship import (
            RelationshipStore,
            RelationshipTracker,
        )

        tmp = tempfile.TemporaryDirectory()
        db = ChatDatabase(Path(tmp.name) / "together.db")
        store = RelationshipStore(db)
        store.get_or_create("default")
        first_seen = (
            timephrase.utcnow() - timedelta(days=days_ago)
        ).isoformat(timespec="seconds")
        db._get_conn().execute(
            "UPDATE user_relationship SET first_seen_at = ?, total_turns = ? "
            "WHERE user_id = ?",
            (first_seen, int(turns), "default"),
        )
        if last_milestone is not None:
            label, at = last_milestone
            db._get_conn().execute(
                "UPDATE user_relationship SET milestone_label = ?, "
                "last_milestone_at = ? WHERE user_id = ?",
                (label, at, "default"),
            )
        controller = SessionController.__new__(SessionController)
        controller._relationship_tracker = RelationshipTracker(store)
        controller._user_id = "default"
        controller._chat_db = db
        controller._world_store = None
        controller._shared_moments_store = None
        controller._settings = _SettingsStub(assistant=_AssistantStub())  # type: ignore[attr-defined]
        return controller, db, tmp

    def test_crossed_milestones_are_marked(self) -> None:
        controller, db, tmp = self._make(turns=1726, days_ago=74.0)
        summary = controller.get_together_summary()
        crossed = {
            m["label"] for m in summary["milestones"] if m["crossed"]
        }
        self.assertEqual(
            crossed,
            {
                "first_hundred_turns",
                "first_week_together",
                "first_month_together",
            },
        )
        # Not yet reached, and not claimed.
        uncrossed = {
            m["label"] for m in summary["milestones"] if not m["crossed"]
        }
        self.assertEqual(
            uncrossed,
            {
                "hundred_days_together",
                "six_months_together",
                "first_year_together",
            },
        )
        self._drop(db, tmp)

    def test_the_counters_reach_the_tab(self) -> None:
        # The same read feeds the header, so a broken one zeroes far more
        # than the milestone list.
        controller, db, tmp = self._make(turns=1726, days_ago=74.0)
        summary = controller.get_together_summary()
        self.assertEqual(summary["total_turns"], 1726)
        self.assertEqual(summary["days_known"], 74)
        self.assertNotEqual(summary["phase"], "new")
        self.assertTrue(summary["first_seen_at"])
        self._drop(db, tmp)

    def test_a_day_based_milestone_carries_its_date(self) -> None:
        controller, db, tmp = self._make(turns=1726, days_ago=74.0)
        by_label = {
            m["label"]: m for m in controller.get_together_summary()["milestones"]
        }
        self.assertTrue(by_label["first_week_together"]["crossed_at"])
        self._drop(db, tmp)

    def _seed_user_turns(
        self, db, *, count: int, start_iso: str, spacing_minutes: int = 1
    ) -> list[str]:
        """Insert ``count`` user messages, one every ``spacing_minutes``."""
        from datetime import datetime, timedelta

        start = datetime.fromisoformat(start_iso)
        conn = db._get_conn()
        stamps: list[str] = []
        for i in range(count):
            ts = (
                start + timedelta(minutes=i * spacing_minutes)
            ).isoformat(timespec="seconds")
            stamps.append(ts)
            conn.execute(
                "INSERT INTO messages (session_id, role, content, "
                "token_count, created_at) VALUES (?, 'user', 'x', 0, ?)",
                ("seed", ts),
            )
        conn.commit()
        return stamps

    def _first_seen(self, db) -> str:
        row = db.execute_fetchone(
            "SELECT first_seen_at FROM user_relationship WHERE user_id = ?",
            ("default",),
        )
        return str(row[0])


    # The turn-based milestone's date comes from the message log.
    # ``last_milestone_at`` records when a milestone was last *written*, not
    # when its threshold was crossed. Reading it as a crossing date dated
    # "first hundred turns" to the v25 backfill -- six weeks after the real
    # hundredth turn, and rendered *after* "first month together" in a list
    # where it belonged three days in.

    def test_the_date_is_the_hundredth_turn(self) -> None:
        controller, db, tmp = self._make(turns=1726, days_ago=74.0)
        stamps = self._seed_user_turns(
            db, count=150, start_iso=self._first_seen(db)
        )
        by_label = {
            m["label"]: m for m in controller.get_together_summary()["milestones"]
        }
        self.assertEqual(
            by_label["first_hundred_turns"]["crossed_at"], stamps[99]
        )
        self._drop(db, tmp)

    def test_the_write_stamp_does_not_shadow_the_real_crossing(self) -> None:
        # The exact shape of the bug: the stored stamp is weeks later than
        # the hundredth turn, and it used to win.
        from datetime import timedelta

        from app.core.infra import timephrase

        late = (timephrase.utcnow() - timedelta(days=29)).isoformat(
            timespec="seconds"
        )
        controller, db, tmp = self._make(
            turns=1726,
            days_ago=74.0,
            last_milestone=("first_hundred_turns", late),
        )
        stamps = self._seed_user_turns(
            db, count=150, start_iso=self._first_seen(db)
        )
        by_label = {
            m["label"]: m for m in controller.get_together_summary()["milestones"]
        }
        crossed_at = by_label["first_hundred_turns"]["crossed_at"]
        self.assertEqual(crossed_at, stamps[99])
        self.assertNotEqual(crossed_at, late)
        self._drop(db, tmp)

    def test_a_hundred_turns_may_predate_a_week_together(self) -> None:
        # Chatting 100 times in the first days is normal, so the badge is
        # allowed to sort before "first week" -- the old stamp made that
        # ordering impossible and the list read as inconsistent.
        controller, db, tmp = self._make(turns=1726, days_ago=74.0)
        self._seed_user_turns(
            db, count=150, start_iso=self._first_seen(db), spacing_minutes=30
        )
        by_label = {
            m["label"]: m for m in controller.get_together_summary()["milestones"]
        }
        self.assertLess(
            by_label["first_hundred_turns"]["crossed_at"],
            by_label["first_week_together"]["crossed_at"],
        )
        self._drop(db, tmp)

    def test_it_falls_back_to_the_write_stamp_without_a_log(self) -> None:
        # Pruned history: no derivable date, so the stored stamp is still
        # better than a badge with no date at all.
        from datetime import timedelta

        from app.core.infra import timephrase

        stamp = (timephrase.utcnow() - timedelta(days=40)).isoformat(
            timespec="seconds"
        )
        controller, db, tmp = self._make(
            turns=1726,
            days_ago=74.0,
            last_milestone=("first_hundred_turns", stamp),
        )
        by_label = {
            m["label"]: m for m in controller.get_together_summary()["milestones"]
        }
        self.assertEqual(by_label["first_hundred_turns"]["crossed_at"], stamp)
        self._drop(db, tmp)

    def test_a_short_log_leaves_the_date_empty_rather_than_wrong(self) -> None:
        controller, db, tmp = self._make(turns=1726, days_ago=74.0)
        self._seed_user_turns(db, count=12, start_iso=self._first_seen(db))
        by_label = {
            m["label"]: m for m in controller.get_together_summary()["milestones"]
        }
        self.assertIsNone(by_label["first_hundred_turns"]["crossed_at"])
        # The day-based neighbours are unaffected.
        self.assertTrue(by_label["first_month_together"]["crossed_at"])
        self._drop(db, tmp)

    def test_turns_before_the_relationship_row_do_not_shift_the_count(self) -> None:
        # Messages predating first_seen_at (the row was added later than the
        # log) must not make the hundredth turn look earlier than the
        # counter that fired the milestone.
        from datetime import datetime, timedelta

        controller, db, tmp = self._make(turns=1726, days_ago=74.0)
        first_seen = self._first_seen(db)
        early = (
            datetime.fromisoformat(first_seen) - timedelta(days=3)
        ).isoformat(timespec="seconds")
        self._seed_user_turns(db, count=40, start_iso=early)
        stamps = self._seed_user_turns(db, count=150, start_iso=first_seen)
        by_label = {
            m["label"]: m for m in controller.get_together_summary()["milestones"]
        }
        self.assertEqual(
            by_label["first_hundred_turns"]["crossed_at"], stamps[99]
        )
        self._drop(db, tmp)

    def test_a_fresh_relationship_claims_nothing(self) -> None:
        controller, db, tmp = self._make(turns=3, days_ago=0.0)
        summary = controller.get_together_summary()
        self.assertFalse(any(m["crossed"] for m in summary["milestones"]))
        self._drop(db, tmp)


class ResetTests(unittest.TestCase):
    def test_reseed_world_emits_snapshot(self) -> None:
        controller, _, tmp = _make_controller()
        # Add a custom item the reseed should wipe.
        controller.add_world_item(name="extra rock", kind="other")
        captured: list[dict[str, Any]] = []
        controller.add_world_listener(lambda patch: captured.append(dict(patch)))
        result = controller.reseed_world(force=True)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(len(captured), 1)
        names = {i["name"] for i in result["items"]}
        self.assertNotIn("extra rock", names)
        _cleanup(tmp, controller)


if __name__ == "__main__":
    unittest.main()
