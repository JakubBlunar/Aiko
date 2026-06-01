"""Controller-level tests for the K28 turning-over provider.

Exercises
:meth:`InnerLifeProvidersMixin._render_turning_over_block` by
building a minimal stub that simulates the controller surface it
reads from. Avoids spinning up the full
:class:`SessionController` which would import half the world.

The picker itself is covered in ``tests/test_turning_over_picker.py``;
this module focuses on the provider plumbing -- master-switch
gate, force-next bypass, one-shot pending-slot clear, threshold
double-check, INFO log emission, and the memory_store /
goal_store / rag_store dependency surface.
"""
from __future__ import annotations

import logging
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import numpy as np

from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin


# ── Test stubs ─────────────────────────────────────────────────────────


@dataclass(slots=True)
class _StubMemory:
    id: int
    content: str
    embedding: np.ndarray
    created_at: str


class _FakeMemoryStore:
    def __init__(self, rows: list[_StubMemory]) -> None:
        self._rows = rows

    def iter_by_kind(self, kind: str) -> list[_StubMemory]:
        if kind != "reflection":
            return []
        return list(self._rows)


class _FakeGoalStore:
    def __init__(self, vectors: list[np.ndarray]) -> None:
        self._vectors = vectors

    def active_goal_vectors(self) -> list[np.ndarray]:
        return list(self._vectors)


class _FakeRagStore:
    def __init__(self, vectors: list[np.ndarray]) -> None:
        self._vectors = vectors
        self.calls: list[tuple[str, int]] = []

    def list_recent_user_vectors(
        self, *, user_id_prefix: str, limit: int,
    ) -> list[np.ndarray]:
        self.calls.append((user_id_prefix, limit))
        return list(self._vectors)


