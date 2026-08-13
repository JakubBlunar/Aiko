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
    INELIGIBLE_REASONS,
    OUTCOME_DECLINED,
    OUTCOME_SURFACED,
    REASON_CADENCE_BLOCK,
    REASON_CROSS_LANE,
    REASON_IMPORTANCE_FLOOR,
    REASON_LOST_PRIORITY,
    REASON_NO_OPENING,
    REASON_NO_STOCK,
    REASON_PROVIDER,
    REASON_QUESTION_BALANCE,
    REASON_TOPIC_MISS,
    armed_cues,
    decisions_from_block_chars,
    is_eligible_decline,
    note_decline,
    take_decline_notes,
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

    def test_the_belief_probe_is_the_last_thing_she_opens_with(self) -> None:
        # ``GAP_CUE_ORDER`` is the mutex priority, and asking someone
        # about their own character out of a silence is the heaviest
        # opening in the set -- it yields to every other gap cue.
        self.assertEqual(GAP_CUE_ORDER[-1], "concept_hypothesis")

    def test_the_belief_probe_arms_on_stock_not_a_slot(self) -> None:
        # Deliberately slot-less while still ``gap_cue``: it is the one
        # dual-mode type, and most of its firings come from the topic
        # path where no slot is involved. Arming on the pending slot
        # would under-count those opportunities.
        spec = CUE_SPECS["concept_hypothesis"]
        self.assertTrue(spec.gap_cue)
        self.assertFalse(spec.slot_attr)

    def test_every_spec_has_an_arming_signal(self) -> None:
        # A spec with no slot, no journal, no predicate and no pool can
        # never be armed, so it would sit in ``never_armed`` forever
        # looking like a broken worker rather than a broken registry
        # entry. The pool counts as a signal in its own right: for a
        # pool-only type a queued row *is* the opportunity, which is the
        # branch ``armed_cues`` takes when the others are absent.
        from app.core.proactive.cue_accounting import POOLED_CUES

        for name, spec in CUE_SPECS.items():
            with self.subTest(cue=name):
                self.assertTrue(
                    spec.slot_attr
                    or spec.journal_key
                    or spec.armed_when is not None
                    or name in POOLED_CUES,
                    f"{name} has no way to be detected as armed",
                )

    def test_being_caught_mid_beat_does_not_arm_on_another_cues_slot(
        self,
    ) -> None:
        """H32: the proxy that made this cue's ratio a ratio over returns.

        It shared ``away_activities``' gap slot, so it read as armed on
        every return -- while its provider ignores that slot entirely and
        asks only whether a beat is open. A return is common and a return
        landing inside an open beat is rare, so 7 of 7 declines came out
        as gap-mutex losses on turns with very likely no beat to catch.
        """
        spec = CUE_SPECS["caught_mid_activity"]
        self.assertFalse(spec.slot_attr)
        self.assertIsNotNone(spec.armed_when)

    def test_no_two_cues_share_a_slot(self) -> None:
        # Two cues on one slot means one of them is arming on a proxy for
        # the other's opportunity, which is what H32 was.
        seen: dict[str, str] = {}
        for name, spec in CUE_SPECS.items():
            if not spec.slot_attr:
                continue
            other = seen.get(spec.slot_attr)
            self.assertIsNone(
                other,
                f"{name} and {other} both arm on {spec.slot_attr}",
            )
            seen[spec.slot_attr] = name

    def test_coarse_arming_is_the_watermarkless_journals_minus_the_pool(
        self,
    ) -> None:
        """A pooled cue's stock is an exact count, so it is not coarse."""
        from app.core.proactive.cue_accounting import POOLED_CUES

        self.assertEqual(
            COARSE_ARMING,
            frozenset(
                n for n, s in CUE_SPECS.items()
                if s.journal_key and not s.watermark_key
            ) - POOLED_CUES,
        )

    def test_the_five_topic_gated_cues_left_coarse_arming(self) -> None:
        self.assertFalse(COARSE_ARMING & {
            "interest_drift",
            "associative_wander",
            "curiosity_gradient",
            "dormant_interest",
            "knowledge_gap_notice",
        })


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


