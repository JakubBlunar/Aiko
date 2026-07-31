"""L9 living beliefs: contradiction penalty math + the read-only detector.

Covers:
  * ``apply_contradiction_penalty`` (plasticity damping + floor clamp);
  * ``ConceptContradictionDetector.detect`` across the F5 three tiers --
    ``definite`` (no LLM), ``borderline`` + LLM ``YES``, ``no`` (dropped),
    and the rate-limited borderline path (LLM skipped) -- plus the
    cosine-band filter and the no-embedding short-circuit.

L3 wiring (``active -> contradicted`` + revival + event + batch cap) is in
``tests/test_concept_lifecycle_worker.py``.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

import numpy as np

from app.core.concepts.concept_contradiction import ConceptContradictionDetector
from app.core.concepts.concept_lifecycle import apply_contradiction_penalty


# ── penalty math ──────────────────────────────────────────────────────


class PenaltyTests(unittest.TestCase):
    def test_plastic_belief_drops_more_than_sticky(self) -> None:
        # Same penalty, higher plasticity => bigger effective drop.
        sticky = apply_contradiction_penalty(0.8, penalty=0.3, plasticity=0.0)
        plastic = apply_contradiction_penalty(0.8, penalty=0.3, plasticity=1.0)
        self.assertGreater(sticky, plastic)  # sticky kept more confidence
        # plasticity 0 => 0.5x, plasticity 1 => 1x.
        self.assertAlmostEqual(sticky, 0.8 - 0.3 * 0.5, places=6)
        self.assertAlmostEqual(plastic, 0.8 - 0.3 * 1.0, places=6)

    def test_floor_clamp(self) -> None:
        got = apply_contradiction_penalty(
            0.1, penalty=0.9, plasticity=1.0, floor=0.05
        )
        self.assertAlmostEqual(got, 0.05, places=6)

    def test_negative_penalty_treated_as_zero(self) -> None:
        got = apply_contradiction_penalty(0.5, penalty=-1.0, plasticity=0.5)
        self.assertAlmostEqual(got, 0.5, places=6)


# ── detector fakes ────────────────────────────────────────────────────


class _Hit:
    def __init__(self, memory) -> None:
        self.memory = memory
        self.score = 1.0


class _Mem:
    def __init__(self, mem_id: int, content: str, embedding) -> None:
        self.id = mem_id
        self.content = content
        self.embedding = np.asarray(embedding, dtype=np.float32)


class _MemoryStore:
    def __init__(self, hits) -> None:
        self._hits = hits

    def search(self, query_embedding, *, top_k=6, min_score=0.4):
        return list(self._hits)[: top_k]


class _RateLimiter:
    def __init__(self, allowed: bool = True) -> None:
        self._allowed = allowed
        self.calls = 0

    def allow(self, now=None) -> bool:
        self.calls += 1
        return self._allowed


class _Ollama:
    def __init__(self, verdict_json: str) -> None:
        self._verdict_json = verdict_json
        self.calls = 0

    def chat_stream(self, messages, **_kw):
        self.calls += 1
        yield self._verdict_json


def _concept(label: str, *, rationale: str = "", embedding=(1.0, 0.0)):
    return SimpleNamespace(
        concept_id=1,
        label=label,
        rationale=rationale,
        embedding=np.asarray(embedding, dtype=np.float32),
    )


# In-band memory embedding: cosine to concept [1, 0] is 0.8 (in [0.6, 0.95)).
_IN_BAND = (0.8, 0.6)
# Out-of-band (too similar): cosine 1.0 >= 0.95 upper bound => filtered.
_TOO_CLOSE = (1.0, 0.0)


def _detector(memory_store, *, rate_limiter=None, ollama=None):
    return ConceptContradictionDetector(
        memory_store=memory_store,
        ollama=ollama or _Ollama('{"verdict": "YES", "reason": "x"}'),
        chat_model="test-model",
        rate_limiter=rate_limiter or _RateLimiter(True),
        cancel_event=None,
        similarity_min=0.6,
        similarity_max=0.95,
        max_candidates=6,
    )


class DetectorTests(unittest.TestCase):
    def test_definite_skips_llm(self) -> None:
        # loves/hates antonym => definite => confirmed without an LLM call.
        store = _MemoryStore([
            _Hit(_Mem(11, "Jacob hates understanding systems", _IN_BAND)),
        ])
        rl = _RateLimiter(True)
        ollama = _Ollama('{"verdict": "NO"}')
        det = _detector(store, rate_limiter=rl, ollama=ollama)
        verdict = det.detect(
            _concept("Jacob loves understanding systems")
        )
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict.heuristic_label, "definite")
        self.assertIsNone(verdict.llm_verdict)
        self.assertEqual(verdict.memory_id, 11)
        self.assertEqual(rl.calls, 0)  # no LLM budget consumed
        self.assertEqual(ollama.calls, 0)

    def test_borderline_confirmed_by_llm(self) -> None:
        # Number mismatch on overlapping content => borderline => LLM YES.
        store = _MemoryStore([
            _Hit(_Mem(22, "Jacob owns 2 cats", _IN_BAND)),
        ])
        rl = _RateLimiter(True)
        ollama = _Ollama('{"verdict": "YES", "reason": "count differs"}')
        det = _detector(
            _MemoryStore(store._hits), rate_limiter=rl, ollama=ollama
        )
        verdict = det.detect(_concept("Jacob owns 5 cats"))
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict.heuristic_label, "borderline")
        self.assertEqual(verdict.llm_verdict, "YES")
        self.assertEqual(rl.calls, 1)
        self.assertEqual(ollama.calls, 1)

    def test_borderline_llm_no_drops(self) -> None:
        store = _MemoryStore([_Hit(_Mem(22, "Jacob owns 2 cats", _IN_BAND))])
        ollama = _Ollama('{"verdict": "NO", "reason": "both fine"}')
        det = _detector(store, ollama=ollama)
        self.assertIsNone(det.detect(_concept("Jacob owns 5 cats")))

    def test_rate_limited_skips_llm(self) -> None:
        store = _MemoryStore([_Hit(_Mem(22, "Jacob owns 2 cats", _IN_BAND))])
        rl = _RateLimiter(False)
        ollama = _Ollama('{"verdict": "YES"}')
        det = _detector(store, rate_limiter=rl, ollama=ollama)
        self.assertIsNone(det.detect(_concept("Jacob owns 5 cats")))
        self.assertEqual(rl.calls, 1)
        self.assertEqual(ollama.calls, 0)  # budget denied => no call

    def test_no_signal_dropped_without_llm(self) -> None:
        # Topically near but no opposition signal => heuristic "no".
        store = _MemoryStore([
            _Hit(_Mem(33, "Jacob enjoys hiking on weekends", _IN_BAND)),
        ])
        rl = _RateLimiter(True)
        ollama = _Ollama('{"verdict": "YES"}')
        det = _detector(store, rate_limiter=rl, ollama=ollama)
        self.assertIsNone(det.detect(_concept("Jacob loves cooking")))
        self.assertEqual(ollama.calls, 0)

    def test_out_of_band_candidate_filtered(self) -> None:
        # A near-duplicate memory (cosine 1.0) is above the band and never
        # considered, even though it contradicts.
        store = _MemoryStore([
            _Hit(_Mem(44, "Jacob hates understanding systems", _TOO_CLOSE)),
        ])
        det = _detector(store)
        self.assertIsNone(
            det.detect(_concept("Jacob loves understanding systems"))
        )

    def test_no_embedding_short_circuits(self) -> None:
        store = _MemoryStore([
            _Hit(_Mem(55, "Jacob hates understanding systems", _IN_BAND)),
        ])
        det = _detector(store)
        c = _concept("Jacob loves understanding systems", embedding=())
        self.assertIsNone(det.detect(c))


if __name__ == "__main__":
    unittest.main()
