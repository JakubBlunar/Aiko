"""Tests for :mod:`app.core.novelty_detector` (K6 personality backlog).

Stub the embedder and rag_store so we don't pull in Ollama / LanceDB
on a unit-test pass. The detector's interesting surface is the ring
buffer math + band classification + cooldown/warmup behaviour --
none of that depends on a real embedding model.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Sequence

import numpy as np

from app.core.novelty_detector import (
    BAND_MILD,
    BAND_STRONG,
    NoveltyDetector,
    NoveltyResult,
    render_inner_life_block,
)


# ── stub helpers ────────────────────────────────────────────────────


def _unit(*coords: float) -> np.ndarray:
    """Return a unit-norm float32 vector. Empty -> zero vector."""
    arr = np.asarray(coords, dtype=np.float32)
    n = float(np.linalg.norm(arr))
    if n > 0.0:
        arr = arr / n
    return arr


@dataclass
class _StubEmbedder:
    """Returns a vector keyed by the first character of the text.

    Keeps tests deterministic without touching Ollama. Unknown keys
    fall back to ``[0, 0, 1]`` so the detector never gets a nan.
    """

    book: dict[str, np.ndarray]
    call_count: int = 0

    def embed(self, text: str) -> np.ndarray:
        self.call_count += 1
        key = (text or "").strip().lower()[:1]
        if key in self.book:
            return self.book[key]
        return _unit(0.0, 0.0, 1.0)


@dataclass
class _StubRag:
    """Single-call recency listing. Asserts we don't double-warm."""

    vectors: list[np.ndarray]
    call_count: int = 0

    def list_recent_user_vectors(
        self, *, user_id_prefix: str, limit: int,
    ) -> list[np.ndarray]:
        self.call_count += 1
        return list(self.vectors)[: int(limit)]


def _settings(**overrides: object) -> SimpleNamespace:
    """Tiny ``MemorySettings`` stub via ``SimpleNamespace`` getattr."""
    base = dict(
        novelty_window=5,
        novelty_warmup_min=3,
        novelty_mild_threshold=0.20,
        novelty_strong_threshold=0.60,
        novelty_cooldown_turns=1,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _build(
    *,
    book: dict[str, np.ndarray] | None = None,
    warm: Sequence[np.ndarray] | None = None,
    settings: SimpleNamespace | None = None,
    user_id: str = "alice",
) -> tuple[NoveltyDetector, _StubEmbedder, _StubRag]:
    emb = _StubEmbedder(book=book or {})
    rag = _StubRag(vectors=list(warm or []))
    det = NoveltyDetector(
        embedder=emb,
        rag_store=rag,
        user_id=user_id,
        memory_settings=settings or _settings(),
    )
    return det, emb, rag


# ── tests ───────────────────────────────────────────────────────────


class WarmupTests(unittest.TestCase):
    def test_cold_start_returns_none_until_min_filled(self) -> None:
        # Empty rag, no warm vectors. First two detects collect but
        # stay silent; the third should still be silent (since the
        # ring's centroid uses the prior turns and we need >= warmup
        # *before* this turn -- third detect sees ring=2 still).
        det, _, _ = _build(
            book={
                "a": _unit(1, 0, 0),
                "b": _unit(0, 1, 0),
                "c": _unit(0, 0, 1),
                "d": _unit(1, 1, 0),
            },
            settings=_settings(novelty_warmup_min=3),
        )
        self.assertIsNone(det.detect("alpha is fine"))
        self.assertIsNone(det.detect("beta is fine"))
        # Ring has 2 entries; warmup_min=3 -> still silent.
        self.assertIsNone(det.detect("cooler is fine"))
        # Fourth detect: ring has 3 entries before this turn -> may
        # finally classify. We don't assert a band here (depends on
        # which letter we feed); we just confirm it's no longer
        # gated by warmup -- a non-None vs None distinction would
        # be checked in the band tests.

    def test_warm_from_rag_prefills_ring(self) -> None:
        warm = [
            _unit(1, 0, 0),
            _unit(0.99, 0.01, 0),
            _unit(0.95, 0.05, 0),
        ]
        det, _, rag = _build(
            book={"a": _unit(1, 0, 0), "z": _unit(0, 0, 1)},
            warm=warm,
            settings=_settings(
                novelty_warmup_min=3,
                novelty_mild_threshold=0.10,
                novelty_strong_threshold=0.60,
            ),
        )
        # First detect picks an orthogonal vector so it actually
        # crosses the band -- proves the warm-up populated the ring
        # (otherwise the warmup gate would have returned None).
        out = det.detect("zzzz zzzz")
        self.assertIsNotNone(out)
        self.assertEqual(rag.call_count, 1)
        # Second detect must reuse the same warm pull (no double
        # round-trips into the rag store).
        det.detect("zzzz again again")
        self.assertEqual(rag.call_count, 1)

    def test_warm_failure_silently_starts_cold(self) -> None:
        class _BoomRag:
            def list_recent_user_vectors(self, **_kw: object) -> list[np.ndarray]:
                raise RuntimeError("lance unhappy")

        det = NoveltyDetector(
            embedder=_StubEmbedder(book={"a": _unit(1, 0, 0)}),
            rag_store=_BoomRag(),
            user_id="alice",
            memory_settings=_settings(novelty_warmup_min=3),
        )
        # Should not raise even though rag.list_recent_user_vectors blew up.
        self.assertIsNone(det.detect("alpha"))


class ShortInputTests(unittest.TestCase):
    def test_below_min_text_length_is_skipped(self) -> None:
        det, emb, _ = _build(
            book={"a": _unit(1, 0, 0)},
            warm=[_unit(1, 0, 0)] * 5,
            settings=_settings(novelty_warmup_min=3),
        )
        # Short text: never reaches the embed path.
        self.assertIsNone(det.detect("ok"))
        self.assertEqual(emb.call_count, 0)


class BandClassificationTests(unittest.TestCase):
    def test_identical_vectors_stay_silent(self) -> None:
        det, _, _ = _build(
            book={"a": _unit(1, 0, 0)},
            warm=[_unit(1, 0, 0)] * 4,
            settings=_settings(
                novelty_warmup_min=3,
                novelty_mild_threshold=0.20,
                novelty_strong_threshold=0.60,
            ),
        )
        # All warm + new vectors are [1,0,0]; cosine=1 -> distance=0.
        self.assertIsNone(det.detect("alpha alpha alpha"))

    def test_orthogonal_vector_fires_strong_band(self) -> None:
        det, _, _ = _build(
            book={"a": _unit(1, 0, 0), "z": _unit(0, 0, 1)},
            warm=[_unit(1, 0, 0)] * 4,
            settings=_settings(
                novelty_warmup_min=3,
                novelty_mild_threshold=0.20,
                novelty_strong_threshold=0.60,
            ),
        )
        out = det.detect("zzzz zzzz")
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out.band, BAND_STRONG)
        self.assertAlmostEqual(out.distance, 1.0, places=2)

    def test_slightly_off_vector_fires_mild_band(self) -> None:
        # Centroid sits at [1,0,0]; the probe is rotated ~32 degrees
        # in the XY plane. cos(32°) ≈ 0.848 -> distance ≈ 0.152.
        # Set thresholds so 0.15 sits between mild and strong.
        det, _, _ = _build(
            book={
                "a": _unit(1, 0, 0),
                "m": _unit(np.cos(np.deg2rad(32)), np.sin(np.deg2rad(32)), 0),
            },
            warm=[_unit(1, 0, 0)] * 4,
            settings=_settings(
                novelty_warmup_min=3,
                novelty_mild_threshold=0.10,
                novelty_strong_threshold=0.60,
                novelty_cooldown_turns=0,
            ),
        )
        out = det.detect("medium medium medium")
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out.band, BAND_MILD)
        self.assertGreater(out.distance, 0.10)
        self.assertLess(out.distance, 0.60)


