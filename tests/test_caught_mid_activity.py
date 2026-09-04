"""H26 provider: being caught mid-something rather than reporting it.

Companion to ``tests/test_in_progress_beat.py`` (the state machine) and
the worker tests (who opens and closes a beat). This file covers the
prompt side: when the block fires, what it says, and the one thing it
must never do — coexist with the K36 "while you were away I finished X"
line, which would have her both still doing a thing and done with it.
"""
from __future__ import annotations

import json
import random
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.core.session.cue_pool_mixin import CuePoolMixin
from app.core.session.inner_life_part2 import InnerLifePart2Mixin
from app.core.world import in_progress_beat


class _FakeChatDb:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values: dict[str, str] = dict(values or {})

    def kv_get(self, key: str) -> str | None:
        return self.values.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.values[key] = value


class _Host(InnerLifePart2Mixin, CuePoolMixin):
    """Minimal provider host.

    ``CuePoolMixin`` without a store: every pool call short-circuits, so
    ``record_surfaced_cue`` is exercised but writes nowhere, which is
    also what a session with a failed store looks like.
    """

    def __init__(
        self,
        *,
        chat_db: _FakeChatDb | None,
        enabled: bool = True,
        gap_cue_surfaced: bool = False,
        pending_away_seconds: float | None = None,
    ) -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(away_activities_enabled=enabled),
        )
        self._chat_db = chat_db
        self._gap_cue_surfaced = gap_cue_surfaced
        self._pending_away_activities_seconds = pending_away_seconds
        self.user_display_name = "Jacob"


def _open_beat(
    *,
    activity: str = "reading on the sofa",
    summary: str = "got a few chapters in",
    started_minutes_ago: float = 20.0,
    ends_in_minutes: float = 25.0,
    interrupted: bool = False,
    used_item_id: int | None = None,
) -> in_progress_beat.InProgressBeat:
    now = datetime.now(timezone.utc)
    beat = in_progress_beat.build(
        key="reading",
        activity=activity,
        posture="curled up",
        summary=summary,
        now=now - timedelta(minutes=started_minutes_ago),
        rng=random.Random(0),
        used_item_id=used_item_id,
    )
    beat.expected_end_at = (
        now + timedelta(minutes=ends_in_minutes)
    ).isoformat(timespec="seconds")
    if interrupted:
        beat.interrupted_at = now.isoformat(timespec="seconds")
    return beat


def _db_with(beat: in_progress_beat.InProgressBeat | None) -> _FakeChatDb:
    db = _FakeChatDb()
    if beat is not None:
        in_progress_beat.save(db.kv_set, beat)
    return db


class SilencePathTests(unittest.TestCase):
    def test_master_switch_off_is_silent(self) -> None:
        host = _Host(chat_db=_db_with(_open_beat()), enabled=False)
        self.assertEqual(host._render_caught_mid_activity_block(), "")

    def test_nothing_open_is_silent(self) -> None:
        host = _Host(chat_db=_db_with(None))
        self.assertEqual(host._render_caught_mid_activity_block(), "")

    def test_no_chat_db_is_silent(self) -> None:
        host = _Host(chat_db=None)
        self.assertEqual(host._render_caught_mid_activity_block(), "")

    def test_an_elapsed_beat_is_silent(self) -> None:
        # She would have finished it by now, so the K36 report is the
        # correct cue for this return, not an interruption.
        host = _Host(
            chat_db=_db_with(
                _open_beat(started_minutes_ago=90.0, ends_in_minutes=-30.0)
            ),
        )
        self.assertEqual(host._render_caught_mid_activity_block(), "")

    def test_an_already_interrupted_beat_does_not_fire_twice(self) -> None:
        # He came back, she put it down. A second turn must not catch
        # her at the same thing again.
        host = _Host(chat_db=_db_with(_open_beat(interrupted=True)))
        self.assertEqual(host._render_caught_mid_activity_block(), "")

    def test_another_gap_cue_already_spoke(self) -> None:
        host = _Host(chat_db=_db_with(_open_beat()), gap_cue_surfaced=True)
        self.assertEqual(host._render_caught_mid_activity_block(), "")


