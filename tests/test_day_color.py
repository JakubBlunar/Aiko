"""Tests for :mod:`app.core.affect.day_color` (K27 personality backlog).

The K27 pure module has no I/O and no scheduler; the tests just feed
scripted inputs and assert the dataclass / palette / function outputs.
No mocks needed; everything runs in-process and finishes in
milliseconds.
"""
from __future__ import annotations

import random
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone

from app.core.affect import day_color
from app.core.affect.day_color import (
    PALETTE,
    DayColor,
    get_color_by_name,
    is_stale,
    render_inner_life_block,
    roll_for_today,
)


# ── palette shape ────────────────────────────────────────────────────


class PaletteShapeTests(unittest.TestCase):
    """The palette is a public contract -- the MCP debug tool dumps
    it, the persona file lists each colour, and the settings doc
    references the count. If a name changes, both must move in
    lockstep, so this test pins the shape."""

    def test_palette_has_ten_entries(self) -> None:
        # The patterns.md spec names exactly 10 colours. Adjust this
        # test deliberately when adding/removing one -- it's a guard
        # against an accidental palette mutation.
        self.assertEqual(len(PALETTE), 10)

    def test_palette_names_are_unique(self) -> None:
        names = [c.name for c in PALETTE]
        self.assertEqual(len(names), len(set(names)))

    def test_palette_names_lowercase_and_non_empty(self) -> None:
        # The name field is the canonical identifier stored in
        # kv_meta and returned by the MCP debug tool. Mixed-case or
        # blank names would break the case-insensitive lookup in
        # ``get_color_by_name``.
        for entry in PALETTE:
            self.assertIsInstance(entry, DayColor)
            self.assertEqual(entry.name, entry.name.lower())
            self.assertTrue(entry.name.strip())

    def test_palette_taglines_non_empty(self) -> None:
        for entry in PALETTE:
            self.assertTrue(entry.tagline.strip())

    def test_dayclass_is_frozen(self) -> None:
        sample = PALETTE[0]
        with self.assertRaises(Exception):
            sample.name = "other"  # type: ignore[misc]


# ── roll_for_today ──────────────────────────────────────────────────


class RollForTodayTests(unittest.TestCase):
    def test_seeded_rng_is_deterministic(self) -> None:
        rng_a = random.Random(42)
        rng_b = random.Random(42)
        # Same seed -> same sequence of choices. This guarantees the
        # tests in this file and any downstream provider tests can
        # pin a specific colour without depending on system entropy.
        chosen_a = [roll_for_today(rng=rng_a).name for _ in range(10)]
        chosen_b = [roll_for_today(rng=rng_b).name for _ in range(10)]
        self.assertEqual(chosen_a, chosen_b)

    def test_default_rng_returns_palette_entry(self) -> None:
        # No seed -> system entropy; all we can assert is membership.
        chosen = roll_for_today()
        self.assertIn(chosen, PALETTE)

    def test_uniform_distribution_over_many_rolls(self) -> None:
        # Light statistical check: 5000 rolls with a fixed seed should
        # visit every palette entry (no missing colour) and the
        # tail/head ratio should stay within a sane bound. Not a
        # rigorous chi-square -- just a smoke check that the uniform
        # distribution doesn't degenerate.
        rng = random.Random(123)
        counts = Counter(roll_for_today(rng=rng).name for _ in range(5000))
        self.assertEqual(len(counts), len(PALETTE))
        top = counts.most_common(1)[0][1]
        bottom = counts.most_common()[-1][1]
        # Uniform over 10 buckets * 5000 = ~500 per bucket; allow
        # generous tolerance so flakey CI seeds don't trip the test.
        self.assertLess(top / bottom, 2.0)

    def test_empty_palette_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            roll_for_today(palette=())

    def test_now_parameter_accepted_but_ignored_by_v1(self) -> None:
        # K27 v1 ships uniform; ``now`` is accepted only for symmetry
        # with ``is_stale`` and so a future affect-trend-weighted
        # variant can read it. Same seed must produce same output
        # regardless of ``now`` value -- pinning this behaviour so a
        # future weighted variant has to explicitly opt in.
        rng_a = random.Random(7)
        rng_b = random.Random(7)
        morning = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
        evening = datetime(2026, 6, 1, 22, 0, tzinfo=timezone.utc)
        self.assertEqual(
            roll_for_today(now=morning, rng=rng_a).name,
            roll_for_today(now=evening, rng=rng_b).name,
        )


