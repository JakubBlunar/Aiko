from __future__ import annotations

import unittest

from app.core.services.response_text_service import (
    safe_visible_prefix,
    strip_action_meta_for_tts,
    strip_all_meta_tags,
)


class ResponseTextServiceTests(unittest.TestCase):
    def test_strip_inline_action_suffix(self) -> None:
        source = (
            'Assistant: I will send the Win+M shortcut to minimize VSCode! '
            "[Action] Executed MCP tool 'mcp.windows.Shortcut'. Pressed Win+M."
        )

        cleaned = strip_action_meta_for_tts(source)

        self.assertNotIn("[Action]", cleaned)
        self.assertNotIn("Executed MCP tool", cleaned)
        self.assertIn("I will send the Win+M shortcut", cleaned)


class StripAllMetaTagsTests(unittest.TestCase):
    def test_drops_detail_block_and_keeps_spoken_content(self) -> None:
        source = (
            "[[reaction:calm]]\n"
            "[[spoken]]a[[/spoken]]\n"
            "[[detail]]b[[/detail]]"
        )
        out = strip_all_meta_tags(source).strip()
        self.assertEqual(out, "a")

    def test_unclosed_detail_is_suppressed(self) -> None:
        out = strip_all_meta_tags("hello [[detail]]private rambling")
        self.assertIn("hello", out)
        self.assertNotIn("private", out)
        self.assertNotIn("[[detail", out)

    def test_remember_tag_is_dropped_with_content(self) -> None:
        out = strip_all_meta_tags(
            "Welcome back! [[remember:Jacob likes mochi]] How was today?"
        )
        self.assertNotIn("[[remember", out)
        self.assertNotIn("Jacob likes mochi", out)
        self.assertIn("Welcome back", out)
        self.assertIn("How was today", out)

    def test_unclosed_remember_at_end_is_suppressed(self) -> None:
        out = strip_all_meta_tags("hi there [[remember:in flight")
        self.assertEqual(out.strip(), "hi there")

    def test_reaction_tag_is_dropped(self) -> None:
        out = strip_all_meta_tags("[[reaction:cheerful]] hello").strip()
        self.assertEqual(out, "hello")


class SafeVisiblePrefixTests(unittest.TestCase):
    """Streaming holdback: simulate token deltas and verify nothing leaks."""

    def _stream(self, deltas: list[str]) -> list[str]:
        """Replay the same inner-loop logic the TurnRunner uses."""
        emitted: list[str] = []
        sent = 0
        accumulator = ""
        for delta in deltas:
            accumulator += delta
            visible = safe_visible_prefix(accumulator)
            if len(visible) > sent:
                emitted.append(visible[sent:])
                sent = len(visible)
        # Final flush like TurnRunner does at end-of-stream.
        final = strip_all_meta_tags(accumulator)
        if len(final) > sent:
            emitted.append(final[sent:])
        return emitted

    def test_partial_spoken_tag_never_leaks(self) -> None:
        emitted = self._stream(
            ["[[spo", "ken]]hi", " there", "[[/spo", "ken]]"]
        )
        joined = "".join(emitted)
        self.assertNotIn("[[", joined)
        self.assertNotIn("spo", joined.replace("space", ""))
        self.assertIn("hi", joined)
        self.assertIn("there", joined)

    def test_partial_detail_block_never_leaks(self) -> None:
        emitted = self._stream(
            [
                "[[reaction:calm]]\n",
                "Hello there.",
                " [[de",
                "tail]]secret",
                " more secret",
                "[[/det",
                "ail]]",
                " trailing tail",
            ]
        )
        joined = "".join(emitted)
        self.assertNotIn("secret", joined)
        self.assertNotIn("[[", joined)
        self.assertIn("Hello there", joined)
        self.assertIn("trailing tail", joined)

    def test_partial_remember_tag_never_leaks(self) -> None:
        emitted = self._stream(
            ["nice ", "[[remem", "ber:Jacob likes mochi]]", " bye"]
        )
        joined = "".join(emitted)
        self.assertNotIn("[[", joined)
        self.assertNotIn("remember", joined)
        self.assertNotIn("Jacob likes mochi", joined)
        self.assertIn("nice", joined)
        self.assertIn("bye", joined)

    def test_lone_open_bracket_eventually_emits(self) -> None:
        # A single '[' that turns out NOT to be a tag should appear in output
        # after the final flush.
        emitted = self._stream(["array", "[", "0", "]", " is fine"])
        joined = "".join(emitted)
        self.assertIn("array[0] is fine", joined)


if __name__ == "__main__":
    unittest.main()
