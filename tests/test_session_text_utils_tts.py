"""TTS text-preparation regressions.

Reported audio glitches, in the order they were found:

* a stray / empty double-quote pair (``""``) made the TTS model emit a
  weird artifact -> ``prepare_tts_text`` now strips ``"``;
* ``report.txt`` was read with a sentence-end pause (the ``.`` looked
  like a terminator) -> the streaming chunker no longer breaks on a
  period glued to a word char, and ``prepare_tts_text`` speaks the dot;
* dashes made her lurch into a pause mid-sentence, and emoticons were
  read out as letters (":P" -> a spoken "P"). Emoticons are now stripped
  on the *spoken* side only: banning them outright (persona + sanitiser)
  just made her emit broken halves -- a swallowed ":3" reached the
  transcript as a bare "3". So the transcript keeps the face and
  ``prepare_tts_text`` is the sole gate before audio, which is also why
  it can't lean on ``sanitize_assistant_text``: the streaming voice path
  hands it raw model text, long before the persisted copy exists;
* the same split was then found missing on the *incoming* side, where
  ``sanitize_user_text``'s punctuation whitelist had been deleting the
  "<" of an incoming "<3" and leaving the digit. That put 230 turns of
  "I love you 3" in the history Aiko reads as her own transcript, she
  learned the bare 3 as the way to write affection, and TTS read it back
  as "three" ("Sleep well, Jacob. three"). Both sanitisers now hand a
  face through whole, and ``prepare_tts_text`` also silences the orphaned
  digit for as long as the old shape is still in her context.
"""

from __future__ import annotations

import unittest

from app.core.session.session_text_utils import (
    drain_tts_stream_chunks,
    prepare_tts_text,
    sanitize_assistant_text,
    sanitize_user_text,
    strip_speech_fillers,
)


class PrepareTtsQuotesTests(unittest.TestCase):
    def test_double_quotes_are_stripped(self) -> None:
        self.assertEqual(prepare_tts_text('She said "hello" softly'), "She said hello softly")

    def test_empty_quote_pair_is_removed(self) -> None:
        self.assertEqual(prepare_tts_text('Look at this: ""'), "Look at this:")

    def test_apostrophes_survive(self) -> None:
        self.assertEqual(prepare_tts_text("I don't think it's done"), "I don't think it's done")


class PrepareTtsExtensionTests(unittest.TestCase):
    def test_filename_extension_becomes_dot(self) -> None:
        self.assertEqual(prepare_tts_text("Open report.txt now"), "Open report dot txt now")

    def test_multi_dot_filename(self) -> None:
        self.assertEqual(prepare_tts_text("archive.tar.gz"), "archive dot tar dot gz")

    def test_decimals_are_left_alone(self) -> None:
        self.assertEqual(prepare_tts_text("It is 3.14 meters"), "It is 3.14 meters")

    def test_version_number_left_alone(self) -> None:
        self.assertEqual(prepare_tts_text("running v2.0 build"), "running v2.0 build")

    def test_sentence_period_unaffected(self) -> None:
        # A normal period followed by a space is not glued to a letter.
        self.assertEqual(prepare_tts_text("All done. Next up"), "All done. Next up")


class PrepareTtsDashTests(unittest.TestCase):
    def test_em_dash_becomes_a_space(self) -> None:
        self.assertEqual(
            prepare_tts_text("I wanted to say \u2014 I missed you"),
            "I wanted to say I missed you",
        )

    def test_unspaced_em_dash_does_not_glue_words(self) -> None:
        self.assertEqual(prepare_tts_text("wait\u2014what?"), "wait what?")

    def test_en_dash_and_double_hyphen(self) -> None:
        self.assertEqual(prepare_tts_text("yeah \u2013 sure"), "yeah sure")
        self.assertEqual(prepare_tts_text("yeah -- sure"), "yeah sure")

    def test_hyphenated_compound_becomes_two_words(self) -> None:
        # "wellknown" would be mispronounced; "well known" reads correctly.
        self.assertEqual(
            prepare_tts_text("a well-known state-of-the-art thing"),
            "a well known state of the art thing",
        )

    def test_numeric_range_keeps_its_meaning(self) -> None:
        self.assertEqual(
            prepare_tts_text("about 3-4 hours"), "about 3 to 4 hours"
        )

    def test_sentence_punctuation_survives(self) -> None:
        self.assertEqual(
            prepare_tts_text("Really? Yes! Okay, then."),
            "Really? Yes! Okay, then.",
        )


