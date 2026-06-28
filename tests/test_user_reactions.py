"""Tests for :mod:`app.core.relationship.user_reactions` (K32).

Covers:

  - Taxonomy invariants (six kinds, ``surprise`` is signal-only).
  - ``compute_deltas`` soft-cap behaviour.
  - ``apply_daily_cap`` arithmetic + UTC rollover.
  - kv_meta serde round-trip.
  - ``render_user_reactions_block`` cue shapes for the three
    branches (single, single-kind multi, mixed-kind multi).

Pure data layer -- no I/O beyond an in-memory :class:`ChatDatabase`
for the kv_meta round-trip. Runs in milliseconds.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.core.relationship import user_reactions as ur
from app.core.relationship.user_reactions import (
    DailyCapState,
    DailyCapVerdict,
    KV_USER_REACTIONS_DAILY,
    REACTION_KINDS,
    apply_daily_cap,
    compute_deltas,
    deserialize_daily_state,
    get_reaction_kind,
    is_valid_kind,
    load_daily_state,
    reactions_metadata,
    render_user_reactions_block,
    reset_daily_state,
    save_daily_state,
    serialize_daily_state,
)


# ── Taxonomy invariants ────────────────────────────────────────────


class TaxonomyTests(unittest.TestCase):
    def test_kinds_in_canonical_order(self) -> None:
        self.assertEqual(
            REACTION_KINDS,
            (
                "heart",
                "hug",
                "laugh",
                "thumbs",
                "rose",
                "grateful",
                "blush",
                "eyeroll",
                "moved",
                "surprise",
            ),
        )

    def test_each_kind_has_emoji_and_label(self) -> None:
        for kind in REACTION_KINDS:
            meta = get_reaction_kind(kind)
            self.assertIsNotNone(meta, f"missing: {kind}")
            assert meta is not None
            self.assertTrue(meta.emoji, kind)
            self.assertTrue(meta.label, kind)
            self.assertIn(" ", meta.label, f"{kind} label should be a phrase")

    def test_surprise_has_no_axis_deltas(self) -> None:
        # surprise reads as signal-only by design.
        self.assertEqual(compute_deltas("surprise"), {})

    def test_other_kinds_carry_at_least_one_delta(self) -> None:
        for kind in REACTION_KINDS:
            if kind == "surprise":
                continue
            deltas = compute_deltas(kind)
            self.assertTrue(deltas, f"{kind} should carry at least one axis delta")

    def test_is_valid_kind_normalises_input(self) -> None:
        self.assertTrue(is_valid_kind("heart"))
        self.assertTrue(is_valid_kind(" HEART "))
        self.assertFalse(is_valid_kind(""))
        self.assertFalse(is_valid_kind("rage"))


# ── compute_deltas soft-cap ───────────────────────────────────────


class ComputeDeltasTests(unittest.TestCase):
    def test_unknown_kind_returns_empty(self) -> None:
        self.assertEqual(compute_deltas("nope"), {})

    def test_soft_cap_clamps_each_value(self) -> None:
        # Force the cap down to 0.005 -- every entry should saturate
        # at that bound, regardless of sign.
        deltas = compute_deltas("hug", soft_cap=0.005)
        for axis, value in deltas.items():
            self.assertLessEqual(abs(value), 0.005, axis)

    def test_default_cap_lets_table_values_through(self) -> None:
        deltas = compute_deltas("hug")
        # ``hug`` has closeness=0.04 in the table; with the default
        # 0.04 cap that should land intact.
        self.assertAlmostEqual(deltas["closeness"], 0.04)


# ── DailyCapState serde ────────────────────────────────────────────


class SerdeTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        state = DailyCapState(
            daily_date="2026-06-01",
            axis_totals={"closeness": 0.05, "trust": 0.02},
        )
        revived = deserialize_daily_state(serialize_daily_state(state))
        self.assertEqual(revived.daily_date, state.daily_date)
        self.assertAlmostEqual(revived.axis_totals["closeness"], 0.05)
        self.assertAlmostEqual(revived.axis_totals["trust"], 0.02)

    def test_corrupt_returns_empty(self) -> None:
        revived = deserialize_daily_state("not json")
        self.assertEqual(revived.daily_date, "")
        self.assertEqual(revived.axis_totals, {})

    def test_load_save_through_chat_db(self) -> None:
        # Minimal kv-only stand-in -- the user_reactions helpers
        # only call kv_get / kv_set / kv_delete on the db.
        class _MemoryDb:
            def __init__(self) -> None:
                self.store: dict[str, str] = {}

            def kv_get(self, key: str) -> str | None:
                return self.store.get(key)

            def kv_set(self, key: str, value: str) -> None:
                self.store[key] = value

            def kv_delete(self, key: str) -> None:
                self.store.pop(key, None)

        db = _MemoryDb()
        state = DailyCapState(
            daily_date="2026-06-01",
            axis_totals={"humor": 0.07},
        )
        save_daily_state(db, state)  # type: ignore[arg-type]
        revived = load_daily_state(db)  # type: ignore[arg-type]
        self.assertEqual(revived.daily_date, state.daily_date)
        self.assertAlmostEqual(revived.axis_totals["humor"], 0.07)
        reset_daily_state(db)  # type: ignore[arg-type]
        self.assertNotIn(KV_USER_REACTIONS_DAILY, db.store)


# ── apply_daily_cap arithmetic ─────────────────────────────────────


class ApplyDailyCapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        self.empty = DailyCapState(daily_date="2026-06-01", axis_totals={})

    def test_under_cap_passes_through(self) -> None:
        verdict = apply_daily_cap(
            {"closeness": 0.03},
            self.empty,
            now=self.now,
            daily_cap=0.15,
        )
        self.assertEqual(verdict.effective_deltas, {"closeness": 0.03})
        self.assertEqual(verdict.capped_axes, ())
        self.assertAlmostEqual(verdict.new_state.axis_totals["closeness"], 0.03)

    def test_over_cap_trims_to_remaining(self) -> None:
        # Already 0.12 of 0.15 used -> only 0.03 remaining.
        state = DailyCapState(
            daily_date="2026-06-01",
            axis_totals={"closeness": 0.12},
        )
        verdict = apply_daily_cap(
            {"closeness": 0.05},
            state,
            now=self.now,
            daily_cap=0.15,
        )
        self.assertAlmostEqual(verdict.effective_deltas["closeness"], 0.03)
        self.assertIn("closeness", verdict.capped_axes)
        self.assertAlmostEqual(verdict.new_state.axis_totals["closeness"], 0.15)

    def test_at_cap_drops_axis(self) -> None:
        state = DailyCapState(
            daily_date="2026-06-01",
            axis_totals={"closeness": 0.15},
        )
        verdict = apply_daily_cap(
            {"closeness": 0.04},
            state,
            now=self.now,
            daily_cap=0.15,
        )
        # Nothing should land for closeness; the axis should be
        # listed in ``capped_axes`` for diagnostic logging.
        self.assertNotIn("closeness", verdict.effective_deltas)
        self.assertIn("closeness", verdict.capped_axes)
        self.assertAlmostEqual(
            verdict.new_state.axis_totals["closeness"], 0.15,
        )

    def test_date_rollover_resets_ledger(self) -> None:
        state = DailyCapState(
            daily_date="2026-05-31",
            axis_totals={"closeness": 0.15},
        )
        verdict = apply_daily_cap(
            {"closeness": 0.04},
            state,
            now=self.now,
            daily_cap=0.15,
        )
        # Date rolled -- yesterday's saturation is irrelevant.
        self.assertAlmostEqual(verdict.effective_deltas["closeness"], 0.04)
        self.assertAlmostEqual(
            verdict.new_state.axis_totals["closeness"], 0.04,
        )
        self.assertEqual(verdict.new_state.daily_date, "2026-06-01")

    def test_zero_cap_blocks_everything(self) -> None:
        verdict = apply_daily_cap(
            {"closeness": 0.03, "humor": 0.02},
            self.empty,
            now=self.now,
            daily_cap=0.0,
        )
        self.assertEqual(verdict.effective_deltas, {})
        self.assertIn("closeness", verdict.capped_axes)
        self.assertIn("humor", verdict.capped_axes)


# ── render_user_reactions_block ───────────────────────────────────


class RenderBlockTests(unittest.TestCase):
    def test_empty_returns_blank(self) -> None:
        self.assertEqual(render_user_reactions_block([]), "")

    def test_single_reaction_cue(self) -> None:
        block = render_user_reactions_block(
            [(42, "heart")], user_display_name="Jacob",
        )
        self.assertIn("Jacob", block)
        self.assertIn("hearted", block)

    def test_multi_same_kind_uses_plural_phrasing(self) -> None:
        block = render_user_reactions_block(
            [(1, "heart"), (2, "heart")], user_display_name="Jacob",
        )
        self.assertIn("Jacob", block)
        self.assertIn("several", block)
        # The single-shot label "just hearted your reply" should NOT
        # appear -- the multi cue uses a different phrasing.
        self.assertNotIn("just hearted your reply", block)

    def test_mixed_kinds_lists_them(self) -> None:
        block = render_user_reactions_block(
            [(1, "heart"), (2, "laugh"), (3, "hug")],
            user_display_name="Jacob",
        )
        self.assertIn("Jacob", block)
        self.assertIn("heart", block)
        self.assertIn("laugh", block)
        self.assertIn("hug", block)

    def test_unknown_kinds_silently_dropped(self) -> None:
        # The cue should still render the surviving valid reaction.
        block = render_user_reactions_block(
            [(1, "no_such_kind"), (2, "heart")],
            user_display_name="Jacob",
        )
        self.assertIn("hearted", block)


# ── reactions_metadata snapshot ───────────────────────────────────


class MetadataTests(unittest.TestCase):
    def test_metadata_has_one_entry_per_kind_and_carries_deltas(self) -> None:
        rows = reactions_metadata()
        self.assertEqual(len(rows), len(REACTION_KINDS))
        for row in rows:
            self.assertIn("kind", row)
            self.assertIn("emoji", row)
            self.assertIn("label", row)
            self.assertIn("deltas", row)
            self.assertTrue(is_valid_kind(row["kind"]))


if __name__ == "__main__":
    unittest.main()
