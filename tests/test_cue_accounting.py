"""G4 -- cue outcome accounting.

The load-bearing properties, in the order they can silently break:

1. **Arming is read before the providers run.** Several T6 providers
   *consume* the state arming is detected from -- ``turning_over`` clears
   its pending slot, the journal-backed cues advance their watermark. A
   snapshot taken after assembly would report almost nothing as armed, and
   the reach ratio would look perfect exactly when the machinery was
   busiest. Every count here would still look plausible.
2. **``block_chars`` comes from *this* turn.** ``_last_system_prompt`` is
   stamped after post-turn runs, so reading block sizes from there would
   measure the previous assembly.
3. **Cue names match the registered block names.** The whole
   surfaced-detection mechanism is a dict lookup by name; a renamed block
   would silently report the cue as declined forever.
4. **Declines stay out of ``surfacing_outcomes``.** Every aggregate over
   that table means "of the times this reached the prompt".
5. **Surfaced cues settle.** They ride the L37 carry rather than a second
   insert, because the ledger drops its carry pointer on a turn that
   surfaced nothing -- a cue on a concept-less turn would never settle.
"""
from __future__ import annotations

import json
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase, _SCHEMA_VERSION
from app.core.memory.cue_decision_store import CueDecisionStore
from app.core.memory.surfacing_outcome_store import (
    ITEM_KIND_CUE,
    ITEM_KIND_MEMORY,
    SurfacedItem,
    SurfacingOutcomeStore,
)
from app.core.proactive.cue_accounting import (
    COARSE_ARMING,
    CUE_SPECS,
    GAP_CUE_ORDER,
    OUTCOME_DECLINED,
    OUTCOME_SURFACED,
    REASON_LOST_PRIORITY,
    REASON_PROVIDER,
    REASON_QUESTION_BALANCE,
    armed_cues,
    decisions_from_block_chars,
)
from app.core.session.inner_life_part1 import InnerLifePart1Mixin
from app.core.session.post_turn_helpers_mixin import PostTurnHelpersMixin


class _Fixture:
    def __init__(self) -> None:
        self.tmp = TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "chat.db"
        self.db = ChatDatabase(self.db_path)
        self.store = CueDecisionStore(self.db)
        self.ledger = SurfacingOutcomeStore(self.db)

    def close(self) -> None:
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

    def backdate(self, cue: str, days: int) -> None:
        stamp = (timephrase.utcnow() - timedelta(days=days)).isoformat()
        conn = self.db._get_conn()
        conn.execute(
            "UPDATE cue_decisions SET created_at = ? WHERE cue = ?",
            (stamp, str(cue)),
        )
        conn.commit()


# ── 1. the registry lines up with the prompt assembler ────────────────


class RegistryTests(unittest.TestCase):
    """Surfaced-detection is a name lookup, so a rename must fail loudly.

    Without this, renaming ``turning_over_block`` would leave the cue
    permanently reported as declined -- a plausible-looking number rather
    than an error, which is the worst failure mode a diagnostic can have.
    """

    def test_every_cue_maps_to_a_registered_block(self) -> None:
        from app.core.session.prompt_assembler import _BLOCK_TIER_OF

        missing = sorted(
            name for name in CUE_SPECS
            if name not in _BLOCK_TIER_OF
            and f"{name}_block" not in _BLOCK_TIER_OF
        )
        self.assertEqual(
            missing, [],
            "these cue names resolve to no registered prompt block "
            f"(renamed?): {missing}",
        )

    def test_gap_cues_agree_with_the_registry(self) -> None:
        flagged = sorted(n for n, s in CUE_SPECS.items() if s.gap_cue)
        self.assertEqual(flagged, sorted(GAP_CUE_ORDER))

    def test_every_spec_has_an_arming_signal(self) -> None:
        # A spec with neither a slot nor a journal can never be armed, so
        # it would sit in ``never_armed`` forever looking like a broken
        # worker rather than a broken registry entry.
        for name, spec in CUE_SPECS.items():
            with self.subTest(cue=name):
                self.assertTrue(
                    spec.slot_attr or spec.journal_key,
                    f"{name} has no way to be detected as armed",
                )

    def test_coarse_arming_is_exactly_the_watermarkless_journals(self) -> None:
        self.assertEqual(
            COARSE_ARMING,
            frozenset(
                n for n, s in CUE_SPECS.items()
                if s.journal_key and not s.watermark_key
            ),
        )


