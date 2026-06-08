"""TTS text-preparation regressions: quotes + filename extensions.

Two reported audio glitches:

* a stray / empty double-quote pair (``""``) made the TTS model emit a
  weird artifact -> ``prepare_tts_text`` now strips ``"``;
* ``report.txt`` was read with a sentence-end pause (the ``.`` looked
  like a terminator) -> the streaming chunker no longer breaks on a
  period glued to a word char, and ``prepare_tts_text`` speaks the dot.
"""

from __future__ import annotations

import unittest

from app.core.session.session_text_utils import (
    drain_tts_stream_chunks,
    prepare_tts_text,
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
