"""Tests for the engagement-clock path in :class:`MemoryDecayWorker`.

The worker can drive ``elapsed_days`` from the shared
:class:`EngagementClock` (active-conversation time) instead of
wall-clock. These cover:
- a quiet/away stretch (clock doesn't advance) applies ~0 decay;
- heavy engagement (clock advances) applies proportional decay;
- ``memory_decay_use_engagement_clock=False`` falls back to the
  explicit wall-clock behaviour (regression guard);
- the first run with no anchor stores a baseline and applies 0.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.infra.engagement_clock import EngagementClock
from app.core.memory.memory_decay_worker import MemoryDecayWorker
from app.core.memory.memory_store import MemoryStore


class _FakeEmbedder:
    DIM = 16

    def embed(self, text: str) -> np.ndarray:
        rng = np.random.default_rng(seed=hash(text) & 0xFFFFFFFF)
        v = rng.normal(size=self.DIM).astype(np.float32)
        v /= max(1e-6, float(np.linalg.norm(v)))
        return v


def _emb(text: str) -> np.ndarray:
    return _FakeEmbedder().embed(text)


def _store() -> MemoryStore:
    d = tempfile.mkdtemp()
    path = Path(d) / "mem.db"
    ChatDatabase(path)
    return MemoryStore(path)


def _settings(**over) -> SimpleNamespace:
    base = dict(
        tiers_enabled=True,
        decay_worker_interval_seconds=1800,
        decay_rate_scratchpad=0.05,
        decay_rate_long_term=0.02,
        decay_rate_archive=0.0,
        revival_coefficient=0.0,
        revival_decay_per_day=0.0,
        decay_max_catchup_days=30.0,
        memory_decay_use_engagement_clock=True,
        # engagement clock knobs
        engagement_clock_enabled=True,
        engagement_seconds_per_day=3600.0,
        engagement_idle_cap_seconds=300.0,
        engagement_min_turn_seconds=15.0,
    )
    base.update(over)
    return SimpleNamespace(**base)


class _KV:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = str(value)


def _clock(kv: _KV, settings) -> EngagementClock:
    return EngagementClock(
        kv_get=kv.get, kv_set=kv.set, settings=settings,
        clock=lambda: datetime(2026, 7, 1, tzinfo=timezone.utc),
    )


class EngagementDecayTests(unittest.TestCase):
    def _worker(self, store, settings, kv, clock):
        return MemoryDecayWorker(
            store, settings, engagement_clock=clock,
            kv_get=kv.get, kv_set=kv.set,
        )

    def test_first_run_stores_baseline_applies_zero(self) -> None:
        store = _store()
        mem = store.add("anchor", "fact", _emb("a"), salience=1.0)
        assert mem is not None
        settings = _settings()
        kv = _KV()
        clock = _clock(kv, settings)
        kv.set("engagement.total_units", "7200.0")  # 2 engaged days worth
        worker = self._worker(store, settings, kv, clock)
        worker.run()
        # No decay on the first run; baseline anchor persisted.
        self.assertAlmostEqual(store.get(mem.id).salience, 1.0, places=5)
        self.assertEqual(kv.get("memory.last_decay_engagement"), repr(7200.0))

    def test_quiet_stretch_applies_no_decay(self) -> None:
        store = _store()
        mem = store.add("quiet row", "fact", _emb("q"), salience=1.0)
        assert mem is not None
        settings = _settings()
        kv = _KV()
        clock = _clock(kv, settings)
        kv.set("engagement.total_units", "0.0")
        worker = self._worker(store, settings, kv, clock)
        worker.run()  # baseline at 0
        # No turns happen -> clock stays at 0 -> second run decays nothing.
        worker.run()
        self.assertAlmostEqual(store.get(mem.id).salience, 1.0, places=5)

    def test_engagement_applies_proportional_decay(self) -> None:
        store = _store()
        mem = store.add("engaged row", "fact", _emb("e"), salience=1.0)
        assert mem is not None
        settings = _settings()
        kv = _KV()
        clock = _clock(kv, settings)
        kv.set("engagement.total_units", "0.0")
        worker = self._worker(store, settings, kv, clock)
        worker.run()  # baseline at 0
        # Simulate a lot of active conversation: 7 engaged days (7*3600s).
        kv.set("engagement.total_units", str(7 * 3600.0))
        worker.run()
        # long_term rate 0.02/day * 7 days = 0.14 -> salience 0.86.
        self.assertAlmostEqual(store.get(mem.id).salience, 0.86, places=4)
        self.assertEqual(
            kv.get("memory.last_decay_engagement"), repr(7 * 3600.0),
        )

    def test_disabled_setting_uses_wall_clock(self) -> None:
        store = _store()
        mem = store.add("wall row", "fact", _emb("w"), salience=1.0)
        assert mem is not None
        settings = _settings(memory_decay_use_engagement_clock=False)
        kv = _KV()
        clock = _clock(kv, settings)
        kv.set("engagement.total_units", str(30 * 3600.0))
        worker = self._worker(store, settings, kv, clock)
        # First wall-clock run just seeds the store's own anchor (applies 0);
        # our engagement anchor is never touched.
        worker.run()
        self.assertIsNone(kv.get("memory.last_decay_engagement"))
        self.assertAlmostEqual(store.get(mem.id).salience, 1.0, places=5)


if __name__ == "__main__":
    unittest.main()