def _beat_kv(*, minutes_left: float, interrupted: bool = False) -> dict[str, str]:
    """A stored in-progress beat, open or closed by ``minutes_left``."""
    from datetime import timedelta

    from app.core.infra import timephrase
    from app.core.world.in_progress_beat import IN_PROGRESS_KEY

    now = timephrase.utcnow()
    return {
        IN_PROGRESS_KEY: json.dumps({
            "key": "reading",
            "activity": "reading on the sofa",
            "posture": "curled up",
            "summary": "reading",
            "started_at": (now - timedelta(minutes=20)).isoformat(),
            "expected_end_at": (
                now + timedelta(minutes=minutes_left)
            ).isoformat(),
            "interrupted_at": now.isoformat() if interrupted else "",
        }),
    }


class EventArmingTests(unittest.TestCase):
    """H32: ``armed_when`` — a cue armed by a live fact about the world.

    The three declarative signals are all proxies. When the proxy
    available is true far more often than the provider's own test, the
    result is not a conservative ratio, it is a ratio over the wrong
    population: ``caught_mid_activity`` measured returns rather than
    returns that caught her at something.
    """

    def test_an_open_beat_arms_the_cue(self) -> None:
        host = _session(_beat_kv(minutes_left=15.0))
        self.assertIn("caught_mid_activity", armed_cues(host))

    def test_a_bare_gap_no_longer_arms_it(self) -> None:
        host = _session(_pending_away_activities_seconds=7200.0)
        self.assertNotIn("caught_mid_activity", armed_cues(host))

    def test_a_finished_beat_does_not_arm_it(self) -> None:
        host = _session(_beat_kv(minutes_left=-5.0))
        self.assertNotIn("caught_mid_activity", armed_cues(host))

    def test_a_beat_she_already_set_down_does_not_arm_it(self) -> None:
        # Interrupted means a return already caught her at this one; it is
        # now a thread to resume, not a cue to spend again.
        host = _session(_beat_kv(minutes_left=15.0, interrupted=True))
        self.assertNotIn("caught_mid_activity", armed_cues(host))

    def test_a_beat_with_no_activity_does_not_arm_it(self) -> None:
        from app.core.world.in_progress_beat import IN_PROGRESS_KEY

        host = _session({IN_PROGRESS_KEY: json.dumps({"key": "reading"})})
        self.assertNotIn("caught_mid_activity", armed_cues(host))

    def test_an_open_beat_arms_it_without_any_gap_at_all(self) -> None:
        # The provider has no minimum-absence bar -- "is she mid-something
        # right now" is equally true whether he stepped out for an hour or
        # closed the laptop yesterday -- so arming must not need one.
        host = _session(_beat_kv(minutes_left=15.0))
        self.assertIsNone(
            getattr(host, "_pending_away_activities_seconds", None),
        )
        self.assertIn("caught_mid_activity", armed_cues(host))

    def test_a_predicate_that_raises_reads_as_unarmed(self) -> None:
        # Arming is a diagnostic and runs on the prompt-assembly path, so
        # it must never be the thing that breaks a turn.
        from app.core.proactive.cue_accounting import CueSpec, _armed_by_event

        def boom(_session: object) -> bool:
            raise RuntimeError("kv exploded")

        spec = CueSpec("x", armed_when=boom)
        self.assertFalse(_armed_by_event(_session(), spec))

    def test_a_missing_chat_db_reads_as_unarmed(self) -> None:
        from app.core.proactive.cue_accounting import _open_beat_armed

        self.assertFalse(_open_beat_armed(SimpleNamespace(_chat_db=None)))


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

    def test_a_lower_ranked_winner_cannot_have_blocked_it(self) -> None:
        """H32: the mutex only explains a loss to something *above* you.

        ``sleep_return`` is second of six, so ``away_activities`` firing
        is not what stopped it -- it declined on its own gate and then ran
        first anyway. Recorded as a defeat, and worse, the defeat
        overwrote the real reason, since this branch outranks ``notes``.
        """
        d = decisions_from_block_chars(
            {"sleep_return"},
            {"sleep_return_block": 0, "away_activities_block": 40},
            provider_reasons={"sleep_return": REASON_CADENCE_BLOCK},
        )
        self.assertEqual(d.declined["sleep_return"], REASON_CADENCE_BLOCK)

    def test_a_lower_ranked_winner_falls_back_to_the_catch_all(self) -> None:
        # No provider reason to fall through to: the honest answer is the
        # catch-all, not a fabricated mutex loss.
        d = decisions_from_block_chars(
            {"sleep_return"},
            {"sleep_return_block": 0, "forward_curiosity_block": 40},
        )
        self.assertEqual(d.declined["sleep_return"], REASON_PROVIDER)

    def test_the_first_cue_in_the_order_can_never_lose_the_mutex(self) -> None:
        for winner in GAP_CUE_ORDER[1:]:
            with self.subTest(winner=winner):
                d = decisions_from_block_chars(
                    {GAP_CUE_ORDER[0]},
                    {f"{GAP_CUE_ORDER[0]}_block": 0, f"{winner}_block": 40},
                )
                self.assertNotIn(
                    REASON_LOST_PRIORITY, d.declined[GAP_CUE_ORDER[0]],
                )

    def test_every_recordable_mutex_loss_names_a_higher_ranked_winner(
        self,
    ) -> None:
        """The invariant the live ledger violated 7 times in 129 rows."""
        from app.core.proactive.cue_accounting import _gap_rank

        for loser in GAP_CUE_ORDER:
            for winner in GAP_CUE_ORDER:
                if winner == loser:
                    continue
                d = decisions_from_block_chars(
                    {loser},
                    {f"{loser}_block": 0, f"{winner}_block": 40},
                )
                reason = d.declined[loser]
                if reason.startswith(f"{REASON_LOST_PRIORITY}:"):
                    named = reason.split(":", 1)[1]
                    self.assertLess(
                        _gap_rank(named), _gap_rank(loser),
                        f"{loser} recorded a loss to {named}, which it "
                        "outranks",
                    )

    def test_an_unknown_cue_name_sorts_last(self) -> None:
        from app.core.proactive.cue_accounting import _gap_rank

        self.assertEqual(_gap_rank("not_a_cue"), len(GAP_CUE_ORDER))

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


