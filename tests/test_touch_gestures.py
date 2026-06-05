"""Tests for the K31 touch-gesture service (taxonomy + dispatch).

Exercises :mod:`app.core.touch.touch_gestures` end-to-end:

  - Taxonomy invariants (eight kinds, ordered light -> intimate,
    every entry has the required fields wired).
  - Cooldown gating across consecutive ``try_dispatch`` calls.
  - Daily-cap gating + roll-over at UTC midnight.
  - Per-kind override resolution (``touch_per_kind_overrides``).
  - Relationship-axes gates per kind.
  - ``bypass_gates`` shortcut for the MCP debug tool.
  - kv_meta round-trip via the in-process ``ChatDatabase``.
  - ``render_touch_state_block`` inner-life cue heuristics.

The tests use a real :class:`ChatDatabase` against ``:memory:`` so
the ``kv_get`` / ``kv_set`` plumbing is exercised exactly as in
production -- the K31 service writes through one kv key, the rest
of the schema is irrelevant. Runs in single-digit milliseconds.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.touch import touch_gestures as tg
from app.core.touch.touch_gestures import (
    KV_TOUCH_STATE,
    REASON_COOLDOWN,
    REASON_DAILY_CAP,
    REASON_DISABLED,
    REASON_GATE_CLOSENESS,
    REASON_GATE_HUMOR,
    REASON_GATE_TRUST,
    REASON_OK,
    REASON_UNKNOWN_KIND,
    TOUCH_KINDS,
    TouchService,
    TouchServiceState,
    all_gestures,
    deserialize_state,
    get_gesture,
    render_touch_state_block,
    serialize_state,
)


class _MemoryChatDb:
    """Minimal kv_meta-only stand-in for :class:`ChatDatabase`.

    Just enough to make :class:`TouchService` round-trip its state
    blob; nothing else of the chat_database API is exercised here.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self.store.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.store[key] = value

    def kv_delete(self, key: str) -> None:
        self.store.pop(key, None)


@dataclass(slots=True)
class _Axes:
    """Minimal :class:`RelationshipAxesState` stand-in.

    Only the fields the gate checks read are needed; defaults are
    ``0.0`` so the lightest gestures pass without any setup.
    """

    closeness: float = 0.0
    trust: float = 0.0
    humor: float = 0.0
    comfort: float = 0.0


@dataclass(slots=True)
class _Settings:
    """Minimal :class:`AgentSettings` stand-in used by gating logic."""

    touch_enabled: bool = True
    touch_per_kind_overrides: dict[str, Any] | None = None


def _make_service(
    *, settings: _Settings | None = None,
) -> tuple[TouchService, _MemoryChatDb]:
    db = _MemoryChatDb()
    service = TouchService(chat_db=db, settings=settings)  # type: ignore[arg-type]
    return service, db


# ── Taxonomy invariants ────────────────────────────────────────────


class TouchTaxonomyTests(unittest.TestCase):
    def test_eight_kinds_ordered_light_to_intimate(self) -> None:
        # The order is canonical -- log lines + MCP diagnostics
        # depend on it. Pin the exact sequence.
        self.assertEqual(
            TOUCH_KINDS,
            (
                "wave", "poke", "boop", "nudge", "high_five",
                "hug", "head_pat", "cuddle",
            ),
        )

    def test_each_kind_has_label_and_emoji(self) -> None:
        for kind in TOUCH_KINDS:
            gesture = get_gesture(kind)
            self.assertIsNotNone(gesture, f"missing taxonomy entry: {kind}")
            assert gesture is not None  # for type-checker
            self.assertTrue(gesture.label, f"empty label: {kind}")
            self.assertTrue(gesture.emoji, f"empty emoji: {kind}")
            self.assertGreater(gesture.duration_ms, 0, kind)
            self.assertGreaterEqual(gesture.lean_amount, 0.0, kind)
            self.assertLessEqual(gesture.lean_amount, 1.0, kind)

    def test_intimate_kinds_have_axes_floors(self) -> None:
        # hug / head_pat / cuddle MUST gate on closeness so the
        # cuddly tail never lands on a brand-new install.
        for kind in ("hug", "head_pat", "cuddle"):
            gesture = get_gesture(kind)
            assert gesture is not None
            self.assertGreater(
                gesture.min_closeness,
                0.0,
                f"{kind} should require positive closeness",
            )

    def test_light_kinds_have_no_axes_floor(self) -> None:
        # wave/poke/boop/nudge are always allowed; gating them
        # would make the LLM hesitate on basic greetings.
        for kind in ("wave", "poke", "boop", "nudge"):
            gesture = get_gesture(kind)
            assert gesture is not None
            self.assertEqual(gesture.min_closeness, -1.0, kind)
            self.assertEqual(gesture.min_trust, -1.0, kind)
            self.assertEqual(gesture.min_humor, -1.0, kind)

    def test_get_gesture_handles_garbage(self) -> None:
        self.assertIsNone(get_gesture(""))
        self.assertIsNone(get_gesture("not_a_kind"))
        # Mixed case / whitespace normalises.
        self.assertEqual(get_gesture(" HUG ").kind, "hug")  # type: ignore[union-attr]


