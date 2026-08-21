"""Her transcript has to be able to hold the words she said.

Reported after a desktop reload showed a message the phone had rendered
correctly minutes earlier: *"Rain is very likely tonight, sweetheart,
about a 97% chance in Kamenn Poruba. Around 17 to 25C too"*. Not mojibake
-- the characters were **gone**, and had been for the entire history: 448k
characters of stored replies with not one non-ASCII character among them,
while her memories and concepts over the same period held accents, em
dashes and a euro sign.

The cause was a printable-ASCII whitelist in ``sanitize_assistant_text``,
whose only consumer is the transcript. The spoken copy is cleaned from raw
model text by ``prepare_tts_text``, so nothing was being protected. These
tests pin both halves: what must now survive, and what must still not.
"""
from __future__ import annotations

import unicodedata
import unittest

from app.core.session.session_text_utils import (
    prepare_tts_text,
    sanitize_assistant_text,
)


class TheReportedLineTests(unittest.TestCase):
    def test_the_place_name_keeps_its_accent(self) -> None:
        self.assertEqual(
            sanitize_assistant_text("a 97% chance in Kamenn\u00e1 Poruba."),
            "a 97% chance in Kamenn\u00e1 Poruba.",
        )

    def test_the_temperature_keeps_its_degree_sign(self) -> None:
        self.assertEqual(
            sanitize_assistant_text("Around 17 to 25\u00b0C too"),
            "Around 17 to 25\u00b0C too",
        )

    def test_the_whole_line_round_trips(self) -> None:
        line = (
            "Rain is very likely tonight, sweetheart, about a 97% chance "
            "in Kamenn\u00e1 Poruba. Around 17 to 25\u00b0C too, so the "
            "colder air comes with proper rainy-night atmosphere."
        )
        self.assertEqual(sanitize_assistant_text(line), line)


class WhatMustSurviveTests(unittest.TestCase):
    """Everything her memories and concepts have been storing all along."""

    def test_accented_letters(self) -> None:
        for word in ("Kamenn\u00e1", "caf\u00e9", "\u010cadca", "na\u00efve"):
            with self.subTest(word=word):
                self.assertEqual(sanitize_assistant_text(word), word)

    def test_symbols_that_carry_meaning(self) -> None:
        for text in ("25\u00b0C", "\u20ac40", "\u00a35", "\u00bd", "40\u2103"):
            with self.subTest(text=text):
                self.assertEqual(
                    sanitize_assistant_text(text),
                    # NFKC folds a few of these onto their plain spellings;
                    # what matters is that nothing is *deleted*.
                    unicodedata.normalize("NFKC", text),
                )

    def test_the_degree_sign_is_not_a_compatibility_character(self) -> None:
        # The one from the report, so pin it exactly rather than through
        # a normalisation the assertion could hide behind.
        self.assertEqual(sanitize_assistant_text("25\u00b0C"), "25\u00b0C")

    def test_non_latin_scripts(self) -> None:
        # She discusses anime by name; a Japanese title is not corruption.
        for text in ("\u3042\u306e\u82b1", "\u041c\u043e\u0441\u043a\u0432\u0430"):
            with self.subTest(text=text):
                self.assertEqual(sanitize_assistant_text(text), text)

    def test_a_combining_accent_is_kept_as_written(self) -> None:
        # NFKC composes this, so it arrives as one codepoint either way --
        # what matters is that neither the mark nor its base is dropped.
        composed = sanitize_assistant_text("a\u0301")
        self.assertEqual(composed, "\u00e1")


