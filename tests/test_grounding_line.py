"""Unit tests for :mod:`app.core.conversation.grounding_line` (K16).

The renderer is pure / deterministic, so the tests assert on exact
phrasing for representative slot combinations. If a refactor changes
the texture intentionally, the test file is the right place to record
the new texture.
"""
from __future__ import annotations

import unittest

from app.core.conversation.grounding_line import (
    GroundingContext,
    GroundingLineRenderer,
    render,
)


class GroundingContextEmptinessTests(unittest.TestCase):
    """``is_empty()`` short-circuits the renderer; lock its semantics."""

    def test_default_context_is_empty(self) -> None:
        self.assertTrue(GroundingContext().is_empty())

    def test_unknown_user_state_does_not_count(self) -> None:
        ctx = GroundingContext(
            user_perceived_mood="unknown",
            user_perceived_energy="unknown",
            user_perceived_focus="unknown",
        )
        self.assertTrue(ctx.is_empty())

    def test_relationship_new_with_zero_days_is_empty(self) -> None:
        ctx = GroundingContext(relationship_phase="new", relationship_days=0)
        self.assertTrue(ctx.is_empty())

    def test_any_real_slot_makes_context_non_empty(self) -> None:
        for kwargs in (
            {"weekday": "Monday"},
            {"period": "morning"},
            {"hour": 9, "minute": 30},
            {"mood_label": "content"},
            {"user_perceived_mood": "upbeat"},
            {"world_location": "your room"},
            {"world_posture": "sitting"},
            {"user_app": "Cursor"},
            {"noise_level": "loud"},
            {"relationship_phase": "regular"},
            {"relationship_days": 3},
        ):
            with self.subTest(**kwargs):
                self.assertFalse(GroundingContext(**kwargs).is_empty())