# ── weighted roll + weather bias (H11) ──────────────────────────────


class WeightedRollTests(unittest.TestCase):
    def test_weights_skew_distribution_toward_high_weight(self) -> None:
        rng = random.Random(99)
        weights = {"cozy": 50.0}  # heavily favour one entry
        counts = Counter(
            roll_for_today(rng=rng, weights=weights).name
            for _ in range(2000)
        )
        # cozy carries weight 50 against 9 entries at the implicit 1.0,
        # so it must dominate by a wide margin.
        self.assertGreater(counts["cozy"], counts.most_common()[-1][1] * 10)

    def test_empty_weights_falls_back_to_uniform(self) -> None:
        rng_a = random.Random(3)
        rng_b = random.Random(3)
        self.assertEqual(
            roll_for_today(rng=rng_a, weights={}).name,
            roll_for_today(rng=rng_b).name,
        )

    def test_nonpositive_total_falls_back_to_uniform(self) -> None:
        rng_a = random.Random(11)
        rng_b = random.Random(11)
        # All weights <= 0 -> uniform draw, identical to no weights.
        bad = {c.name: 0.0 for c in PALETTE}
        self.assertEqual(
            roll_for_today(rng=rng_a, weights=bad).name,
            roll_for_today(rng=rng_b).name,
        )

    def test_unknown_names_in_weights_are_ignored(self) -> None:
        rng = random.Random(5)
        # A table referencing a name not in the palette must not raise
        # and the present names still bias.
        weights = {"nonexistent_colour": 100.0, "cozy": 5.0}
        chosen = roll_for_today(rng=rng, weights=weights)
        self.assertIn(chosen.name, {c.name for c in PALETTE})


class WeatherPaletteWeightsTests(unittest.TestCase):
    def test_none_for_missing_or_unknown_condition(self) -> None:
        self.assertIsNone(day_color.weather_palette_weights(None))
        self.assertIsNone(day_color.weather_palette_weights(""))
        self.assertIsNone(day_color.weather_palette_weights("nonsense"))

    def test_known_conditions_return_palette_names(self) -> None:
        for condition in ("rain", "storm", "snow", "fog", "cloudy", "clear"):
            weights = day_color.weather_palette_weights(condition)
            self.assertIsInstance(weights, dict)
            assert weights is not None
            for name in weights:
                self.assertIn(name, {c.name for c in PALETTE})

    def test_case_insensitive(self) -> None:
        self.assertEqual(
            day_color.weather_palette_weights("RAIN"),
            day_color.weather_palette_weights("rain"),
        )


# ── is_stale ─────────────────────────────────────────────────────────


