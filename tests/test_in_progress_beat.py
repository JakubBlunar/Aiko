"""H26 "caught mid-something": beats with a span, and the return cue.

The behaviour being pinned is the difference between an away-life that
reads as a changelog and one that reads as a life. Before H26 every beat
was already finished by the time he came back, so opening the app never
interrupted anything. These tests cover the three states an open beat
moves through — running, interrupted by a return, finished — and the one
rule that keeps the prompt coherent: she cannot simultaneously be told
she finished a thing and that she is still doing it.
"""
from __future__ import annotations

import random
import unittest
from datetime import datetime, timedelta, timezone

from app.core.world import in_progress_beat


def _now() -> datetime:
    return datetime(2026, 8, 12, 15, 0, tzinfo=timezone.utc)


class _FakeKV:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value


class DurationTests(unittest.TestCase):
    def test_durations_are_keyed_off_the_beat_kind(self) -> None:
        rng = random.Random(0)
        # Making tea is not a two-hour affair; a book can be.
        teas = [in_progress_beat.pick_duration_minutes("tea", rng)
                for _ in range(40)]
        books = [in_progress_beat.pick_duration_minutes("reading", rng)
                 for _ in range(40)]
        self.assertLessEqual(max(teas), 18)
        self.assertGreaterEqual(max(books), 30)

    def test_an_unknown_kind_still_gets_a_plausible_span(self) -> None:
        minutes = in_progress_beat.pick_duration_minutes("interpretive dance")
        self.assertGreaterEqual(minutes, 5)
        self.assertLessEqual(minutes, 120)


class LifecycleTests(unittest.TestCase):
    def _beat(self, now: datetime) -> in_progress_beat.InProgressBeat:
        return in_progress_beat.build(
            key="reading",
            activity="reading on the sofa",
            posture="curled up",
            summary="got a few chapters in",
            now=now,
            rng=random.Random(1),
        )

    def test_a_fresh_beat_is_open_and_not_yet_due(self) -> None:
        now = _now()
        beat = self._beat(now)
        self.assertTrue(beat.is_open_at(now))
        self.assertFalse(beat.is_due_at(now))

    def test_the_window_elapsing_closes_it(self) -> None:
        now = _now()
        beat = self._beat(now)
        later = now + timedelta(hours=4)
        self.assertFalse(beat.is_open_at(later))
        self.assertTrue(beat.is_due_at(later))

    def test_an_interrupted_beat_is_no_longer_open(self) -> None:
        # She put it down when he arrived, so a second return must not
        # catch her at the same thing again.
        now = _now()
        kv = _FakeKV()
        beat = self._beat(now)
        in_progress_beat.mark_interrupted(kv.set, beat, now)
        self.assertFalse(beat.is_open_at(now))
        self.assertTrue(beat.is_due_at(now))
        self.assertTrue(beat.interrupted)

    def test_minutes_in_reports_how_far_along_she_is(self) -> None:
        now = _now()
        beat = self._beat(now)
        self.assertEqual(beat.minutes_in(now + timedelta(minutes=17)), 17)

    def test_round_trip_through_kv_preserves_the_span(self) -> None:
        now = _now()
        kv = _FakeKV()
        beat = self._beat(now)
        in_progress_beat.save(kv.set, beat)

        loaded = in_progress_beat.load(kv.get)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.key, "reading")
        self.assertEqual(loaded.activity, "reading on the sofa")
        self.assertEqual(loaded.summary, "got a few chapters in")
        self.assertEqual(loaded.expected_end_at, beat.expected_end_at)

    def test_round_trip_preserves_the_used_item(self) -> None:
        now = _now()
        kv = _FakeKV()
        beat = in_progress_beat.build(
            key="read_book",
            activity="reading",
            posture="curled_up",
            summary="got a few chapters in",
            now=now,
            rng=random.Random(1),
            used_item_id=4,
        )
        in_progress_beat.save(kv.set, beat)
        loaded = in_progress_beat.load(kv.get)
        assert loaded is not None
        self.assertEqual(loaded.used_item_id, 4)

    def test_clear_removes_it(self) -> None:
        kv = _FakeKV()
        in_progress_beat.save(kv.set, self._beat(_now()))
        in_progress_beat.clear(kv.set)
        self.assertIsNone(in_progress_beat.load(kv.get))

    def test_absent_and_corrupt_state_both_read_as_nothing_open(self) -> None:
        kv = _FakeKV()
        self.assertIsNone(in_progress_beat.load(kv.get))
        kv.set(in_progress_beat.IN_PROGRESS_KEY, "{not json")
        self.assertIsNone(in_progress_beat.load(kv.get))
        kv.set(in_progress_beat.IN_PROGRESS_KEY, "[1, 2]")
        self.assertIsNone(in_progress_beat.load(kv.get))


if __name__ == "__main__":
    unittest.main()