class ProviderReasonTests(unittest.TestCase):
    """H7: the catch-all made 86% of one cue type's declines unreadable.

    A provider knows things the block-chars attribution cannot see -- that
    nothing on the shelf was topical, that a cooldown was still running,
    that another lane had already claimed the material. It reports them
    through :func:`note_decline`, and they land here.
    """

    def test_a_provider_reason_replaces_the_catch_all(self) -> None:
        d = decisions_from_block_chars(
            {"knowledge_gap_notice"},
            {"knowledge_gap_notice_block": 0},
            provider_reasons={"knowledge_gap_notice": REASON_TOPIC_MISS},
        )
        self.assertEqual(
            d.declined["knowledge_gap_notice"], REASON_TOPIC_MISS,
        )

    def test_an_unreported_provider_still_gets_the_catch_all(self) -> None:
        """The fallback stays: an unaccounted cue is worse than a vague one."""
        d = decisions_from_block_chars(
            {"self_callback"},
            {"self_callback_block": 0},
            provider_reasons={"knowledge_gap_notice": REASON_TOPIC_MISS},
        )
        self.assertEqual(d.declined["self_callback"], REASON_PROVIDER)

    def test_the_structural_reasons_still_outrank_it(self) -> None:
        """A cue that lost the gap mutex never reached the gate it
        reported, so naming that gate would send the reader to the wrong
        mechanism."""
        d = decisions_from_block_chars(
            {"turning_over", "concept_hypothesis"},
            {"turning_over_block": 90, "concept_hypothesis_block": 0},
            provider_reasons={"concept_hypothesis": REASON_IMPORTANCE_FLOOR},
        )
        self.assertEqual(
            d.declined["concept_hypothesis"],
            f"{REASON_LOST_PRIORITY}:turning_over",
        )
        d2 = decisions_from_block_chars(
            {"concept_hypothesis"},
            {"concept_hypothesis_block": 0},
            question_balance_suppressed=True,
            provider_reasons={"concept_hypothesis": REASON_IMPORTANCE_FLOOR},
        )
        self.assertEqual(
            d2.declined["concept_hypothesis"], REASON_QUESTION_BALANCE,
        )

    def test_note_decline_keeps_the_first_gate(self) -> None:
        """A provider walks its gates in order and returns at the first
        refusal, so a later gate it would also have failed is not what
        happened to it."""
        host = SimpleNamespace()
        note_decline(host, "concept_hypothesis", REASON_TOPIC_MISS)
        note_decline(host, "concept_hypothesis", REASON_IMPORTANCE_FLOOR)
        self.assertEqual(
            take_decline_notes(host),
            {"concept_hypothesis": REASON_TOPIC_MISS},
        )

    def test_draining_clears_the_turn(self) -> None:
        """A note left behind would be attributed to the next assembly,
        where the cue may well have surfaced."""
        host = SimpleNamespace()
        note_decline(host, "self_callback", REASON_NO_STOCK)
        self.assertTrue(take_decline_notes(host))
        self.assertEqual(take_decline_notes(host), {})


