"""Tests for the K31 / B7 touch-gesture service (taxonomy + dispatch).

Exercises :mod:`app.core.touch.touch_gestures`:

  - Taxonomy invariants (eight built-in kinds, ordered light ->
    intimate, every entry has the required fields wired).
  - B7 open-vocabulary dispatch: every emitted gesture lands (no
    relationship-axes / cooldown / daily-cap gating any more), unknown
    kinds are synthesized into generic custom gestures, and the only
    rejection left is the ``touch_enabled`` master flag.
  - ``synthesize_custom_gesture`` sanitisation (label fallback, emoji
    default, length clamps).
  - The dormant ``TouchServiceState`` codec still round-trips (kept for
    the MCP state snapshot), though ``try_dispatch`` no longer writes it.

Runs in single-digit milliseconds; no real I/O beyond an in-memory
kv stand-in.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.core.touch.touch_gestures import (
    DEFAULT_CUSTOM_DURATION_MS,
    DEFAULT_CUSTOM_LEAN,
    KV_TOUCH_STATE,
    REASON_DISABLED,
    REASON_OK,
    REASON_UNKNOWN_KIND,
    TOUCH_KINDS,
    TouchService,
    TouchServiceState,
    all_gestures,
    deserialize_state,
    get_gesture,
    serialize_state,
    synthesize_custom_gesture,
)


class _MemoryChatDb:
    """Minimal kv_meta-only stand-in for :class:`ChatDatabase`."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self.store.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.store[key] = value

    def kv_delete(self, key: str) -> None:
        self.store.pop(key, None)


@dataclass(slots=True)
class _Settings:
    """Minimal :class:`AgentSettings` stand-in (master enable flag)."""

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

    def test_get_gesture_handles_garbage(self) -> None:
        self.assertIsNone(get_gesture(""))
        self.assertIsNone(get_gesture("not_a_kind"))
        # Mixed case / whitespace normalises.
        self.assertEqual(get_gesture(" HUG ").kind, "hug")  # type: ignore[union-attr]


# ── Serde round-trip (dormant codec, kept for MCP snapshot) ─────────


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


# ── B7 dispatch (no gating, no state writes) ────────────────────────


class TryDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    def test_empty_kind_rejected(self) -> None:
        service, db = _make_service()
        report = service.try_dispatch("")
        self.assertFalse(report.dispatched)
        self.assertEqual(report.reason, REASON_UNKNOWN_KIND)
        self.assertIsNone(report.gesture)
        self.assertNotIn(KV_TOUCH_STATE, db.store)

    def test_builtin_dispatch_succeeds_without_persisting(self) -> None:
        service, db = _make_service()
        report = service.try_dispatch("wave")
        self.assertTrue(report.dispatched)
        self.assertEqual(report.reason, REASON_OK)
        assert report.gesture is not None
        self.assertEqual(report.gesture.kind, "wave")
        # B7: no state machine -- nothing is written to kv_meta.
        self.assertIsNone(report.new_state)
        self.assertNotIn(KV_TOUCH_STATE, db.store)

    def test_back_to_back_always_lands_no_cooldown(self) -> None:
        # B7 removed cooldown gating: firing the same kind twice in a
        # row both dispatch.
        service, _ = _make_service()
        first = service.try_dispatch("wave")
        second = service.try_dispatch("wave")
        self.assertTrue(first.dispatched)
        self.assertTrue(second.dispatched)

    def test_intimate_kind_lands_without_axes(self) -> None:
        # B7 removed axes floors: a cuddle on a brand-new install (no
        # axes wired) still dispatches.
        service, _ = _make_service()
        report = service.try_dispatch("cuddle")
        self.assertTrue(report.dispatched, report.reason)
        assert report.gesture is not None
        self.assertEqual(report.gesture.kind, "cuddle")

    def test_disabled_setting_rejects(self) -> None:
        service, _ = _make_service(settings=_Settings(touch_enabled=False))
        report = service.try_dispatch("wave")
        self.assertFalse(report.dispatched)
        self.assertEqual(report.reason, REASON_DISABLED)

    def test_unknown_kind_synthesized_as_custom(self) -> None:
        service, _ = _make_service()
        report = service.try_dispatch(
            "fist_bump", emoji="🤜", label="bumped your fist",
        )
        self.assertTrue(report.dispatched, report.reason)
        assert report.gesture is not None
        self.assertEqual(report.gesture.kind, "fist_bump")
        self.assertEqual(report.gesture.label, "bumped your fist")
        self.assertEqual(report.gesture.emoji, "🤜")
        self.assertEqual(report.gesture.lean_amount, DEFAULT_CUSTOM_LEAN)
        self.assertEqual(report.gesture.overlays, ())

    def test_custom_kind_without_label_humanizes_slug(self) -> None:
        service, _ = _make_service()
        report = service.try_dispatch("tug_sleeve")
        self.assertTrue(report.dispatched, report.reason)
        assert report.gesture is not None
        self.assertEqual(report.gesture.label, "tug sleeve")
        self.assertEqual(report.gesture.emoji, "")

    def test_axes_and_now_kwargs_are_ignored(self) -> None:
        # Retained for call-site compatibility; passing them changes
        # nothing about the verdict.
        service, _ = _make_service()
        report = service.try_dispatch(
            "hug", axes=None, now=self.now, bypass_gates=False,
        )
        self.assertTrue(report.dispatched, report.reason)


# ── synthesize_custom_gesture sanitisation ──────────────────────────


class SynthesizeCustomGestureTests(unittest.TestCase):
    def test_defaults_when_label_and_emoji_missing(self) -> None:
        g = synthesize_custom_gesture("fist_bump")
        self.assertEqual(g.kind, "fist_bump")
        self.assertEqual(g.label, "fist bump")
        self.assertEqual(g.emoji, "")
        self.assertEqual(g.duration_ms, DEFAULT_CUSTOM_DURATION_MS)
        self.assertEqual(g.lean_amount, DEFAULT_CUSTOM_LEAN)
        # No relationship gating residue -- floors are wide open.
        self.assertEqual(g.min_closeness, -1.0)
        self.assertEqual(g.cooldown_seconds, 0)
        self.assertEqual(g.daily_cap, 0)

    def test_uses_supplied_label_and_emoji(self) -> None:
        g = synthesize_custom_gesture(
            "salute", emoji="🫡", label="snapped you a salute",
        )
        self.assertEqual(g.label, "snapped you a salute")
        self.assertEqual(g.emoji, "🫡")

    def test_label_whitespace_collapsed_and_clamped(self) -> None:
        g = synthesize_custom_gesture("x", label="a" * 200)
        self.assertLessEqual(len(g.label), 60)

    def test_kind_lowercased_and_clamped(self) -> None:
        g = synthesize_custom_gesture("FIST_BUMP")
        self.assertEqual(g.kind, "fist_bump")
        long_kind = "k" * 100
        g2 = synthesize_custom_gesture(long_kind)
        self.assertLessEqual(len(g2.kind), 40)

    def test_emoji_clamped(self) -> None:
        g = synthesize_custom_gesture("x", emoji="🤜" * 20)
        self.assertLessEqual(len(g.emoji), 8)


# ── all_gestures ordering ──────────────────────────────────────────


class AllGesturesTests(unittest.TestCase):
    def test_canonical_order_is_kinds_order(self) -> None:
        gestures = all_gestures()
        self.assertEqual(tuple(g.kind for g in gestures), TOUCH_KINDS)


if __name__ == "__main__":
    unittest.main()