class PrepareTtsEmoticonTests(unittest.TestCase):
    def test_tongue_emoticon_is_not_spoken(self) -> None:
        self.assertEqual(prepare_tts_text("gotcha :P"), "gotcha")

    def test_common_faces(self) -> None:
        for face in (":)", ":-)", ";)", ":D", ":(", "^_^", ">_<", "<3", "xD"):
            with self.subTest(face=face):
                self.assertEqual(prepare_tts_text(f"hey {face} there"), "hey there")

    def test_glued_emoticon_is_caught(self) -> None:
        self.assertEqual(prepare_tts_text("hey:P"), "hey")

    def test_emoji_is_dropped(self) -> None:
        self.assertEqual(prepare_tts_text("good night \U0001F31D"), "good night")

    def test_clock_time_is_not_an_emoticon(self) -> None:
        self.assertEqual(prepare_tts_text("meet at 3:30 today"), "meet at 3:30 today")

    def test_colon_before_a_word_survives(self) -> None:
        # ":O" glued to "h" must not eat the "Oh".
        self.assertEqual(prepare_tts_text("listen: Oh no"), "listen: Oh no")

    def test_parenthesised_variable_survives(self) -> None:
        # "x)" is only an emoticon in the xD form; f(x) is real text.
        self.assertIn("x", prepare_tts_text("the function f(x) here"))


class PrepareTtsUnspeakableSymbolTests(unittest.TestCase):
    def test_lone_underscore_and_pipe_go(self) -> None:
        self.assertEqual(prepare_tts_text("a _ b | c"), "a b c")

    def test_meaningful_symbols_stay(self) -> None:
        # These have spoken forms the model produces correctly.
        self.assertEqual(prepare_tts_text("50% & up"), "50% & up")


class SanitizeKeepsEmoticonsTests(unittest.TestCase):
    """Emoticons are hers to write; only the audio is protected from them.

    The transcript is the readable copy, and a face there costs nothing.
    Stripping it was what produced the bare "3" in "my hug has done its
    job, 3" -- so the assertions below are the inverse of the spoken ones
    in ``PrepareTtsEmoticonTests``.
    """

    def test_emoticon_survives_in_the_transcript(self) -> None:
        self.assertEqual(sanitize_assistant_text("gotcha :P"), "gotcha :P")

    def test_cat_face_survives_whole(self) -> None:
        # The regression: ":3" must not arrive as a lone "3".
        self.assertEqual(
            sanitize_assistant_text("my hug has done its job, :3 And you're fine."),
            "my hug has done its job, :3 And you're fine.",
        )

    def test_common_faces_survive(self) -> None:
        for face in (":)", ":-)", ";)", ":D", "^_^", "<3", "xD"):
            with self.subTest(face=face):
                self.assertEqual(
                    sanitize_assistant_text(f"hey {face} there"), f"hey {face} there"
                )

    def test_pictograph_still_cannot_be_persisted(self) -> None:
        # Not an emoticon decision -- the ASCII-only filter below drops it.
        self.assertEqual(sanitize_assistant_text("night \U0001F31D"), "night")

    def test_spoken_copy_still_loses_the_face(self) -> None:
        # The pairing that makes the split safe: written yes, spoken no.
        text = "my hug has done its job, :3 And you're fine."
        self.assertIn(":3", sanitize_assistant_text(text))
        self.assertNotIn(":3", prepare_tts_text(text))
        self.assertNotIn(" 3 ", prepare_tts_text(text))

    def test_url_colon_slash_survives_sanitize(self) -> None:
        # The strict word boundary exists to protect this; prepare_tts_text
        # removes URLs outright before its looser glued-form pass.
        self.assertIn("https://", sanitize_assistant_text("see https://a.b/c"))

    def test_clock_survives_sanitize(self) -> None:
        self.assertEqual(sanitize_assistant_text("at 3:30"), "at 3:30")