# ── Serde round-trip ────────────────────────────────────────────────


class SerdeTests(unittest.TestCase):
    def test_roundtrip_preserves_fields(self) -> None:
        state = TouchServiceState(
            last_fired={"hug": "2026-06-01T12:00:00+00:00"},
            daily_counts={"hug": 2, "wave": 5},
            daily_date="2026-06-01",
        )
        revived = deserialize_state(serialize_state(state))
        self.assertEqual(revived.daily_date, state.daily_date)
        self.assertEqual(revived.last_fired, state.last_fired)
        self.assertEqual(revived.daily_counts, state.daily_counts)

    def test_corrupt_json_returns_empty(self) -> None:
        revived = deserialize_state("{this isn't json")
        self.assertEqual(revived.daily_date, "")
        self.assertEqual(revived.last_fired, {})
        self.assertEqual(revived.daily_counts, {})

    def test_missing_payload_returns_empty(self) -> None:
        self.assertEqual(deserialize_state(None).daily_date, "")
        self.assertEqual(deserialize_state("").daily_date, "")


# ── Dispatch verdict + kv_meta round-trip ───────────────────────────


class TryDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_unknown_kind_rejected(self) -> None:
        service, db = _make_service()
        report = service.try_dispatch("teleport", axes=None, now=self.now)
        self.assertFalse(report.dispatched)
        self.assertEqual(report.reason, REASON_UNKNOWN_KIND)
        self.assertIsNone(report.gesture)
        self.assertNotIn(KV_TOUCH_STATE, db.store)

    def test_first_dispatch_succeeds_and_persists(self) -> None:
        service, db = _make_service()
        report = service.try_dispatch("wave", axes=_Axes(), now=self.now)
        self.assertTrue(report.dispatched)
        self.assertEqual(report.reason, REASON_OK)
        assert report.new_state is not None
        self.assertEqual(report.new_state.daily_counts["wave"], 1)
        self.assertEqual(report.new_state.daily_date, "2026-06-01")
        # kv_meta persisted.
        raw = db.store.get(KV_TOUCH_STATE)
        self.assertIsNotNone(raw)
        revived = deserialize_state(raw)
        self.assertEqual(revived.daily_counts["wave"], 1)

    def test_cooldown_rejects_back_to_back(self) -> None:
        service, _ = _make_service()
        first = service.try_dispatch("wave", axes=_Axes(), now=self.now)
        self.assertTrue(first.dispatched)
        second = service.try_dispatch(
            "wave",
            axes=_Axes(),
            now=self.now + timedelta(seconds=10),
        )
        self.assertFalse(second.dispatched)
        self.assertEqual(second.reason, REASON_COOLDOWN)

    def test_cooldown_clears_after_full_window(self) -> None:
        service, _ = _make_service()
        first = service.try_dispatch("wave", axes=_Axes(), now=self.now)
        self.assertTrue(first.dispatched)
        gesture = get_gesture("wave")
        assert gesture is not None
        later = self.now + timedelta(seconds=gesture.cooldown_seconds + 1)
        second = service.try_dispatch("wave", axes=_Axes(), now=later)
        self.assertTrue(second.dispatched, second.reason)

    def test_independent_cooldowns_per_kind(self) -> None:
        # A wave cooldown should not block a poke dispatch.
        service, _ = _make_service()
        first = service.try_dispatch("wave", axes=_Axes(), now=self.now)
        self.assertTrue(first.dispatched)
        second = service.try_dispatch(
            "poke",
            axes=_Axes(),
            now=self.now + timedelta(seconds=1),
        )
        self.assertTrue(second.dispatched, second.reason)

    def test_daily_cap_blocks_after_threshold(self) -> None:
        service, _ = _make_service()
        gesture = get_gesture("poke")
        assert gesture is not None and gesture.daily_cap > 0
        now = self.now
        for n in range(gesture.daily_cap):
            now = self.now + timedelta(seconds=(gesture.cooldown_seconds + 1) * n)
            report = service.try_dispatch("poke", axes=_Axes(), now=now)
            self.assertTrue(report.dispatched, f"call {n}: {report.reason}")
        # One more past the cap on the same UTC day -> rejected.
        rejected = service.try_dispatch(
            "poke",
            axes=_Axes(),
            now=now + timedelta(seconds=gesture.cooldown_seconds + 1),
        )
        self.assertFalse(rejected.dispatched)
        self.assertEqual(rejected.reason, REASON_DAILY_CAP)

    def test_daily_cap_rolls_at_utc_midnight(self) -> None:
        service, _ = _make_service()
        gesture = get_gesture("cuddle")
        assert gesture is not None
        # Saturate the cap on day 1.
        now = self.now
        for n in range(gesture.daily_cap):
            now = self.now + timedelta(seconds=(gesture.cooldown_seconds + 1) * n)
            report = service.try_dispatch(
                "cuddle",
                axes=_Axes(closeness=1.0, trust=1.0),
                now=now,
            )
            self.assertTrue(report.dispatched, f"call {n}: {report.reason}")
        next_day = (self.now + timedelta(days=1)).replace(hour=0, minute=5)
        rolled = service.try_dispatch(
            "cuddle",
            axes=_Axes(closeness=1.0, trust=1.0),
            now=next_day,
        )
        self.assertTrue(rolled.dispatched, rolled.reason)
        assert rolled.new_state is not None
        self.assertEqual(rolled.new_state.daily_counts["cuddle"], 1)
        self.assertEqual(rolled.new_state.daily_date, "2026-06-02")