class FiringTests(unittest.TestCase):
    def test_block_names_the_activity_in_the_present_tense(self) -> None:
        host = _Host(chat_db=_db_with(_open_beat()))
        block = host._render_caught_mid_activity_block()
        self.assertIn("in the middle of got a few chapters in", block)
        self.assertNotIn("in the middle of reading on the sofa", block)
        self.assertIn("didn't finish it", block)

    def test_empty_summary_falls_back_to_the_activity(self) -> None:
        host = _Host(chat_db=_db_with(_open_beat(summary="")))
        block = host._render_caught_mid_activity_block()
        self.assertIn("in the middle of reading on the sofa", block)

    def test_block_reports_how_far_in_she_is(self) -> None:
        host = _Host(chat_db=_db_with(_open_beat(started_minutes_ago=20.0)))
        block = host._render_caught_mid_activity_block()
        self.assertIn("20 minutes into it", block)

    def test_a_just_started_beat_omits_the_elapsed_clause(self) -> None:
        # "You're about 0 minutes into it" is noise.
        host = _Host(chat_db=_db_with(_open_beat(started_minutes_ago=0.0)))
        block = host._render_caught_mid_activity_block()
        self.assertTrue(block)
        self.assertNotIn("minutes into it", block)

    def test_firing_marks_the_beat_interrupted_for_the_worker(self) -> None:
        # This is the handoff that lets her go back to it later.
        db = _db_with(_open_beat())
        host = _Host(chat_db=db)
        host._render_caught_mid_activity_block()

        stored = json.loads(db.values[in_progress_beat.IN_PROGRESS_KEY])
        self.assertTrue(stored["interrupted_at"])

    def test_interrupt_puts_the_used_item_down(self) -> None:
        calls: list[tuple[int, int | None]] = []

        class _Store:
            def get_state(self) -> SimpleNamespace:
                return SimpleNamespace(location_id=7)

            def put_down(self, item_id: int, *, location_id=None):
                calls.append((item_id, location_id))
                item = SimpleNamespace(id=item_id, location_id=location_id)
                item.to_dict = lambda: {"id": item_id}
                return item

        host = _Host(chat_db=_db_with(_open_beat(used_item_id=4)))
        host._world_store = _Store()
        host._notify_world = lambda _patch: None
        self.assertTrue(host._render_caught_mid_activity_block())
        self.assertEqual(calls, [(4, 7)])

    def test_firing_claims_the_shared_gap_slot(self) -> None:
        # Both halves matter: the flag stops away_activities contradicting
        # this block, and the pending slot is spent so it isn't retried.
        host = _Host(
            chat_db=_db_with(_open_beat()),
            pending_away_seconds=6 * 3600.0,
        )
        self.assertTrue(host._render_caught_mid_activity_block())
        self.assertTrue(host._gap_cue_surfaced)
        self.assertIsNone(host._pending_away_activities_seconds)

    def test_it_fires_without_a_long_absence(self) -> None:
        # Unlike the other gap cues there is no minimum-gap bar: whether
        # she is mid-something does not depend on how long he was gone.
        host = _Host(chat_db=_db_with(_open_beat()), pending_away_seconds=None)
        self.assertTrue(host._render_caught_mid_activity_block())


