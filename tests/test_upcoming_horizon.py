"""K-time3 — upcoming-horizon pure module + provider plumbing tests."""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.conversation.upcoming_horizon import (
    build_signature,
    render_block,
    select_upcoming,
)
from app.core.infra import timephrase
from app.core.session.inner_life_part2 import InnerLifePart2Mixin


_NOW = datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class _Mem:
    id: int
    content: str
    event_time: str | None


def _mem(mid: int, content: str, event_time: str | None) -> _Mem:
    return _Mem(id=mid, content=content, event_time=event_time)


# ── pure module ──────────────────────────────────────────────────────────


class SelectUpcomingTests(unittest.TestCase):
    def test_filters_window_and_sorts_soonest_first(self) -> None:
        mems = [
            _mem(3, "trip", "2026-06-29T18:00:00+00:00"),  # in 3 days
            _mem(1, "dentist", "2026-06-27T09:00:00+00:00"),  # tomorrow
            _mem(9, "vacation", "2026-07-10T09:00:00+00:00"),  # beyond 7d
            _mem(8, "past thing", "2026-06-25T09:00:00+00:00"),  # past
            _mem(7, "no time", None),  # unparseable
        ]
        out = select_upcoming(mems, _NOW, horizon_days=7, max_items=5)
        self.assertEqual([m.id for m in out], [1, 3])

    def test_max_items_caps(self) -> None:
        mems = [
            _mem(1, "a", "2026-06-27T09:00:00+00:00"),
            _mem(2, "b", "2026-06-28T09:00:00+00:00"),
            _mem(3, "c", "2026-06-29T09:00:00+00:00"),
        ]
        out = select_upcoming(mems, _NOW, horizon_days=7, max_items=2)
        self.assertEqual([m.id for m in out], [1, 2])

    def test_empty_when_nothing_in_window(self) -> None:
        mems = [_mem(1, "past", "2026-01-01T09:00:00+00:00")]
        self.assertEqual(select_upcoming(mems, _NOW, horizon_days=7, max_items=3), [])


class SignatureTests(unittest.TestCase):
    def test_stable_for_same_set(self) -> None:
        a = [_mem(1, "x", "2026-06-27T09:00:00+00:00")]
        b = [_mem(1, "x", "2026-06-27T09:00:00+00:00")]
        self.assertEqual(build_signature(a), build_signature(b))

    def test_changes_when_set_changes(self) -> None:
        a = [_mem(1, "x", "2026-06-27T09:00:00+00:00")]
        b = [_mem(2, "y", "2026-06-28T09:00:00+00:00")]
        self.assertNotEqual(build_signature(a), build_signature(b))


class RenderBlockTests(unittest.TestCase):
    def test_renders_resolved_times_and_guard(self) -> None:
        mems = [
            _mem(1, "dentist appointment.", "2026-06-27T09:00:00+00:00"),
            _mem(2, "road trip", "2026-06-29T18:00:00+00:00"),
        ]
        out = render_block(mems, _NOW, "Jacob")
        self.assertIn("Coming up for Jacob", out)
        self.assertIn("dentist appointment", out)
        # Resolved phrasing from timephrase, never a raw date.
        self.assertIn(timephrase.humanize_future(mems[0].event_time, _NOW), out)
        self.assertIn("heads-up", out.lower())
        self.assertNotIn("2026-06-27", out)

    def test_blank_when_no_renderable_content(self) -> None:
        self.assertEqual(
            render_block([_mem(1, "   ", "2026-06-27T09:00:00+00:00")], _NOW, "Jacob"),
            "",
        )


# ── provider plumbing ─────────────────────────────────────────────────────


class _Agent:
    upcoming_horizon_enabled = True


class _MemSettings:
    upcoming_horizon_days = 7
    upcoming_horizon_max_items = 3
    upcoming_horizon_cooldown_turns = 6


class _Settings:
    def __init__(self) -> None:
        self.agent = _Agent()


class _Store:
    def __init__(self, mems: list[_Mem]) -> None:
        self._mems = mems

    def list_by_temporal_type(self, temporal_type: str) -> list[_Mem]:
        assert temporal_type == "future_plan"
        return list(self._mems)


class _Host(InnerLifePart2Mixin):
    def __init__(self, mems: list[_Mem]) -> None:
        self._settings = _Settings()
        self._memory_settings = _MemSettings()
        self._memory_store = _Store(mems)

    @property
    def user_display_name(self) -> str:
        return "Jacob"


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        timephrase.set_now_provider(lambda: _NOW)
        self.addCleanup(timephrase.set_now_provider, None)

    def _host(self, mems: list[_Mem]) -> _Host:
        return _Host(mems)

    def test_surfaces_upcoming(self) -> None:
        host = self._host([_mem(1, "dentist", "2026-06-27T09:00:00+00:00")])
        out = host._render_upcoming_horizon_block()
        self.assertIn("Coming up for Jacob", out)
        self.assertIn("dentist", out)

    def test_disabled_blank(self) -> None:
        host = self._host([_mem(1, "dentist", "2026-06-27T09:00:00+00:00")])
        host._settings.agent.upcoming_horizon_enabled = False
        self.assertEqual(host._render_upcoming_horizon_block(), "")

    def test_empty_window_blank(self) -> None:
        host = self._host([_mem(1, "past", "2026-01-01T09:00:00+00:00")])
        self.assertEqual(host._render_upcoming_horizon_block(), "")

    def test_cooldown_suppresses_unchanged_set(self) -> None:
        host = self._host([_mem(1, "dentist", "2026-06-27T09:00:00+00:00")])
        first = host._render_upcoming_horizon_block()
        self.assertTrue(first)
        self.assertEqual(host._upcoming_horizon_cooldown, 6)
        # Same set next turn -> suppressed, cooldown decrements.
        second = host._render_upcoming_horizon_block()
        self.assertEqual(second, "")
        self.assertEqual(host._upcoming_horizon_cooldown, 5)

    def test_changed_set_resurfaces_immediately(self) -> None:
        host = self._host([_mem(1, "dentist", "2026-06-27T09:00:00+00:00")])
        self.assertTrue(host._render_upcoming_horizon_block())
        # A new plan appears -> signature changes -> re-surfaces despite cooldown.
        host._memory_store = _Store(
            [
                _mem(1, "dentist", "2026-06-27T09:00:00+00:00"),
                _mem(2, "interview", "2026-06-28T14:00:00+00:00"),
            ]
        )
        out = host._render_upcoming_horizon_block()
        self.assertIn("interview", out)

    def test_force_bypasses_cooldown(self) -> None:
        host = self._host([_mem(1, "dentist", "2026-06-27T09:00:00+00:00")])
        self.assertTrue(host._render_upcoming_horizon_block())
        host._upcoming_horizon_force_next = True
        out = host._render_upcoming_horizon_block()
        self.assertTrue(out)
        self.assertFalse(host._upcoming_horizon_force_next)


if __name__ == "__main__":
    unittest.main()
