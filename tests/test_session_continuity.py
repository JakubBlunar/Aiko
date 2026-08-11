"""Session-continuity bridge: the pure renderer and the seam gate."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.core.session import session_continuity as sc


NOW = datetime(2026, 8, 11, 22, 0, 0, tzinfo=timezone.utc)


def _iso(**delta) -> str:
    return (NOW - timedelta(**delta)).isoformat()


class TrimNoteTests(unittest.TestCase):
    def test_short_notes_pass_through_whitespace_collapsed(self) -> None:
        self.assertEqual(sc.trim_note("  a   b\nc "), "a b c")

    def test_a_long_note_is_cut_at_a_sentence_end(self) -> None:
        note = ("A" * 200) + ". " + ("B" * 400)
        out = sc.trim_note(note, max_chars=300)
        self.assertTrue(out.endswith("."))
        self.assertNotIn("B", out)

    def test_a_long_note_with_no_sentence_end_is_clipped(self) -> None:
        out = sc.trim_note("C" * 900, max_chars=100)
        self.assertTrue(out.endswith("..."))
        self.assertLessEqual(len(out), 103)

    def test_an_early_sentence_end_does_not_gut_the_note(self) -> None:
        """A note opening with "Hi. " must not be trimmed to "Hi." --
        the boundary is only worth taking in the back half."""
        note = "Hi. " + ("D" * 400)
        out = sc.trim_note(note, max_chars=200)
        self.assertGreater(len(out), 100)


class RenderTests(unittest.TestCase):
    def test_a_recent_seam_says_carry_on(self) -> None:
        block = sc.render_continuity_block(
            last_message_iso=_iso(minutes=20),
            note="They talked about the cat.",
            now=NOW,
            user_name="Jacob",
        )
        self.assertIn("20 minutes ago", block)
        self.assertIn("They talked about the cat.", block)
        self.assertIn("same sitting", block)
        self.assertNotIn("Enough time has passed", block)

    def test_an_old_seam_allows_noticing_the_gap(self) -> None:
        block = sc.render_continuity_block(
            last_message_iso=_iso(days=4),
            note="They talked about the cat.",
            now=NOW,
            user_name="Jacob",
        )
        self.assertIn("4 days ago", block)
        self.assertIn("Enough time has passed", block)
        self.assertNotIn("same sitting", block)

    def test_the_boundary_is_the_continuous_window(self) -> None:
        just_inside = sc.render_continuity_block(
            last_message_iso=_iso(seconds=sc.CONTINUOUS_WINDOW_SECONDS - 60),
            note="", now=NOW, user_name="Jacob",
        )
        just_outside = sc.render_continuity_block(
            last_message_iso=_iso(seconds=sc.CONTINUOUS_WINDOW_SECONDS + 60),
            note="", now=NOW, user_name="Jacob",
        )
        self.assertIn("same sitting", just_inside)
        self.assertIn("Enough time has passed", just_outside)

    def test_a_missing_note_still_carries_the_elapsed_time(self) -> None:
        """Only 27 of 45 sessions have a K21 note. "How long ago" is half
        the point of the block, so the other 18 must not go silent."""
        block = sc.render_continuity_block(
            last_message_iso=_iso(hours=2), note="", now=NOW,
            user_name="Jacob",
        )
        self.assertIn("2 hours ago", block)
        self.assertNotIn("Where that thread stood", block)

    def test_an_unparseable_timestamp_renders_nothing(self) -> None:
        """Better silent than "you last spoke in the past"."""
        self.assertEqual(
            sc.render_continuity_block(
                last_message_iso="not-a-date", note="x", now=NOW,
                user_name="Jacob",
            ),
            "",
        )

    def test_the_display_name_fallback_is_not_read_out(self) -> None:
        """``_resolve_user_display_name`` returns the literal "the user"
        when no provider is set, which reads as a stage direction."""
        block = sc.render_continuity_block(
            last_message_iso=_iso(hours=1), note="", now=NOW,
            user_name="the user",
        )
        self.assertNotIn("the user", block)
        self.assertIn("how he keeps", block)

    def test_the_elapsed_time_is_computed_not_quoted_from_the_note(self) -> None:
        """The regression this guards.

        K21 notes carry their own dates and they are not reliable -- the
        live store has one opening "Jacob fell asleep on June 29, 2026"
        on a thread whose messages are all from August. The block must
        take its timing from the message timestamp only.
        """
        block = sc.render_continuity_block(
            last_message_iso=_iso(hours=3),
            note="Jacob fell asleep on June 29, 2026, after a long day.",
            now=NOW,
            user_name="Jacob",
        )
        self.assertIn("You two last spoke 3 hours ago.", block)


if __name__ == "__main__":
    unittest.main()