# ── 2. arming detection ───────────────────────────────────────────────


class _FakeDb:
    def __init__(self, kv: dict[str, str] | None = None) -> None:
        self.kv = dict(kv or {})

    def kv_get(self, key: str):
        return self.kv.get(key)


def _session(kv=None, **slots):
    host = SimpleNamespace(_chat_db=_FakeDb(kv))
    for spec in CUE_SPECS.values():
        if spec.slot_attr:
            setattr(host, spec.slot_attr, None)
    for attr, value in slots.items():
        setattr(host, attr, value)
    return host


def _journal(*stamps: str) -> str:
    return json.dumps([{"at": s} for s in stamps])


class ArmingTests(unittest.TestCase):
    def test_slot_only_cue_arms_on_a_pending_gap(self) -> None:
        host = _session(_pending_turning_over_seconds=7200.0)
        self.assertIn("turning_over", armed_cues(host))

    def test_slot_only_cue_is_not_armed_without_a_gap(self) -> None:
        self.assertNotIn("turning_over", armed_cues(_session()))

    def test_zero_second_gap_still_counts_as_armed(self) -> None:
        # The slot is checked for ``is None``, not truthiness: a 0.0 gap is
        # an armed slot the provider will still evaluate, and treating it
        # as unarmed would drop a real decision.
        host = _session(_pending_turning_over_seconds=0.0)
        self.assertIn("turning_over", armed_cues(host))

    def test_journal_cue_arms_when_newest_entry_is_past_the_watermark(self) -> None:
        host = _session({
            "aiko.follow_up_cues": _journal("2026-01-01T00:00:00"),
            "follow_up.last_surfaced_at": "2025-12-01T00:00:00",
        })
        self.assertIn("follow_up", armed_cues(host))

    def test_journal_cue_is_not_armed_once_surfaced(self) -> None:
        host = _session({
            "aiko.follow_up_cues": _journal("2026-01-01T00:00:00"),
            "follow_up.last_surfaced_at": "2026-01-01T00:00:00",
        })
        self.assertNotIn("follow_up", armed_cues(host))

    def test_only_the_newest_entry_counts(self) -> None:
        # The providers read ``ring[-1]``, so arming must too: an older
        # unsurfaced entry is not something any provider will render, and
        # counting it would inflate the denominator permanently.
        host = _session({
            "aiko.follow_up_cues": _journal("2025-01-01", "2026-01-01"),
            "follow_up.last_surfaced_at": "2026-01-01",
        })
        self.assertNotIn("follow_up", armed_cues(host))

    def test_empty_and_malformed_journals_read_as_unarmed(self) -> None:
        for raw in ("", "[]", "not json", "{}", '[{"no_at": 1}]', "[[]]"):
            with self.subTest(raw=raw):
                host = _session({"aiko.follow_up_cues": raw})
                self.assertNotIn("follow_up", armed_cues(host))

    def test_watermarkless_journal_arms_on_any_entry(self) -> None:
        host = _session({"aiko.interest_drifts": _journal("2026-01-01")})
        self.assertIn("interest_drift", armed_cues(host))

    def test_hybrid_cue_needs_both_slot_and_journal(self) -> None:
        # ``away_activities`` needs a gap AND journalled content, matching
        # what its provider requires -- arming on either alone would
        # count turns the provider could never have fired on.
        kv = {
            "aiko.away_activities": _journal("2026-01-01"),
            "away_activity.last_surfaced_at": "2025-01-01",
        }
        self.assertNotIn("away_activities", armed_cues(_session(kv)))
        self.assertNotIn(
            "away_activities",
            armed_cues(_session(None, _pending_away_activities_seconds=60.0)),
        )
        self.assertIn(
            "away_activities",
            armed_cues(_session(kv, _pending_away_activities_seconds=60.0)),
        )

    def test_a_raising_database_reads_as_unarmed(self) -> None:
        class _Boom:
            def kv_get(self, key):
                raise RuntimeError("nope")

        host = _session()
        host._chat_db = _Boom()
        self.assertEqual(armed_cues(host), set())

    def test_missing_database_reads_as_unarmed(self) -> None:
        host = _session()
        host._chat_db = None
        self.assertEqual(armed_cues(host), set())