class EligibilityTests(unittest.TestCase):
    """Which declines were ever a chance the cue had.

    The split the reach ratio was missing: a cue inside its own cooldown
    is armed on every turn of it and can surface on none, so counting
    those turns as misses is what made four correctly-rare cues read as
    starving ones (H30).
    """

    def test_the_reasons_that_mean_never_in_play(self) -> None:
        for reason in (
            REASON_CADENCE_BLOCK, REASON_NO_OPENING, REASON_QUESTION_BALANCE,
            REASON_NO_STOCK,
        ):
            self.assertFalse(is_eligible_decline(reason), reason)

    def test_a_clock_and_a_missing_lull_are_recorded_apart(self) -> None:
        """Both mean "not in play", and they resolve differently: waiting
        fixes a cooldown, and nothing fixes a room that never goes quiet.
        Sharing one reason is what hid ``dormant_interest``'s real
        problem."""
        self.assertNotEqual(REASON_NO_OPENING, REASON_CADENCE_BLOCK)
        self.assertIn(REASON_NO_OPENING, INELIGIBLE_REASONS)

    def test_the_reasons_that_are_the_cue_s_own_judgement(self) -> None:
        for reason in (
            REASON_TOPIC_MISS, REASON_IMPORTANCE_FLOOR, REASON_CROSS_LANE,
            REASON_PROVIDER,
        ):
            self.assertTrue(is_eligible_decline(reason), reason)

    def test_the_winner_s_name_does_not_change_the_verdict(self) -> None:
        # Compared on the part before the colon: a mutex loss is a chance
        # the cue had and lost, whichever rival took it.
        self.assertTrue(is_eligible_decline(REASON_LOST_PRIORITY))
        self.assertTrue(
            is_eligible_decline(f"{REASON_LOST_PRIORITY}:turning_over"),
        )

    def test_every_recorded_reason_is_classified(self) -> None:
        """The vocabulary is closed, so a new reason must be a deliberate
        choice on both sides of this line rather than a silent default
        into the eligible bucket."""
        known = {
            REASON_CADENCE_BLOCK, REASON_CROSS_LANE, REASON_IMPORTANCE_FLOOR,
            REASON_LOST_PRIORITY, REASON_NO_OPENING, REASON_NO_STOCK,
            REASON_PROVIDER, REASON_QUESTION_BALANCE, REASON_TOPIC_MISS,
        }
        self.assertTrue(INELIGIBLE_REASONS <= known)
        self.assertEqual(len(INELIGIBLE_REASONS), 4)


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

    def test_a_cooldown_turn_was_never_a_missed_chance(self) -> None:
        """The second denominator, and the reason it exists.

        ``self_callback`` carries a ten-day cadence, so it is armed on
        every turn of that cooldown and can surface on none of them.
        Dividing by all of them reads as a 33% failure; dividing by the
        turns it was actually in play reads as the design working.
        """
        self.fx.store.add_many(1, [("self_callback", OUTCOME_SURFACED, "")])
        for msg in (2, 3, 4):
            self.fx.store.add_many(
                msg,
                [("self_callback", OUTCOME_DECLINED, REASON_CADENCE_BLOCK)],
            )
        row = self.fx.store.reach()[0]
        self.assertEqual((row["armed"], row["eligible"]), (4, 1))
        self.assertAlmostEqual(row["reach_rate"], 0.25, places=4)
        self.assertAlmostEqual(row["eligible_rate"], 1.0, places=4)

    def test_a_topic_gate_that_never_matches_stays_in_the_denominator(
        self,
    ) -> None:
        # The opposite case, and the one the ratio exists to find: stock
        # was there, the cue was free to surface, and its gate refused.
        for msg in (1, 2, 3):
            self.fx.store.add_many(
                msg,
                [("curiosity_gradient", OUTCOME_DECLINED, REASON_TOPIC_MISS)],
            )
        row = self.fx.store.reach()[0]
        self.assertEqual((row["armed"], row["eligible"]), (3, 3))
        self.assertEqual(row["eligible_rate"], 0.0)

    def test_a_mutex_loss_counts_as_a_chance_it_had(self) -> None:
        # ``lost_priority`` carries the winner's name, so eligibility has
        # to test the prefix -- an equality check would silently drop
        # every mutex loss out of the denominator.
        self.fx.store.add_many(
            1,
            [(
                "concept_hypothesis",
                OUTCOME_DECLINED,
                f"{REASON_LOST_PRIORITY}:turning_over",
            )],
        )
        row = self.fx.store.reach()[0]
        self.assertEqual((row["armed"], row["eligible"]), (1, 1))

    def test_an_undiagnosed_decline_cannot_flatter_the_rate(self) -> None:
        # The catch-all stays in the eligible denominator on purpose: a
        # decline nobody has diagnosed must not improve the number by
        # being assumed harmless.
        self.fx.store.add_many(
            1, [("follow_up", OUTCOME_DECLINED, REASON_PROVIDER)],
        )
        row = self.fx.store.reach()[0]
        self.assertEqual((row["armed"], row["eligible"]), (1, 1))

    def test_never_in_play_reports_no_rate_rather_than_zero(self) -> None:
        # A cue stocked through a long cooldown and never once free to
        # surface has no rate to report; zero would read as a failure.
        for msg in (1, 2):
            self.fx.store.add_many(
                msg,
                [("wellbeing_concern", OUTCOME_DECLINED, REASON_CADENCE_BLOCK)],
            )
        row = self.fx.store.reach()[0]
        self.assertEqual(row["eligible"], 0)
        self.assertIsNone(row["eligible_rate"])
        self.assertEqual(row["reach_rate"], 0.0)

    def test_cues_stay_ordered_by_how_often_they_were_armed(self) -> None:
        for msg in (1, 2, 3):
            self.fx.store.add_many(msg, [("busy", OUTCOME_SURFACED, "")])
        self.fx.store.add_many(4, [("quiet", OUTCOME_SURFACED, "")])
        self.assertEqual(
            [r["cue"] for r in self.fx.store.reach()], ["busy", "quiet"],
        )

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