class SanitizeUserKeepsEmoticonsTests(unittest.TestCase):
    """A face Jacob types has to survive the punctuation whitelist.

    It couldn't tell "<3" from a stray angle bracket, so it deleted the
    "<" and left the digit -- and the mangled copy is what gets persisted,
    shown, *and* replayed into the prompt as Aiko's own history. She read
    230 turns of "I love you 3" and started writing the bare 3 back.
    """

    def test_heart_survives(self) -> None:
        self.assertEqual(
            sanitize_user_text("I love you so much <3"), "I love you so much <3"
        )

    def test_heart_mid_sentence_survives(self) -> None:
        self.assertEqual(
            sanitize_user_text("you <3 let me give you the next one"),
            "you <3 let me give you the next one",
        )

    def test_caret_faces_survive(self) -> None:
        # "^" is not in the whitelist either, so "^^" used to vanish whole.
        for face in ("^^", "^_^", ">_<", "T_T", "-_-", ":3", ";)", ":D", "xD"):
            with self.subTest(face=face):
                self.assertEqual(
                    sanitize_user_text(f"hehe {face} yes"), f"hehe {face} yes"
                )

    def test_heart_run_survives(self) -> None:
        self.assertEqual(sanitize_user_text("I love you <3<3<3"), "I love you <3<3<3")

    def test_everything_else_is_still_filtered(self) -> None:
        # The whitelist keeps its job between the faces -- only a matched
        # emoticon span is handed through.
        self.assertEqual(
            sanitize_user_text("math: 5 < 7 > 2 and a | pipe"),
            "math: 5 7 2 and a pipe",
        )

    def test_pictographs_and_control_chars_still_go(self) -> None:
        self.assertEqual(
            sanitize_user_text("night \U0001F31D and\u2028gone"), "night and gone"
        )

    def test_clock_and_ratio_are_not_faces(self) -> None:
        self.assertEqual(
            sanitize_user_text("meet at 3:30, a 1:2 ratio"), "meet at 3:30, a 1:2 ratio"
        )

    def test_the_face_is_still_absent_from_audio(self) -> None:
        # The pairing that makes preserving it safe.
        text = sanitize_user_text("I love you so much <3")
        self.assertIn("<3", text)
        self.assertEqual(prepare_tts_text(text), "I love you so much")


class SwallowedHeartTests(unittest.TestCase):
    """The digit she learned before the incoming side was fixed.

    ``sanitize_user_text`` no longer produces these, but hundreds are
    still in the context she reads, so she keeps emitting them for now --
    and a bare "3" is a number to a grapheme-driven engine. The transcript
    keeps whatever she wrote either way; this is audio only.
    """

    def test_trailing_digit_is_not_spoken(self) -> None:
        self.assertEqual(prepare_tts_text("Sleep well, Jacob. 3"), "Sleep well, Jacob.")

    def test_digit_before_a_new_sentence_is_not_spoken(self) -> None:
        self.assertEqual(
            prepare_tts_text("You have it, sleepyhead 3 Come settle in close."),
            "You have it, sleepyhead Come settle in close.",
        )

    def test_transcript_still_shows_what_she_wrote(self) -> None:
        self.assertEqual(
            sanitize_assistant_text("Sleep well, Jacob. 3"), "Sleep well, Jacob. 3"
        )

    def test_counted_numbers_survive(self) -> None:
        # Every one of these appears in her replies; none is a heart.
        for line in (
            "I need 3.",
            "About 3 cookies left.",
            "In 3 minutes, Jacob.",
            "It is 3.14 meters and 3:30 now.",
            "You should have 3 outfits available.",
            "That is a nearly 3 a.m. problem.",
        ):
            with self.subTest(line=line):
                self.assertIn("3", prepare_tts_text(line))

    def test_capitalised_clock_survives(self) -> None:
        # The one collision worth excluding by hand: "meet me at" with no
        # time left in it is worse than a leaked "three".
        self.assertIn("3", prepare_tts_text("Meet me at 3 AM sharp."))
        self.assertIn("3", prepare_tts_text("Meet me at 3 P.M. sharp."))

    def test_intact_heart_run_leaves_no_orphan_digit(self) -> None:
        # "<3<3" failed the leading boundary on the second heart, so the
        # spoken copy used to keep a bare "3" from the middle of the run.
        self.assertEqual(prepare_tts_text("I love you <3<3<3"), "I love you")


class SanitizeDashTests(unittest.TestCase):
    """The transcript keeps a readable dash; only the spoken copy loses it.

    Everything downstream is ASCII-only, so a unicode dash has to become
    *something* here or it vanishes mid-sentence.
    """

    def test_em_dash_becomes_a_spaced_hyphen(self) -> None:
        # The regression: this used to render "Yes-that", which reads as a
        # typo rather than as the clause break the model wrote.
        self.assertEqual(
            sanitize_assistant_text("Yes\u2014that gives it a cycle"),
            "Yes - that gives it a cycle",
        )

    def test_already_spaced_em_dash_does_not_double_up(self) -> None:
        self.assertEqual(
            sanitize_assistant_text("Yes \u2014 that works"), "Yes - that works"
        )

    def test_en_dash_range(self) -> None:
        self.assertEqual(sanitize_assistant_text("3\u20134 hours"), "3 - 4 hours")

    def test_unicode_hyphen_stays_glued(self) -> None:
        # U+2010/U+2011 join words; spacing them would break the compound.
        self.assertEqual(sanitize_assistant_text("well\u2010known"), "well-known")
        self.assertEqual(sanitize_assistant_text("e\u2011mail"), "e-mail")

    def test_ascii_hyphen_untouched(self) -> None:
        self.assertEqual(sanitize_assistant_text("well-known"), "well-known")

    def test_transcript_dash_still_leaves_the_audio_clean(self) -> None:
        # The two surfaces disagree on purpose: readable in the bubble,
        # absent in the voice.
        self.assertEqual(
            prepare_tts_text(sanitize_assistant_text("Yes\u2014that works")),
            "Yes that works",
        )