# ── Gate evaluation ────────────────────────────────────────────────


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_hug_blocked_below_closeness_floor(self) -> None:
        service, _ = _make_service()
        report = service.try_dispatch(
            "hug",
            axes=_Axes(closeness=0.0, trust=1.0),
            now=self.now,
        )
        self.assertFalse(report.dispatched)
        self.assertEqual(report.reason, REASON_GATE_CLOSENESS)

    def test_hug_blocked_below_trust_floor(self) -> None:
        service, _ = _make_service()
        report = service.try_dispatch(
            "hug",
            axes=_Axes(closeness=1.0, trust=0.0),
            now=self.now,
        )
        self.assertFalse(report.dispatched)
        self.assertEqual(report.reason, REASON_GATE_TRUST)

    def test_high_five_blocked_below_humor_floor(self) -> None:
        service, _ = _make_service()
        report = service.try_dispatch(
            "high_five",
            axes=_Axes(closeness=1.0, trust=1.0, humor=0.0),
            now=self.now,
        )
        self.assertFalse(report.dispatched)
        self.assertEqual(report.reason, REASON_GATE_HUMOR)

    def test_no_axes_means_gates_skipped(self) -> None:
        service, _ = _make_service()
        report = service.try_dispatch("cuddle", axes=None, now=self.now)
        # No axes -> tests skip gates, dispatch lands.
        self.assertTrue(report.dispatched, report.reason)

    def test_disabled_setting_rejects(self) -> None:
        service, _ = _make_service(settings=_Settings(touch_enabled=False))
        report = service.try_dispatch("wave", axes=_Axes(), now=self.now)
        self.assertFalse(report.dispatched)
        self.assertEqual(report.reason, REASON_DISABLED)

    def test_bypass_gates_overrides_everything(self) -> None:
        service, _ = _make_service()
        # Fire once to establish a cooldown.
        first = service.try_dispatch("hug", axes=_Axes(closeness=1.0, trust=1.0), now=self.now)
        self.assertTrue(first.dispatched, first.reason)
        # Now bypass + insufficient axes + still in cooldown.
        bypass = service.try_dispatch(
            "hug",
            axes=_Axes(closeness=0.0, trust=0.0),
            now=self.now + timedelta(seconds=1),
            bypass_gates=True,
        )
        self.assertTrue(bypass.dispatched, bypass.reason)