class NfkcFoldsSomeThingsTests(unittest.TestCase):
    """What NFKC costs, written down so nobody rediscovers it as a bug.

    NFKC applies *compatibility* mappings, which is why it folds a
    non-breaking space onto a real one and a fullwidth digit onto ASCII --
    both wanted for a transcript. The same pass flattens superscripts, so
    an area comes out as "m2" rather than "m2" with the exponent. That is a
    genuine if small loss, and it is the price of the NBSP fold: NFC would
    keep the exponent and also keep the NBSP, which shows up as a bubble
    wrapping where nothing else does.

    Living with it because the exponent is rare in her replies and "m2"
    still reads, where a stray NBSP is a layout bug nobody would trace
    back to here. Revisit by switching to NFC and folding the spaces by
    hand if superscripts ever start mattering.
    """

    def test_a_superscript_loses_its_exponent(self) -> None:
        self.assertEqual(sanitize_assistant_text("50 m\u00b2"), "50 m2")

    def test_a_micro_sign_becomes_greek_mu(self) -> None:
        self.assertEqual(sanitize_assistant_text("50\u00b5s"), "50\u03bcs")

    def test_a_fullwidth_digit_becomes_ascii(self) -> None:
        self.assertEqual(sanitize_assistant_text("\uff12\uff15"), "25")


class WhatMustStillGoTests(unittest.TestCase):
    def test_emoji_are_still_refused(self) -> None:
        self.assertEqual(sanitize_assistant_text("night \U0001F31D"), "night")

    def test_zero_width_joiner_goes(self) -> None:
        self.assertEqual(sanitize_assistant_text("a\u200db"), "ab")

    def test_bidi_override_goes(self) -> None:
        self.assertEqual(sanitize_assistant_text("safe\u202etxt"), "safetxt")

    def test_control_characters_go(self) -> None:
        self.assertEqual(sanitize_assistant_text("a\x00\x07b"), "ab")

    def test_line_separator_does_not_become_a_paragraph(self) -> None:
        self.assertEqual(sanitize_assistant_text("a\u2028b"), "ab")

    def test_non_breaking_space_folds_to_a_real_space(self) -> None:
        # NFKC's job, and worth pinning: an NBSP from tool output should not
        # survive as one, or a bubble wraps in a place nothing else does.
        self.assertEqual(sanitize_assistant_text("17\u00a0C"), "17 C")


class TheSubstitutionsStillApplyTests(unittest.TestCase):
    """Kept deliberately: these lose nothing and read better."""

    def test_curly_quotes_become_straight(self) -> None:
        self.assertEqual(
            sanitize_assistant_text("\u201cdon\u2019t\u201d"), '"don\'t"'
        )

    def test_em_dash_becomes_a_spaced_hyphen(self) -> None:
        self.assertEqual(
            sanitize_assistant_text("Yes\u2014that works"), "Yes - that works"
        )


class TheSpokenCopyIsUnaffectedTests(unittest.TestCase):
    """The split this bug came from confusing.

    ``sanitize_assistant_text`` is storage; ``prepare_tts_text`` is speech.
    Widening the first must not put anything new in front of the engine --
    and it cannot, because the spoken path never reads the first one's
    output on a normal turn.
    """

    def test_the_two_paths_disagree_on_purpose(self) -> None:
        line = "Around 17 to 25\u00b0C in Kamenn\u00e1 Poruba \U0001F31D"
        stored = sanitize_assistant_text(line)
        self.assertIn("\u00b0", stored)
        self.assertIn("\u00e1", stored)
        self.assertNotIn("\U0001F31D", stored)

    def test_speech_still_drops_the_emoji_from_raw_text(self) -> None:
        spoken = prepare_tts_text("night \U0001F31D, sleep well")
        self.assertNotIn("\U0001F31D", spoken)

    def test_a_degree_sign_reaching_speech_is_a_separate_matter(self) -> None:
        """Documented, not fixed here.

        The engine has always received this: on a normal turn the TTS
        buffer is fed raw model text, so widening the *transcript* filter
        changes nothing about what is spoken. Whether "25 deg C" should be
        voiced as "degrees Celsius" is a speech decision, and making it
        silently as part of a storage fix is how her voice changes without
        anyone deciding to change it.
        """
        self.assertIn("\u00b0", prepare_tts_text("Around 25\u00b0C today"))


if __name__ == "__main__":
    unittest.main()
