"""Tests for the K31 ``[[touch:KIND]]`` tag in response_text_service.

The new tag must:

  - parse cleanly via :func:`extract_touch_commands`,
  - strip cleanly via :func:`strip_all_meta_tags` (no leakage to
    transcript or TTS),
  - swallow a partial open at EOL via the open-tail pattern so a
    chunk boundary doesn't leak ``[[touch:`` into the visible text,
  - coexist with the rest of the inline-tag grammar without
    accidentally matching ``[[touchy:`` or other lookalikes.

Pure regex tests; no I/O. Runs in <50ms.
"""
from __future__ import annotations

import unittest

from app.core.services.response_text_service import (
    extract_touch_commands,
    safe_visible_prefix,
    strip_all_meta_tags,
)


class ExtractTouchCommandsTests(unittest.TestCase):
    def test_extracts_single_tag_with_offset(self) -> None:
        out = extract_touch_commands("come here [[touch:hug]] you")
        self.assertEqual(out, [("hug", 10)])

    def test_extracts_multiple_tags_in_order(self) -> None:
        out = extract_touch_commands(
            "hey [[touch:wave]] and also [[touch:head_pat]]"
        )
        self.assertEqual([k for k, _ in out], ["wave", "head_pat"])

    def test_repeated_kind_preserved_as_two_entries(self) -> None:
        # Two hugs in a row is unusual but should not be coalesced
        # at the parser level. The dispatcher's TouchService is the
        # surface that rate-limits.
        out = extract_touch_commands("[[touch:hug]] [[touch:hug]]")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0][0], "hug")
        self.assertEqual(out[1][0], "hug")

    def test_case_insensitive_normalised_to_lower(self) -> None:
        out = extract_touch_commands("[[touch:HUG]]")
        self.assertEqual(out, [("hug", 0)])

    def test_empty_input_returns_empty_list(self) -> None:
        self.assertEqual(extract_touch_commands(""), [])
        self.assertEqual(extract_touch_commands(None), [])  # type: ignore[arg-type]


class StripTouchTagsTests(unittest.TestCase):
    def test_strips_touch_tag_from_visible_text(self) -> None:
        out = strip_all_meta_tags(
            "Welcome back [[touch:hug]] it's good to see you"
        )
        self.assertNotIn("[[touch", out)
        self.assertNotIn("hug", out)
        self.assertIn("Welcome back", out)
        self.assertIn("good to see you", out)

    def test_strips_partial_open_tail_at_eof(self) -> None:
        out = strip_all_meta_tags("hold on [[touch:")
        self.assertEqual(out.strip(), "hold on")

    def test_does_not_match_unrelated_tag(self) -> None:
        # ``[[touchy:foo]]`` is gibberish but it must not be eaten
        # by the touch pattern; the strip pass leaves unknown tags
        # to be handled elsewhere (or shown as-is).
        out = strip_all_meta_tags("[[touchy:foo]] hello")
        self.assertIn("[[touchy:foo]]", out)

    def test_multiple_touch_tags_all_stripped(self) -> None:
        out = strip_all_meta_tags(
            "[[touch:wave]] hi [[touch:nudge]] you doing okay?"
        )
        self.assertNotIn("[[touch", out)
        self.assertIn("hi", out)
        self.assertIn("you doing okay", out)


class SafeVisiblePrefixTests(unittest.TestCase):
    """The streaming dispatcher uses ``safe_visible_prefix`` to cut at
    a safe boundary that doesn't expose a half-formed tag. Touch must
    be in the list of openers the prefix function knows about, so a
    half-open ``[[touch:`` at EOL is held back rather than leaked to
    the transcript / TTS.
    """

    def test_half_open_touch_held_back(self) -> None:
        # The window ends mid-touch-open. The half-open ``[[touch``
        # MUST NOT appear in the visible prefix. We assert on the
        # crucial substring rather than exact whitespace.
        out = safe_visible_prefix("hold on [[touch")
        self.assertNotIn("[[touch", out)
        self.assertIn("hold on", out)

    def test_half_open_kind_held_back(self) -> None:
        # ``[[touch:hu`` could still grow into ``[[touch:hug]]`` --
        # the prefix function must hold it back so the user doesn't
        # see the partial kind name.
        out = safe_visible_prefix("hold on [[touch:hu")
        self.assertNotIn("[[touch", out)
        self.assertNotIn("hu", out.split("hold on")[-1].strip().lstrip())

    def test_closed_touch_stripped_text_after_kept(self) -> None:
        # ``safe_visible_prefix`` runs strip_all_meta_tags first, so
        # a closed touch tag is removed from the visible prefix and
        # the text after the tag survives.
        out = safe_visible_prefix("hi [[touch:wave]] there")
        self.assertNotIn("[[touch", out)
        self.assertIn("hi", out)
        self.assertIn("there", out)


if __name__ == "__main__":
    unittest.main()