def _vec(*values: float) -> np.ndarray:
    arr = np.asarray(values, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm else arr


_VEC_ALIGNED = _vec(1.0, 0.0, 0.0)


def _make_agent_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(turning_over_enabled=True)
    base.update(overrides)
    return SimpleNamespace(**base)


def _make_memory_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(
        turning_over_min_gap_minutes=90.0,
        turning_over_min_age_hours=24.0,
        turning_over_max_age_hours=72.0,
        turning_over_min_topical_similarity=0.30,
        turning_over_recent_msgs_window=12,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Host(InnerLifeProvidersMixin):
    """Minimal mixin host with the attributes the provider reads."""

    def __init__(
        self,
        *,
        reflections: list[_StubMemory] | None = None,
        goal_vecs: list[np.ndarray] | None = None,
        recent_user_vecs: list[np.ndarray] | None = None,
        pending_seconds: float | None = None,
        force_next: bool = False,
        agent_settings: SimpleNamespace | None = None,
        memory_settings: SimpleNamespace | None = None,
        user_id: str = "default",
    ) -> None:
        self._settings = SimpleNamespace(
            agent=agent_settings or _make_agent_settings(),
        )
        self._memory_settings = memory_settings or _make_memory_settings()
        self._memory_store = _FakeMemoryStore(reflections or [])
        self._goal_store = _FakeGoalStore(goal_vecs or [])
        self._rag_store = _FakeRagStore(recent_user_vecs or [])
        self._user_id = user_id
        self._pending_turning_over_seconds = pending_seconds
        self._turning_over_force_next = force_next
        self._last_turning_over: Any = None
        self.user_display_name = "Jacob"


def _iso_ago(hours: float, *, now: datetime | None = None) -> str:
    """Build an ISO timestamp ``hours`` hours before ``now`` (UTC)."""
    when = now or datetime.now(timezone.utc)
    return (when - timedelta(hours=hours)).isoformat()


def _make_reflection(
    *,
    id_: int = 1,
    content: str = "Jacob mentioned the interview prep is harder",
    aligned_with: np.ndarray = _VEC_ALIGNED,
    hours_ago: float = 30.0,
) -> _StubMemory:
    return _StubMemory(
        id=id_,
        content=content,
        embedding=aligned_with,
        created_at=_iso_ago(hours_ago),
    )


# ── Master switch ──────────────────────────────────────────────────────


class MasterSwitchTests(unittest.TestCase):
    def test_disabled_returns_empty(self) -> None:
        host = _Host(
            reflections=[_make_reflection()],
            goal_vecs=[_VEC_ALIGNED],
            pending_seconds=120 * 60.0,
            agent_settings=_make_agent_settings(turning_over_enabled=False),
        )
        self.assertEqual(host._render_turning_over_block(), "")
        # Pending slot is preserved when the master switch is off
        # (no harm in leaving the value for when the switch flips).
        self.assertEqual(host._pending_turning_over_seconds, 120 * 60.0)


# ── Pending-slot gate ─────────────────────────────────────────────────


class PendingSlotTests(unittest.TestCase):
    def test_no_pending_value_silent(self) -> None:
        host = _Host(
            reflections=[_make_reflection()],
            goal_vecs=[_VEC_ALIGNED],
            pending_seconds=None,
        )
        self.assertEqual(host._render_turning_over_block(), "")
        # Slot stays None.
        self.assertIsNone(host._pending_turning_over_seconds)

    def test_one_shot_clears_slot_on_fire(self) -> None:
        host = _Host(
            reflections=[_make_reflection()],
            goal_vecs=[_VEC_ALIGNED],
            pending_seconds=120 * 60.0,
        )
        out = host._render_turning_over_block()
        self.assertTrue(out.startswith("Turning over:"))
        # Slot is cleared so the cue doesn't re-fire next turn.
        self.assertIsNone(host._pending_turning_over_seconds)
        # Last-fire diagnostic populated.
        self.assertIsNotNone(host._last_turning_over)

    def test_one_shot_clears_slot_on_silent_picker(self) -> None:
        # Picker returns None (orthogonal vectors → below threshold).
        host = _Host(
            reflections=[_make_reflection(aligned_with=_VEC_ALIGNED)],
            goal_vecs=[_vec(0.0, 1.0, 0.0)],
            pending_seconds=120 * 60.0,
        )
        out = host._render_turning_over_block()
        self.assertEqual(out, "")
        # Slot is still cleared on silent path so we don't reattempt
        # the same picker on the next turn.
        self.assertIsNone(host._pending_turning_over_seconds)


# ── Force-next bypass ──────────────────────────────────────────────────


class ForceNextTests(unittest.TestCase):
    def test_force_next_bypasses_missing_slot(self) -> None:
        host = _Host(
            reflections=[_make_reflection()],
            goal_vecs=[_VEC_ALIGNED],
            pending_seconds=None,
            force_next=True,
        )
        out = host._render_turning_over_block()
        self.assertTrue(out.startswith("Turning over:"))
        # Bypass consumed.
        self.assertFalse(host._turning_over_force_next)

    def test_force_next_consumed_even_on_silent(self) -> None:
        # Empty reflections corpus → silent.
        host = _Host(
            reflections=[],
            goal_vecs=[_VEC_ALIGNED],
            pending_seconds=None,
            force_next=True,
        )
        self.assertEqual(host._render_turning_over_block(), "")
        # The bypass is still consumed -- it's strictly one-turn.
        self.assertFalse(host._turning_over_force_next)


# ── Threshold double-check ─────────────────────────────────────────────


class ThresholdDoubleCheckTests(unittest.TestCase):
    def test_stashed_value_below_threshold_silenced(self) -> None:
        # Slot armed with 10 min, threshold is 90 min → double-check
        # filters it out even though the post-turn arm shouldn't have
        # done this. Defensive against settings changes between turns.
        host = _Host(
            reflections=[_make_reflection()],
            goal_vecs=[_VEC_ALIGNED],
            pending_seconds=10 * 60.0,
        )
        self.assertEqual(host._render_turning_over_block(), "")
        # Slot is cleared regardless.
        self.assertIsNone(host._pending_turning_over_seconds)

    def test_threshold_at_boundary_passes(self) -> None:
        host = _Host(
            reflections=[_make_reflection()],
            goal_vecs=[_VEC_ALIGNED],
            pending_seconds=90 * 60.0,  # exactly at threshold
        )
        out = host._render_turning_over_block()
        self.assertTrue(out.startswith("Turning over:"))


# ── Picker integration ───────────────────────────────────────────────


class PickerIntegrationTests(unittest.TestCase):
    def test_empty_reflections_silent(self) -> None:
        host = _Host(
            reflections=[],
            goal_vecs=[_VEC_ALIGNED],
            pending_seconds=120 * 60.0,
        )
        self.assertEqual(host._render_turning_over_block(), "")
        self.assertIsNone(host._last_turning_over)

    def test_uses_user_id_for_rag(self) -> None:
        host = _Host(
            reflections=[_make_reflection()],
            recent_user_vecs=[_VEC_ALIGNED],
            pending_seconds=120 * 60.0,
            user_id="alice",
        )
        host._render_turning_over_block()
        # Rag store called with the configured user prefix.
        self.assertEqual(host._rag_store.calls[0][0], "alice")
        # And the configured window.
        self.assertEqual(host._rag_store.calls[0][1], 12)

    def test_zero_msgs_window_skips_rag(self) -> None:
        host = _Host(
            reflections=[_make_reflection()],
            goal_vecs=[_VEC_ALIGNED],
            recent_user_vecs=[_VEC_ALIGNED],
            pending_seconds=120 * 60.0,
            memory_settings=_make_memory_settings(
                turning_over_recent_msgs_window=0,
            ),
        )
        host._render_turning_over_block()
        # No call to RAG when the window is 0.
        self.assertEqual(host._rag_store.calls, [])


# ── INFO logging ──────────────────────────────────────────────────────


class LoggingTests(unittest.TestCase):
    def test_fire_emits_info_log(self) -> None:
        host = _Host(
            reflections=[_make_reflection()],
            goal_vecs=[_VEC_ALIGNED],
            pending_seconds=120 * 60.0,
        )
        with self.assertLogs("app.session", level="INFO") as captured:
            out = host._render_turning_over_block()
        self.assertTrue(out)
        self.assertTrue(
            any("turning-over fire:" in r for r in captured.output),
            captured.output,
        )

    def test_silent_path_no_info_log(self) -> None:
        host = _Host(
            reflections=[],
            goal_vecs=[_VEC_ALIGNED],
            pending_seconds=120 * 60.0,
        )
        # Use a sentinel handler to verify no INFO records were emitted
        # by ``app.session`` during the silent path.
        logger = logging.getLogger("app.session")
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                if record.levelno >= logging.INFO:
                    records.append(record)

        handler = _Capture(level=logging.DEBUG)
        logger.addHandler(handler)
        try:
            host._render_turning_over_block()
        finally:
            logger.removeHandler(handler)

        self.assertFalse(
            any("turning-over fire" in r.getMessage() for r in records),
            [r.getMessage() for r in records],
        )


if __name__ == "__main__":
    unittest.main()
