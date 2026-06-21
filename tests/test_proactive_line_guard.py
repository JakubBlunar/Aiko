"""Tests for the proactive-line quality guard.

The guard is the shared backstop that stops raw third-person memory
narration (and internal markers) from being spoken as a proactive line.
It must PASS normal first-person weave output and REJECT only clear
leaks.
"""
from __future__ import annotations

import unittest

from app.core.proactive.proactive_line_guard import validate_proactive_line


class ValidProactiveLineTests(unittest.TestCase):
    def test_accepts_natural_first_person_lines(self):
        good = [
            "Hey, did you ever get back to that Python book?",
            "I keep thinking about that deploy flakiness — any new theories?",
            "Random thought — want to come back to the anime list?",
            "Is it weird that I'm still curious how the interview went?",
            "Jacob, want to pick that thread back up?",  # vocative is fine
            "How's the side project going?",
        ]
        for line in good:
            with self.subTest(line=line):
                ok, reason = validate_proactive_line(
                    line, user_display_name="Jacob",
                )
                self.assertTrue(ok, f"should accept {line!r} (reason={reason})")
                self.assertEqual(reason, "ok")


class RejectProactiveLineTests(unittest.TestCase):
    def test_rejects_empty(self):
        ok, reason = validate_proactive_line("   ")
        self.assertFalse(ok)
        self.assertEqual(reason, "empty")

    def test_rejects_multiline(self):
        ok, reason = validate_proactive_line("line one\nline two")
        self.assertFalse(ok)
        self.assertEqual(reason, "multiline")

    def test_rejects_too_long(self):
        ok, reason = validate_proactive_line("x" * 400)
        self.assertFalse(ok)
        self.assertEqual(reason, "too_long")

    def test_rejects_internal_markers(self):
        for text, marker in [
            ("Quick check — did you ever get to Jacob promised: watch anime?",
             "promised:"),
            ("I was thinking [[reaction:warm]] about that", "[["),
            ("Source content: something", "source content"),
            ("Still curious what the user wanted there", "the user"),
        ]:
            with self.subTest(text=text):
                ok, reason = validate_proactive_line(
                    text, user_display_name="Jacob",
                )
                self.assertFalse(ok)
                self.assertEqual(reason, f"banned:{marker}")

    def test_rejects_narration_openers(self):
        for text, word in [
            ("Wonders if Jacob picked the python book back up", "wonders"),
            ("Notices that he warms up after coffee", "notices"),
            ("Thinks the deploy is flaky", "thinks"),
            ("Realizes he never replied", "realizes"),
        ]:
            with self.subTest(text=text):
                ok, reason = validate_proactive_line(
                    text, user_display_name="Jacob",
                )
                self.assertFalse(ok)
                self.assertEqual(reason, f"narration_opener:{word}")

    def test_rejects_name_as_third_person_subject(self):
        for text in [
            "Jacob is learning Japanese this month",
            "Jacob wants to finish the dashboard",
            "Jacob seems tired lately",
        ]:
            with self.subTest(text=text):
                ok, reason = validate_proactive_line(
                    text, user_display_name="Jacob",
                )
                self.assertFalse(ok)
                self.assertEqual(reason, "third_person_subject")

    def test_vocative_name_is_not_third_person(self):
        ok, reason = validate_proactive_line(
            "Jacob, was that the book you meant?", user_display_name="Jacob",
        )
        self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main()
