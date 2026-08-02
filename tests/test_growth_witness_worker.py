"""Tests for :mod:`app.core.proactive.growth_witness_worker` (K70).

The worker reads the mood-drift ring and, rarely, drafts a private "you
have grown" cue. These tests pin the scheduler contract: what
``is_ready`` vetoes, what ``demand()`` reports for a given ring, and
that probing neither drafts nor eats the MCP force flag.

The detection maths itself is covered by ``tests/test_growth_witness.py``.
"""
from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.core.affect import mood_drift as _md
from app.core.affect.mood_drift import DriftSample
from app.core.proactive.growth_witness_worker import GrowthWitnessWorker
from app.core.relationship import growth_witness as _gw

_UTC = timezone.utc
_KV_LAST_FIRED_AT = "growth_witness.last_fired_at"
_KV_LAST_SIGNATURE = "growth_witness.last_signature"


class _FakeKv:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value


def _ramp(attr: str, low: float, high: float, n: int = 15) -> list[DriftSample]:
    """A ring of ``n`` samples ramping ``attr`` linearly low -> high."""
    out: list[DriftSample] = []
    for i in range(n):
        frac = i / (n - 1)
        fields: dict[str, float] = {
            "valence": 0.0,
            "closeness": 0.0,
            "humor": 0.0,
            "trust": 0.0,
            "comfort": 0.0,
        }
        fields[attr] = low + (high - low) * frac
        out.append(
            DriftSample(
                date=(date(2026, 1, 1) + timedelta(days=i)).isoformat(),
                **fields,
            )
        )
    return out


def _seed_ring(kv: _FakeKv, samples: list[DriftSample]) -> None:
    kv.store[_md.KV_SAMPLES] = _md.serialize_samples(samples)


def _worker(kv: _FakeKv, **overrides) -> GrowthWitnessWorker:
    kwargs: dict[str, Any] = dict(
        kv_get=kv.get,
        kv_set=kv.set,
        user_display_name_provider=lambda: "Jacob",
        cooldown_days=14.0,
    )
    kwargs.update(overrides)
    return GrowthWitnessWorker(**kwargs)


class IsReadyTests(unittest.TestCase):
    def test_timing_is_no_longer_a_veto(self) -> None:
        w = _worker(_FakeKv())
        now = datetime.now(_UTC)
        self.assertTrue(w.is_ready(now=now, last_run_at=None))
        self.assertTrue(
            w.is_ready(now=now, last_run_at=now - timedelta(seconds=30))
        )

    def test_disabled_blocks(self) -> None:
        w = _worker(_FakeKv(), enabled_provider=lambda: False)
        self.assertFalse(
            w.is_ready(now=datetime.now(_UTC), last_run_at=None)
        )


class DemandTests(unittest.TestCase):
    def _probe(self, worker: GrowthWitnessWorker):
        return worker.demand(now=datetime.now(_UTC), last_run_at=None)

    def test_a_durable_climb_is_full_pressure(self) -> None:
        kv = _FakeKv()
        _seed_ring(kv, _ramp("valence", -0.5, 0.5))
        signal = self._probe(_worker(kv))
        self.assertEqual(signal.pressure, 1.0)
        self.assertEqual(signal.reason, "lighter")
        self.assertFalse(signal.needs_llm)

    def test_an_empty_ring_is_no_pressure(self) -> None:
        signal = self._probe(_worker(_FakeKv()))
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "no_finding")

    def test_a_flat_ring_is_no_pressure(self) -> None:
        kv = _FakeKv()
        _seed_ring(kv, _ramp("valence", 0.1, 0.1))
        self.assertEqual(self._probe(_worker(kv)).pressure, 0.0)

    def test_the_cooldown_is_checked_before_the_ring(self) -> None:
        # The cheapest gate has to come first: one kv read settles most
        # ticks without deserializing the ring at all.
        kv = _FakeKv()
        _seed_ring(kv, _ramp("valence", -0.5, 0.5))
        kv.store[_KV_LAST_FIRED_AT] = (
            datetime.now(_UTC) - timedelta(days=1)
        ).isoformat()
        signal = self._probe(_worker(kv))
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "skipped_cooldown")

    def test_the_signature_watermark_suppresses(self) -> None:
        kv = _FakeKv()
        ring = _ramp("valence", -0.5, 0.5)
        _seed_ring(kv, ring)
        finding = _gw.detect_growth(ring)
        assert finding is not None
        kv.store[_KV_LAST_SIGNATURE] = finding.signature
        signal = self._probe(_worker(kv))
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "same_signature")

    def test_disabled_is_no_pressure(self) -> None:
        kv = _FakeKv()
        _seed_ring(kv, _ramp("valence", -0.5, 0.5))
        w = _worker(kv, enabled_provider=lambda: False)
        self.assertEqual(self._probe(w).reason, "disabled")

    def test_probing_writes_nothing_and_drafts_nothing(self) -> None:
        kv = _FakeKv()
        _seed_ring(kv, _ramp("valence", -0.5, 0.5))
        before = dict(kv.store)
        w = _worker(kv)
        self._probe(w)
        self._probe(w)
        self.assertEqual(kv.store, before)

    def test_probing_does_not_eat_the_force_flag(self) -> None:
        kv = _FakeKv()
        _seed_ring(kv, _ramp("valence", -0.5, 0.5))
        kv.store[_KV_LAST_FIRED_AT] = datetime.now(_UTC).isoformat()
        w = _worker(kv)
        w.force_next()
        self.assertEqual(self._probe(w).pressure, 0.0)
        self.assertEqual(w.run()["drafted"], 1)


class RunTests(unittest.TestCase):
    def test_run_drafts_and_marks_fired(self) -> None:
        kv = _FakeKv()
        _seed_ring(kv, _ramp("valence", -0.5, 0.5))
        out = _worker(kv).run()
        self.assertEqual(out["drafted"], 1)
        self.assertEqual(out["kind"], "lighter")
        self.assertIn(_KV_LAST_FIRED_AT, kv.store)
        self.assertIn(_KV_LAST_SIGNATURE, kv.store)

    def test_run_honours_the_cooldown(self) -> None:
        kv = _FakeKv()
        _seed_ring(kv, _ramp("valence", -0.5, 0.5))
        kv.store[_KV_LAST_FIRED_AT] = (
            datetime.now(_UTC) - timedelta(days=1)
        ).isoformat()
        out = _worker(kv).run()
        self.assertEqual(out["drafted"], 0)
        self.assertTrue(out.get("skipped_cooldown"))

    def test_run_reports_the_sample_count_when_nothing_found(self) -> None:
        kv = _FakeKv()
        ring = _ramp("valence", 0.1, 0.1)
        _seed_ring(kv, ring)
        out = _worker(kv).run()
        self.assertTrue(out.get("no_finding"))
        self.assertEqual(out["samples"], len(ring))


class WorkerShapeTests(unittest.TestCase):
    def test_name_is_stable(self) -> None:
        self.assertEqual(GrowthWitnessWorker.name, "growth_witness")


if __name__ == "__main__":
    unittest.main()