class DashFillerInteractionTests(unittest.TestCase):
    def test_dash_bracketed_filler_is_left_alone(self) -> None:
        # Documented consequence of removing dashes: the filler loses the
        # punctuation brackets it needed, which puts it in the category
        # strip_speech_fillers deliberately does not guess at.
        spoken = prepare_tts_text("I mean -- uhm -- yeah")
        self.assertEqual(spoken, "I mean uhm yeah")
        self.assertEqual(strip_speech_fillers(spoken), "I mean uhm yeah")

    def test_comma_bracketed_filler_still_stripped(self) -> None:
        spoken = prepare_tts_text("I mean, uhm, yeah")
        self.assertEqual(strip_speech_fillers(spoken), "I mean, yeah")


class PrepareTtsTagLeakTests(unittest.TestCase):
    """A meta tag must never become audio.

    The old strip was ``\\[\\[[^\\]]*\\]\\]`` followed by deleting bare
    brackets, so any tag that missed that exact shape lost its brackets and
    kept its body -- and the body got *spoken*. These leaks are invisible
    everywhere else: the tag is gone from the transcript, so the only symptom
    is Aiko saying "moment tender we finished the arcs" out loud.
    """

    PROSE = "narrative arcs prepared?"
    TAIL = " That's a huge step."

    def _spoken(self, tag: str) -> str:
        return prepare_tts_text(f"{self.PROSE}{tag}{self.TAIL}")

    def test_well_formed_tag_is_silent(self) -> None:
        self.assertEqual(
            self._spoken("[[moment:tender:we finished the arcs]]"),
            "narrative arcs prepared? That's a huge step.",
        )

    def test_single_bracket_tag_is_not_spoken(self) -> None:
        # The most common LLM slip.
        self.assertNotIn("moment", self._spoken("[moment:tender:we finished]"))
        self.assertNotIn("remember", self._spoken("[remember:he finished it]"))

    def test_spaced_brackets_are_not_spoken(self) -> None:
        self.assertNotIn("moment", self._spoken("[ [moment:tender:done] ]"))

    def test_curly_mis_render_is_not_spoken(self) -> None:
        self.assertNotIn("moment", self._spoken("{{moment:tender:done}}"))

    def test_bracket_inside_content_does_not_defeat_the_strip(self) -> None:
        # "[^\\]]*" could not cross the "]" of "array[0]", so the whole tag
        # fell through to the bracket-delete fallback and was read aloud.
        self.assertNotIn(
            "remember", self._spoken("[[remember:he used array[0] today]]")
        )

    def test_unterminated_tag_swallows_its_body(self) -> None:
        spoken = prepare_tts_text(f"{self.PROSE} [[moment:tender:we finished")
        self.assertEqual(spoken, "narrative arcs prepared?")

    def test_ordinary_bracketed_prose_survives(self) -> None:
        # Only a known tag name triggers the single-bracket rule, so this is
        # still spoken (minus the brackets themselves).
        self.assertIn("see note", prepare_tts_text("check it [see note] now"))

    def test_plain_sentence_is_untouched(self) -> None:
        self.assertEqual(
            prepare_tts_text("That's a huge step, Jacob."),
            "That's a huge step, Jacob.",
        )


class DrainChunkExtensionTests(unittest.TestCase):
    def test_filename_does_not_split_chunk(self) -> None:
        text = "Here is the file you wanted: report.txt and more"
        chunks, remainder = drain_tts_stream_chunks(text, flush=True)
        # The filename stays intact (no chunk ends in "report.").
        joined = " ".join(chunks)
        self.assertIn("report.txt", joined)
        for chunk in chunks:
            self.assertFalse(chunk.endswith("report."))

    def test_real_sentence_still_splits(self) -> None:
        text = "This is a complete sentence here. And here is another one too."
        chunks, _ = drain_tts_stream_chunks(text, flush=True)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertTrue(chunks[0].endswith("here."))

    def test_trailing_period_waits_for_more_context(self) -> None:
        # Streaming: buffer ends right after a period -> don't split yet,
        # the next delta reveals whether it's a sentence end or ".ext".
        chunks, remainder = drain_tts_stream_chunks(
            "Here is the file called report.", flush=False,
        )
        self.assertEqual(chunks, [])
        self.assertEqual(remainder, "Here is the file called report.")

    def test_decimal_does_not_split(self) -> None:
        text = "The total cost came out to about 3.14 dollars even"
        chunks, _ = drain_tts_stream_chunks(text, flush=True)
        joined = " ".join(chunks)
        self.assertIn("3.14", joined)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
