"""K92 -- the stance arbiter, its two axes, and what it may say.

Five things are worth pinning here, in descending order of how badly a
regression would hurt:

1. **The arbiter is keyed on real block names.** Every name in
   ``_OFFERS`` must be registered in ``_PROMPT_BLOCK_TIERS``. A typo
   would not raise; the arbiter would simply never see that offer and
   would keep producing plausible rows. This whole exercise started
   from a table dominated by an uninstrumented bail, so the same class
   of silent hole is the one thing worth a structural test.
2. **The ceiling is a filter, not a weight.** An accumulated want must
   not be able to outvote a direct question, because that is the exact
   regression K95 exists to insure against.
3. **The protected-arc veto expires.** It is the single most consequential
   number in the module -- 65% of all clamps -- and an untimed version
   suppressed her for days at a stretch (H39). The tests below pin both
   halves: it applies while the beat is fresh, and it stops.
4. **Brevity is a second axis, not a rung.** A turn can be both ``SHARE``
   and short. Phase 1 put ``HOLD`` at the bottom of the ladder and it was
   chosen zero times in 682 turns.
5. **Phase 2 may only ask for restraint.** The block renders for
   ``FOLLOW`` and for brevity and for nothing else -- a line agreeing
   with a steer that already rendered is the eleventh permission slip
   this whole family exists to argue against.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from app.core.conversation.stance import (
    ASK,
    CALLBACK,
    FOLLOW,
    FOLLOW_AND_ADD,
    HOLD,
    INITIATE,
    REDIRECT,
    SHARE,
    STANCE_LADDER,
    StanceInputs,
    decide,
    render_block,
)
from app.core.conversation import stance as stance_mod


class BlockNameTests(unittest.TestCase):
    def test_every_offer_names_a_registered_block(self) -> None:
        from app.core.session.prompt_assembler import _PROMPT_BLOCK_TIERS

        registered = {
            name
            for names in _PROMPT_BLOCK_TIERS.values()
            for name in names
        }
        unknown = sorted(set(stance_mod._OFFER_OF) - registered)
        self.assertEqual(
            unknown, [],
            "these blocks are not in the tier ladder, so the arbiter can "
            "never see them -- rename or remove them",
        )

    def test_no_block_offers_two_stances(self) -> None:
        seen: dict[str, str] = {}
        for stance, blocks in stance_mod._OFFERS.items():
            for block in blocks:
                self.assertNotIn(
                    block, seen,
                    f"{block} offers both {seen.get(block)} and {stance}",
                )
                seen[block] = stance


class LadderTests(unittest.TestCase):
    def test_the_ladder_runs_from_following_to_taking_the_floor(self) -> None:
        # Order is the mechanism -- ``min`` over it is the clamp -- so a
        # reshuffle is a behaviour change and should fail loudly.
        self.assertEqual(
            STANCE_LADDER,
            (FOLLOW, FOLLOW_AND_ADD, ASK, CALLBACK, SHARE,
             REDIRECT, INITIATE),
        )

    def test_hold_is_not_a_rung(self) -> None:
        """The phase-2 correction, pinned as a structural fact.

        ``HOLD`` answers "how many words", every rung answers "how much
        of the floor". Mixing them is what made it unreachable: as the
        bottom rung it could only be chosen when *nothing* was offered,
        and something is offered on 97.5% of turns.
        """
        self.assertNotIn(HOLD, STANCE_LADDER)
        self.assertNotIn(HOLD, stance_mod._RANK)


class CeilingTests(unittest.TestCase):
    def test_a_direct_question_caps_her_at_answering_and_adding(self) -> None:
        d = decide(StanceInputs(
            blocks=frozenset({"initiative_block"}),
            user_text="what did you think of the ending?",
        ))
        self.assertEqual(d.desire, INITIATE)
        self.assertEqual(d.stance, FOLLOW_AND_ADD)
        self.assertEqual(d.reason, "direct_question")
        self.assertTrue(d.clamped)

    def test_a_question_mark_counts_even_without_the_tag(self) -> None:
        # The tagger is regex-first and folds soft requests in, so the
        # punctuation is checked independently: missing a real question
        # means talking over it.
        d = decide(StanceInputs(
            blocks=frozenset({"initiative_block"}),
            user_text="so you'd rather stay in?",
            dialogue_act="chitchat",
        ))
        self.assertEqual(d.stance, FOLLOW_AND_ADD)

    def test_venting_caps_her_at_following(self) -> None:
        d = decide(StanceInputs(
            blocks=frozenset({"curiosity_seeds_block"}),
            user_text="i am so done with all of it",
            dialogue_act="vent",
        ))
        self.assertEqual(d.desire, ASK)
        self.assertEqual(d.stance, FOLLOW)
        self.assertEqual(d.reason, "vent")

    def test_the_most_restrictive_cap_wins_and_is_named(self) -> None:
        # Both vent and direct_question apply; vent is lower, so it is
        # the binding constraint and the one the row should blame.
        d = decide(StanceInputs(
            blocks=frozenset({"turning_over_block"}),
            user_text="why does everything have to be this hard?",
            dialogue_act="vent",
        ))
        self.assertEqual(d.stance, FOLLOW)
        self.assertEqual(d.reason, "vent")

    def test_a_long_message_caps_her_the_same_way_k53_does(self) -> None:
        d = decide(StanceInputs(
            blocks=frozenset({"initiative_block"}),
            user_text="x" * stance_mod.SUBSTANTIAL_CHARS,
        ))
        self.assertEqual(d.stance, FOLLOW_AND_ADD)
        self.assertEqual(d.reason, "user_substantial")

    def test_an_ordinary_turn_imposes_no_cap(self) -> None:
        d = decide(StanceInputs(
            blocks=frozenset({"initiative_block"}),
            user_text="finished work early today",
        ))
        self.assertEqual(d.ceiling, INITIATE)
        self.assertEqual(d.stance, INITIATE)
        self.assertFalse(d.clamped)

    def test_the_ceiling_cannot_be_outvoted_by_piling_on_offers(self) -> None:
        # The K95 guarantee. Seven providers all pushing at once still
        # do not get past a direct question -- a weight would have.
        d = decide(StanceInputs(
            blocks=frozenset({
                "initiative_block", "topic_appetite_block",
                "turning_over_block", "wants_block", "curiosity_seeds_block",
                "thread_ownership_block", "idle_seeds_block",
            }),
            user_text="did you get the parcel?",
        ))
        self.assertEqual(d.stance, FOLLOW_AND_ADD)


class DesireTests(unittest.TestCase):
    def test_the_most_floor_taking_offer_wins(self) -> None:
        d = decide(StanceInputs(
            blocks=frozenset({"wants_block", "turning_over_block"}),
            user_text="ok",
        ))
        self.assertEqual(d.desire, SHARE)
        self.assertEqual(d.reason, "turning_over_block")

    def test_the_shortlist_holds_one_entry_per_stance(self) -> None:
        # Two curiosity cues are one option with two backers, not two
        # options -- listing both would imply a weight there isn't.
        d = decide(StanceInputs(
            blocks=frozenset({
                "curiosity_seeds_block", "dormant_interest_block",
                "wants_block",
            }),
            user_text="mm",
        ))
        self.assertEqual([s for s, _ in d.shortlist], [ASK, FOLLOW_AND_ADD])

    def test_the_shortlist_is_ordered_most_floor_taking_first(self) -> None:
        d = decide(StanceInputs(
            blocks=frozenset({
                "wants_block", "initiative_block", "curiosity_seeds_block",
                "thread_ownership_block",
            }),
            user_text="nice",
        ))
        self.assertEqual(
            [s for s, _ in d.shortlist],
            [INITIATE, CALLBACK, ASK, FOLLOW_AND_ADD],
        )

    def test_an_unknown_block_offers_nothing(self) -> None:
        d = decide(StanceInputs(
            blocks=frozenset({"persona", "ambient", "axes_block"}),
            user_text="that makes sense, thanks for explaining it",
        ))
        self.assertEqual(d.stance, FOLLOW)
        self.assertEqual(d.reason, "no_offer")
        self.assertEqual(d.shortlist, ())

    def test_redirect_is_reachable(self) -> None:
        d = decide(StanceInputs(
            blocks=frozenset({"topic_appetite_block"}),
            user_text="yeah",
        ))
        self.assertEqual(d.stance, REDIRECT)


class ProtectedArcTests(unittest.TestCase):
    """H39: the veto is real, and it expires.

    ``arc`` is a conversation-level label -- 137 runs over 2,355 turns,
    mean length 17, not one run of length 1 -- so an untimed cap does not
    protect a moment, it removes a capability for the rest of the day.
    """

    def _share_turn(self, *, arc: str, age: int):
        return decide(StanceInputs(
            blocks=frozenset({"turning_over_block"}),
            user_text="that was a rough one honestly",
            arc=arc,
            arc_age_turns=age,
        ))

    def test_a_fresh_protected_arc_still_holds_her_back(self) -> None:
        d = self._share_turn(arc="support", age=0)
        self.assertEqual(d.desire, SHARE)
        self.assertEqual(d.stance, FOLLOW_AND_ADD)
        self.assertEqual(d.reason, "arc_protected")
        self.assertTrue(d.clamped)

    def test_the_veto_covers_the_whole_opening_beat(self) -> None:
        for age in range(stance_mod.PROTECTED_ARC_FRESH_TURNS):
            with self.subTest(age=age):
                self.assertEqual(
                    self._share_turn(arc="support", age=age).stance,
                    FOLLOW_AND_ADD,
                )

    def test_it_stops_once_the_arc_is_no_longer_fresh(self) -> None:
        d = self._share_turn(
            arc="support", age=stance_mod.PROTECTED_ARC_FRESH_TURNS,
        )
        self.assertEqual(d.stance, SHARE)
        self.assertFalse(d.clamped)

    def test_a_stale_arc_does_not_shield_a_live_vent(self) -> None:
        # The per-turn caps are untouched by the freshness window: they
        # describe what he is doing *now* rather than what the
        # conversation was about an hour ago.
        d = decide(StanceInputs(
            blocks=frozenset({"turning_over_block"}),
            user_text="i am so done with all of it",
            dialogue_act="vent",
            arc="support",
            arc_age_turns=99,
        ))
        self.assertEqual(d.stance, FOLLOW)
        self.assertEqual(d.reason, "vent")

    def test_the_window_can_be_switched_off_entirely(self) -> None:
        d = decide(
            StanceInputs(
                blocks=frozenset({"turning_over_block"}),
                user_text="mm that is rough",
                arc="support",
                arc_age_turns=0,
            ),
            protected_arc_turns=0,
        )
        self.assertEqual(d.stance, SHARE)

    def test_an_unprotected_arc_never_caps(self) -> None:
        d = self._share_turn(arc="playful", age=0)
        self.assertEqual(d.stance, SHARE)


class BrevityTests(unittest.TestCase):
    """The axis ``HOLD`` became: her verbosity, not the size of his turn."""

    def _turn(self, words: tuple[int, ...], **kw):
        return decide(StanceInputs(
            blocks=frozenset({"wants_block"}),
            user_text=kw.pop("user_text", "yeah that tracks"),
            recent_reply_words=words,
            **kw,
        ))

    def test_a_run_of_long_replies_asks_for_a_short_one(self) -> None:
        d = self._turn((55, 48))
        self.assertTrue(d.brevity)
        self.assertEqual(d.brevity_reason, "long_run")

    def test_one_long_reply_is_not_a_run(self) -> None:
        self.assertFalse(self._turn((55, 12)).brevity)

    def test_short_replies_leave_the_brake_off(self) -> None:
        self.assertFalse(self._turn((14, 20)).brevity)

    def test_too_little_history_leaves_the_brake_off(self) -> None:
        # Session start and post-restart both land here. The brake needs
        # evidence, not the absence of it.
        self.assertFalse(self._turn((99,)).brevity)
        self.assertFalse(self._turn(()).brevity)

    def test_a_direct_question_overrides_it(self) -> None:
        # Being asked something and answering in six words is a
        # non-answer, not restraint.
        d = self._turn((80, 80), user_text="so did the parcel arrive?")
        self.assertFalse(d.brevity)

    def test_brevity_is_orthogonal_to_the_stance(self) -> None:
        # The whole point of taking it off the ladder: she can bring
        # something of her own *and* be brief about it.
        d = decide(StanceInputs(
            blocks=frozenset({"turning_over_block"}),
            user_text="mm",
            recent_reply_words=(60, 60),
        ))
        self.assertEqual(d.stance, SHARE)
        self.assertTrue(d.brevity)

    def test_the_thresholds_are_tunable(self) -> None:
        words = (30, 30, 30)
        self.assertFalse(self._turn(words).brevity)
        d = decide(
            StanceInputs(
                blocks=frozenset({"wants_block"}),
                user_text="right",
                recent_reply_words=words,
            ),
            brevity_word_floor=25,
            brevity_run=3,
        )
        self.assertTrue(d.brevity)


class SequencingTests(unittest.TestCase):
    """K94 — the third axis, and the only one about shape.

    ``wants_block`` offers ``FOLLOW_AND_ADD``, which is the only rung
    where placement is a question at all.
    """

    def _turn(self, **kw) -> object:
        blocks = kw.pop("blocks", frozenset({"wants_block"}))
        return decide(StanceInputs(
            blocks=blocks,
            user_text=kw.pop("user_text", "the deploy finally went out"),
            last_reply_anaphoric=kw.pop("last_reply_anaphoric", True),
            **kw,
        ))

    def test_it_fires_on_a_follow_and_add_after_an_anaphoric_reply(
        self,
    ) -> None:
        d = self._turn()
        self.assertEqual(d.stance, stance_mod.FOLLOW_AND_ADD)
        self.assertTrue(d.sequencing)
        self.assertEqual(d.sequencing_reason, "anaphoric_run")

    def test_no_evidence_no_cue(self) -> None:
        """The cadence, and what makes this self-extinguishing.

        FOLLOW_AND_ADD is chosen on 45.7% of turns; a clause on all of
        them would be ambient by K92's own definition and formulaic by
        K94's own warning.
        """
        self.assertFalse(self._turn(last_reply_anaphoric=False).sequencing)

    def test_it_stands_down_when_K88_is_already_speaking(self) -> None:
        # style_pattern_block says the same thing off a twelve-turn
        # window. Two voices on one habit is the crowding K92 exists to
        # arbitrate, and the arbiter is handed the offer set to do it.
        d = self._turn(
            blocks=frozenset({"wants_block", "style_pattern_block"}),
        )
        self.assertTrue(d.sequencing is False)

    def test_it_does_not_fire_on_other_rungs(self) -> None:
        # SHARE and INITIATE already have a provider speaking for them,
        # and "answer him first" is not a coherent ask on a turn that is
        # not an answer.
        for block in ("turning_over_block", "initiative_block"):
            with self.subTest(block=block):
                d = self._turn(blocks=frozenset({block}))
                self.assertNotEqual(d.stance, stance_mod.FOLLOW_AND_ADD)
                self.assertFalse(d.sequencing)

    def test_a_clamped_turn_still_gets_it(self) -> None:
        """Resolved after the rung, which is the point of that ordering.

        A turn the ceiling pulled down from INITIATE to FOLLOW_AND_ADD is
        exactly a turn where placement advice is worth having.
        """
        d = decide(StanceInputs(
            blocks=frozenset({"initiative_block"}),
            user_text="did the parcel arrive?",
            last_reply_anaphoric=True,
        ))
        self.assertEqual(d.stance, stance_mod.FOLLOW_AND_ADD)
        self.assertEqual(d.desire, stance_mod.INITIATE)
        self.assertTrue(d.sequencing)

    def test_the_flag_turns_it_off(self) -> None:
        d = decide(
            StanceInputs(
                blocks=frozenset({"wants_block"}),
                user_text="the deploy went out",
                last_reply_anaphoric=True,
            ),
            sequencing_enabled=False,
        )
        self.assertFalse(d.sequencing)

    def test_it_is_orthogonal_to_brevity(self) -> None:
        d = decide(StanceInputs(
            blocks=frozenset({"wants_block"}),
            user_text="mm",
            recent_reply_words=(60, 60),
            last_reply_anaphoric=True,
        ))
        self.assertTrue(d.sequencing)
        self.assertTrue(d.brevity)


class SequencingRenderTests(unittest.TestCase):
    def _rendered(self, **kw) -> str:
        d = decide(StanceInputs(
            blocks=frozenset({"wants_block"}),
            user_text=kw.pop("user_text", "the deploy went out"),
            last_reply_anaphoric=kw.pop("last_reply_anaphoric", True),
            **kw,
        ))
        return stance_mod.render_block(d, user_display_name="Jacob")

    def test_it_asks_for_an_order_not_for_less(self) -> None:
        text = self._rendered()
        self.assertIn("first clause", text)
        self.assertIn("Jacob", text)
        # The one thing it must not read as: permission to under-answer.
        self.assertIn("Answer him fully", text)

    def test_it_never_asks_her_to_end_on_a_question(self) -> None:
        """Her question-ending rate is already 3.1%, from 14.3%.

        "Leave it open" read as "ask him something" would walk back into
        the interviewing pattern other features were built to suppress,
        so the closing clause is a statement he can pick up.
        """
        text = self._rendered().lower()
        self.assertNotIn("question", text)
        self.assertNotIn("ask him", text)

    def test_silent_without_the_cue(self) -> None:
        self.assertEqual(self._rendered(last_reply_anaphoric=False), "")

    def test_order_when_several_clauses_fire(self) -> None:
        # Each qualifies the one before: what she answers with, where her
        # own part goes, how long the whole thing runs.
        text = self._rendered(user_text="mm", recent_reply_words=(60, 60))
        self.assertLess(text.index("Shape for this reply"), text.index("run long"))


class ShortlistTextTests(unittest.TestCase):
    def test_the_log_column_pairs_stance_with_its_backer(self) -> None:
        d = decide(StanceInputs(
            blocks=frozenset({"initiative_block", "wants_block"}),
            user_text="hey",
        ))
        self.assertEqual(
            d.shortlist_text(),
            "INITIATE:initiative_block,FOLLOW_AND_ADD:wants_block",
        )


class RecorderTests(unittest.TestCase):
    """The post-turn seam, with the store stubbed."""

    def _host(self):
        from app.core.infra.agent_settings import AgentSettings
        from app.core.session.post_turn_helpers_mixin import (
            PostTurnHelpersMixin,
        )

        class Host(PostTurnHelpersMixin):
            def __init__(self) -> None:
                self._turn_stance_store = MagicMock()
                self._turn_stance_store.add_turn.return_value = True
                self._last_user_dialogue_act = None
                self._last_user_arc = None
                self._arc_age_turns = 0
                self._recent_reply_words: tuple[int, ...] = ()
                self._last_stance_decision = None
                self._settings = MagicMock()
                self._settings.agent = AgentSettings()

        return Host()

    def test_it_prefers_the_decision_the_assembler_rendered_from(
        self,
    ) -> None:
        """The ledger has to describe the prompt, not improve on it.

        By post-turn the reply-length window has grown and the act tagger
        has re-run, so a recomputation is a different question. Here the
        stashed decision disagrees with anything a recompute could
        produce, which is the only way to tell the two paths apart.
        """
        from app.core.conversation.stance import StanceDecision

        host = self._host()
        host._last_stance_decision = StanceDecision(
            stance=REDIRECT, reason="stashed", desire=REDIRECT,
            ceiling=INITIATE, brevity=True, brevity_reason="long_run",
        )
        telemetry = MagicMock()
        telemetry.block_chars = {"initiative_block": 400}
        host._record_turn_stance(
            assistant_message_id=5, telemetry=telemetry, user_text="hey",
        )
        _, decision = host._turn_stance_store.add_turn.call_args[0]
        self.assertEqual(decision.reason, "stashed")
        self.assertTrue(decision.brevity)

    def test_the_stash_is_consumed_so_it_cannot_describe_two_turns(
        self,
    ) -> None:
        from app.core.conversation.stance import StanceDecision

        host = self._host()
        host._last_stance_decision = StanceDecision(
            stance=REDIRECT, reason="stashed", desire=REDIRECT,
            ceiling=INITIATE,
        )
        telemetry = MagicMock()
        telemetry.block_chars = {"initiative_block": 400}
        host._record_turn_stance(
            assistant_message_id=5, telemetry=telemetry, user_text="hey",
        )
        host._record_turn_stance(
            assistant_message_id=6, telemetry=telemetry, user_text="hey",
        )
        _, second = host._turn_stance_store.add_turn.call_args[0]
        self.assertEqual(second.reason, "initiative_block")

    def test_it_records_the_decision_for_the_turn(self) -> None:
        host = self._host()
        telemetry = MagicMock()
        telemetry.block_chars = {"initiative_block": 400, "persona": 38000}
        host._record_turn_stance(
            assistant_message_id=77, telemetry=telemetry, user_text="hey",
        )
        host._turn_stance_store.add_turn.assert_called_once()
        message_id, decision = host._turn_stance_store.add_turn.call_args[0]
        self.assertEqual(message_id, 77)
        self.assertEqual(decision.stance, INITIATE)

    def test_empty_blocks_are_dropped_rather_than_recorded_as_follow(
        self,
    ) -> None:
        # Banter and aborted turns build no prompt. Recording one as a
        # FOLLOW she never had the chance to choose would put a turn in
        # every denominator that never had an assembly.
        host = self._host()
        telemetry = MagicMock()
        telemetry.block_chars = {}
        host._record_turn_stance(
            assistant_message_id=77, telemetry=telemetry, user_text="hey",
        )
        host._turn_stance_store.add_turn.assert_not_called()

    def test_zero_char_blocks_do_not_count_as_offers(self) -> None:
        # The assembler reports every registered block including the
        # empty ones; treating a zero as an offer would have the arbiter
        # reading steers that never rendered.
        host = self._host()
        telemetry = MagicMock()
        telemetry.block_chars = {"initiative_block": 0, "persona": 38000}
        host._record_turn_stance(
            assistant_message_id=77, telemetry=telemetry,
            user_text="that all makes sense to me, thank you",
        )
        _, decision = host._turn_stance_store.add_turn.call_args[0]
        self.assertEqual(decision.stance, FOLLOW)

    def test_a_missing_store_is_silent(self) -> None:
        host = self._host()
        host._turn_stance_store = None
        telemetry = MagicMock()
        telemetry.block_chars = {"wants_block": 200}
        host._record_turn_stance(
            assistant_message_id=77, telemetry=telemetry, user_text="hey",
        )  # must not raise


class StoreRoundTripTests(unittest.TestCase):
    """Every axis survives the write, against a real schema.

    The store hand-writes its column list, so a new axis is one
    miscounted placeholder away from either an exception or -- worse --
    values landing in the neighbouring column.
    """

    def _store(self):
        import tempfile
        from pathlib import Path

        from app.core.infra.chat_database import ChatDatabase
        from app.core.memory.turn_stance_store import TurnStanceStore

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = ChatDatabase(Path(tmp.name) / "chat.db")
        self.addCleanup(lambda: db._get_conn().close())
        return db, TurnStanceStore(db)

    def test_all_three_axes_round_trip(self) -> None:
        db, store = self._store()
        decision = decide(
            StanceInputs(
                blocks=frozenset({"wants_block"}),
                user_text="the deploy went out",
                recent_reply_words=(60, 60),
                last_reply_anaphoric=True,
            ),
        )
        self.assertTrue(decision.sequencing)
        self.assertTrue(decision.brevity)
        self.assertTrue(store.add_turn(11, decision))
        row = db._get_conn().execute(
            "SELECT stance, brevity, brevity_reason, sequencing, "
            "sequencing_reason FROM turn_stance WHERE "
            "assistant_message_id = 11"
        ).fetchone()
        self.assertEqual(row[0], stance_mod.FOLLOW_AND_ADD)
        self.assertEqual(row[1], 1)
        self.assertEqual(row[2], "long_run")
        self.assertEqual(row[3], 1)
        self.assertEqual(row[4], "anaphoric_run")

    def test_a_quiet_turn_writes_zeroes_not_nulls(self) -> None:
        db, store = self._store()
        decision = decide(StanceInputs(
            blocks=frozenset({"wants_block"}), user_text="hey",
        ))
        self.assertTrue(store.add_turn(12, decision))
        row = db._get_conn().execute(
            "SELECT sequencing, sequencing_reason FROM turn_stance "
            "WHERE assistant_message_id = 12"
        ).fetchone()
        self.assertEqual(row[0], 0)
        self.assertEqual(row[1], "")


class RenderTests(unittest.TestCase):
    """Phase 2's contract, which replaces phase 1's import-edge test.

    Phase 1 pinned "nothing that builds the prompt may import the
    arbiter". That was a statement about *when*, and its time is up. What
    survives it is the reason it existed: the arbiter must not become
    another voice asking her to speak.
    """

    def _decide(self, blocks: set[str], **kw):
        return decide(StanceInputs(
            blocks=frozenset(blocks),
            user_text=kw.pop("user_text", "mm"),
            **kw,
        ))

    def test_it_says_nothing_for_a_stance_that_already_has_a_provider(
        self,
    ) -> None:
        # The shipping test for the whole phase. Every rung above FOLLOW
        # has a block putting a sentence in the prompt already; a second
        # sentence agreeing with it is the eleventh permission slip.
        for block, stance in (
            ("initiative_block", INITIATE),
            ("topic_appetite_block", REDIRECT),
            ("turning_over_block", SHARE),
            ("thread_ownership_block", CALLBACK),
            ("curiosity_seeds_block", ASK),
            ("wants_block", FOLLOW_AND_ADD),
        ):
            with self.subTest(block=block):
                d = self._decide({block})
                self.assertEqual(d.stance, stance)
                self.assertEqual(render_block(d), "")

    def test_it_gives_following_a_voice(self) -> None:
        d = self._decide({"persona"}, user_text="yeah that makes sense")
        self.assertEqual(d.stance, FOLLOW)
        text = render_block(d, user_display_name="Jacob")
        self.assertIn("Jacob", text)
        self.assertNotIn("{", text)

    def test_the_follow_line_does_not_ask_her_to_add_anything(self) -> None:
        # A "following" cue that reads as "and also bring something" is
        # just the permission slip again, and the measured regression is
        # replies growing while own material fell.
        text = render_block(self._decide({"persona"}))
        self.assertNotIn("bring up", text.lower())

    def test_brevity_renders_on_its_own(self) -> None:
        d = decide(StanceInputs(
            blocks=frozenset({"wants_block"}),
            user_text="mm",
            recent_reply_words=(70, 70),
        ))
        self.assertEqual(d.stance, FOLLOW_AND_ADD)
        self.assertEqual(render_block(d).count("\n\n"), 0)
        self.assertIn("shorter", render_block(d))

    def test_both_clauses_can_land_and_brevity_goes_second(self) -> None:
        d = decide(StanceInputs(
            blocks=frozenset({"persona"}),
            user_text="yeah that makes sense",
            recent_reply_words=(70, 70),
        ))
        self.assertEqual(d.stance, FOLLOW)
        self.assertTrue(d.brevity)
        parts = render_block(d).split("\n\n")
        self.assertEqual(len(parts), 2)
        self.assertIn("Stance this turn", parts[0])
        self.assertIn("shorter", parts[1])

    def test_the_block_is_registered_in_the_tier_ladder(self) -> None:
        # Unregistered means ``block_char_table`` never measures it, so
        # it would be invisible to the report that judges this phase --
        # and to the arbiter's own offer set.
        from app.core.session.prompt_assembler import _PROMPT_BLOCK_TIERS

        self.assertIn("stance_block", _PROMPT_BLOCK_TIERS["T6_detectors"])

    def test_it_lands_last_so_it_can_see_every_steer(self) -> None:
        from app.core.session.prompt_assembler import _PROMPT_BLOCK_TIERS

        self.assertEqual(
            _PROMPT_BLOCK_TIERS["T6_detectors"][-1], "stance_block",
        )

    def test_the_stance_block_offers_no_stance_of_its_own(self) -> None:
        # It reports on the offers; if it were also an offer it would
        # feed itself on the next turn.
        self.assertNotIn("stance_block", stance_mod._OFFER_OF)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
