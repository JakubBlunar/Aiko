"""K90: the lead/follow measurements.

The anaphoric-opener detector carries most of the weight here -- it is
the tell the whole family is being judged against, and K88's cue fires
on it -- so the false-positive cases (a connective followed by her own
subject, expletive "it") get as much attention as the true ones.
"""
import unittest

from app.core.persona.lead_follow_metrics import (
    LeadFollowSummary,
    as_dict,
    brings_own_material,
    first_sentence,
    is_anaphoric_opener,
    is_measurable,
    measure_turn,
    opener_echo,
    own_material_ratio,
    own_material_words,
    summarise,
)


class FirstSentenceTests(unittest.TestCase):
    def test_splits_on_terminal_punctuation(self):
        self.assertEqual(
            first_sentence("Yeah, that lands. I've been thinking about it."),
            "Yeah, that lands.",
        )

    def test_a_single_sentence_is_returned_whole(self):
        self.assertEqual(first_sentence("no full stop here"), "no full stop here")

    def test_empty_input_is_empty(self):
        self.assertEqual(first_sentence(""), "")
        self.assertEqual(first_sentence("   "), "")


class AnaphoricOpenerTests(unittest.TestCase):
    def test_demonstrative_subject_is_anaphoric(self):
        self.assertTrue(is_anaphoric_opener("That makes a lot of sense."))
        self.assertTrue(is_anaphoric_opener("This is exactly what I meant."))
        self.assertTrue(is_anaphoric_opener("Those are the good ones."))

    def test_connective_plus_demonstrative_is_anaphoric(self):
        self.assertTrue(
            is_anaphoric_opener("Then those pokes are reserved for you.")
        )
        self.assertTrue(is_anaphoric_opener("So that's settled, I guess."))

    def test_pure_acknowledgement_is_anaphoric(self):
        self.assertTrue(is_anaphoric_opener("Exactly."))
        self.assertTrue(is_anaphoric_opener("Oh, right."))
        self.assertTrue(is_anaphoric_opener("Yeah."))
        self.assertTrue(is_anaphoric_opener("Fair enough."))

    def test_echo_inversions_are_anaphoric(self):
        self.assertTrue(is_anaphoric_opener("So am I, honestly."))
        self.assertTrue(is_anaphoric_opener("Me too."))
        self.assertTrue(is_anaphoric_opener("You're right about that."))
        self.assertTrue(is_anaphoric_opener("Neither do I."))

    def test_a_connective_before_her_own_subject_still_leads(self):
        # The hinge is his, the content is hers. Counting this would
        # make the metric a ban on connectives.
        self.assertFalse(is_anaphoric_opener("But I finally finished the book."))
        self.assertFalse(is_anaphoric_opener("Oh, I watered the lettuce today."))
        self.assertFalse(is_anaphoric_opener("So I've been thinking about rooftops."))

    def test_expletive_it_is_not_counted(self):
        # A dummy subject introducing her own observation. Counting
        # "it" would inflate the rate with the turns we want to reward.
        self.assertFalse(is_anaphoric_opener("It's been raining here all afternoon."))
        self.assertFalse(is_anaphoric_opener("They keep changing the schedule."))

    def test_her_own_subject_leads(self):
        self.assertFalse(is_anaphoric_opener("The garden needed water badly."))
        self.assertFalse(is_anaphoric_opener("I finished chapter nine."))

    def test_only_the_first_sentence_is_examined(self):
        self.assertFalse(
            is_anaphoric_opener("I finished the book. That was satisfying.")
        )
        self.assertTrue(
            is_anaphoric_opener("That was satisfying. I finished the book.")
        )

    def test_empty_is_not_anaphoric(self):
        self.assertFalse(is_anaphoric_opener(""))
        self.assertFalse(is_anaphoric_opener("..."))


