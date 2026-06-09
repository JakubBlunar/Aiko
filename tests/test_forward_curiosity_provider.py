"""Controller-level tests for the K34 forward-curiosity provider.

Exercises
:meth:`InnerLifeProvidersMixin._render_forward_curiosity_block` via a
minimal mixin host stub (the same approach as
``tests/test_turning_over_provider.py``). Focuses on the provider
plumbing: master-switch gate, pending-slot one-shot clear, threshold
double-check, the one-of ``_gap_cue_surfaced`` guard (defer to
turning_over / away_activities), the surfacing watermark, and the
force-next bypass.
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any

from app.core.proactive.forward_curiosity_worker import (
    FORWARD_CURIOSITY_JOURNAL_KEY,
)
from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin


class _FakeChatDb:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self.store.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.store[key] = value


def _make_agent_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(forward_curiosity_enabled=True)
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_memory_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(forward_curiosity_min_gap_hours=4.0)
    base.update(overrides)
    return SimpleNamespace(**base)


class _Host(InnerLifeProvidersMixin):
    def __init__(
        self,
        *,
        questions: list[dict[str, Any]] | None = None,
        pending_seconds: float | None = None,
        force_next: bool = False,
        gap_cue_surfaced: bool = False,
        agent_settings: SimpleNamespace | None = None,
        memory_settings: SimpleNamespace | None = None,
    ) -> None:
        self._settings = SimpleNamespace(
            agent=agent_settings or _make_agent_settings(),
        )
        self._memory_settings = memory_settings or _make_memory_settings()
        self._chat_db = _FakeChatDb()
        if questions:
            self._chat_db.store[FORWARD_CURIOSITY_JOURNAL_KEY] = json.dumps(
                questions
            )
        self._pending_forward_curiosity_seconds = pending_seconds
        self._forward_curiosity_force_next = force_next
        self._gap_cue_surfaced = gap_cue_surfaced
        self.user_display_name = "Jacob"


def _q(at: str = "2026-06-10T00:00:00+00:00") -> dict[str, Any]:
    return {
        "at": at,
        "question": "how the espresso machine is going",
        "source": "future_plan",
        "source_id": "7",
    }


class MasterSwitchTests(unittest.TestCase):
    def test_disabled_returns_empty(self) -> None:
        host = _Host(
            questions=[_q()],
            pending_seconds=5 * 3600.0,
            agent_settings=_make_agent_settings(
                forward_curiosity_enabled=False
            ),
        )
        self.assertEqual(host._render_forward_curiosity_block(), "")


class PendingSlotTests(unittest.TestCase):
    def test_no_pending_value_silent(self) -> None:
        host = _Host(questions=[_q()], pending_seconds=None)
        self.assertEqual(host._render_forward_curiosity_block(), "")

    def test_fires_and_clears_slot(self) -> None:
        host = _Host(questions=[_q()], pending_seconds=5 * 3600.0)
        out = host._render_forward_curiosity_block()
        self.assertTrue(out.startswith("You've been wondering"))
        self.assertIn("espresso", out)
        self.assertIsNone(host._pending_forward_curiosity_seconds)
        # Sets the gap-cue flag so nothing else surfaces this assembly.
        self.assertTrue(host._gap_cue_surfaced)
        # Watermark advanced.
        self.assertEqual(
            host._chat_db.store.get("forward_curiosity.last_surfaced_at"),
            _q()["at"],
        )

    def test_below_threshold_silent(self) -> None:
        host = _Host(questions=[_q()], pending_seconds=2 * 3600.0)  # < 4h
        self.assertEqual(host._render_forward_curiosity_block(), "")

    def test_empty_ring_silent(self) -> None:
        host = _Host(questions=[], pending_seconds=5 * 3600.0)
        self.assertEqual(host._render_forward_curiosity_block(), "")


class OneOfGuardTests(unittest.TestCase):
    def test_defers_when_gap_cue_already_surfaced(self) -> None:
        # turning_over or away_activities already fired this assembly.
        host = _Host(
            questions=[_q()],
            pending_seconds=5 * 3600.0,
            gap_cue_surfaced=True,
        )
        self.assertEqual(host._render_forward_curiosity_block(), "")
        # Slot is NOT consumed when we defer on the guard, so the value
        # is preserved (the higher-priority cue owns this return).
        self.assertEqual(
            host._pending_forward_curiosity_seconds, 5 * 3600.0
        )

    def test_force_next_overrides_gap_cue_guard(self) -> None:
        host = _Host(
            questions=[_q()],
            force_next=True,
            gap_cue_surfaced=True,
        )
        out = host._render_forward_curiosity_block()
        self.assertTrue(out.startswith("You've been wondering"))
        # Force flag consumed.
        self.assertFalse(host._forward_curiosity_force_next)


class WatermarkTests(unittest.TestCase):
    def test_already_surfaced_is_silent(self) -> None:
        host = _Host(questions=[_q()], pending_seconds=5 * 3600.0)
        host._chat_db.store["forward_curiosity.last_surfaced_at"] = _q()["at"]
        self.assertEqual(host._render_forward_curiosity_block(), "")


if __name__ == "__main__":
    unittest.main()