class GroundingLineRendererTests(unittest.TestCase):
    """End-to-end paragraph composition across slot combinations."""

    def setUp(self) -> None:
        self.renderer = GroundingLineRenderer()

    def test_empty_context_renders_empty_string(self) -> None:
        self.assertEqual(self.renderer.render(GroundingContext()), "")

    def test_module_render_matches_class(self) -> None:
        ctx = GroundingContext(weekday="Monday", period="morning", hour=9, minute=2)
        self.assertEqual(render(ctx), self.renderer.render(ctx))

    def test_time_only(self) -> None:
        ctx = GroundingContext(
            weekday="Sunday", period="morning", hour=9, minute=42,
        )
        self.assertEqual(self.renderer.render(ctx), "It's Sunday morning, 9:42 AM.")

    def test_time_with_drowsy_rider(self) -> None:
        ctx = GroundingContext(
            weekday="Friday",
            period="evening",
            hour=23,
            minute=17,
            is_drowsy=True,
        )
        out = self.renderer.render(ctx)
        self.assertIn("Friday evening", out)
        self.assertIn("11:17 PM", out)
        self.assertIn("energy is low and you feel a touch drowsy", out)

    def test_time_with_loud_noise_rider(self) -> None:
        ctx = GroundingContext(
            weekday="Wednesday",
            period="afternoon",
            hour=14,
            minute=5,
            noise_level="loud",
        )
        out = self.renderer.render(ctx)
        self.assertIn("Wednesday afternoon, 2:05 PM.", out)
        self.assertIn("noticeably loud", out)

    def test_time_with_soft_hum_rider(self) -> None:
        ctx = GroundingContext(
            weekday="Thursday",
            period="morning",
            hour=8,
            minute=15,
            noise_level="soft_hum",
        )
        out = self.renderer.render(ctx)
        self.assertIn("soft hum", out)

    def test_lazy_sunday_afternoon_phrase(self) -> None:
        ctx = GroundingContext(
            weekday="Sunday",
            is_weekend=True,
            period="afternoon",
            hour=14,
            minute=30,
        )
        self.assertIn("a lazy Sunday afternoon", self.renderer.render(ctx))

    def test_quiet_sunday_evening_phrase(self) -> None:
        ctx = GroundingContext(
            weekday="Sunday",
            is_weekend=True,
            period="evening",
            hour=20,
            minute=0,
        )
        self.assertIn("a quiet Sunday evening", self.renderer.render(ctx))

    def test_clock_only_when_no_day_known(self) -> None:
        ctx = GroundingContext(hour=10, minute=4)
        self.assertEqual(self.renderer.render(ctx), "It's 10:04 AM.")

    def test_activity_user_app_only(self) -> None:
        ctx = GroundingContext(user_display_name="Jacob", user_app="Cursor")
        self.assertEqual(self.renderer.render(ctx), "Jacob's in Cursor.")

    def test_activity_with_user_perceived(self) -> None:
        ctx = GroundingContext(
            user_display_name="Jacob",
            user_app="Cursor",
            user_perceived_mood="upbeat",
            user_perceived_energy="normal",
        )
        out = self.renderer.render(ctx)
        self.assertIn("Jacob's in Cursor", out)
        self.assertIn("reads upbeat, energy normal", out)

    def test_perceived_only_no_app(self) -> None:
        ctx = GroundingContext(
            user_display_name="Jacob", user_perceived_mood="tired",
        )
        self.assertEqual(self.renderer.render(ctx), "Jacob reads tired.")

    def test_user_perceived_drops_unknown_slots(self) -> None:
        ctx = GroundingContext(
            user_display_name="Jacob",
            user_perceived_mood="tired",
            user_perceived_energy="unknown",
            user_perceived_focus="unknown",
        )
        out = self.renderer.render(ctx)
        self.assertIn("Jacob reads tired.", out)
        self.assertNotIn("unknown", out)

    def test_mood_only(self) -> None:
        ctx = GroundingContext(mood_label="content")
        self.assertEqual(
            self.renderer.render(ctx), "Your private feeling is content.",
        )

    def test_mood_label_underscores_normalised(self) -> None:
        ctx = GroundingContext(mood_label="quietly_pleased")
        out = self.renderer.render(ctx)
        self.assertIn("quietly pleased", out)
        self.assertNotIn("_", out)

    def test_relationship_familiar_phase_with_days(self) -> None:
        ctx = GroundingContext(
            user_display_name="Jacob",
            relationship_phase="familiar",
            relationship_days=14,
        )
        out = self.renderer.render(ctx)
        # Leads sentence 3 here so it gets capitalised.
        self.assertIn("You and Jacob are in the familiar phase", out)
        self.assertIn("~14 days in", out)

    def test_relationship_phase_new_suppresses_clause(self) -> None:
        ctx = GroundingContext(
            user_display_name="Jacob",
            relationship_phase="new",
            relationship_days=0,
        )
        # "new" + 0 days collapses to empty paragraph (nothing else set).
        self.assertEqual(self.renderer.render(ctx), "")

    def test_world_indoor(self) -> None:
        ctx = GroundingContext(
            world_location="your room",
            world_posture="sitting",
            world_activity="reading",
        )
        out = self.renderer.render(ctx)
        # World clause now lives in its own sentence (sentence 4)
        # and always anchors with "In your apartment" so the LLM
        # can't merge Aiko's room with the user's setting. The
        # legacy "your room" location collapses to the apartment
        # framing rather than emitting "In your apartment at your
        # room.".
        self.assertEqual(out, "In your apartment, you're sitting, reading.")

    def test_world_indoor_specific_spot(self) -> None:
        # When the location is a specific spot inside the
        # apartment (the desk, the bed, ...), the spot rides on
        # the sentence so we get "In your apartment at the desk,
        # you're sitting, reading.". Mirrors the canonical
        # phrasing in :func:`world_store.WorldState.prompt_block`.
        ctx = GroundingContext(
            world_location="the desk",
            world_posture="sitting",
            world_activity="reading",
        )
        out = self.renderer.render(ctx)
        self.assertEqual(
            out, "In your apartment at the desk, you're sitting, reading.",
        )

    def test_world_indoor_no_posture_or_activity(self) -> None:
        # With only the location populated, the apartment
        # sentence still stands alone with the spot anchor.
        ctx = GroundingContext(world_location="the bookshelf")
        out = self.renderer.render(ctx)
        self.assertEqual(out, "In your apartment at the bookshelf.")

    def test_world_outdoor_flips_framing(self) -> None:
        ctx = GroundingContext(
            world_location="the garden",
            world_posture="standing",
            world_activity="watering plants",
            world_outdoor=True,
        )
        out = self.renderer.render(ctx)
        # Outdoor anchors at home so the spot ("the garden")
        # can't be misread as the user's setting.
        self.assertEqual(
            out,
            "Outside at home in the garden, you're standing, watering plants.",
        )

    def test_full_paragraph_four_sentences(self) -> None:
        ctx = GroundingContext(
            user_display_name="Jacob",
            weekday="Sunday",
            period="morning",
            hour=9,
            minute=42,
            mood_label="content",
            user_app="Cursor",
            user_perceived_mood="upbeat",
            user_perceived_energy="normal",
            world_location="the desk",
            world_posture="sitting",
            world_activity="working",
            relationship_phase="familiar",
            relationship_days=21,
            noise_level=None,
        )
        out = self.renderer.render(ctx)
        # Exactly four sentences, joined with single spaces.
        # Sentence 4 (the apartment) is split off from sentence
        # 3 (mood + relationship) so the LLM can't merge Aiko's
        # space with the user's setting from sentence 2.
        self.assertEqual(out.count(". "), 3)
        self.assertTrue(out.endswith("."))
        # Each sentence shape is recognisable.
        self.assertIn("Sunday morning, 9:42 AM.", out)
        self.assertIn("Jacob's in Cursor", out)
        self.assertIn("reads upbeat, energy normal", out)
        self.assertIn("your private feeling is content", out.lower())
        self.assertIn("familiar phase", out)
        # Apartment sentence anchors clearly to Aiko's own space.
        self.assertIn("In your apartment at the desk, you're sitting, working.", out)

    def test_apartment_sentence_separates_from_inner_state(self) -> None:
        # Regression for the K16 cohabitation bug: with both an
        # inner-state clause AND a world clause set, the line
        # must produce TWO sentences (mood/relationship +
        # apartment) rather than fold the world clause back
        # inside sentence 3. Otherwise the LLM merges Aiko's
        # apartment with the user's setting.
        ctx = GroundingContext(
            user_display_name="Jacob",
            mood_label="content",
            world_location="the desk",
            world_posture="sitting",
            world_activity="working",
        )
        out = self.renderer.render(ctx)
        self.assertEqual(out.count(". "), 1)
        self.assertEqual(
            out,
            "Your private feeling is content. "
            "In your apartment at the desk, you're sitting, working.",
        )

    def test_only_noise_no_time_drops_rider(self) -> None:
        # Noise rider clings to sentence 1; without a time sentence
        # there's no carrier and the rider disappears.
        ctx = GroundingContext(noise_level="loud")
        self.assertEqual(self.renderer.render(ctx), "")

    def test_user_display_name_fallback(self) -> None:
        # Empty display name should fall back to "the user" rather
        # than emit "'s in Cursor" with a leading apostrophe-s.
        ctx = GroundingContext(user_display_name="", user_app="Cursor")
        out = self.renderer.render(ctx)
        self.assertIn("the user's in Cursor", out)


if __name__ == "__main__":
    unittest.main()