# ── 3. outcome attribution from block_chars ───────────────────────────


class AttributionTests(unittest.TestCase):
    def test_non_empty_block_counts_as_surfaced(self) -> None:
        d = decisions_from_block_chars(
            {"turning_over"}, {"turning_over_block": 120},
        )
        self.assertEqual(d.surfaced, {"turning_over"})
        self.assertEqual(d.rows(), [("turning_over", OUTCOME_SURFACED, "")])

    def test_zero_length_block_counts_as_declined(self) -> None:
        # P31a records 0 rather than omitting empty blocks, which is what
        # makes "rendered but empty" distinguishable from "never ran".
        d = decisions_from_block_chars(
            {"turning_over"}, {"turning_over_block": 0},
        )
        self.assertEqual(d.surfaced, set())
        self.assertEqual(
            d.rows(), [("turning_over", OUTCOME_DECLINED, REASON_PROVIDER)],
        )

    def test_gap_cue_loser_is_attributed_to_the_winner(self) -> None:
        d = decisions_from_block_chars(
            {"turning_over", "forward_curiosity"},
            {"turning_over_block": 90, "forward_curiosity_block": 0},
        )
        self.assertEqual(
            d.declined["forward_curiosity"],
            f"{REASON_LOST_PRIORITY}:turning_over",
        )

    def test_priority_order_decides_the_named_winner(self) -> None:
        # Two gap cues rendering in one assembly should not happen (the
        # mutex forbids it), but if it ever does the earlier one in the
        # priority order is the one the loser lost to.
        d = decisions_from_block_chars(
            {"forward_curiosity"},
            {
                "sleep_return_block": 40,
                "away_activities_block": 40,
                "forward_curiosity_block": 0,
            },
        )
        self.assertEqual(
            d.declined["forward_curiosity"],
            f"{REASON_LOST_PRIORITY}:sleep_return",
        )

    def test_no_winner_means_no_priority_blame(self) -> None:
        d = decisions_from_block_chars(
            {"turning_over"}, {"turning_over_block": 0},
        )
        self.assertNotIn(REASON_LOST_PRIORITY, d.declined["turning_over"])

    def test_question_balance_veto_is_named(self) -> None:
        d = decisions_from_block_chars(
            {"follow_up"},
            {"follow_up_block": 0},
            question_balance_suppressed=True,
        )
        self.assertEqual(d.declined["follow_up"], REASON_QUESTION_BALANCE)

    def test_priority_loss_outranks_the_question_balance_veto(self) -> None:
        # Both were true; the mutex is what actually decided it, and
        # blaming K47 would send the reader to the wrong mechanism.
        d = decisions_from_block_chars(
            {"turning_over", "forward_curiosity"},
            {"turning_over_block": 90, "forward_curiosity_block": 0},
            question_balance_suppressed=True,
        )
        self.assertEqual(
            d.declined["forward_curiosity"],
            f"{REASON_LOST_PRIORITY}:turning_over",
        )

    def test_unarmed_cues_produce_no_rows(self) -> None:
        d = decisions_from_block_chars(set(), {"follow_up_block": 0})
        self.assertEqual(d.rows(), [])

    def test_surfaced_without_being_detected_as_armed_still_records(self) -> None:
        # A gap in the arming model must not swallow a real surfacing: it
        # rendered, so it definitionally had material.
        d = decisions_from_block_chars(set(), {"follow_up_block": 55})
        self.assertIn("follow_up", d.armed)
        self.assertEqual(d.rows(), [("follow_up", OUTCOME_SURFACED, "")])

    def test_reach_can_never_exceed_one(self) -> None:
        d = decisions_from_block_chars(
            {"follow_up"}, {"follow_up_block": 55, "tension_block": 20},
        )
        self.assertTrue(d.surfaced <= d.armed)

    def test_unregistered_blocks_are_ignored(self) -> None:
        d = decisions_from_block_chars(set(), {"persona": 78000})
        self.assertEqual(d.rows(), [])

    def test_missing_block_chars_entry_reads_as_declined(self) -> None:
        d = decisions_from_block_chars({"tension"}, {})
        self.assertEqual(
            d.rows(), [("tension", OUTCOME_DECLINED, REASON_PROVIDER)],
        )