class OpenerEchoTests(unittest.TestCase):
    def test_full_echo_scores_one(self):
        self.assertEqual(
            opener_echo(
                "Deployment pipeline trouble.",
                "the deployment pipeline is giving me trouble",
            ),
            1.0,
        )

    def test_no_overlap_scores_zero(self):
        self.assertEqual(
            opener_echo("Lettuce needed water.", "the deployment broke again"),
            0.0,
        )

    def test_contentless_opener_is_none_not_zero(self):
        # "Yeah." echoed nothing, but that is a different failure from
        # parroting and scoring it 0.0 would read as a good number.
        self.assertIsNone(opener_echo("Yeah.", "the deployment broke"))

    def test_a_contentless_user_turn_scores_zero(self):
        self.assertEqual(opener_echo("Lettuce needed water.", ""), 0.0)

    def test_partial_overlap_is_a_fraction(self):
        value = opener_echo(
            "Deployment trouble and rooftop weather.",
            "the deployment is giving me trouble",
        )
        assert value is not None
        self.assertGreater(value, 0.0)
        self.assertLess(value, 1.0)


class GenericVocabularyTests(unittest.TestCase):
    """The filter that keeps this from measuring vocabulary novelty."""

    def test_generic_words_are_not_material(self):
        # Measured on the live log before this filter existed, these are
        # exactly the words that pushed the rate to 95%.
        fresh = own_material_words(
            "That sounds like a good kind of thing, honestly.",
            "how was work",
        )
        self.assertEqual(fresh, set())

    def test_contractions_are_not_material(self):
        fresh = own_material_words("You're sure it's fine? I'll wait.", "hey")
        self.assertEqual(fresh, set())

    def test_a_real_noun_survives(self):
        fresh = own_material_words("The lettuce recovered nicely.", "hey")
        self.assertIn("lettuce", fresh)


class OwnMaterialTests(unittest.TestCase):
    def test_words_absent_from_both_sides_are_hers(self):
        fresh = own_material_words(
            "The lettuce finally recovered.",
            "how was the deployment",
            ["nothing about plants here"],
        )
        self.assertIn("lettuce", fresh)
        self.assertNotIn("deployment", fresh)

    def test_history_suppresses_a_repeat(self):
        fresh = own_material_words(
            "The lettuce finally recovered.",
            "how was the deployment",
            ["we talked about the lettuce yesterday"],
        )
        self.assertNotIn("lettuce", fresh)

    def test_a_reply_that_only_restates_him_adds_nothing(self):
        self.assertFalse(
            brings_own_material(
                "The deployment pipeline sounds rough.",
                "the deployment pipeline is rough today",
            )
        )

    def test_a_reply_carrying_its_own_detail_counts(self):
        self.assertTrue(
            brings_own_material(
                "Rough. Meanwhile the lettuce recovered and the paperback "
                "reached its last chapter.",
                "the deployment pipeline is rough today",
            )
        )

    def test_a_lone_novel_word_is_an_aside(self):
        self.assertFalse(
            brings_own_material(
                "The deployment pipeline trouble on the staging cluster, "
                "the rollback scripts, that timeout — and lettuce.",
                "the deployment pipeline trouble on the staging cluster, "
                "the rollback scripts, and that timeout",
            )
        )

    def test_three_new_words_buried_in_a_restatement_miss_the_ratio(self):
        # Clears the word floor, fails the share gate: most of what she
        # said was still his.
        self.assertFalse(
            brings_own_material(
                "The deployment pipeline trouble on the staging cluster, "
                "the rollback scripts, that timeout — lettuce, garden, rooftop.",
                "the deployment pipeline trouble on the staging cluster, "
                "the rollback scripts, and that timeout",
            )
        )

    def test_a_short_reply_with_two_new_words_is_below_the_floor(self):
        self.assertFalse(
            brings_own_material("Lettuce recovered.", "how was work"),
        )

    def test_a_contentless_reply_adds_nothing(self):
        self.assertFalse(brings_own_material("Yeah.", "how was work"))