class IsStaleTests(unittest.TestCase):
    def test_missing_value_is_stale(self) -> None:
        now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(is_stale(None, now))

    def test_empty_string_is_stale(self) -> None:
        now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(is_stale("", now))
        self.assertTrue(is_stale("   ", now))

    def test_unparseable_value_is_stale(self) -> None:
        # A corrupt kv_meta row shouldn't permanently silence the
        # feature. ``is_stale`` returns True so the caller's roll
        # path overwrites the bad value.
        now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        self.assertTrue(is_stale("not-a-date", now))
        self.assertTrue(is_stale("2026-13-99", now))

    def test_same_local_date_not_stale(self) -> None:
        now = datetime(
            2026, 6, 1, 23, 30, tzinfo=timezone(timedelta(hours=2))
        )
        stored = (
            datetime(2026, 6, 1, 0, 5, tzinfo=timezone(timedelta(hours=2)))
            .isoformat()
        )
        self.assertFalse(is_stale(stored, now))

    def test_different_local_date_is_stale(self) -> None:
        # Use a clearly different day across all common timezones so
        # the test doesn't depend on the runner's local offset.
        now = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
        stored = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc).isoformat()
        self.assertTrue(is_stale(stored, now))

    def test_z_suffix_iso_string_parsed(self) -> None:
        # Some legacy persistence may store the Zulu suffix. The
        # parser swaps it for +00:00 so ``datetime.fromisoformat``
        # accepts it on every supported Python version.
        now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(is_stale("2026-06-01T08:00:00Z", now))

    def test_naive_stored_value_treated_as_local(self) -> None:
        # A naive isoformat (no tz suffix) is interpreted as local
        # wall-clock by the implementation. The check should NOT
        # raise even when one side is naive and the other is aware.
        now = datetime(2026, 6, 1, 12, 0)  # naive local
        stored = "2026-06-01T08:00:00"  # naive local, same day
        self.assertFalse(is_stale(stored, now))

    def test_default_now_uses_system_clock(self) -> None:
        # When ``now=None`` the function reads ``datetime.now()``;
        # we can't assert a specific date, but we can at least
        # verify the call doesn't raise.
        self.assertTrue(is_stale(None))


# ── render_inner_life_block ─────────────────────────────────────────


class RenderInnerLifeBlockTests(unittest.TestCase):
    def test_none_returns_empty_string(self) -> None:
        self.assertEqual(render_inner_life_block(None), "")

    def test_includes_name_and_tagline(self) -> None:
        sample = PALETTE[0]
        rendered = render_inner_life_block(sample)
        self.assertIn(sample.name, rendered)
        self.assertIn(sample.tagline, rendered)
        # The prompt cue must fit a single line so it clusters cleanly
        # next to the circadian block. Pinning the prefix here means
        # any future tweak to the shape (eg. adding a leading emoji)
        # forces a test update + explicit decision.
        self.assertTrue(rendered.startswith("Your day's colour today:"))

    def test_all_palette_entries_render_safely(self) -> None:
        for entry in PALETTE:
            rendered = render_inner_life_block(entry)
            self.assertTrue(rendered)
            self.assertIn(entry.name, rendered)


# ── get_color_by_name ───────────────────────────────────────────────


class GetColorByNameTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        for entry in PALETTE:
            self.assertIs(get_color_by_name(entry.name), entry)

    def test_case_insensitive(self) -> None:
        sample = PALETTE[0]
        self.assertIs(get_color_by_name(sample.name.upper()), sample)
        self.assertIs(
            get_color_by_name(f"  {sample.name.upper()}  "), sample,
        )

    def test_unknown_returns_none(self) -> None:
        self.assertIsNone(get_color_by_name("not_a_colour"))

    def test_none_or_empty_returns_none(self) -> None:
        self.assertIsNone(get_color_by_name(None))
        self.assertIsNone(get_color_by_name(""))
        self.assertIsNone(get_color_by_name("   "))


# ── module exports ──────────────────────────────────────────────────


class ModuleExportsTests(unittest.TestCase):
    """The module's ``__all__`` is what the worker and provider
    import; missing one of these would surface as an ImportError at
    boot. Pinning the list so a refactor doesn't quietly drop a
    public symbol."""

    def test_public_symbols_present(self) -> None:
        for symbol in (
            "DayColor",
            "PALETTE",
            "roll_for_today",
            "is_stale",
            "render_inner_life_block",
            "get_color_by_name",
            "weather_palette_weights",
        ):
            self.assertTrue(hasattr(day_color, symbol))


if __name__ == "__main__":
    unittest.main()