# ── 4. the store ──────────────────────────────────────────────────────


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.close)

    def test_reach_reports_counts_beside_the_rate(self) -> None:
        self.fx.store.add_many(1, [("follow_up", OUTCOME_SURFACED, "")])
        self.fx.store.add_many(
            2, [("follow_up", OUTCOME_DECLINED, REASON_PROVIDER)],
        )
        self.fx.store.add_many(
            3, [("follow_up", OUTCOME_DECLINED, REASON_PROVIDER)],
        )
        row = self.fx.store.reach()[0]
        self.assertEqual(
            (row["cue"], row["armed"], row["surfaced"], row["declined"]),
            ("follow_up", 3, 1, 2),
        )
        self.assertAlmostEqual(row["reach_rate"], 1 / 3, places=4)

    def test_decline_reasons_exclude_surfaced_rows(self) -> None:
        self.fx.store.add_many(1, [("tension", OUTCOME_SURFACED, "")])
        self.fx.store.add_many(
            2, [("tension", OUTCOME_DECLINED, REASON_QUESTION_BALANCE)],
        )
        reasons = self.fx.store.decline_reasons()
        self.assertEqual(
            reasons, [{"cue": "tension", "reason": REASON_QUESTION_BALANCE,
                       "count": 1}],
        )

    def test_window_bounds_the_aggregate(self) -> None:
        self.fx.store.add_many(1, [("tension", OUTCOME_SURFACED, "")])
        self.fx.backdate("tension", 90)
        self.assertEqual(self.fx.store.reach(window_days=30), [])
        self.assertEqual(len(self.fx.store.reach()), 1)

    def test_rows_without_a_message_id_are_dropped(self) -> None:
        # The id is the only link to the reply a cue helped produce, so a
        # row without one is unattributable rather than merely incomplete.
        self.assertEqual(
            self.fx.store.add_many(0, [("tension", OUTCOME_SURFACED, "")]), 0,
        )
        self.assertEqual(self.fx.store.count(), 0)

    def test_prune_drops_only_old_rows(self) -> None:
        self.fx.store.add_many(1, [("old_cue", OUTCOME_SURFACED, "")])
        self.fx.backdate("old_cue", 200)
        self.fx.store.add_many(2, [("new_cue", OUTCOME_SURFACED, "")])
        self.assertEqual(self.fx.store.prune(30), 1)
        self.assertEqual(
            [r["cue"] for r in self.fx.store.reach()], ["new_cue"],
        )


# ── 5. the ledger mirror ──────────────────────────────────────────────


class LedgerMirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.close)

    def test_cue_rows_are_name_keyed(self) -> None:
        self.fx.ledger.add_many(
            5,
            [SurfacedItem(
                item_kind=ITEM_KIND_CUE, item_id=0, item_key="turning_over",
                lane="cue",
            )],
            echoes={},
        )
        conn = self.fx.db._get_conn()
        row = conn.execute(
            "SELECT item_kind, item_id, item_key, echoed, echo_kind "
            "FROM surfacing_outcomes"
        ).fetchone()
        self.assertEqual(tuple(row[:3]), (ITEM_KIND_CUE, 0, "turning_over"))
        # Echo has no meaning for a cue -- it is an instruction, not
        # something she might quote -- so NULL is the correct reading.
        self.assertIsNone(row[3])
        self.assertIsNone(row[4])

    def test_a_row_naming_nothing_is_refused(self) -> None:
        self.assertEqual(
            self.fx.ledger.add_many(
                5,
                [SurfacedItem(item_kind=ITEM_KIND_CUE, item_id=0)],
                echoes={},
            ),
            0,
        )

    def test_integer_kinds_keep_a_null_key(self) -> None:
        self.fx.ledger.add_many(
            5, [SurfacedItem(item_kind=ITEM_KIND_MEMORY, item_id=42)],
            echoes={},
        )
        conn = self.fx.db._get_conn()
        row = conn.execute(
            "SELECT item_id, item_key FROM surfacing_outcomes"
        ).fetchone()
        self.assertEqual((row[0], row[1]), (42, None))

    def test_cues_settle_with_the_rest_of_the_turn(self) -> None:
        self.fx.ledger.add_many(
            7,
            [
                SurfacedItem(item_kind=ITEM_KIND_MEMORY, item_id=1),
                SurfacedItem(
                    item_kind=ITEM_KIND_CUE, item_id=0, item_key="follow_up",
                ),
            ],
            echoes={},
        )
        self.assertEqual(self.fx.ledger.settle(7, "engaged"), 2)

    def test_leaderboard_exposes_the_cue_name(self) -> None:
        self.fx.ledger.add_many(
            7,
            [SurfacedItem(
                item_kind=ITEM_KIND_CUE, item_id=0, item_key="follow_up",
            )],
            echoes={},
        )
        self.fx.ledger.settle(7, "engaged")
        board = self.fx.ledger.leaderboard(min_settled=1)
        self.assertEqual(board[0]["item_key"], "follow_up")

    def test_two_cues_do_not_collapse_into_one_row(self) -> None:
        # Both carry ``item_id = 0``, so grouping by id alone would merge
        # every cue's history into a single meaningless line.
        self.fx.ledger.add_many(
            7,
            [
                SurfacedItem(
                    item_kind=ITEM_KIND_CUE, item_id=0, item_key="follow_up",
                ),
                SurfacedItem(
                    item_kind=ITEM_KIND_CUE, item_id=0, item_key="tension",
                ),
            ],
            echoes={},
        )
        self.fx.ledger.settle(7, "engaged")
        keys = sorted(
            r["item_key"] for r in self.fx.ledger.leaderboard(min_settled=1)
        )
        self.assertEqual(keys, ["follow_up", "tension"])


# ── 6. the session wiring ─────────────────────────────────────────────


class _Host(PostTurnHelpersMixin, InnerLifePart1Mixin):
    """Minimal session standing in for the wiring under test.

    Composes the real ``InnerLifePart1Mixin`` rather than stubbing
    ``_question_balance_suppressed``, so the snapshot is tested against the
    actual gate the providers consult. The recorder swallows an
    ``AttributeError`` here, so a stub would have kept passing if the two
    mixins ever stopped being composed together on the real controller.
    """

    def __init__(self, fx: _Fixture, *, kv=None, **slots) -> None:
        self._chat_db = _FakeDb(kv)
        self._cue_decision_store = fx.store
        self._surfacing_outcome_store = fx.ledger
        self._cue_armed_snapshot = set()
        self._cue_question_balance_snapshot = False
        self._last_cue_decisions = None
        self._last_surfaced_items = []
        self._prev_surfacing_message_id = 0
        self.debug_overrides.arm("question_balance_suppress_remaining", 0)
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(question_balance_enabled=True),
        )
        for spec in CUE_SPECS.values():
            if spec.slot_attr:
                setattr(self, spec.slot_attr, None)
        for attr, value in slots.items():
            setattr(self, attr, value)


class WiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = _Fixture()
        self.addCleanup(self.fx.close)

    def test_snapshot_captures_arming_and_the_veto(self) -> None:
        host = _Host(self.fx, _pending_turning_over_seconds=60.0)
        host.debug_overrides.arm("question_balance_suppress_remaining", 2)
        host._snapshot_armed_cues()
        self.assertEqual(host._cue_armed_snapshot, {"turning_over"})
        self.assertTrue(host._cue_question_balance_snapshot)

    def test_snapshot_is_a_noop_without_a_store(self) -> None:
        host = _Host(self.fx, _pending_turning_over_seconds=60.0)
        host._cue_decision_store = None
        host._snapshot_armed_cues()
        self.assertEqual(host._cue_armed_snapshot, set())

    def test_record_writes_the_armed_turn(self) -> None:
        host = _Host(self.fx, _pending_turning_over_seconds=60.0)
        host._snapshot_armed_cues()
        host._record_cue_decisions(
            assistant_message_id=11,
            telemetry=SimpleNamespace(block_chars={"turning_over_block": 88}),
        )
        self.assertEqual(
            [(r["cue"], r["surfaced"]) for r in self.fx.store.reach()],
            [("turning_over", 1)],
        )

    def test_the_veto_snapshot_survives_the_post_turn_decrement(self) -> None:
        # K47's countdown is decremented during post-turn, BEFORE the cue
        # recorder runs. Reading it at attribution time would miss a
        # suppression that was active during assembly and blame the
        # provider instead.
        host = _Host(self.fx, _pending_turning_over_seconds=60.0)
        host.debug_overrides.arm("question_balance_suppress_remaining", 1)
        host._snapshot_armed_cues()
        # post-turn decay
        host.debug_overrides.arm("question_balance_suppress_remaining", 0)
        host._record_cue_decisions(
            assistant_message_id=11,
            telemetry=SimpleNamespace(block_chars={"turning_over_block": 0}),
        )
        self.assertEqual(
            self.fx.store.decline_reasons()[0]["reason"],
            REASON_QUESTION_BALANCE,
        )

    def test_the_snapshot_is_consumed(self) -> None:
        # A stale snapshot would credit the next turn with this turn's
        # armed set, which on a banter turn (no assembly) would blame cues
        # for a prompt that was never built.
        host = _Host(self.fx, _pending_turning_over_seconds=60.0)
        host._snapshot_armed_cues()
        host._record_cue_decisions(
            assistant_message_id=11,
            telemetry=SimpleNamespace(block_chars={"turning_over_block": 88}),
        )
        self.assertEqual(host._cue_armed_snapshot, set())
        self.assertFalse(host._cue_question_balance_snapshot)

    def test_a_turn_without_an_assembly_records_nothing(self) -> None:
        host = _Host(self.fx, _pending_turning_over_seconds=60.0)
        host._snapshot_armed_cues()
        host._record_cue_decisions(assistant_message_id=11, telemetry=None)
        self.assertEqual(self.fx.store.count(), 0)

    def test_a_turn_with_no_reply_row_records_nothing(self) -> None:
        host = _Host(self.fx, _pending_turning_over_seconds=60.0)
        host._snapshot_armed_cues()
        host._record_cue_decisions(
            assistant_message_id=None,
            telemetry=SimpleNamespace(block_chars={"turning_over_block": 88}),
        )
        self.assertEqual(self.fx.store.count(), 0)

    def test_surfaced_cues_join_the_ledger_carry(self) -> None:
        host = _Host(self.fx, _pending_turning_over_seconds=60.0)
        host._snapshot_armed_cues()
        host._record_cue_decisions(
            assistant_message_id=11,
            telemetry=SimpleNamespace(block_chars={"turning_over_block": 88}),
        )
        # Appended to the carry, NOT written directly: the L37 recorder
        # owns the insert and the settle pointer.
        self.assertEqual(
            [(i.item_kind, i.item_key) for i in host._last_surfaced_items],
            [(ITEM_KIND_CUE, "turning_over")],
        )
        self.assertEqual(self.fx.ledger.count(), 0)

    def test_a_cue_only_turn_still_settles(self) -> None:
        # The regression the carry ordering exists to prevent: on a turn
        # with a cue but no concepts or memories, a direct second insert
        # would have left the cue row unsettled forever.
        host = _Host(self.fx, _pending_turning_over_seconds=60.0)
        host._snapshot_armed_cues()
        host._record_cue_decisions(
            assistant_message_id=11,
            telemetry=SimpleNamespace(block_chars={"turning_over_block": 88}),
        )
        host._record_surfacing_outcomes(
            assistant_text="sure",
            assistant_message_id=11,
            engagement_label=None,
        )
        self.assertEqual(host._prev_surfacing_message_id, 11)
        self.assertEqual(self.fx.ledger.settle(11, "engaged"), 1)

    def test_declines_stay_out_of_the_outcome_ledger(self) -> None:
        host = _Host(self.fx, _pending_turning_over_seconds=60.0)
        host._snapshot_armed_cues()
        host._record_cue_decisions(
            assistant_message_id=11,
            telemetry=SimpleNamespace(block_chars={"turning_over_block": 0}),
        )
        self.assertEqual(host._last_surfaced_items, [])
        self.assertEqual(self.fx.ledger.count(), 0)

    def test_a_failing_store_cannot_reach_the_turn(self) -> None:
        class _Boom:
            def add_many(self, *a, **k):
                raise RuntimeError("nope")

        host = _Host(self.fx, _pending_turning_over_seconds=60.0)
        host._snapshot_armed_cues()
        host._cue_decision_store = _Boom()
        # The store swallows its own failures; this asserts the caller does
        # not depend on that.
        with self.assertRaises(RuntimeError):
            host._record_cue_decisions(
                assistant_message_id=11,
                telemetry=SimpleNamespace(
                    block_chars={"turning_over_block": 0},
                ),
            )


