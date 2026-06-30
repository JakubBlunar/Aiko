"""Pure-module tests for K69 implicit-need reading.

Covers the response-mode classifier (witness / problem_solve / reassure /
celebrate / neutral), the restraint tie-break, the affect/arc corroboration,
and the render. No I/O, no controller -- runs in milliseconds.
"""
from __future__ import annotations

import unittest

from app.core.conversation import implicit_need as n


class WitnessTests(unittest.TestCase):
    def test_clear_vent_is_witness(self) -> None:
        r = n.classify("ugh today was the worst day, I'm so done with all of it")
        self.assertEqual(r.mode, n.MODE_WITNESS)

    def test_just_need_to_vent(self) -> None:
        r = n.classify("I just need to vent for a sec, work was brutal")
        self.assertEqual(r.mode, n.MODE_WITNESS)

    def test_low_mood_corroborates_soft_vent(self) -> None:
        # "ugh" alone is weight 1.0 (below floor); a low mood read pushes
        # it over.
        r = n.classify("ugh", perceived_mood="low")
        self.assertEqual(r.mode, n.MODE_WITNESS)


class ProblemSolveTests(unittest.TestCase):
    def test_explicit_advice_request(self) -> None:
        r = n.classify("how do I fix this merge conflict?")
        self.assertEqual(r.mode, n.MODE_PROBLEM_SOLVE)

    def test_what_should_i(self) -> None:
        r = n.classify("what should I do about my landlord raising the rent?")
        self.assertEqual(r.mode, n.MODE_PROBLEM_SOLVE)

    def test_strong_request_beats_mild_vent(self) -> None:
        # "how do I" (2.0) outweighs "awful" venting flavour -> fix.
        r = n.classify("my code is awful, how do I refactor this mess?")
        self.assertEqual(r.mode, n.MODE_PROBLEM_SOLVE)


class ReassureTests(unittest.TestCase):
    def test_anxiety(self) -> None:
        r = n.classify("I'm so anxious about the interview tomorrow")
        self.assertEqual(r.mode, n.MODE_REASSURE)

    def test_overthinking(self) -> None:
        r = n.classify("I can't stop thinking I messed everything up")
        self.assertEqual(r.mode, n.MODE_REASSURE)

    def test_vocal_anxious_corroborates(self) -> None:
        r = n.classify("what if it goes wrong", vocal_tags=("anxious",))
        self.assertEqual(r.mode, n.MODE_REASSURE)


class CelebrateTests(unittest.TestCase):
    def test_got_the_job(self) -> None:
        r = n.classify("I got the job!!!")
        self.assertEqual(r.mode, n.MODE_CELEBRATE)

    def test_good_news(self) -> None:
        r = n.classify("guess what, great news -- we won the pitch!")
        self.assertEqual(r.mode, n.MODE_CELEBRATE)

    def test_exclaim_only_amplifies_existing_celebrate(self) -> None:
        # A venting message with exclamations must NOT read as celebrate.
        r = n.classify("I'm so done with this!!!", perceived_mood="low")
        self.assertEqual(r.mode, n.MODE_WITNESS)


class NeutralTests(unittest.TestCase):
    def test_plain_question_is_neutral(self) -> None:
        r = n.classify("what time is it in Tokyo?")
        self.assertEqual(r.mode, n.MODE_NEUTRAL)

    def test_empty_is_neutral(self) -> None:
        self.assertEqual(n.classify("").mode, n.MODE_NEUTRAL)
        self.assertEqual(n.classify("   ").mode, n.MODE_NEUTRAL)

    def test_smalltalk_is_neutral(self) -> None:
        self.assertEqual(n.classify("lol nice").mode, n.MODE_NEUTRAL)

    def test_single_soft_cue_below_floor_stays_neutral(self) -> None:
        # "what if" alone (weight 1.0) is below the 2.0 floor.
        self.assertEqual(n.classify("what if").mode, n.MODE_NEUTRAL)


class RestraintTieBreakTests(unittest.TestCase):
    def test_witness_wins_tie_over_problem_solve(self) -> None:
        # Construct an equal-score tie via arc + a weak problem cue vs a
        # witness cue; restraint -> witness.
        r = n.classify(
            "what do I do, this is so frustrating",
        )
        # "what do i do" (1.0) + "so frustrat" (2.0 witness). Witness wins.
        self.assertEqual(r.mode, n.MODE_WITNESS)

    def test_arc_is_weak_prior_only(self) -> None:
        # A planning arc must not flip a clear vent into problem_solve.
        r = n.classify(
            "ugh I'm so done, what a terrible week", arc="planning",
        )
        self.assertEqual(r.mode, n.MODE_WITNESS)

    def test_arc_breaks_genuine_tie(self) -> None:
        # With no lexical signal, a support arc nudges toward witness but
        # stays below the floor (0.5 < 2.0) -> still neutral (restraint).
        r = n.classify("hm", arc="support")
        self.assertEqual(r.mode, n.MODE_NEUTRAL)


class MinConfidenceTests(unittest.TestCase):
    def test_lower_floor_lets_soft_cue_fire(self) -> None:
        r = n.classify("what if it all goes wrong", min_confidence=1.0)
        self.assertEqual(r.mode, n.MODE_REASSURE)


class RenderTests(unittest.TestCase):
    def test_neutral_renders_empty(self) -> None:
        r = n.NeedResult(n.MODE_NEUTRAL, 0.0, {}, ())
        self.assertEqual(n.render_inner_life_block(r), "")

    def test_none_renders_empty(self) -> None:
        self.assertEqual(n.render_inner_life_block(None), "")

    def test_witness_render_mentions_heard_not_fixed(self) -> None:
        r = n.NeedResult(n.MODE_WITNESS, 3.0, {}, ())
        out = n.render_inner_life_block(r, user_display_name="Jacob")
        self.assertIn("Jacob", out)
        self.assertIn("heard", out)

    def test_celebrate_render_mentions_match_the_high(self) -> None:
        r = n.NeedResult(n.MODE_CELEBRATE, 3.0, {}, ())
        out = n.render_inner_life_block(r)
        self.assertIn("Match the high", out)

    def test_each_mode_renders_nonempty(self) -> None:
        for mode in (
            n.MODE_WITNESS,
            n.MODE_PROBLEM_SOLVE,
            n.MODE_REASSURE,
            n.MODE_CELEBRATE,
        ):
            out = n.render_inner_life_block(n.NeedResult(mode, 3.0, {}, ()))
            self.assertTrue(out)


if __name__ == "__main__":
    unittest.main()
