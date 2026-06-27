from __future__ import annotations

import unittest

from app.core.services.response_text_service import (
    parse_reaction_at_start,
    parse_reaction_stack_at_start,
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

    def test_stacked_reaction_tag_is_dropped(self) -> None:
        # Phase 3 grammar: ``[[reaction:A+B]]`` is a stacked reaction
        # and must still strip cleanly from the visible text.
        out = strip_all_meta_tags("[[reaction:cheerful+blush]] hello").strip()
        self.assertEqual(out, "hello")

    def test_stacked_overlay_tag_is_dropped(self) -> None:
        # Same for ``[[overlay:A+B]]`` — the LLM emits the stacked
        # form inline and the visible transcript / TTS must not see
        # it.
        out = strip_all_meta_tags(
            "before [[overlay:sweat+question]] after",
        ).strip()
        self.assertEqual(out, "before  after".strip())

    def test_conflict_tag_is_dropped_with_reason(self) -> None:
        # F5 [[conflict:reason]] is private and must not leak to chat
        # or TTS, but the reason text must survive in the parsed
        # extraction path.
        from app.core.services.response_text_service import (
            extract_conflict_tags,
        )

        text = (
            "Wait, [[conflict:vegetarian last week, now steakhouse]] "
            "that doesn't quite add up."
        )
        out = strip_all_meta_tags(text)
        self.assertNotIn("[[conflict", out)
        self.assertNotIn("vegetarian last week", out)
        self.assertIn("Wait", out)
        self.assertIn("that doesn't quite add up", out)
        # Reason survives in the extraction.
        reasons = extract_conflict_tags(text)
        self.assertEqual(
            reasons, ["vegetarian last week, now steakhouse"],
        )

    def test_unclosed_conflict_at_end_is_suppressed(self) -> None:
        out = strip_all_meta_tags("hello there [[conflict:still typing")
        self.assertEqual(out.strip(), "hello there")
        self.assertNotIn("[[conflict", out)

    def test_diary_tag_is_dropped_with_content(self) -> None:
        # H9 [[diary:...]] is Aiko's private journal entry: invisible in
        # chat / TTS, but the body survives in the extraction path.
        from app.core.services.response_text_service import extract_diary_entries

        text = (
            "That really landed for me. "
            "[[diary:Today felt close. I want to remember this one.]] "
            "Anyway, what else is up?"
        )
        out = strip_all_meta_tags(text)
        self.assertNotIn("[[diary", out)
        self.assertNotIn("I want to remember this one", out)
        self.assertIn("That really landed for me", out)
        self.assertIn("what else is up", out)
        entries = extract_diary_entries(text)
        self.assertEqual(
            entries, ["Today felt close. I want to remember this one."],
        )

    def test_unclosed_diary_at_end_is_suppressed(self) -> None:
        out = strip_all_meta_tags("hello there [[diary:still writing")
        self.assertEqual(out.strip(), "hello there")
        self.assertNotIn("[[diary", out)

    def test_diary_tag_held_back_while_streaming(self) -> None:
        # The in-progress tail must be held until the closing ]] arrives.
        held = safe_visible_prefix("I think [[diary:today was")
        self.assertNotIn("[[diary", held)
        self.assertNotIn("today was", held)
        self.assertIn("I think", held)
        done = safe_visible_prefix("I think [[diary:today was good]] really.")
        self.assertNotIn("[[diary", done)
        self.assertIn("I think", done)
        self.assertIn("really", done)

    def test_extract_multiple_diary_entries_in_order(self) -> None:
        from app.core.services.response_text_service import extract_diary_entries

        text = "[[diary:first thought]] middle [[diary:second thought]]"
        self.assertEqual(
            extract_diary_entries(text), ["first thought", "second thought"],
        )

    def test_extract_diary_entries_empty(self) -> None:
        from app.core.services.response_text_service import extract_diary_entries

        self.assertEqual(extract_diary_entries(""), [])
        self.assertEqual(extract_diary_entries("no tags here"), [])


class ConflictTagHoldbackTests(unittest.TestCase):
    """The streaming holdback must wait for the closing ``]]`` before
    emitting any of the ``[[conflict:`` body."""

    def test_partial_opener_holds_back(self) -> None:
        from app.core.services.response_text_service import (
            safe_visible_prefix,
        )

        partial = "Wait [[conflict:user said"
        out = safe_visible_prefix(partial)
        # Everything from the opener onward must be held until close.
        self.assertEqual(out, "Wait ")

    def test_complete_tag_strips_cleanly(self) -> None:
        from app.core.services.response_text_service import (
            safe_visible_prefix,
        )

        full = "Wait [[conflict:user said]] really?"
        out = safe_visible_prefix(full)
        self.assertNotIn("[[conflict", out)
        self.assertIn("Wait", out)
        self.assertIn("really?", out)


class PredictTagTests(unittest.TestCase):
    """K2 ``[[predict:kind:topic:state:confidence]]`` tag handling."""

    def test_predict_tag_is_dropped_and_extracted(self) -> None:
        from app.core.services.response_text_service import (
            extract_predict_tags,
        )

        text = (
            "Sounds like [[predict:mood:tokyo trip:nervous:0.7]] "
            "you're getting nervous about it."
        )
        out = strip_all_meta_tags(text)
        self.assertNotIn("[[predict", out)
        self.assertNotIn("nervous:0.7", out)
        self.assertIn("Sounds like", out)
        self.assertIn("you're getting nervous", out)
        tags = extract_predict_tags(text)
        self.assertEqual(len(tags), 1)
        self.assertEqual(tags[0].kind, "mood")
        self.assertEqual(tags[0].topic, "tokyo trip")
        self.assertEqual(tags[0].predicted_state, "nervous")
        self.assertAlmostEqual(tags[0].confidence, 0.7)

    def test_predict_two_tags_in_one_message(self) -> None:
        from app.core.services.response_text_service import (
            extract_predict_tags,
        )

        text = (
            "Two reads: [[predict:mood:tokyo:excited:0.8]] and "
            "[[predict:opinion:rust language:overhyped:0.6]]"
        )
        tags = extract_predict_tags(text)
        self.assertEqual(len(tags), 2)
        kinds = {t.kind for t in tags}
        self.assertEqual(kinds, {"mood", "opinion"})

    def test_predict_invalid_kind_rejected(self) -> None:
        from app.core.services.response_text_service import (
            extract_predict_tags,
        )

        tags = extract_predict_tags("[[predict:bogus:x:y:0.5]]")
        self.assertEqual(tags, [])

    def test_predict_confidence_clamped(self) -> None:
        from app.core.services.response_text_service import (
            extract_predict_tags,
        )

        tags = extract_predict_tags("[[predict:mood:topic:state:1.5]]")
        self.assertEqual(len(tags), 1)
        self.assertEqual(tags[0].confidence, 1.0)

    def test_predict_unclosed_at_end_is_suppressed(self) -> None:
        from app.core.services.response_text_service import (
            safe_visible_prefix,
        )

        partial = "hi [[predict:mood:tokyo"
        out = safe_visible_prefix(partial)
        self.assertEqual(out.strip(), "hi")
        self.assertNotIn("[[predict", out)

    def test_predict_complete_tag_streams_cleanly(self) -> None:
        from app.core.services.response_text_service import (
            safe_visible_prefix,
        )

        full = "Wait [[predict:mood:tokyo:nervous:0.7]] really?"
        out = safe_visible_prefix(full)
        self.assertNotIn("[[predict", out)
        self.assertIn("Wait", out)
        self.assertIn("really?", out)


class GoalTagTests(unittest.TestCase):
    """K1 ``[[goal:summary]]`` tag handling: stripped from chat / TTS
    but the body survives in the extraction path."""

    def test_goal_tag_is_dropped_and_extracted(self) -> None:
        from app.core.services.response_text_service import (
            extract_goal_tags,
        )

        text = (
            "I want to [[goal:get better at listening for sevenths and ninths]] "
            "and that's it for tonight."
        )
        out = strip_all_meta_tags(text)
        self.assertNotIn("[[goal", out)
        self.assertNotIn("sevenths and ninths", out)
        self.assertIn("I want to", out)
        self.assertIn("that's it for tonight", out)
        tags = extract_goal_tags(text)
        self.assertEqual(len(tags), 1)
        self.assertIn("sevenths", tags[0])

    def test_goal_two_tags_in_one_message(self) -> None:
        from app.core.services.response_text_service import (
            extract_goal_tags,
        )

        text = (
            "Two threads: [[goal:write a short essay every weekend]] and "
            "[[goal:learn cyrillic alphabet]]"
        )
        tags = extract_goal_tags(text)
        self.assertEqual(len(tags), 2)

    def test_goal_dedupes_repeats_case_insensitively(self) -> None:
        from app.core.services.response_text_service import (
            extract_goal_tags,
        )

        text = (
            "[[goal:Practice piano scales]] [[goal:practice piano scales]]"
        )
        tags = extract_goal_tags(text)
        self.assertEqual(len(tags), 1)

    def test_goal_rejects_short_body(self) -> None:
        from app.core.services.response_text_service import (
            extract_goal_tags,
        )

        self.assertEqual(extract_goal_tags("[[goal:hi]]"), [])

    def test_goal_rejects_bracket_in_body(self) -> None:
        from app.core.services.response_text_service import (
            extract_goal_tags,
        )

        self.assertEqual(
            extract_goal_tags("[[goal:learn [bad] thing]]"), []
        )

    def test_goal_unclosed_at_end_is_suppressed(self) -> None:
        from app.core.services.response_text_service import (
            safe_visible_prefix,
        )

        partial = "hi [[goal:still typing"
        out = safe_visible_prefix(partial)
        self.assertEqual(out.strip(), "hi")
        self.assertNotIn("[[goal", out)


class ParseReactionAtStartStackTests(unittest.TestCase):
    """Phase 3 stacked-reaction grammar: ``[[reaction:A+B]]`` must
    surface ``A`` as the primary mood (preserving the existing
    one-token signature for legacy callers) while exposing the full
    stack via :func:`parse_reaction_stack_at_start`."""

    def test_plain_reaction_returns_single_token_primary(self) -> None:
        primary, rest = parse_reaction_at_start("[[reaction:cheerful]] hi")
        self.assertEqual(primary, "cheerful")
        self.assertEqual(rest, "hi")

    def test_stacked_reaction_legacy_parse_returns_primary_only(self) -> None:
        # Existing callers (affect updater, TTS reaction filler) keep
        # working against a single-token string. The companion
        # overlays are dispatched via the stack variant below.
        primary, rest = parse_reaction_at_start(
            "[[reaction:cheerful+blush]] hi",
        )
        self.assertEqual(primary, "cheerful")
        self.assertEqual(rest, "hi")

    def test_stacked_reaction_stack_parse_returns_components(self) -> None:
        primary, companions, rest = parse_reaction_stack_at_start(
            "[[reaction:cheerful+blush+grin]] hi",
        )
        self.assertEqual(primary, "cheerful")
        self.assertEqual(companions, ["blush", "grin"])
        self.assertEqual(rest, "hi")

    def test_stack_parse_deduplicates_repeats(self) -> None:
        # Defensive: a model that emits ``cheerful+cheerful`` collapses
        # to a single component so we don't double-fire any companion.
        primary, companions, _ = parse_reaction_stack_at_start(
            "[[reaction:cheerful+cheerful]]",
        )
        self.assertEqual(primary, "cheerful")
        self.assertEqual(companions, [])

    def test_no_tag_returns_none_primary(self) -> None:
        primary, companions, rest = parse_reaction_stack_at_start("hi there")
        self.assertIsNone(primary)
        self.assertEqual(companions, [])
        self.assertEqual(rest, "hi there")


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