class OwnMaterialRatioTests(unittest.TestCase):
    def test_a_pure_restatement_scores_zero(self):
        self.assertEqual(
            own_material_ratio(
                "Deployment pipeline trouble.",
                "the deployment pipeline is giving me trouble",
            ),
            0.0,
        )

    def test_an_entirely_fresh_reply_scores_one(self):
        self.assertEqual(
            own_material_ratio(
                "The lettuce recovered.", "the deployment broke",
            ),
            1.0,
        )

    def test_a_contentless_reply_has_no_ratio(self):
        self.assertIsNone(own_material_ratio("Yeah, exactly.", "how was work"))

    def test_history_counts_against_the_ratio(self):
        with_history = own_material_ratio(
            "The lettuce recovered.",
            "the deployment broke",
            ["the lettuce was struggling yesterday"],
        )
        self.assertEqual(with_history, 0.5)


class MeasurableTests(unittest.TestCase):
    def test_one_word_reactions_are_not_measurable(self):
        self.assertFalse(is_measurable("yeah."))
        self.assertFalse(is_measurable(""))

    def test_a_real_reply_is_measurable(self):
        self.assertTrue(is_measurable("yeah, that tracks"))


class MeasureTurnTests(unittest.TestCase):
    def test_it_carries_every_signal(self):
        metrics = measure_turn(
            "That makes sense. How did the rest of it go?",
            "the deployment finally worked",
        )
        self.assertTrue(metrics.anaphoric_opener)
        self.assertTrue(metrics.ends_with_question)
        self.assertEqual(metrics.question_count, 1)
        self.assertEqual(metrics.opener, "that")
        self.assertGreater(metrics.word_count, 5)

    def test_a_leading_turn_scores_the_other_way(self):
        metrics = measure_turn(
            "I spent the afternoon in the garden and the lettuce recovered.",
            "how was work",
        )
        self.assertFalse(metrics.anaphoric_opener)
        self.assertFalse(metrics.ends_with_question)
        assert metrics.own_material is not None
        self.assertGreater(metrics.own_material, 0.5)


class SummariseTests(unittest.TestCase):
    def _turns(self):
        return [
            measure_turn("That makes sense. Did it hold?", "the deploy worked"),
            measure_turn("Exactly.", "the deploy worked"),
            measure_turn(
                "I spent the afternoon watering lettuce in the garden.",
                "the deploy worked",
            ),
            measure_turn("Yeah, that tracks.", "the deploy worked"),
            measure_turn("That was a long week for you.", "the deploy worked"),
        ]

    def test_rates_are_fractions_of_the_turn_count(self):
        summary = summarise(self._turns())
        self.assertEqual(summary.turns, 5)
        self.assertEqual(summary.question_end_rate, 0.2)
        self.assertEqual(summary.anaphoric_opener_rate, 0.8)
        self.assertGreater(summary.mean_words, 0.0)
        self.assertGreater(summary.median_words, 0.0)

    def test_contentless_openers_are_excluded_from_the_echo_mean(self):
        summary = summarise(self._turns())
        # "Exactly." echoed nothing because it said nothing; averaging
        # it in as a zero would read as a good parroting score.
        self.assertLess(summary.opener_echo_turns, summary.turns)

    def test_top_openers_are_ranked(self):
        summary = summarise(self._turns())
        self.assertEqual(summary.top_openers[0][0], "that")

    def test_contentless_replies_are_excluded_from_the_material_mean(self):
        turns = [
            measure_turn("Yeah, exactly.", "how was the deploy"),
            measure_turn("The lettuce recovered nicely.", "how was the deploy"),
        ]
        summary = summarise(turns)
        self.assertEqual(summary.turns, 2)
        self.assertEqual(summary.own_material_turns, 1)
        self.assertEqual(summary.mean_own_material, 1.0)

    def test_an_empty_run_is_all_zeroes_not_a_crash(self):
        summary = summarise([])
        self.assertIsInstance(summary, LeadFollowSummary)
        self.assertEqual(summary.turns, 0)
        self.assertEqual(summary.mean_words, 0.0)
        self.assertEqual(summary.top_openers, ())

    def test_as_dict_is_json_shaped(self):
        payload = as_dict(summarise(self._turns()))
        self.assertEqual(payload["turns"], 5)
        self.assertIsInstance(payload["top_openers"], list)
        self.assertIn("opener", payload["top_openers"][0])


if __name__ == "__main__":
    unittest.main()