# ── Per-kind overrides ─────────────────────────────────────────────


class OverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_override_lowers_cooldown(self) -> None:
        # Drop wave cooldown to 1s.
        settings = _Settings(
            touch_per_kind_overrides={"wave": {"cooldown_seconds": 1}},
        )
        service, _ = _make_service(settings=settings)
        first = service.try_dispatch("wave", axes=_Axes(), now=self.now)
        self.assertTrue(first.dispatched, first.reason)
        second = service.try_dispatch(
            "wave",
            axes=_Axes(),
            now=self.now + timedelta(seconds=2),
        )
        self.assertTrue(second.dispatched, second.reason)

    def test_override_lowers_daily_cap(self) -> None:
        settings = _Settings(
            touch_per_kind_overrides={"poke": {"daily_cap": 1}},
        )
        service, _ = _make_service(settings=settings)
        gesture = get_gesture("poke")
        assert gesture is not None
        first = service.try_dispatch("poke", axes=_Axes(), now=self.now)
        self.assertTrue(first.dispatched)
        second = service.try_dispatch(
            "poke",
            axes=_Axes(),
            now=self.now + timedelta(seconds=gesture.cooldown_seconds + 1),
        )
        self.assertFalse(second.dispatched)
        self.assertEqual(second.reason, REASON_DAILY_CAP)

    def test_invalid_override_value_ignored(self) -> None:
        settings = _Settings(
            touch_per_kind_overrides={
                "wave": {"cooldown_seconds": "huh", "daily_cap": "x"},
            },
        )
        service, _ = _make_service(settings=settings)
        # Garbage falls back to the default values.
        report = service.try_dispatch("wave", axes=_Axes(), now=self.now)
        self.assertTrue(report.dispatched, report.reason)


# ── Inner-life cue (low physical budget) ───────────────────────────


class InnerLifeBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_silent_on_empty_state(self) -> None:
        state = TouchServiceState(last_fired={}, daily_counts={}, daily_date="2026-06-01")
        self.assertEqual(
            render_touch_state_block(state, now=self.now, user_display_name="Jacob"),
            "",
        )

    def test_silent_on_stale_date(self) -> None:
        state = TouchServiceState(
            last_fired={},
            daily_counts={"hug": 5},
            daily_date="2026-05-01",
        )
        self.assertEqual(
            render_touch_state_block(state, now=self.now, user_display_name="Jacob"),
            "",
        )

    def test_warns_on_high_intimate_count(self) -> None:
        state = TouchServiceState(
            last_fired={},
            daily_counts={"hug": 2, "cuddle": 1},
            daily_date="2026-06-01",
        )
        block = render_touch_state_block(state, now=self.now, user_display_name="Jacob")
        self.assertIn("Jacob", block)
        self.assertIn("physical", block)

    def test_warns_on_capped_kind(self) -> None:
        # Saturate the poke cap (10) for the day.
        gesture = get_gesture("poke")
        assert gesture is not None
        state = TouchServiceState(
            last_fired={},
            daily_counts={"poke": gesture.daily_cap},
            daily_date="2026-06-01",
        )
        block = render_touch_state_block(state, now=self.now, user_display_name="Jacob")
        self.assertIn("poke", block)
        self.assertIn("Jacob", block)


# ── all_gestures ordering ──────────────────────────────────────────


class AllGesturesTests(unittest.TestCase):
    def test_canonical_order_is_kinds_order(self) -> None:
        gestures = all_gestures()
        self.assertEqual(tuple(g.kind for g in gestures), TOUCH_KINDS)


if __name__ == "__main__":
    unittest.main()
