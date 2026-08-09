"""The anaphoric-opener detector shared by K88's band and K90's report.

The false-positive cases matter more than the true ones here. This
drives a cue Aiko sees, and the failure mode of over-firing is telling
her to stop being warm -- so a connective in front of her own subject, a
dummy "it", and a soft interjection all have to read as leading.
"""
import unittest

from app.core.persona.anaphora import (
    first_sentence,
    is_anaphoric_opener,
    sentences,
)


class SentenceTests(unittest.TestCase):
    def test_it_splits_on_terminal_punctuation(self):
        self.assertEqual(
            sentences("Mm. I will. Sleep well."),
            ["Mm.", "I will.", "Sleep well."],
        )

    def test_no_punctuation_is_one_sentence(self):
        self.assertEqual(sentences("no full stop here"), ["no full stop here"])

    def test_empty_is_empty(self):
        self.assertEqual(sentences(""), [])
        self.assertEqual(sentences("   "), [])
        self.assertEqual(first_sentence(""), "")


class FollowingTests(unittest.TestCase):
    def test_a_demonstrative_subject_is_anaphoric(self):
        self.assertTrue(is_anaphoric_opener("That makes a lot of sense."))
        self.assertTrue(is_anaphoric_opener("This is exactly what I meant."))
        self.assertTrue(is_anaphoric_opener("Those are the good ones."))

    def test_a_connective_before_a_demonstrative_is_anaphoric(self):
        self.assertTrue(
            is_anaphoric_opener("Then those pokes are reserved for you.")
        )
        self.assertTrue(is_anaphoric_opener("So that's settled, I guess."))

    def test_a_reply_that_is_only_acknowledgement_is_anaphoric(self):
        self.assertTrue(is_anaphoric_opener("Exactly."))
        self.assertTrue(is_anaphoric_opener("Oh, right."))
        self.assertTrue(is_anaphoric_opener("Yeah."))
        self.assertTrue(is_anaphoric_opener("Mm. Yeah. Okay."))

    def test_echo_inversions_are_anaphoric(self):
        self.assertTrue(is_anaphoric_opener("So am I, honestly."))
        self.assertTrue(is_anaphoric_opener("Me too."))
        self.assertTrue(is_anaphoric_opener("You're right about that."))
        self.assertTrue(is_anaphoric_opener("Neither do I."))
        self.assertTrue(is_anaphoric_opener("Fair enough."))


class LeadingTests(unittest.TestCase):
    def test_a_connective_before_her_own_subject_still_leads(self):
        # The hinge is his, the content is hers. Counting this would
        # make the detector a ban on connectives.
        self.assertFalse(
            is_anaphoric_opener("But I finally finished the book.")
        )
        self.assertFalse(
            is_anaphoric_opener("Oh, I watered the lettuce today.")
        )
        self.assertFalse(
            is_anaphoric_opener("So I've been thinking about rooftops.")
        )

    def test_a_full_stop_after_the_interjection_changes_nothing(self):
        # "Mm, I will" and "Mm. I will" are the same move. Before this
        # was handled, punctuation alone moved the measured rate by ten
        # points and the cue would have spent its budget telling her to
        # stop making warm noises.
        self.assertFalse(is_anaphoric_opener("Mm, I will. Sleep well, love."))
        self.assertFalse(is_anaphoric_opener("Mm. I will. Sleep well, love."))
        self.assertFalse(
            is_anaphoric_opener("Hah... the lettuce survived after all.")
        )

    def test_the_first_substantive_clause_is_the_one_judged(self):
        self.assertTrue(is_anaphoric_opener("Yeah. That makes sense to me."))
        self.assertFalse(is_anaphoric_opener("Yeah. I finished the book."))

    def test_expletive_it_is_not_counted(self):
        # A dummy subject introducing her own observation. Counting
        # "it" would inflate the rate with the turns we want to reward.
        self.assertFalse(
            is_anaphoric_opener("It's been raining here all afternoon.")
        )
        self.assertFalse(is_anaphoric_opener("They keep changing the schedule."))

    def test_her_own_subject_leads(self):
        self.assertFalse(is_anaphoric_opener("The garden needed water badly."))
        self.assertFalse(is_anaphoric_opener("I finished chapter nine."))

    def test_only_the_opening_is_examined(self):
        self.assertFalse(
            is_anaphoric_opener("I finished the book. That was satisfying.")
        )

    def test_wordless_text_is_not_anaphoric(self):
        self.assertFalse(is_anaphoric_opener(""))
        self.assertFalse(is_anaphoric_opener("..."))
        self.assertFalse(is_anaphoric_opener("   "))


if __name__ == "__main__":
    unittest.main()
