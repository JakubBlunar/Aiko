"""H3 mood-drift narrator — pure module tests.

Covers (de)serialisation, the dedupe-by-date + cap append, the three
detection findings (sustained_low / lifting / axis drift) with their
priority order, and the rendered copy.
"""
from __future__ import annotations

import unittest
from datetime import datetime

from app.core.affect import mood_drift as md


def _s(date: str, *, v=0.0, c=0.0, h=0.0, t=0.0, m=0.0) -> md.DriftSample:
    return md.DriftSample(date=date, valence=v, closeness=c, humor=h, trust=t, comfort=m)


def _day(n: int) -> str:
    return f"2026-06-{n:02d}"


class SerializeTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        samples = [_s(_day(1), v=-0.2, c=0.3), _s(_day(2), v=0.1)]
        blob = md.serialize_samples(samples)
        back = md.deserialize_samples(blob)
        self.assertEqual(back, samples)

    def test_garbage_returns_empty(self) -> None:
        self.assertEqual(md.deserialize_samples(None), [])
        self.assertEqual(md.deserialize_samples(""), [])
        self.assertEqual(md.deserialize_samples("not json"), [])
        self.assertEqual(md.deserialize_samples('{"a":1}'), [])

    def test_skips_bad_rows(self) -> None:
        blob = '[{"date":"2026-06-01","valence":-0.2},"bad",{"valence":1}]'
        out = md.deserialize_samples(blob)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].date, "2026-06-01")


class AppendTests(unittest.TestCase):
    def test_dedupes_by_date(self) -> None:
        samples = [_s(_day(1), v=-0.2)]
        out = md.append_sample(samples, _s(_day(1), v=0.5))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].valence, 0.5)

    def test_cap_keeps_trailing(self) -> None:
        samples = [_s(_day(n)) for n in range(1, 6)]
        out = md.append_sample(samples, _s(_day(6)), cap=3)
        self.assertEqual([s.date for s in out], [_day(4), _day(5), _day(6)])

    def test_sorts_chronologically(self) -> None:
        out = md.append_sample([_s(_day(3)), _s(_day(1))], _s(_day(2)))
        self.assertEqual([s.date for s in out], [_day(1), _day(2), _day(3)])


class DetectTests(unittest.TestCase):
    def test_too_few_samples_none(self) -> None:
        self.assertIsNone(md.detect_drift([_s(_day(1), v=-0.9)]))

    def test_sustained_low(self) -> None:
        samples = [
            _s(_day(1), v=0.1),
            _s(_day(2), v=-0.3),
            _s(_day(3), v=-0.4),
            _s(_day(4), v=-0.5),
        ]
        verdict = md.detect_drift(samples)
        assert verdict is not None
        self.assertEqual(verdict.kind, "sustained_low")
        self.assertEqual(verdict.signature, "mood:low")

    def test_low_broken_by_recent_neutral(self) -> None:
        samples = [
            _s(_day(1), v=-0.4),
            _s(_day(2), v=-0.4),
            _s(_day(3), v=-0.4),
            _s(_day(4), v=0.2),  # recovered today
        ]
        verdict = md.detect_drift(samples)
        # last-3 run isn't all low -> not sustained_low
        if verdict is not None:
            self.assertNotEqual(verdict.kind, "sustained_low")

    def test_lifting(self) -> None:
        samples = [
            _s(_day(1), v=-0.4),
            _s(_day(2), v=-0.5),
            _s(_day(3), v=-0.4),
            _s(_day(4), v=0.1),
            _s(_day(5), v=0.2),
            _s(_day(6), v=0.25),
        ]
        verdict = md.detect_drift(samples)
        assert verdict is not None
        self.assertEqual(verdict.kind, "lifting")
        self.assertEqual(verdict.signature, "mood:lifting")

    def test_axis_rise(self) -> None:
        samples = [
            _s(_day(n), v=0.0, c=0.0 + 0.06 * n) for n in range(1, 8)
        ]
        verdict = md.detect_drift(samples)
        assert verdict is not None
        self.assertEqual(verdict.kind, "axis_rise")
        self.assertEqual(verdict.axis, "closeness")
        self.assertEqual(verdict.signature, "axis:closeness:up")

    def test_axis_fall(self) -> None:
        samples = [
            _s(_day(n), v=0.0, t=0.5 - 0.06 * n) for n in range(1, 8)
        ]
        verdict = md.detect_drift(samples)
        assert verdict is not None
        self.assertEqual(verdict.kind, "axis_fall")
        self.assertEqual(verdict.axis, "trust")

    def test_small_axis_move_ignored(self) -> None:
        samples = [_s(_day(n), v=0.0, c=0.01 * n) for n in range(1, 8)]
        self.assertIsNone(md.detect_drift(samples))

    def test_low_outranks_axis(self) -> None:
        # both a sustained low AND a closeness climb present; low wins.
        samples = [
            _s(_day(1), v=-0.3, c=0.0),
            _s(_day(2), v=-0.3, c=0.1),
            _s(_day(3), v=-0.3, c=0.2),
            _s(_day(4), v=-0.3, c=0.3),
            _s(_day(5), v=-0.3, c=0.4),
            _s(_day(6), v=-0.3, c=0.5),
        ]
        verdict = md.detect_drift(samples)
        assert verdict is not None
        self.assertEqual(verdict.kind, "sustained_low")


class RenderTests(unittest.TestCase):
    def test_sustained_low_mentions_name(self) -> None:
        v = md.DriftVerdict("sustained_low", None, 0.3, "mood:low", "")
        out = md.render_block(v, user_display_name="Jacob")
        self.assertIn("Jacob", out)
        self.assertIn("low", out.lower())

    def test_lifting(self) -> None:
        v = md.DriftVerdict("lifting", None, 0.4, "mood:lifting", "")
        out = md.render_block(v, user_display_name="Jacob")
        self.assertIn("lighter", out.lower())

    def test_axis_rise_closeness(self) -> None:
        v = md.DriftVerdict("axis_rise", "closeness", 0.3, "axis:closeness:up", "")
        out = md.render_block(v, user_display_name="Jacob")
        self.assertIn("closer", out.lower())

    def test_unknown_kind_empty(self) -> None:
        v = md.DriftVerdict("nonsense", None, 0.0, "x", "")
        self.assertEqual(md.render_block(v), "")


if __name__ == "__main__":
    unittest.main()