# ── 8. the cue pool policy registry ───────────────────────────────────


class CuePolicyTests(unittest.TestCase):
    def test_every_pooled_cue_is_a_registered_cue(self) -> None:
        """A policy for a cue the arming model has never heard of is a typo."""
        from app.core.proactive.cue_accounting import CUE_SPECS, POOLED_CUES

        # ``curiosity_seed`` is the one exception: it never used a journal
        # ring, so it has no CueSpec and never needed one.
        self.assertEqual(POOLED_CUES - set(CUE_SPECS), {"curiosity_seed"})

    def test_handling_sections_exist_in_the_notes_file(self) -> None:
        """The hoist is silent when a header is wrong -- so check it here.

        ``strip_persona_section`` is deliberately a no-op on a header it
        cannot find, because the persona is user-editable. That is right at
        runtime and useless as a typo guard, which is what this is. Renaming
        a ``handling_section`` right here in ``CUE_POLICIES`` is the likeliest
        way to break the pairing, hence a guard next to the definitions as
        well as one next to the loader.
        """
        from pathlib import Path

        from app.core.proactive.cue_accounting import CUE_POLICIES

        notes = Path("data/persona/conditional_handling.txt").read_text(
            encoding="utf-8",
        )
        headers = {line.strip() for line in notes.splitlines()}
        for name, policy in CUE_POLICIES.items():
            if not policy.handling_section:
                continue
            self.assertIn(
                policy.handling_section,
                headers,
                f"{name}: handling_section is not a notes-file header",
            )

    def test_every_hoisted_policy_names_its_block(self) -> None:
        """A header with no block never ships; a block typo never warns.

        ``_persona_split`` keys sections by block name, so a policy that
        sets ``handling_section`` without a matching ``block`` extracts its
        text out of T0 and then has nothing to trigger re-emission -- the
        guidance disappears rather than moves.
        """
        from app.core.proactive.cue_accounting import CUE_POLICIES
        from app.core.session.prompt_assembler import _BLOCK_TIER_OF

        for name, policy in CUE_POLICIES.items():
            if not policy.handling_section:
                continue
            with self.subTest(cue=name):
                self.assertTrue(policy.block, f"{name}: no block named")
                self.assertIn(policy.block, _BLOCK_TIER_OF)

    def test_only_off_topic_cues_trust_cosine(self) -> None:
        """The two on-topic-by-construction types stay lexical-only.

        Their subject *is* what is being discussed, so a cosine against the
        reply measures "was on topic" rather than "she used the cue".
        """
        from app.core.proactive.cue_accounting import (
            CUE_POLICIES,
            MATCH_LEXICAL,
        )

        for name in ("knowledge_gap_notice", "interest_drift"):
            self.assertEqual(CUE_POLICIES[name].match_mode, MATCH_LEXICAL)

    def test_question_shaped_cues_are_answered_not_spoken(self) -> None:
        from app.core.proactive.cue_accounting import (
            CUE_POLICIES,
            FULFILMENT_ANSWERED,
        )

        for name in (
            "forward_curiosity",
            "curiosity_gradient",
            "knowledge_gap_notice",
            "dormant_interest",
        ):
            self.assertEqual(
                CUE_POLICIES[name].fulfilment, FULFILMENT_ANSWERED,
            )

    def test_both_retry_budgets_are_bounded(self) -> None:
        """The retry loop must terminate even if the matcher never fires."""
        from app.core.proactive.cue_accounting import CUE_POLICIES

        for name, policy in CUE_POLICIES.items():
            self.assertGreaterEqual(policy.max_surfacings, 1, name)
            self.assertLessEqual(policy.max_surfacings, 3, name)
            self.assertGreaterEqual(policy.max_asks, 1, name)
            self.assertLessEqual(policy.max_asks, 3, name)
            self.assertGreater(policy.ttl_hours, 0.0, name)
            self.assertGreaterEqual(policy.inventory_target, 0, name)

    def test_only_event_armed_types_have_an_empty_shelf(self) -> None:
        """``inventory_target=0`` is a claim, and things read it as one.

        It means "nothing stocks this type, the pool is only a retry
        buffer" -- which is what stops the scheduler seeing a permanent
        deficit and what makes ``armed_cues`` treat a row as an
        opportunity in its own right. A worker-produced type that landed
        on 0 by accident would go quietly unstocked forever.
        """
        from app.core.proactive.cue_accounting import CUE_POLICIES

        empty = {
            name for name, policy in CUE_POLICIES.items()
            if policy.inventory_target == 0
        }
        self.assertEqual(
            empty,
            {
                "turning_over",
                "sleep_return",
                "away_activities",
                "caught_mid_activity",
                "self_correction",
                "user_correction",
                "fact_reversal",
                "long_arc_callback",
                "tease_ledger",
            },
        )

    def test_policy_for_is_none_off_the_pool(self) -> None:
        from app.core.proactive.cue_accounting import policy_for

        self.assertIsNone(policy_for("absence_curiosity"))
        self.assertIsNone(policy_for(""))
        self.assertIsNotNone(policy_for("dormant_interest"))


