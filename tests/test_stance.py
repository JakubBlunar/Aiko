"""K92 phase 1 -- the stance arbiter, its ledger, and its silence.

Three things are worth pinning here, in descending order of how badly a
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
3. **Phase 1 renders nothing.** The contract is that the stance is
   recorded and never reaches the prompt.
"""
from __future__ import annotations

import re
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
    def test_the_ladder_runs_from_silence_to_taking_the_floor(self) -> None:
        # Order is the mechanism -- ``min`` over it is the clamp -- so a
        # reshuffle is a behaviour change and should fail loudly.
        self.assertEqual(
            STANCE_LADDER,
            (HOLD, FOLLOW, FOLLOW_AND_ADD, ASK, CALLBACK, SHARE,
             REDIRECT, INITIATE),
        )


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


class HoldTests(unittest.TestCase):
    def test_a_bare_backchannel_with_nothing_on_the_table_holds(self) -> None:
        d = decide(StanceInputs(blocks=frozenset({"persona"}), user_text="mhm"))
        self.assertEqual(d.stance, HOLD)
        self.assertEqual(d.reason, "no_offer_backchannel")

    def test_a_short_question_is_not_an_invitation_to_hold(self) -> None:
        d = decide(StanceInputs(blocks=frozenset(), user_text="you ok?"))
        self.assertEqual(d.stance, FOLLOW)

    def test_hold_is_unreachable_once_anything_is_offered(self) -> None:
        # Recorded because it is the finding, not an accident: over 432
        # replayed turns HOLD was chosen zero times, since some provider
        # is always offering something. The rule needs a different shape
        # before phase 2 can render it.
        d = decide(StanceInputs(
            blocks=frozenset({"wants_block"}), user_text="mhm",
        ))
        self.assertEqual(d.stance, FOLLOW_AND_ADD)


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
        from app.core.session.post_turn_helpers_mixin import (
            PostTurnHelpersMixin,
        )

        class Host(PostTurnHelpersMixin):
            def __init__(self) -> None:
                self._turn_stance_store = MagicMock()
                self._turn_stance_store.add_turn.return_value = True
                self._last_user_dialogue_act = None
                self._last_user_arc = None

        return Host()

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


class PhaseOneIsSilentTests(unittest.TestCase):
    def test_nothing_that_builds_the_prompt_imports_the_arbiter(self) -> None:
        """The phase-1 contract: computed, recorded, never rendered.

        Checked as an import edge rather than by reading a rendered
        prompt, because the failure worth guarding against is a later
        phase wiring the arbiter in while phase 1's baseline is still
        being collected -- at which point the numbers would describe a
        prompt the arbiter had already changed, and nothing would say so.

        Delete this test when phase 2 starts. It is a statement about
        *when*, not about what is allowed to exist.
        """
        from pathlib import Path

        import app.core.session.inner_life_part1 as part1
        import app.core.session.inner_life_part2 as part2
        import app.core.session.inner_life_part3 as part3
        import app.core.session.prompt_assembler as assembler

        # ``\b`` rather than a substring: ``stance_persistence`` is a
        # real sibling module that several of these legitimately import.
        edge = re.compile(r"conversation(?:\.|\s+import\s+)stance\b")
        for module in (assembler, part1, part2, part3):
            source = Path(module.__file__).read_text(encoding="utf-8")
            self.assertIsNone(
                edge.search(source),
                f"{module.__name__} imports the stance arbiter -- phase 1 "
                f"is shadow-only",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
