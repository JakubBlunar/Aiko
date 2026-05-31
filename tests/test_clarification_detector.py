"""Tests for the K17 clarification-repair detector."""
from __future__ import annotations

import unittest

from app.core.conversation import clarification_detector
from app.core.conversation.clarification_detector import (
    ClarificationResult,
    detect,
    render_inner_life_block,
)


class DetectStrongPatternsTests(unittest.TestCase):
    """Strong band: explicit corrections / repudiations of Aiko's last
    reply. The user is visibly steering -- Aiko should re-read.
    """

    def test_no_thats_not_what_i_meant(self) -> None:
        result = detect("no, that's not what I meant")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, "strong")
        self.assertIn("not what i meant", result.evidence.lower())

    def test_thats_not_what_i_meant_no_lead(self) -> None:
        result = detect("that's not what I meant at all")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, "strong")

    def test_you_misunderstood(self) -> None:
        result = detect("you misunderstood me")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, "strong")

    def test_youre_misunderstanding(self) -> None:
        result = detect("you're misunderstanding what I asked")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, "strong")

    def test_you_got_the_wrong(self) -> None:
        result = detect("you got the wrong idea")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, "strong")

    def test_i_meant_x_not_y(self) -> None:
        result = detect("I meant the morning routine, not the evening one")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, "strong")

    def test_no_im_asking(self) -> None:
        result = detect("no, I'm asking about Tokyo")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, "strong")

    def test_wait_no(self) -> None:
        result = detect("wait, no — go back")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, "strong")

    def test_thats_not_it(self) -> None:
        result = detect("that's not it, you're off")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, "strong")

    def test_missing_the_point(self) -> None:
        result = detect("you're missing the point here")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, "strong")


class DetectMildPatternsTests(unittest.TestCase):
    """Mild band: softer confusion / "I don't follow". Aiko should
    pause once and re-read before charging ahead.
    """

    def test_huh_question_mark(self) -> None:
        result = detect("huh?")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, "mild")

    def test_huh_double_question(self) -> None:
        result = detect("huh??")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, "mild")

    def test_wait_what(self) -> None:
        result = detect("wait, what?")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, "mild")

    def test_what_do_you_mean(self) -> None:
        result = detect("what do you mean by that")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, "mild")

    def test_i_dont_follow(self) -> None:
        result = detect("hmm, I don't follow")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, "mild")

    def test_im_confused(self) -> None:
        result = detect("I'm confused now")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, "mild")

    def test_doesnt_make_sense(self) -> None:
        result = detect("that doesn't make sense to me")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, "mild")


class DetectNonMatchTests(unittest.TestCase):
    """Negative cases: text that should NOT trigger the detector.
    These are the false-positive guardrails -- if any of these flip
    to a positive result, the regex is too greedy.
    """

    def test_empty_string(self) -> None:
        self.assertIsNone(detect(""))

    def test_whitespace_only(self) -> None:
        self.assertIsNone(detect("   \n  "))

    def test_normal_question(self) -> None:
        self.assertIsNone(detect("what time is it?"))

    def test_uh_huh_not_huh(self) -> None:
        # "uh huh" is agreement, not confusion -- the regex requires
        # "huh" to be preceded by a non-letter (or start-of-string)
        # AND followed by "?". "uh huh" with no ? must not fire.
        self.assertIsNone(detect("uh huh"))

    def test_no_alone_does_not_fire(self) -> None:
        # A bare "no" without context is not a clarification beat.
        # The strong patterns all require additional structure.
        self.assertIsNone(detect("no"))

    def test_normal_chitchat(self) -> None:
        self.assertIsNone(detect("yeah, that's cool"))

    def test_meant_without_not(self) -> None:
        # "I meant well" should not trip the "i meant X not Y" pattern.
        self.assertIsNone(detect("I meant well, sorry"))


class StrongBeatsMildTests(unittest.TestCase):
    """When both bands match, ``strong`` wins."""

    def test_strong_pattern_with_mild_in_same_text(self) -> None:
        # "no that's not what I meant, I'm confused" reads as strong.
        result = detect("no that's not what I meant, I'm confused")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.band, "strong")


class EvidenceTrimTests(unittest.TestCase):
    """The ``evidence`` field is the matched phrase, trimmed if long."""

    def test_evidence_is_matched_phrase(self) -> None:
        result = detect("you misunderstood me here")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIn("misunderstood", result.evidence.lower())

    def test_long_match_truncated_with_ellipsis(self) -> None:
        # Construct a text where the regex match itself is long
        # enough to need trimming. The "I meant X not Y" pattern is
        # the easiest to push long because it accepts up to 80 chars
        # between "meant" and "not".
        text = (
            "I meant the part where we were talking about the new framework "
            "for the auth system not the old one"
        )
        result = detect(text)
        self.assertIsNotNone(result)
        assert result is not None
        # Bounded; either fits the cap or is truncated with ellipsis.
        self.assertLessEqual(len(result.evidence), 81)


class RenderInnerLifeBlockTests(unittest.TestCase):
    """The cue rendered into the prompt has two flavours and quotes
    the evidence so the LLM sees what tripped the detector.
    """

    def test_strong_band_rendering(self) -> None:
        result = ClarificationResult(
            band="strong",
            evidence="that's not what I meant",
        )
        block = render_inner_life_block(result, user_display_name="Jacob")
        self.assertIn("Heads-up", block)
        self.assertIn("Jacob", block)
        self.assertIn("missed his last point", block)
        # The trigger evidence is quoted so the LLM can ground the
        # cue in the actual signal.
        self.assertIn("that's not what I meant", block)
        # Re-read instruction lands.
        self.assertIn("Re-read", block)

    def test_mild_band_rendering(self) -> None:
        result = ClarificationResult(band="mild", evidence="huh?")
        block = render_inner_life_block(result, user_display_name="Jacob")
        self.assertIn("Heads-up", block)
        self.assertIn("Jacob", block)
        self.assertIn("confused", block)
        # Mild flavour pauses + checks; doesn't tell Aiko to re-read
        # the prior two messages (that's the strong-band beat).
        self.assertNotIn("Re-read his last two", block)

    def test_evidence_optional(self) -> None:
        # Defensive — if evidence is empty the block still renders
        # without a bare "" fragment.
        result = ClarificationResult(band="mild", evidence="")
        block = render_inner_life_block(result, user_display_name="Jacob")
        self.assertNotIn('("")', block)
        self.assertNotIn("()", block)


if __name__ == "__main__":
    unittest.main()
