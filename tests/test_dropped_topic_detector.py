"""Unit tests for the K82 dropped-sub-topic detector.

Exercises
:func:`app.core.conversation.dropped_topic_detector.detect_dropped_topic`
and :func:`extract_asks` -- the pure, embedding-free completeness check
that catches when a user message had two separable asks and the reply
covered only one of them.

The precision bar is the whole feature: a companion who itemises ordinary
multi-clause messages is worse than one who occasionally misses a point.
These cases pin that bar rather than enumerating every split.
"""
from __future__ import annotations

import unittest

from app.core.conversation.dropped_topic_detector import (
    detect_dropped_topic,
    extract_asks,
    render_cue,
)


_TWO_ASKS = "I went to the store and got milk, how was your day?"
_DAY_ONLY_REPLY = "Pretty quiet over here, just reading."
_BOTH_COVERED_REPLY = (
    "Nice, milk from the store is the good stuff. My day was quiet."
)


class TwoAsksOneMissedTests(unittest.TestCase):
    def test_two_asks_one_question_reply_covers_only_first(self) -> None:
        hit = detect_dropped_topic(_TWO_ASKS, _DAY_ONLY_REPLY)
        self.assertIsNotNone(hit)
        assert hit is not None
        skipped = hit.skipped_ask.lower()
        self.assertIn("day", skipped)
        self.assertNotIn("milk", skipped)

    def test_cue_names_the_skipped_snippet_not_a_list(self) -> None:
        hit = detect_dropped_topic(_TWO_ASKS, _DAY_ONLY_REPLY)
        self.assertIsNotNone(hit)
        assert hit is not None
        cue = render_cue(hit)
        self.assertIn("Heads-up:", cue)
        self.assertIn("day", cue.lower())
        self.assertNotIn("1.", cue)
        self.assertNotIn("2.", cue)
        self.assertNotIn("three points", cue.lower())
        # One skipped thing, not a recap of the store trip.
        self.assertEqual(cue.lower().count("also asked about"), 1)


class ConservativeGateTests(unittest.TestCase):
    def test_one_intent_two_clauses_no_question(self) -> None:
        user = "it was long and tiring and I need tea"
        reply = "yeah that sounds exhausting, sit down"
        self.assertIsNone(detect_dropped_topic(user, reply))

    def test_two_statements_no_ask_stay_silent(self) -> None:
        user = "I went to the store. I also bought milk."
        reply = "cool"
        self.assertIsNone(detect_dropped_topic(user, reply))

    def test_single_question_is_not_a_dropped_topic(self) -> None:
        self.assertIsNone(
            detect_dropped_topic("how was your day?", "pretty quiet"),
        )


class CoverageTests(unittest.TestCase):
    def test_reply_covers_both_including_later_sentence(self) -> None:
        self.assertIsNone(detect_dropped_topic(_TWO_ASKS, _BOTH_COVERED_REPLY))

    def test_empty_reply_with_two_asks_is_a_miss(self) -> None:
        hit = detect_dropped_topic(_TWO_ASKS, "")
        self.assertIsNotNone(hit)


class MergeTests(unittest.TestCase):
    def test_fragments_that_share_content_words_merge_into_one_ask(self) -> None:
        user = (
            "I went to the grocery store. Also the store was packed, "
            "how was your day?"
        )
        asks = extract_asks(user)
        self.assertEqual(len(asks), 2)
        store_ask = next(a for a in asks if "store" in a.lower())
        self.assertIn("packed", store_ask.lower())
        self.assertTrue(any("day" in a.lower() for a in asks))


class ExtractAsksTests(unittest.TestCase):
    def test_compound_store_and_day_splits_on_comma_question(self) -> None:
        asks = extract_asks(_TWO_ASKS)
        self.assertEqual(len(asks), 2)

    def test_empty_text_is_no_asks(self) -> None:
        self.assertEqual(extract_asks(""), [])
        self.assertEqual(extract_asks("   "), [])