class PoolReportTests(unittest.TestCase):
    """``get_cue_outcomes``' pool section -- depth and the real verdict.

    Reach says a block rendered; this says whether the cue was spent.
    The two disagree often enough that the section has to be readable on
    its own, including with cue accounting switched off.
    """

    def setUp(self) -> None:
        from app.core.proactive.cue_store import CueStore
        from app.core.session.cue_pool_mixin import CuePoolMixin

        tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.store = CueStore(ChatDatabase(Path(tmp.name) / "chat.db"))
        self.host = type("_Host", (CuePoolMixin,), {})()
        self.host._cue_store = self.store

    def _report(self, *, decisions=None):
        from app.mcp.server_tools.cue_outcome_tools import build_report

        session = SimpleNamespace(
            cue_pool_stats=self.host.cue_pool_stats,
            _cue_decision_store=decisions,
        )
        return build_report(session, window_days=None)

    def _row(self, report, cue: str) -> dict:
        return next(
            r for r in report["pool"]["by_type"] if r["cue"] == cue
        )

    def test_every_policy_type_has_a_row_even_when_empty(self) -> None:
        from app.core.proactive.cue_accounting import CUE_POLICIES

        rows = self._report()["pool"]["by_type"]
        self.assertEqual(
            {r["cue"] for r in rows}, set(CUE_POLICIES),
        )

    def test_an_empty_shelf_shows_its_whole_target_as_deficit(self) -> None:
        from app.core.proactive.cue_accounting import CUE_POLICIES

        row = self._row(self._report(), "curiosity_seed")
        target = CUE_POLICIES["curiosity_seed"].inventory_target
        self.assertEqual(row["pending"], 0)
        self.assertEqual(row["deficit"], target)

    def test_a_full_shelf_has_no_deficit(self) -> None:
        from app.core.proactive.cue_accounting import CUE_POLICIES

        target = CUE_POLICIES["curiosity_seed"].inventory_target
        for i in range(target):
            self.store.add("curiosity_seed", f"subject {i}", "cue")
        row = self._row(self._report(), "curiosity_seed")
        self.assertEqual(row["pending"], target)
        self.assertEqual(row["deficit"], 0)

    def test_used_and_expired_are_reported_apart(self) -> None:
        spent = self.store.add("curiosity_seed", "bread", "a")
        self.store.mark_surfaced(spent)
        self.store.mark_used(spent, evidence="lexical:1.00")
        dropped = self.store.add("curiosity_seed", "kites", "b")
        self.store.expire(dropped, evidence="max_surfacings")

        row = self._row(self._report(), "curiosity_seed")
        self.assertEqual(row["used"], 1)
        self.assertEqual(row["expired"], 1)
        self.assertEqual(row["pending"], 0)

    def test_the_mean_says_how_many_looks_a_cue_needs(self) -> None:
        cue_id = self.store.add("curiosity_seed", "bread", "a")
        self.store.mark_surfaced(cue_id)
        self.store.mark_surfaced(cue_id)
        self.store.mark_used(cue_id, evidence="lexical:1.00")
        row = self._row(self._report(), "curiosity_seed")
        self.assertEqual(row["mean_surfacings_before_use"], 2.0)

    def test_the_pool_is_readable_with_cue_accounting_off(self) -> None:
        self.store.add("curiosity_seed", "bread", "a")
        report = self._report()
        self.assertFalse(report["enabled"])
        self.assertTrue(report["pool"]["enabled"])
        self.assertEqual(self._row(report, "curiosity_seed")["pending"], 1)

    def test_no_pool_is_reported_as_disabled_not_as_an_error(self) -> None:
        from app.core.session.cue_pool_mixin import CuePoolMixin
        from app.mcp.server_tools.cue_outcome_tools import build_report

        poolless = type("_Host", (CuePoolMixin,), {})()
        session = SimpleNamespace(
            cue_pool_stats=poolless.cue_pool_stats, _cue_decision_store=None,
        )
        report = build_report(session, window_days=None)
        self.assertFalse(report["pool"]["enabled"])

    def test_an_untouched_pool_still_reports_its_shelves(self) -> None:
        """Distinct from no pool at all -- an empty shelf is a deficit.

        Except for the event-armed types, whose shelf is empty at rest by
        design; reporting those as short of stock would read as a stuck
        worker when there is no worker.
        """
        from app.core.proactive.cue_accounting import CUE_POLICIES

        report = self._report()
        self.assertTrue(report["pool"]["enabled"])
        for row in report["pool"]["by_type"]:
            target = CUE_POLICIES[row["cue"]].inventory_target
            with self.subTest(cue=row["cue"]):
                self.assertEqual(row["deficit"] > 0, target > 0)


if __name__ == "__main__":
    unittest.main()