# ── 7. schema ─────────────────────────────────────────────────────────


class SchemaTests(unittest.TestCase):
    def test_v28_lands_on_a_v27_database(self) -> None:
        tmp = TemporaryDirectory()
        try:
            path = Path(tmp.name) / "legacy.db"
            db = ChatDatabase(path)
            conn = db._get_conn()
            # Rewind to a v27-shaped ledger: no ``item_key``, no
            # ``cue_decisions``.
            conn.execute("DROP TABLE IF EXISTS cue_decisions")
            conn.execute("ALTER TABLE surfacing_outcomes RENAME TO _old")
            conn.execute(
                "CREATE TABLE surfacing_outcomes ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " assistant_message_id INTEGER NOT NULL,"
                " item_kind TEXT NOT NULL, item_id INTEGER NOT NULL,"
                " lane TEXT NOT NULL DEFAULT '',"
                " surface_reason TEXT NOT NULL DEFAULT '',"
                " score REAL NOT NULL DEFAULT 0.0,"
                " rank INTEGER NOT NULL DEFAULT 0, echoed INTEGER,"
                " echo_kind TEXT, echo_score REAL, engagement_label TEXT,"
                " created_at TEXT NOT NULL, settled_at TEXT)"
            )
            conn.execute("DROP TABLE _old")
            conn.execute(
                "INSERT INTO surfacing_outcomes "
                "(assistant_message_id, item_kind, item_id, created_at) "
                "VALUES (1, 'memory', 9, '2026-01-01T00:00:00')"
            )
            conn.execute("UPDATE schema_version SET version = 27")
            conn.commit()
            conn.close()
            db._local.conn = None

            db2 = ChatDatabase(path)
            conn2 = db2._get_conn()
            version = conn2.execute(
                "SELECT version FROM schema_version"
            ).fetchone()[0]
            self.assertEqual(int(version), _SCHEMA_VERSION)
            cols = {
                r[1] for r in conn2.execute(
                    "PRAGMA table_info(surfacing_outcomes)"
                )
            }
            self.assertIn("item_key", cols)
            # The pre-existing row keeps a NULL key, which is already
            # correct for an id-identified kind.
            self.assertIsNone(
                conn2.execute(
                    "SELECT item_key FROM surfacing_outcomes"
                ).fetchone()[0]
            )
            # And the new table exists and is writable.
            CueDecisionStore(db2).add_many(
                1, [("follow_up", OUTCOME_SURFACED, "")],
            )
            self.assertEqual(CueDecisionStore(db2).count(), 1)
            conn2.close()
            db2._local.conn = None
        finally:
            try:
                tmp.cleanup()
            except PermissionError:
                pass


if __name__ == "__main__":
    unittest.main()
