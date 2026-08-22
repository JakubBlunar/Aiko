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

    def test_a_future_timestamp_does_not_become_recency(self) -> None:
        """H40. This assertion used to read ``"moments ago"``.

        It was written as a defensive check and was in fact pinning the
        fabrication: asked to describe something that has not happened as
        though it had, the function answered with the freshest phrase it
        owns. That is how 54 stored plans came to read as brand-new, and
        how a courier due the next morning was presented to Aiko as
        something that had just been. There is no recency to report here,
        so the vague past-tense fallback is the only honest answer.
        """
        self.assertEqual(
            tp.humanize_past((_NOW + timedelta(hours=1)).isoformat(), _NOW),
            "in the past",
        )
        # Far enough ahead that no rounding could explain it.
        self.assertEqual(
            tp.humanize_past((_NOW + timedelta(days=9)).isoformat(), _NOW),
            "in the past",
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

    def test_past_event_dated_ahead_falls_back_to_created_at(self) -> None:
        """H40. The store blocks this now, but stored rows still carry it.

        A ``past_event`` whose ``event_time`` sits in the future has two
        fields disagreeing about whether the thing happened. ``created_at``
        is the one we actually know -- the note was written, whatever it
        claims about its subject -- so the tag stays informative instead
        of collapsing to the freshest phrase available.
        """
        out = tp.temporal_suffix(
            temporal_type="past_event",
            event_time=(_NOW + timedelta(hours=9)).isoformat(),
            created_at=(_NOW - timedelta(hours=3)).isoformat(),
            now=_NOW,
        )
        self.assertEqual(out, " (3 hours ago)")

    def test_past_event_dated_ahead_with_no_created_at(self) -> None:
        # Nothing better to anchor on, but still not "moments ago".
        out = tp.temporal_suffix(
            temporal_type="past_event",
            event_time=(_NOW + timedelta(hours=9)).isoformat(),
            created_at=None,
            now=_NOW,
        )
        self.assertEqual(out, " (in the past)")

    def test_past_event_still_prefers_a_valid_event_time(self) -> None:
        # The fallback must not fire when event_time is usable: it is the
        # more precise of the two anchors whenever it is in the past.
        out = tp.temporal_suffix(
            temporal_type="past_event",
            event_time=(_NOW - timedelta(days=4)).isoformat(),
            created_at=(_NOW - timedelta(hours=1)).isoformat(),
            now=_NOW,
        )
        self.assertEqual(out, " (4 days ago)")

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

    def test_hour_plus_keeps_a_relative_reading(self) -> None:
        """The 60-minute cliff: the wall clock must not be the only reading.

        Under the hour a line reads "23 min ago" and needs no arithmetic;
        over it the bare "today 13:32" forced the model to subtract against
        the ambient clock to recover the age, which is exactly the sum it
        gets wrong in a long sitting.
        """
        self.assertIn("(2h ago)", self._fmt(timedelta(hours=2)))
        self.assertIn("(1h 20m ago)", self._fmt(timedelta(hours=1, minutes=20)))
        self.assertIn("(5h 3m ago)", self._fmt(timedelta(hours=5, minutes=3)))
        # The absolute anchor survives alongside it.
        self.assertTrue(self._fmt(timedelta(hours=3)).startswith("today "))

    def test_elapsed_reading_spans_midnight(self) -> None:
        """A late-night sitting crosses the date line mid-conversation.

        Gating the hint on "same calendar day" would drop it exactly when a
        night-owl conversation needs it most, so it is gated on elapsed
        time instead.
        """
        # The band is chosen from *local* calendar days, so anchor the
        # clock in the local zone rather than UTC or the date line lands
        # somewhere else on machines offset from it.
        local_tz = datetime.now().astimezone().tzinfo
        near_midnight = datetime(2026, 6, 1, 1, 0, tzinfo=local_tz)
        earlier = (near_midnight - timedelta(hours=3)).isoformat()
        crossed = tp.age_prefix(earlier, near_midnight)
        self.assertTrue(crossed.startswith("yesterday "), crossed)
        self.assertIn("(3h ago)", crossed)

    def test_elapsed_reading_dropped_once_stale(self) -> None:
        """Past the window the absolute stamp is the useful register alone."""
        out = self._fmt(timedelta(days=3))
        self.assertNotIn("ago)", out)

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

    def test_it_says_nothing_about_direction(self) -> None:
        """The predicate answers staleness only -- H40's whole mistake.

        Both of these will go stale, so both are true here; the caller
        that needs to know which way they point must ask
        ``deictic_direction``.
        """
        self.assertTrue(tp.has_relative_deictic("Jacob mowed the lawn today"))
        self.assertTrue(tp.has_relative_deictic("the courier comes tomorrow"))


class DeicticDirectionTests(unittest.TestCase):
    """H40 — which way the stale wording points."""

    def test_past_pointing(self) -> None:
        for text in (
            "Jacob mowed the lawn today",
            "Yesterday went badly",
            "he's currently between jobs",
            "swamped right now",
            "he's been quiet lately",
            "he fixed it this morning",
        ):
            with self.subTest(text=text):
                self.assertEqual(tp.deictic_direction(text), tp.PAST)

    def test_future_pointing(self) -> None:
        for text in (
            "the interview is tomorrow",
            "he wants a long bath tonight",
            "they're driving to the coast this weekend",
            "a candlelit date next week",
            "the cookies will arrive soon",
        ):
            with self.subTest(text=text):
                self.assertEqual(tp.deictic_direction(text), tp.FUTURE)

    def test_the_delivery_row_that_started_this(self) -> None:
        self.assertEqual(
            tp.deictic_direction(
                "Jacob expects a courier with the first hardware package "
                "tomorrow morning."
            ),
            tp.FUTURE,
        )

    def test_future_wins_a_mixed_sentence(self) -> None:
        """Mis-filing a plan as history is the costlier of the two errors.

        Nothing retires a ``past_event``, the upcoming-horizon block
        cannot see one, and it reads as already-true for as long as it
        lives. A plan that turns out to be history is demoted by the
        decay worker within the hour.
        """
        self.assertEqual(
            tp.deictic_direction(
                "he finished the report today and ships it tomorrow"
            ),
            tp.FUTURE,
        )

    def test_no_wording_is_not_the_present(self) -> None:
        self.assertIsNone(tp.deictic_direction("Jacob is a software engineer"))
        self.assertIsNone(tp.deictic_direction(""))
        self.assertIsNone(tp.deictic_direction(None))

    def test_every_deictic_has_exactly_one_direction(self) -> None:
        """The two lists must partition the one the regex is built from."""
        self.assertEqual(
            set(tp._PAST_DEICTICS) | set(tp._FUTURE_DEICTICS),
            set(tp._RELATIVE_DEICTICS),
        )
        self.assertEqual(
            set(tp._PAST_DEICTICS) & set(tp._FUTURE_DEICTICS), set(),
        )
        for word in tp._RELATIVE_DEICTICS:
            with self.subTest(word=word):
                self.assertIsNotNone(tp.deictic_direction(f"something {word} here"))


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


class ParseLooseDatetimeTests(unittest.TestCase):
    """Reading a time back off an answer we did not get to format.

    :func:`tp.parse_iso` handles timestamps we wrote and can trust the
    shape of. This is the other direction, and it exists because asking a
    model for ISO and hoping is not enough: over 160 stored promise
    deadlines the field came back in six registers, so a caller that only
    tried ``fromisoformat`` understood 24 of the 37 that named a day.
    """

    # A Wednesday, deliberately: several cases turn on the weekday.
    ANCHOR = datetime(2026, 8, 19, 12, 31, tzinfo=timezone.utc)

    def _day(self, text: str, **kw) -> str | None:
        got = tp.parse_loose_datetime(text, anchor=self.ANCHOR, **kw)
        return got.astimezone().date().isoformat() if got else None

    def test_iso_day_and_stamp(self) -> None:
        self.assertEqual(self._day("2026-08-19"), "2026-08-19")
        self.assertEqual(self._day("2026-08-18T08:00:00+00:00"), "2026-08-18")
        self.assertEqual(self._day("2026-08-17T23:30:00.000Z"), "2026-08-18")

    def test_month_name_in_either_field_order(self) -> None:
        self.assertEqual(self._day("August 14, 2026"), "2026-08-14")
        self.assertEqual(self._day("14 August 2026"), "2026-08-14")
        self.assertEqual(self._day("Aug 14th"), "2026-08-14")

    def test_a_year_is_not_mistaken_for_a_day(self) -> None:
        # "14 August 2026" matches the month-first pattern too, and
        # without a guard it read the "20" of the year as the day.
        self.assertEqual(self._day("14 August 2026"), "2026-08-14")

    def test_words_that_name_no_moment_are_refused(self) -> None:
        for text in ("soon", "eventually", "sometime", "when I get to it",
                     "", None, "asap"):
            with self.subTest(text=text):
                self.assertIsNone(
                    tp.parse_loose_datetime(text, anchor=self.ANCHOR),
                )

    def test_relative_words_resolve_against_the_anchor_not_the_reader(self) -> None:
        self.assertEqual(self._day("tomorrow"), "2026-08-20")
        self.assertEqual(self._day("yesterday"), "2026-08-18")
        self.assertEqual(self._day("tonight"), "2026-08-19")
        self.assertEqual(self._day("this weekend"), "2026-08-22")
        self.assertEqual(self._day("next week"), "2026-08-26")

    def test_a_bare_weekday_means_its_next_occurrence(self) -> None:
        self.assertEqual(self._day("Friday"), "2026-08-21")
        # Said on a Wednesday, "Wednesday" is the one coming: a deadline
        # naming today would have been written as "today".
        self.assertEqual(self._day("Wednesday"), "2026-08-26")

    def test_day_end_switches_the_convention_for_a_bare_day(self) -> None:
        noon = tp.parse_loose_datetime("2026-08-19", anchor=self.ANCHOR)
        assert noon is not None
        self.assertEqual(noon.astimezone().hour, 12)
        end = tp.parse_loose_datetime(
            "2026-08-19", anchor=self.ANCHOR, day_end=True,
        )
        assert end is not None
        self.assertEqual((end.astimezone().hour, end.astimezone().minute), (23, 59))

    def test_a_stated_clock_time_wins_over_the_convention(self) -> None:
        for text, expected in (
            ("2026-08-19 18:00", "18:00"),
            ("tomorrow at 9am", "09:00"),
            ("Friday 7pm", "19:00"),
            ("August 19 at 6:30pm", "18:30"),
        ):
            with self.subTest(text=text):
                got = tp.parse_loose_datetime(
                    text, anchor=self.ANCHOR, day_end=True,
                )
                assert got is not None, text
                self.assertEqual(got.astimezone().strftime("%H:%M"), expected)

    def test_an_offsetless_stamp_is_local_where_parse_iso_calls_it_utc(self) -> None:
        # The two functions read text from opposite sources and so must
        # disagree here. ``parse_iso`` handles timestamps we wrote, and we
        # write UTC. This one handles a model describing somebody's
        # afternoon, so promoting to UTC would shift every offset-less
        # deadline by the local offset.
        naive = "2026-08-19T18:00"
        loose = tp.parse_loose_datetime(naive, anchor=self.ANCHOR)
        assert loose is not None
        self.assertEqual(loose.astimezone().strftime("%H:%M"), "18:00")
        self.assertEqual(tp.parse_iso(naive).tzinfo, timezone.utc)

    def test_an_explicit_offset_is_respected(self) -> None:
        got = tp.parse_loose_datetime(
            "2026-08-19T18:00:00+00:00", anchor=self.ANCHOR,
        )
        assert got is not None
        self.assertEqual(got.astimezone(timezone.utc).strftime("%H:%M"), "18:00")

    def test_a_named_part_of_the_day_is_a_time_too(self) -> None:
        got = tp.parse_loose_datetime(
            "tomorrow morning", anchor=self.ANCHOR, day_end=True,
        )
        assert got is not None
        self.assertEqual(got.astimezone().strftime("%H:%M"), "09:00")

    def test_an_impossible_date_is_refused_rather_than_rounded(self) -> None:
        self.assertIsNone(self._day("Feb 30, 2026"))
        self.assertIsNone(self._day("2026-02-30"))

    def test_surrounding_words_do_not_prevent_a_match(self) -> None:
        self.assertEqual(self._day("Before August 18, 2026"), "2026-08-18")
        self.assertEqual(self._day("Monday, August 17, 2026"), "2026-08-17")

    def test_the_anchor_defaults_to_now(self) -> None:
        got = tp.parse_loose_datetime("today")
        assert got is not None
        self.assertEqual(
            got.astimezone().date(), tp.now().astimezone().date(),
        )


class TimeRuleTests(unittest.TestCase):
    """Two halves of one contract, and they must stay distinguishable.

    Both rules answer "how does written text stay true as it ages", and
    they answer it oppositely: a stored note should carry the concrete
    day, a live-state field should carry no date at all. Only the stored
    half existed, so the belief worker pasted it into a present-tense
    field and dutifully produced ``experienced mild evening frustration
    and low energy on august 12 2026``.
    """

    def test_they_are_not_the_same_rule(self) -> None:
        self.assertNotEqual(tp.STORED_TEXT_TIME_RULE, tp.LIVE_STATE_TIME_RULE)

    def test_the_stored_rule_asks_for_a_concrete_day(self) -> None:
        self.assertIn("concrete day", tp.STORED_TEXT_TIME_RULE)
        self.assertIn("past tense", tp.STORED_TEXT_TIME_RULE)

    def test_the_live_rule_forbids_what_the_stored_rule_requires(self) -> None:
        rule = tp.LIVE_STATE_TIME_RULE
        self.assertIn("no concrete date", rule)
        self.assertIn("present tense", rule)
        self.assertNotIn("concrete day", rule)

    def test_the_live_rule_sends_one_off_observations_away(self) -> None:
        # A state that only makes sense pinned to one evening is an event,
        # and stored as a belief it can never be confirmed or contradicted.
        self.assertIn("leave it out", tp.LIVE_STATE_TIME_RULE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
