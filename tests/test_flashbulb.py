"""Tests for K76 affective memory salience (flashbulb encoding).

Two layers: the pure math in ``app.core.memory.flashbulb`` and the
``MemoryStore.add`` hook that boosts salience + stamps
``metadata.affect_at_encoding`` from a live affect provider.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.memory import flashbulb as fb
from app.core.memory.memory_store import MemoryStore


class ComputeChargeTests(unittest.TestCase):
    def test_neutral_arousal_zero_charge(self) -> None:
        self.assertAlmostEqual(
            fb.compute_charge(0.4, 0.0, arousal_neutral=0.4), 0.0
        )

    def test_below_neutral_clamps_to_zero(self) -> None:
        self.assertAlmostEqual(
            fb.compute_charge(0.1, 0.0, arousal_neutral=0.4), 0.0
        )

    def test_high_arousal_charges(self) -> None:
        # arousal 1.0 → component 1.0 → weight 0.6.
        self.assertAlmostEqual(
            fb.compute_charge(
                1.0, 0.0, arousal_weight=0.6, arousal_neutral=0.4
            ),
            0.6,
            places=5,
        )

    def test_episode_charges(self) -> None:
        self.assertAlmostEqual(
            fb.compute_charge(0.4, 0.8, episode_weight=0.7), 0.56, places=5
        )

    def test_combined_clamped_to_one(self) -> None:
        self.assertEqual(
            fb.compute_charge(
                1.0, 1.0, arousal_weight=0.6, episode_weight=0.7
            ),
            1.0,
        )


class ApplyFlashbulbTests(unittest.TestCase):
    def test_neutral_no_boost(self) -> None:
        r = fb.apply_flashbulb(0.5, arousal=0.4, episode_intensity=0.0)
        self.assertAlmostEqual(r.salience, 0.5)
        self.assertAlmostEqual(r.boost, 0.0)

    def test_charged_boosts(self) -> None:
        r = fb.apply_flashbulb(
            0.5, arousal=1.0, episode_intensity=0.0,
            max_boost=0.35, arousal_weight=0.6, arousal_neutral=0.4,
        )
        # charge 0.6 * 0.35 = 0.21 → 0.71.
        self.assertAlmostEqual(r.charge, 0.6, places=5)
        self.assertAlmostEqual(r.salience, 0.71, places=5)

    def test_salience_clamped(self) -> None:
        r = fb.apply_flashbulb(
            0.95, arousal=1.0, episode_intensity=1.0, max_boost=0.35,
        )
        self.assertEqual(r.salience, 1.0)


class _FakeEmbedder:
    DIM = 16

    def embed(self, text: str) -> np.ndarray:
        rng = np.random.default_rng(seed=hash(text) & 0xFFFFFFFF)
        v = rng.normal(size=self.DIM).astype(np.float32)
        v /= max(1e-6, float(np.linalg.norm(v)))
        return v


class _TempStore:
    def __enter__(self) -> MemoryStore:
        self._dir = tempfile.TemporaryDirectory()
        path = Path(self._dir.name) / "mem.db"
        ChatDatabase(path)
        self.store = MemoryStore(path)
        return self.store

    def __exit__(self, *exc):
        try:
            self._dir.cleanup()
        except PermissionError:
            pass


class MemoryStoreHookTests(unittest.TestCase):
    def test_charged_write_boosts_salience_and_stamps(self) -> None:
        with _TempStore() as store:
            store.set_flashbulb(lambda: (1.0, 0.0), enabled=True)
            emb = _FakeEmbedder().embed("a charged moment")
            mem = store.add("A charged moment happened.", "fact", emb,
                            salience=0.5)
            assert mem is not None
            self.assertGreater(mem.salience, 0.5)
            self.assertIn("affect_at_encoding", mem.metadata)
            stamp = mem.metadata["affect_at_encoding"]
            self.assertGreater(stamp["charge"], 0.0)

    def test_neutral_write_unchanged_and_no_stamp(self) -> None:
        with _TempStore() as store:
            store.set_flashbulb(lambda: (0.4, 0.0), enabled=True)
            emb = _FakeEmbedder().embed("small talk")
            mem = store.add("Just small talk.", "fact", emb, salience=0.5)
            assert mem is not None
            self.assertAlmostEqual(mem.salience, 0.5)
            self.assertNotIn("affect_at_encoding", mem.metadata)

    def test_disabled_hook_no_boost(self) -> None:
        with _TempStore() as store:
            store.set_flashbulb(lambda: (1.0, 1.0), enabled=False)
            emb = _FakeEmbedder().embed("disabled charged")
            mem = store.add("Disabled but charged.", "fact", emb,
                            salience=0.5)
            assert mem is not None
            self.assertAlmostEqual(mem.salience, 0.5)

    def test_pinned_write_skips_flashbulb(self) -> None:
        with _TempStore() as store:
            store.set_flashbulb(lambda: (1.0, 1.0), enabled=True)
            emb = _FakeEmbedder().embed("pinned charged")
            mem = store.add("Pinned and charged.", "fact", emb,
                            salience=0.5, pinned=True)
            assert mem is not None
            self.assertNotIn("affect_at_encoding", mem.metadata)

    def test_broken_provider_never_breaks_write(self) -> None:
        with _TempStore() as store:
            def _boom():
                raise RuntimeError("nope")

            store.set_flashbulb(_boom, enabled=True)
            emb = _FakeEmbedder().embed("provider boom")
            mem = store.add("Provider boom.", "fact", emb, salience=0.5)
            assert mem is not None
            self.assertAlmostEqual(mem.salience, 0.5)


if __name__ == "__main__":
    unittest.main()