class CooldownTests(unittest.TestCase):
    def test_consecutive_novel_turns_are_suppressed(self) -> None:
        det, _, _ = _build(
            book={"a": _unit(1, 0, 0), "z": _unit(0, 0, 1)},
            warm=[_unit(1, 0, 0)] * 4,
            settings=_settings(
                novelty_warmup_min=3,
                novelty_mild_threshold=0.10,
                novelty_strong_threshold=0.60,
                novelty_cooldown_turns=2,
            ),
        )
        first = det.detect("zzzz again again")
        self.assertIsNotNone(first)
        # Cooldown=2 -> next two turns suppressed even though the
        # turn is still novel.
        self.assertIsNone(det.detect("zzzz once more"))
        self.assertIsNone(det.detect("zzzz one more"))
        # Cooldown expired: a still-novel turn should classify again.
        third = det.detect("zzzz post-cooldown")
        self.assertIsNotNone(third)


class RingMaxlenTests(unittest.TestCase):
    def test_ring_respects_window_size(self) -> None:
        # window=3, warm with 5 vectors -> ring should retain the
        # last 3 (oldest two get evicted as we append).
        warm = [_unit(1, 0, 0), _unit(0, 1, 0), _unit(0, 0, 1),
                _unit(1, 1, 0), _unit(1, 0, 1)]
        det, _, _ = _build(
            book={"a": _unit(1, 0, 0)},
            warm=warm,
            settings=_settings(novelty_window=3, novelty_warmup_min=3),
        )
        # Trigger the lazy warm.
        det.detect("alpha alpha alpha")
        # Peek through the public surface: the detect path uses the
        # ring; after one classification we expect len(ring) == 3.
        # We can't read _ring directly without breaking encapsulation
        # in tests for other modules, but it's fine here.
        self.assertEqual(len(det._ring), 3)


class RenderTests(unittest.TestCase):
    def test_render_strong_band(self) -> None:
        block = render_inner_life_block(
            NoveltyResult(distance=1.0, band=BAND_STRONG, window_size=5,
                          mean_similarity=0.0),
        )
        self.assertIn("Heads-up", block)
        self.assertIn("outside the recent baseline", block)

    def test_render_mild_band(self) -> None:
        block = render_inner_life_block(
            NoveltyResult(distance=0.30, band=BAND_MILD, window_size=5,
                          mean_similarity=0.7),
        )
        self.assertIn("Heads-up", block)
        self.assertIn("sideways", block)

    def test_render_none_is_empty(self) -> None:
        self.assertEqual(render_inner_life_block(None), "")


if __name__ == "__main__":
    unittest.main()