class OrderingTests(unittest.TestCase):
    def test_it_precedes_away_activities_in_the_gap_cue_order(self) -> None:
        from app.core.proactive.cue_accounting import GAP_CUE_ORDER

        order = list(GAP_CUE_ORDER)
        self.assertLess(
            order.index("caught_mid_activity"),
            order.index("away_activities"),
        )

    def test_the_two_cues_arm_on_different_things(self) -> None:
        """H32 reversed the original reasoning here, and the data is why.

        Sharing ``away_activities``' slot was meant to stop one question
        ("what was she doing while I was gone") arming two opportunities.
        But the two are not the same opportunity: a return is common, a
        return that lands inside an open beat is rare, and the provider
        never consults the slot at all. So the shared slot did not avoid a
        double count, it counted every return as a chance this cue missed
        -- 7 armed, 0 surfaced, all 7 filed as gap-mutex losses on turns
        with very likely no beat to be caught at.
        """
        from app.core.proactive.cue_accounting import CUE_SPECS

        spec = CUE_SPECS["caught_mid_activity"]
        self.assertFalse(spec.slot_attr)
        self.assertIsNotNone(spec.armed_when)
        self.assertEqual(
            CUE_SPECS["away_activities"].slot_attr,
            "_pending_away_activities_seconds",
        )

    def test_a_declined_turn_reports_that_there_was_no_beat(self) -> None:
        # The bail that produced most of this cue's declines used to be
        # silent, so the ledger fell through to naming whichever gap cue
        # did fire as the winner. ``no_stock`` is in INELIGIBLE_REASONS:
        # a turn with nothing to be caught at is not a chance she missed.
        from app.core.proactive.cue_accounting import (
            REASON_NO_STOCK,
            take_decline_notes,
        )

        host = _Host(chat_db=_db_with(None), pending_away_seconds=7200.0)
        self.assertEqual(host._render_caught_mid_activity_block(), "")
        self.assertEqual(
            take_decline_notes(host).get("caught_mid_activity"),
            REASON_NO_STOCK,
        )

    def test_the_prompt_block_is_tiered_next_to_its_sibling(self) -> None:
        # The T0→T6 ladder protects the prompt cache; an untiered block
        # is a silent cache miss. It belongs beside away_activities:
        # same lifetime, and the two are alternatives.
        from app.core.session.prompt_assembler import _PROMPT_BLOCK_TIERS

        tier = {
            name: names
            for name, names in _PROMPT_BLOCK_TIERS.items()
            if "caught_mid_activity_block" in names
        }
        self.assertEqual(list(tier), ["T6_detectors"])
        names = list(tier["T6_detectors"])
        self.assertEqual(
            names.index("caught_mid_activity_block") + 1,
            names.index("away_activities_block"),
        )

    def test_the_persona_explains_how_to_handle_it(self) -> None:
        # The block states the situation; the persona says what to do
        # with it. A cue with no handling section reads as a stage
        # direction she narrates back.
        from pathlib import Path

        from app.core.proactive.cue_accounting import CUE_POLICIES

        section = CUE_POLICIES["caught_mid_activity"].handling_section
        text = Path(
            "data/persona/conditional_handling.txt"
        ).read_text(encoding="utf-8")
        self.assertIn(section, text)


class DebugSurfaceTests(unittest.TestCase):
    """The two public accessors the MCP tools use.

    Public rather than reached-through on purpose: see
    ``tests/test_private_reach_guard.py``.
    """

    def _host(self, beat: in_progress_beat.InProgressBeat | None) -> _Host:
        host = _Host(chat_db=_db_with(beat))
        host._memory_settings = SimpleNamespace(
            away_activities_in_progress_ratio=0.3,
        )
        return host

    def test_state_with_nothing_open(self) -> None:
        state = self._host(None).in_progress_beat_state()
        self.assertIsNone(state["beat"])
        self.assertEqual(state["in_progress_ratio"], 0.3)
        self.assertFalse(state["force_next"])

    def test_state_reports_the_open_beat(self) -> None:
        state = self._host(
            _open_beat(started_minutes_ago=10.0, ends_in_minutes=20.0)
        ).in_progress_beat_state()
        beat = state["beat"]
        assert isinstance(beat, dict)
        self.assertEqual(beat["activity"], "reading on the sofa")
        self.assertTrue(beat["open"])
        self.assertEqual(beat["minutes_in"], 10)
        self.assertEqual(beat["minutes_left"], 19)
        self.assertIsNone(beat["interrupted_at"])

    def test_state_shows_an_interrupted_beat_as_closed(self) -> None:
        beat = self._host(
            _open_beat(interrupted=True)
        ).in_progress_beat_state()["beat"]
        assert isinstance(beat, dict)
        self.assertFalse(beat["open"])
        self.assertTrue(beat["interrupted_at"])

    def test_force_without_a_worker_reports_why(self) -> None:
        out = self._host(None).force_caught_mid_activity()
        self.assertIn("error", out)


class _ContradictionHost(_Host):
    """Runs both providers in the order the assembler uses them."""

    def both(self) -> tuple[str, str]:
        caught = self._render_caught_mid_activity_block()
        away = self._render_away_activities_block()
        return caught, away


class ContradictionTests(unittest.TestCase):
    def test_away_activities_stands_down_when_she_is_caught(self) -> None:
        journal = [{
            "summary": "watered the basil",
            "at": datetime.now(timezone.utc).isoformat(),
        }]
        db = _db_with(_open_beat())
        db.kv_set("aiko.away_activities", json.dumps(journal))
        host = _ContradictionHost(
            chat_db=db, pending_away_seconds=6 * 3600.0,
        )
        host._memory_settings = SimpleNamespace(
            away_activities_min_gap_hours=4.0,
        )

        caught, away = host.both()

        self.assertTrue(caught)
        self.assertEqual(away, "", "she can't be finished and still at it")


if __name__ == "__main__":
    unittest.main()
