"""Tests for H21 — sleep & overnight rhythm + dream linkage.

Two layers:

* **Pure module** (:mod:`app.core.world.sleep_return`) — the overnight
  gate and the sleep-spot phrasing, both I/O-free and deterministic.
* **Provider plumbing**
  (:meth:`InnerLifePart2Mixin._render_sleep_return_block`) via a minimal
  mixin host stub — master switch, one-of ``_gap_cue_surfaced`` defer,
  pending-slot one-shot, the non-overnight silent path (flag untouched),
  force-next bypass, and the optional ``[dream]`` reflection weaving.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app.core.session.inner_life_part2 import InnerLifePart2Mixin
from app.core.world import sleep_return as sr


# ── Pure module: looks_like_overnight ──────────────────────────────────


class LooksLikeOvernightTests(unittest.TestCase):
    def test_short_gap_is_never_overnight(self) -> None:
        # Below min_gap regardless of hour.
        for hour in range(24):
            self.assertFalse(
                sr.looks_like_overnight(2.0, hour, min_gap_hours=5.0)
            )

    def test_morning_return_after_min_gap(self) -> None:
        # 6h gap returning at 08:00 → slept.
        self.assertTrue(
            sr.looks_like_overnight(6.0, 8, min_gap_hours=5.0, overnight_hours=9.0)
        )

    def test_medium_gap_outside_morning_band_silent(self) -> None:
        # 6h gap returning at 15:00 (afternoon) → she was up, not asleep.
        self.assertFalse(
            sr.looks_like_overnight(6.0, 15, min_gap_hours=5.0, overnight_hours=9.0)
        )

    def test_very_long_gap_overnight_any_hour(self) -> None:
        # >= overnight_hours reads as a sleep at any clock hour.
        for hour in range(24):
            self.assertTrue(
                sr.looks_like_overnight(
                    10.0, hour, min_gap_hours=5.0, overnight_hours=9.0
                )
            )

    def test_min_gap_boundary_inclusive(self) -> None:
        self.assertTrue(
            sr.looks_like_overnight(5.0, 7, min_gap_hours=5.0, overnight_hours=9.0)
        )

    def test_garbage_gap_is_false(self) -> None:
        self.assertFalse(sr.looks_like_overnight("nope", 8))  # type: ignore[arg-type]


# ── Pure module: sleep_spot_phrase ─────────────────────────────────────


class SleepSpotPhraseTests(unittest.TestCase):
    def test_known_slugs(self) -> None:
        self.assertEqual(sr.sleep_spot_phrase("bed"), "in bed")
        self.assertEqual(
            sr.sleep_spot_phrase("beanbag"), "curled up on the beanbag"
        )
        self.assertEqual(sr.sleep_spot_phrase("BED"), "in bed")

    def test_unknown_slug_falls_back(self) -> None:
        self.assertEqual(
            sr.sleep_spot_phrase("garden"), sr._DEFAULT_SPOT_PHRASE
        )

    def test_none_falls_back(self) -> None:
        self.assertEqual(sr.sleep_spot_phrase(None), sr._DEFAULT_SPOT_PHRASE)


# ── Pure module: render_sleep_line ─────────────────────────────────────


class RenderSleepLineTests(unittest.TestCase):
    def test_without_dream(self) -> None:
        line = sr.render_sleep_line(
            "in bed", user_display_name="Jacob", dream_gist=None
        )
        self.assertIn("Jacob", line)
        self.assertIn("in bed", line)
        self.assertIn("dozed off", line)
        self.assertNotIn("dream", line.lower())

    def test_with_dream(self) -> None:
        line = sr.render_sleep_line(
            "curled up on the beanbag",
            user_display_name="Jacob",
            dream_gist="a city made of glass",
        )
        self.assertIn("dream", line.lower())
        self.assertIn("a city made of glass", line)


# ── Provider plumbing ──────────────────────────────────────────────────


@dataclass(slots=True)
class _StubMemory:
    id: int
    content: str
    kind: str
    created_at: str


class _FakeMemoryStore:
    def __init__(self, rows: list[_StubMemory]) -> None:
        self._rows = rows

    def iter_by_kind(self, kind: str) -> list[_StubMemory]:
        return [m for m in self._rows if m.kind == kind]


class _FakeLocation:
    def __init__(self, slug: str) -> None:
        self.slug = slug


class _FakeWorldStore:
    def __init__(self, slug: str | None) -> None:
        self._slug = slug

    def get_state(self) -> Any:
        return SimpleNamespace(location_id=1 if self._slug else None)

    def get_location_by_id(self, location_id: int) -> Any:
        return _FakeLocation(self._slug) if self._slug else None


def _agent(**overrides: Any) -> SimpleNamespace:
    base = dict(sleep_return_enabled=True)
    base.update(overrides)
    return SimpleNamespace(**base)


def _mem_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(
        sleep_return_min_gap_hours=5.0,
        sleep_return_overnight_hours=9.0,
        sleep_return_dream_lookback_hours=18.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _iso_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


class _Host(InnerLifePart2Mixin):
    def __init__(
        self,
        *,
        pending_seconds: float | None = None,
        force_next: bool = False,
        gap_cue_surfaced: bool = False,
        agent_settings: SimpleNamespace | None = None,
        memory_settings: SimpleNamespace | None = None,
        dreams: list[_StubMemory] | None = None,
        location_slug: str | None = "beanbag",
    ) -> None:
        self._settings = SimpleNamespace(agent=agent_settings or _agent())
        self._memory_settings = memory_settings or _mem_settings()
        self._memory_store = _FakeMemoryStore(dreams or [])
        self._world_store = _FakeWorldStore(location_slug)
        self._pending_sleep_return_seconds = pending_seconds
        self._sleep_return_force_next = force_next
        self._gap_cue_surfaced = gap_cue_surfaced
        self._last_sleep_return: Any = None
        self.user_display_name = "Jacob"


class ProviderTests(unittest.TestCase):
    def test_disabled_returns_empty(self) -> None:
        host = _Host(
            pending_seconds=10 * 3600.0,
            agent_settings=_agent(sleep_return_enabled=False),
        )
        self.assertEqual(host._render_sleep_return_block(), "")

    def test_no_pending_slot_silent(self) -> None:
        host = _Host(pending_seconds=None)
        self.assertEqual(host._render_sleep_return_block(), "")

    def test_one_of_guard_defers(self) -> None:
        # turning_over already surfaced → stand down, slot preserved.
        host = _Host(pending_seconds=10 * 3600.0, gap_cue_surfaced=True)
        self.assertEqual(host._render_sleep_return_block(), "")
        self.assertEqual(host._pending_sleep_return_seconds, 10 * 3600.0)

    def test_long_gap_fires_and_sets_flag(self) -> None:
        # 10h >= overnight_hours → overnight regardless of clock hour.
        host = _Host(pending_seconds=10 * 3600.0, location_slug="bed")
        out = host._render_sleep_return_block()
        self.assertIn("dozed off", out)
        self.assertIn("in bed", out)
        # One-shot: slot cleared; one-of flag set so siblings defer.
        self.assertIsNone(host._pending_sleep_return_seconds)
        self.assertTrue(host._gap_cue_surfaced)
        self.assertIsNotNone(host._last_sleep_return)

    def test_non_overnight_gap_silent_flag_untouched(self) -> None:
        # 5.5h gap but min_gap raised to 12h → never overnight, and the
        # one-of flag is NOT set so the ordinary away/forward cues proceed.
        host = _Host(
            pending_seconds=5.5 * 3600.0,
            memory_settings=_mem_settings(sleep_return_min_gap_hours=12.0),
        )
        self.assertEqual(host._render_sleep_return_block(), "")
        # Slot consumed (one-shot) but the gap-cue family flag stays False.
        self.assertIsNone(host._pending_sleep_return_seconds)
        self.assertFalse(host._gap_cue_surfaced)

    def test_force_next_bypasses_gates(self) -> None:
        host = _Host(pending_seconds=None, force_next=True)
        out = host._render_sleep_return_block()
        self.assertIn("dozed off", out)
        self.assertFalse(host._sleep_return_force_next)

    def test_recent_dream_woven_in(self) -> None:
        dreams = [
            _StubMemory(
                id=1,
                content="[dream] a quiet train through a snowfield",
                kind="reflection",
                created_at=_iso_ago(3.0),
            )
        ]
        host = _Host(pending_seconds=10 * 3600.0, dreams=dreams)
        out = host._render_sleep_return_block()
        self.assertIn("dream", out.lower())
        self.assertIn("a quiet train through a snowfield", out)
        self.assertTrue(host._last_sleep_return["dream"])

    def test_stale_dream_not_woven(self) -> None:
        dreams = [
            _StubMemory(
                id=1,
                content="[dream] an old dream from days ago",
                kind="reflection",
                created_at=_iso_ago(48.0),  # beyond 18h lookback
            )
        ]
        host = _Host(pending_seconds=10 * 3600.0, dreams=dreams)
        out = host._render_sleep_return_block()
        self.assertNotIn("an old dream", out)
        self.assertFalse(host._last_sleep_return["dream"])

    def test_non_dream_reflection_ignored(self) -> None:
        rows = [
            _StubMemory(
                id=1,
                content="Jacob is learning the guitar",
                kind="reflection",
                created_at=_iso_ago(2.0),
            )
        ]
        host = _Host(pending_seconds=10 * 3600.0, dreams=rows)
        out = host._render_sleep_return_block()
        self.assertNotIn("guitar", out)
        self.assertFalse(host._last_sleep_return["dream"])


if __name__ == "__main__":
    unittest.main()
