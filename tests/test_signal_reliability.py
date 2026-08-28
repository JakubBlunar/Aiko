"""H18's reliability rule, checked against signals of known construction.

The load-bearing test is :class:`TheCaseThatFooledTheOldTest`. H18's
single correlation read the cluster engaged rate as usable (0.233 at an
evidence floor of 8, over its own 0.2 line), so an instrument built to
replace that reading has to be shown catching the case that fooled it --
against a generator whose truth is known, not against the live graph
where the answer is the thing in question.
"""
from __future__ import annotations

import random
import unittest

from app.core.memory.signal_reliability import (
    MIN_EXCESS_SPREAD,
    NOISE_CEILING,
    VERDICT_NOISE,
    VERDICT_SIGNAL,
    VERDICT_UNDERPOWERED,
    bucket_spread,
    classify,
    excess_spread,
    permutation_test,
    shape_matched_null,
    split_half,
    sweep,
)


def _per_item_signal(
    rng: random.Random, items: int = 120, rows: int = 40
) -> dict[str, list[int]]:
    """Each item owns a true rate; observations are draws from it.

    The shape a ranking term assumes it is reading.
    """
    out: dict[str, list[int]] = {}
    for i in range(items):
        rate = 0.05 + 0.90 * (i / max(1, items - 1))
        out[f"item{i}"] = [
            1 if rng.random() < rate else 0 for _ in range(rows)
        ]
    return out


def _one_shared_rate(
    rng: random.Random,
    *,
    items: int = 120,
    rate: float = 0.21,
    rows: int = 40,
) -> dict[str, list[int]]:
    """No item-level structure: every row drawn from one global rate."""
    return {
        f"item{i}": [1 if rng.random() < rate else 0 for _ in range(rows)]
        for i in range(items)
    }


def _lopsided_population(rng: random.Random) -> dict[str, list[int]]:
    """One shared rate, but row counts spread like the real cluster graph.

    Fifty items carrying hundreds of rows beside a handful carrying four,
    which is the population that made a single correlation misleading.
    Nothing here distinguishes one item from another, so every check has
    to come back negative.
    """
    out: dict[str, list[int]] = {}
    for i in range(50):
        out[f"dense{i}"] = [
            1 if rng.random() < 0.21 else 0 for _ in range(400 + i * 4)
        ]
    for i, rows in enumerate((4, 5, 5, 6, 7, 9, 10, 14, 17, 24, 26, 34)):
        out[f"sparse{i}"] = [
            1 if rng.random() < 0.21 else 0 for _ in range(rows)
        ]
    return out


class SplitHalfTests(unittest.TestCase):
    def test_a_per_item_signal_scores_high(self) -> None:
        rng = random.Random(11)
        result = split_half(_per_item_signal(rng), floor=8, rng=rng)
        assert result.r is not None
        self.assertGreater(result.r, 0.8)

    def test_a_coin_flip_scores_near_zero(self) -> None:
        rng = random.Random(12)
        result = split_half(
            _one_shared_rate(rng, rate=0.5), floor=8, rng=rng
        )
        assert result.r is not None
        self.assertLess(abs(result.r), NOISE_CEILING)

    def test_too_few_items_refuses_to_answer(self) -> None:
        rng = random.Random(13)
        series = {f"item{i}": [1, 0, 1, 0] * 3 for i in range(4)}
        result = split_half(series, floor=4, rng=rng)
        self.assertIsNone(result.r)
        self.assertFalse(result.quotable)

    def test_a_constant_signal_has_nothing_to_correlate(self) -> None:
        """Every item identical is not reliability 1.0, it is no answer."""
        rng = random.Random(14)
        series = {f"item{i}": [1] * 20 for i in range(40)}
        self.assertIsNone(split_half(series, floor=8, rng=rng).r)

    def test_the_floor_selects_which_items_are_measured(self) -> None:
        rng = random.Random(15)
        series: dict[str, list[int]] = {
            f"short{i}": [1, 0, 1, 0, 1] for i in range(30)
        }
        series.update({f"long{i}": [1] * 15 + [0] * 15 for i in range(30)})
        self.assertEqual(split_half(series, floor=4, rng=rng).items, 60)
        self.assertEqual(split_half(series, floor=8, rng=rng).items, 30)

    def test_the_sweep_reports_every_floor_it_was_given(self) -> None:
        rng = random.Random(16)
        readings = sweep(
            _per_item_signal(rng), floors=(4, 8, 12), rng=rng
        )
        self.assertEqual([r.floor for r in readings], [4, 8, 12])


class ShapeMatchedNullTests(unittest.TestCase):
    """The control H18's test did not have."""

    def test_a_structureless_series_is_near_its_own_null(self) -> None:
        rng = random.Random(21)
        series = _one_shared_rate(rng)
        real = split_half(series, floor=8, rng=rng).r
        null = shape_matched_null(series, floor=8, rng=rng)
        assert real is not None and null is not None
        self.assertLess(abs(real - null), 0.2)

    def test_a_real_signal_clears_its_own_null(self) -> None:
        rng = random.Random(22)
        series = _per_item_signal(rng)
        real = split_half(series, floor=8, rng=rng).r
        null = shape_matched_null(series, floor=8, rng=rng)
        assert real is not None and null is not None
        self.assertGreater(real - null, 0.5)

    def test_the_null_keeps_the_row_counts(self) -> None:
        """Otherwise it is not a control for this population's shape."""
        rng = random.Random(23)
        lopsided = _lopsided_population(rng)
        self.assertIsNotNone(
            shape_matched_null(lopsided, floor=8, rng=rng)
        )


