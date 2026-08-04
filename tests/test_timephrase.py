"""Tests for the canonical relative-time module (K-time5/6/7).

Pins the consolidated formatters' behaviour so the ``rag_retriever``
re-exports and ``PromptAssembler._format_age`` delegation stay byte-identical
to the pre-consolidation code, and covers the new worker toolkit.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.core.infra import timephrase as tp


_NOW = datetime(2026, 5, 31, 13, 32, tzinfo=timezone.utc)


class PrimitivesTests(unittest.TestCase):
    def test_to_aware_promotes_naive(self) -> None:
        naive = datetime(2026, 1, 1, 12, 0)
        self.assertIsNotNone(tp.to_aware(naive).tzinfo)

    def test_to_aware_noop_on_aware(self) -> None:
        aware = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.assertIs(tp.to_aware(aware), aware)

    def test_parse_iso_handles_z_suffix(self) -> None:
        dt = tp.parse_iso("2026-05-31T13:32:00Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_parse_iso_bad_input(self) -> None:
        for bad in (None, "", "   ", "not-iso", 123):
            self.assertIsNone(tp.parse_iso(bad))  # type: ignore[arg-type]

    def test_now_provider_override_and_reset(self) -> None:
        fixed = datetime(2020, 2, 2, 2, 2, tzinfo=timezone.utc)
        tp.set_now_provider(lambda: fixed)
        try:
            self.assertEqual(tp.now(), fixed)
        finally:
            tp.set_now_provider(None)
        # After reset the provider is live again (just assert it's aware).
        self.assertIsNotNone(tp.now().tzinfo)

    def test_now_promotes_naive_provider(self) -> None:
        tp.set_now_provider(lambda: datetime(2020, 1, 1, 0, 0))
        try:
            self.assertIsNotNone(tp.now().tzinfo)
        finally:
            tp.set_now_provider(None)


class HumanizePastTests(unittest.TestCase):
    def test_bands(self) -> None:
        self.assertEqual(
            tp.humanize_past("2026-05-27T12:00:00+00:00", _NOW), "4 days ago",
        )
        self.assertEqual(
            tp.humanize_past("2026-05-30T13:32:00+00:00", _NOW), "yesterday",
        )
        self.assertEqual(
            tp.humanize_past("2026-05-14T12:00:00+00:00", _NOW), "2 weeks ago",
        )
        self.assertEqual(
            tp.humanize_past("2025-11-28T12:00:00+00:00", _NOW), "6 months ago",
        )
        self.assertEqual(tp.humanize_past("nonsense", _NOW), "in the past")

    def test_minutes_and_hours(self) -> None:
        self.assertEqual(
            tp.humanize_past((_NOW - timedelta(minutes=5)).isoformat(), _NOW),
            "5 minutes ago",
        )
        self.assertEqual(
            tp.humanize_past((_NOW - timedelta(hours=3)).isoformat(), _NOW),
            "3 hours ago",
        )

    def test_future_input_is_defensive(self) -> None:
        self.assertEqual(
            tp.humanize_past((_NOW + timedelta(hours=1)).isoformat(), _NOW),
            "moments ago",
        )


class HumanizeFutureTests(unittest.TestCase):
    def test_missing_is_soon(self) -> None:
        self.assertEqual(tp.humanize_future(None, _NOW), "soon")
        self.assertEqual(tp.humanize_future("garbage", _NOW), "soon")

    def test_passed_is_earlier(self) -> None:
        self.assertEqual(
            tp.humanize_future((_NOW - timedelta(hours=2)).isoformat(), _NOW),
            "earlier",
        )

    def test_local_noon_buckets(self) -> None:
        # Anchor to local noon so calendar-day math is unambiguous.
        now_local = datetime(2026, 5, 31, 12, 0).astimezone()
        out = tp.humanize_future((now_local + timedelta(hours=2)).isoformat(), now_local)
        self.assertIn("afternoon", out)
        tomorrow = (now_local + timedelta(days=1)).replace(hour=9, minute=0)
        self.assertIn("tomorrow morning", tp.humanize_future(tomorrow.isoformat(), now_local))


class TemporalSuffixTests(unittest.TestCase):
    def test_durable_preference_empty(self) -> None:
        for t in ("durable", "preference", "", None):
            self.assertEqual(
                tp.temporal_suffix(
                    temporal_type=t, event_time=None, created_at=None, now=_NOW,
                ),
                "",
            )

    def test_ongoing(self) -> None:
        self.assertEqual(
            tp.temporal_suffix(
                temporal_type="ongoing", event_time=None,
                created_at=None, now=_NOW,
            ),
            " (ongoing)",
        )

    def test_past_event_uses_event_time_then_created(self) -> None:
        out = tp.temporal_suffix(
            temporal_type="past_event",
            event_time="2026-05-28T10:00:00+00:00",
            created_at=None,
            now=_NOW,
        )
        self.assertEqual(out, " (3 days ago)")
        # created_at fallback when no event_time.
        out2 = tp.temporal_suffix(
            temporal_type="past_event", event_time=None,
            created_at="2026-05-28T10:00:00+00:00", now=_NOW,
        )
        self.assertEqual(out2, " (3 days ago)")

    def test_future_plan_passed_gets_should_be_done(self) -> None:
        out = tp.temporal_suffix(
            temporal_type="future_plan",
            event_time=(_NOW - timedelta(hours=1)).isoformat(),
            created_at=None,
            now=_NOW,
        )
        self.assertIn("should be done by now", out)


class AgePrefixTests(unittest.TestCase):
    """Mirrors the pinned PromptAssembler._format_age bands (K-time1)."""

    def _fmt(self, delta: timedelta) -> str:
        return tp.age_prefix((_NOW - delta).isoformat(), _NOW)

    def test_bands(self) -> None:
        self.assertEqual(self._fmt(timedelta(seconds=0)), "just now")
        self.assertEqual(self._fmt(timedelta(seconds=30)), "just now")
        self.assertEqual(self._fmt(timedelta(minutes=1)), "1 min ago")
        self.assertEqual(self._fmt(timedelta(minutes=45)), "45 min ago")
        self.assertTrue(self._fmt(timedelta(hours=2)).startswith("today "))
        self.assertTrue(
            self._fmt(timedelta(days=1, hours=1)).startswith("yesterday "),
        )

    def test_unparseable_empty(self) -> None:
        for bad in ("", "not-iso", "   ", None):
            self.assertEqual(tp.age_prefix(bad, _NOW), "")

    def test_future_is_just_now(self) -> None:
        self.assertEqual(
            tp.age_prefix((_NOW + timedelta(minutes=2)).isoformat(), _NOW),
            "just now",
        )


class TodayAnchorTests(unittest.TestCase):
    def test_contains_human_and_iso(self) -> None:
        anchor = tp.today_anchor(_NOW)
        self.assertTrue(anchor.startswith("Today is "))
        self.assertIn("Sunday, May 31, 2026", anchor)
        self.assertIn("2026-05-31T13:32:00+00:00", anchor)

    def test_defaults_to_live_now(self) -> None:
        fixed = datetime(2026, 5, 31, 13, 32, tzinfo=timezone.utc)
        tp.set_now_provider(lambda: fixed)
        try:
            self.assertIn("2026", tp.today_anchor())
        finally:
            tp.set_now_provider(None)


class WorkerToolkitTests(unittest.TestCase):
    def _mem(self, **kw):
        base = dict(
            content="Jacob likes ramen",
            kind="preference",
            temporal_type="preference",
            event_time=None,
            created_at="2026-05-28T10:00:00+00:00",
        )
        base.update(kw)
        return SimpleNamespace(**base)

    def test_format_memory_line_durable_still_shows_created_age(self) -> None:
        # Durable/preference would be untagged in RAG, but the worker variant
        # always shows recency so a worker can reason about freshness.
        line = tp.format_memory_line(self._mem(), _NOW)
        self.assertEqual(line, "- Jacob likes ramen (3 days ago)")

    def test_format_memory_line_prefers_temporal_suffix(self) -> None:
        mem = self._mem(
            temporal_type="future_plan",
            event_time=(_NOW + timedelta(days=2)).isoformat(),
        )
        line = tp.format_memory_line(mem, _NOW)
        self.assertIn("planned for", line)
        self.assertNotIn("3 days ago", line)

    def test_format_memory_block_header_and_cap(self) -> None:
        mems = [self._mem(content=f"row {i}") for i in range(5)]
        block = tp.format_memory_block(
            mems, _NOW, header="What you know:", max_items=2,
        )
        self.assertTrue(block.startswith("What you know:\n"))
        self.assertEqual(block.count("\n"), 2)  # header + 2 rows

    def test_format_memory_block_empty(self) -> None:
        self.assertEqual(tp.format_memory_block([], _NOW), "")

    def test_format_transcript_dicts_with_age(self) -> None:
        rows = [
            {
                "role": "user", "content": "hey",
                "created_at": (_NOW - timedelta(minutes=5)).isoformat(),
            },
            {
                "role": "assistant", "content": "hi",
                "created_at": (_NOW - timedelta(minutes=4)).isoformat(),
            },
        ]
        out = tp.format_transcript(rows, _NOW)
        self.assertIn("[5 min ago] User: hey", out)
        self.assertIn("[4 min ago] Aiko: hi", out)

    def test_format_transcript_without_age(self) -> None:
        rows = [{"role": "user", "content": "hey", "created_at": None}]
        out = tp.format_transcript(rows, _NOW, with_age=False)
        self.assertEqual(out, "User: hey")

    def test_format_transcript_skips_empty(self) -> None:
        rows = [{"role": "user", "content": "  ", "created_at": None}]
        self.assertEqual(tp.format_transcript(rows, _NOW), "")


class HasRelativeDeicticTests(unittest.TestCase):
    """K-time10 — the predicate behind the ``MemoryStore.add`` backstop."""

    def test_detects_the_stale_wordings(self) -> None:
        for text in (
            "Jacob mowed the lawn today",
            "he wants a long bath tonight",
            "the interview is tomorrow",
            "Yesterday went badly",
            "he's currently between jobs",
            "swamped right now",
            "he's been quiet lately",
            "they're driving to the coast this weekend",
        ):
            with self.subTest(text=text):
                self.assertTrue(tp.has_relative_deictic(text))

    def test_leaves_genuinely_durable_facts_alone(self) -> None:
        for text in (
            "Jacob is a software engineer",
            "Jacob prefers cozy stories over horror",
            "Jacob always drinks tea in the morning",
            "Jacob's sister is called Mira",
        ):
            with self.subTest(text=text):
                self.assertFalse(tp.has_relative_deictic(text))

    def test_word_boundaries(self) -> None:
        # "todays" / "Tomorrowland" are not the deictics we are after.
        self.assertFalse(tp.has_relative_deictic("he read Tomorrowland"))
        self.assertFalse(tp.has_relative_deictic("the sooner the better"))

    def test_case_insensitive_and_empty(self) -> None:
        self.assertTrue(tp.has_relative_deictic("TODAY was rough"))
        self.assertFalse(tp.has_relative_deictic(""))
        self.assertFalse(tp.has_relative_deictic(None))


class ResolveDeicticsTests(unittest.TestCase):
    """K-time10 — rewriting frozen text against its own write time."""

    _WRITTEN = datetime(2026, 5, 27, 19, 0, tzinfo=timezone.utc)

    def _resolve(self, text: str) -> str:
        return tp.resolve_deictics(text, self._WRITTEN, _NOW)

    def test_day_words_become_real_dates(self) -> None:
        self.assertEqual(
            self._resolve("Jacob mowed the lawn today"),
            "Jacob mowed the lawn on May 27",
        )
        self.assertEqual(
            self._resolve("the interview is tomorrow"),
            "the interview is on May 28",
        )
        self.assertEqual(
            self._resolve("yesterday went badly"),
            "on May 26 went badly",
        )

    def test_vaguer_words_become_vaguer_phrases(self) -> None:
        # No date is invented for a word that never named one.
        self.assertEqual(
            self._resolve("he wants a long bath tonight"),
            "he wants a long bath that evening",
        )
        self.assertEqual(
            self._resolve("he's been quiet lately"),
            "he's been quiet around then",
        )

    def test_same_day_text_is_untouched(self) -> None:
        fresh = _NOW - timedelta(hours=3)
        self.assertEqual(
            tp.resolve_deictics("a bath tonight", fresh, _NOW),
            "a bath tonight",
        )

    def test_unusable_timestamp_returns_input(self) -> None:
        for bad in (None, "", "not-a-date"):
            with self.subTest(source=bad):
                self.assertEqual(
                    tp.resolve_deictics("lawn today", bad, _NOW),
                    "lawn today",
                )

    def test_capitalisation_is_preserved(self) -> None:
        self.assertEqual(
            self._resolve("Today he rested"), "On May 27 he rested",
        )

    def test_cross_year_source_carries_the_year(self) -> None:
        old = datetime(2024, 12, 30, 9, 0, tzinfo=timezone.utc)
        self.assertEqual(
            tp.resolve_deictics("shipped it today", old, _NOW),
            "shipped it on Dec 30, 2024",
        )

    def test_text_without_deictics_is_returned_verbatim(self) -> None:
        self.assertEqual(
            self._resolve("Jacob is a software engineer"),
            "Jacob is a software engineer",
        )

    def test_accepts_an_iso_string_source(self) -> None:
        self.assertEqual(
            tp.resolve_deictics("lawn today", self._WRITTEN.isoformat(), _NOW),
            "lawn on May 27",
        )

    def test_is_idempotent(self) -> None:
        once = self._resolve("mowed the lawn today, bath tonight")
        self.assertEqual(tp.resolve_deictics(once, self._WRITTEN, _NOW), once)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