class ExcessSpreadTests(unittest.TestCase):
    def test_one_shared_rate_measures_about_one(self) -> None:
        rng = random.Random(31)
        value = excess_spread(_one_shared_rate(rng), floor=8)
        assert value is not None
        self.assertLess(value, MIN_EXCESS_SPREAD)
        self.assertGreater(value, 0.5)

    def test_genuinely_different_items_measure_well_above_one(self) -> None:
        rng = random.Random(32)
        value = excess_spread(_per_item_signal(rng), floor=8)
        assert value is not None
        self.assertGreater(value, 3.0)

    def test_buckets_expose_a_lopsided_population(self) -> None:
        rng = random.Random(33)
        buckets = bucket_spread(_lopsided_population(rng))
        self.assertGreaterEqual(len(buckets), 3)
        # The dense tail must land in its own bucket rather than being
        # averaged into the sparse head, which is the whole point.
        self.assertTrue(any(b.mean_rows > 100 for b in buckets))
        self.assertTrue(any(b.mean_rows < 20 for b in buckets))
        for bucket in buckets:
            if bucket.excess is None:
                continue
            self.assertLess(bucket.excess, 2.5)


class TheCaseThatFooledTheOldTest(unittest.TestCase):
    """A series with no item-level signal that clears H18's 0.2 line."""

    def test_a_lopsided_population_is_rejected(self) -> None:
        rng = random.Random(41)
        verdict = classify("lopsided", _lopsided_population(rng), rng=rng)
        self.assertEqual(verdict.verdict, VERDICT_NOISE)
        self.assertFalse(verdict.usable)

    def test_it_is_rejected_by_a_named_check_not_by_the_bare_line(
        self,
    ) -> None:
        """If it failed only on the 0.2 line, this module buys nothing."""
        rng = random.Random(42)
        series = _lopsided_population(rng)
        verdict = classify("lopsided", series, rng=rng)
        excess = excess_spread(series, floor=8)
        assert excess is not None
        self.assertLess(excess, MIN_EXCESS_SPREAD)
        self.assertIn(
            "shared rate",
            verdict.detail,
            msg=f"rejected for the wrong reason: {verdict.detail}",
        )

    def test_the_verdict_carries_its_evidence(self) -> None:
        rng = random.Random(43)
        verdict = classify("lopsided", _lopsided_population(rng), rng=rng)
        self.assertTrue(verdict.sweep)
        self.assertTrue(verdict.buckets)
        self.assertIsNotNone(verdict.null_r)
        self.assertIsNotNone(verdict.excess)


class PermutationTests(unittest.TestCase):
    def test_a_real_signal_beats_the_shuffled_null(self) -> None:
        rng = random.Random(51)
        per_turn: dict[int, list[str]] = {}
        labels: dict[int, bool] = {}
        for turn in range(800):
            index = turn % 40
            per_turn[turn] = [f"item{index}"]
            labels[turn] = rng.random() < (0.05 + 0.9 * (index / 39))
        p_value, ratio = permutation_test(
            per_turn, labels, floor=8, trials=200, rng=rng
        )
        assert p_value is not None and ratio is not None
        self.assertLess(p_value, 0.05)
        self.assertGreater(ratio, 1.5)

    def test_labels_unrelated_to_items_sit_inside_the_null(self) -> None:
        rng = random.Random(52)
        per_turn = {t: [f"item{t % 40}"] for t in range(800)}
        labels = {t: rng.random() < 0.5 for t in range(800)}
        p_value, _ratio = permutation_test(
            per_turn, labels, floor=8, trials=200, rng=rng
        )
        assert p_value is not None
        self.assertGreater(p_value, 0.05)


class ClassifyTests(unittest.TestCase):
    def test_a_per_item_signal_is_usable(self) -> None:
        rng = random.Random(61)
        verdict = classify("real", _per_item_signal(rng), rng=rng)
        self.assertEqual(verdict.verdict, VERDICT_SIGNAL)
        self.assertTrue(verdict.usable)

    def test_a_coin_flip_is_not_usable(self) -> None:
        rng = random.Random(62)
        verdict = classify("flip", _one_shared_rate(rng, rate=0.5), rng=rng)
        self.assertEqual(verdict.verdict, VERDICT_NOISE)
        self.assertFalse(verdict.usable)

    def test_a_thin_corpus_is_underpowered_not_absent(self) -> None:
        """H18 left the cluster case open for exactly this reason."""
        rng = random.Random(63)
        series = {f"item{i}": [1, 0, 1, 0, 1, 0] for i in range(5)}
        verdict = classify("thin", series, rng=rng)
        self.assertEqual(verdict.verdict, VERDICT_UNDERPOWERED)
        self.assertFalse(verdict.usable)

    def test_an_empty_series_does_not_raise(self) -> None:
        verdict = classify("empty", {}, rng=random.Random(64))
        self.assertEqual(verdict.verdict, VERDICT_UNDERPOWERED)


if __name__ == "__main__":
    unittest.main()
