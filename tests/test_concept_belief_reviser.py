"""L15 belief revision: the :class:`ConceptBeliefReviser` arbitration.

Covers the three resolutions the reviser can apply to a contradicted
concept's supporting memories -- (a) inaccurate -> damped/floored
confidence cut, (b) superseded -> ``past_event`` reclassify with a fresh
``relevance_until`` (confidence untouched), (c) keep -> no memory write --
plus the guardrails: pinned memories are never touched, compatible
memories skip the LLM entirely (cheap ``classify_pair`` gate), the LLM
budget defers a genuine conflict when spent, the per-concept
``max_evidence`` cap bounds work, and non-memory evidence edges are
ignored.

The L3 wiring (edge persistence + reviser invocation on the
``-> contradicted`` transition + per-tick batch cap) is covered in
``tests/test_concept_lifecycle_worker.py``.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from app.core.concepts.concept_belief_reviser import ConceptBeliefReviser


_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

_BELIEF = "Jacob loves understanding systems"
_COUNTER = "Jacob hates understanding systems"


# ── fakes ─────────────────────────────────────────────────────────────


class _Mem:
    def __init__(
        self,
        mem_id: int,
        content: str,
        *,
        confidence: float = 0.7,
        pinned: bool = False,
        temporal_type: str = "durable",
        relevance_until: str | None = None,
    ) -> None:
        self.id = mem_id
        self.content = content
        self.confidence = confidence
        self.pinned = pinned
        self.temporal_type = temporal_type
        self.relevance_until = relevance_until
        self.metadata: dict = {}


class _MemoryStore:
    def __init__(self, mems) -> None:
        self._mems = {m.id: m for m in mems}
        self.update_calls: list[tuple] = []
        self.reclassify_calls: list[tuple] = []
        self.notified: list[int] = []

    def get(self, mid):
        return self._mems.get(int(mid))

    def update(
        self,
        mid,
        *,
        confidence=None,
        metadata=None,
        metadata_merge=False,
        **_kw,
    ):
        m = self._mems.get(int(mid))
        if m is None:
            return None
        if confidence is not None:
            m.confidence = confidence
        if metadata is not None:
            m.metadata = (
                {**m.metadata, **metadata} if metadata_merge else dict(metadata)
            )
        self.update_calls.append((int(mid), confidence, dict(metadata or {})))
        return m

    def reclassify(self, mid, *, temporal_type, relevance_until=None, **_kw):
        m = self._mems.get(int(mid))
        if m is None:
            return None
        m.temporal_type = temporal_type
        m.relevance_until = relevance_until
        self.reclassify_calls.append((int(mid), temporal_type, relevance_until))
        return m


class _Edge:
    def __init__(self, src_type: str, src_id, ordinal=None) -> None:
        self.src_type = src_type
        self.src_id = src_id
        self.ordinal = ordinal


class _ConceptStore:
    def __init__(self, edges) -> None:
        self._edges = list(edges)

    def evidence_of(self, _cid):
        return list(self._edges)


class _RateLimiter:
    def __init__(self, allowed: bool = True) -> None:
        self._allowed = allowed
        self.calls = 0

    def allow(self, now=None) -> bool:
        self.calls += 1
        return self._allowed


class _Ollama:
    def __init__(self, resolution_json: str) -> None:
        self._json = resolution_json
        self.calls = 0

    def chat_stream(self, messages, **_kw):
        self.calls += 1
        yield self._json


def _concept(plasticity: float = 0.5):
    return SimpleNamespace(
        concept_id=1, label=_BELIEF, rationale="", plasticity=plasticity,
    )


def _verdict(snippet: str = _COUNTER, memory_id: int = 99):
    return SimpleNamespace(
        snippet=snippet, memory_id=memory_id, similarity=0.8,
    )


def _reviser(
    concept_store,
    memory_store,
    *,
    rate_limiter=None,
    ollama=None,
    max_evidence=6,
    confidence_penalty=0.2,
    confidence_floor=0.2,
    notify=None,
):
    return ConceptBeliefReviser(
        concept_store=concept_store,
        memory_store=memory_store,
        ollama=ollama or _Ollama('{"resolution": "KEEP"}'),
        chat_model="test-model",
        rate_limiter=rate_limiter or _RateLimiter(True),
        cancel_event=None,
        max_evidence=max_evidence,
        confidence_penalty=confidence_penalty,
        confidence_floor=confidence_floor,
        superseded_relevance_days=7.0,
        notify_memory_updated=notify,
        clock=lambda: _NOW,
    )


# ── tests ─────────────────────────────────────────────────────────────


class ArbitrationTests(unittest.TestCase):
    def test_inaccurate_lowers_confidence_floored(self) -> None:
        mem = _Mem(11, _BELIEF, confidence=0.7)
        ms = _MemoryStore([mem])
        cs = _ConceptStore([_Edge("memory", "11")])
        ollama = _Ollama('{"resolution": "INACCURATE", "reason": "wrong"}')
        rv = _reviser(cs, ms, ollama=ollama)
        out = rv.revise(_concept(), _verdict(), now=_NOW)
        self.assertEqual(out.lowered, 1)
        self.assertEqual(out.superseded, 0)
        # L16: plasticity-damped cut. p=0.5 -> 0.75x penalty: 0.7 - 0.2*0.75.
        self.assertAlmostEqual(mem.confidence, 0.55, places=6)
        self.assertEqual(mem.metadata.get("belief_revision"), "inaccurate")
        self.assertEqual(mem.metadata.get("belief_revised_by"), 1)
        self.assertEqual(len(ms.reclassify_calls), 0)

    def test_inaccurate_respects_floor(self) -> None:
        mem = _Mem(11, _BELIEF, confidence=0.3)
        ms = _MemoryStore([mem])
        cs = _ConceptStore([_Edge("memory", "11")])
        ollama = _Ollama('{"resolution": "INACCURATE"}')
        # penalty 0.2 (p=0.5 -> 0.15 effective) takes 0.3 -> 0.15, but the
        # 0.25 floor clamps it.
        rv = _reviser(cs, ms, ollama=ollama, confidence_floor=0.25)
        out = rv.revise(_concept(), _verdict(), now=_NOW)
        self.assertEqual(out.lowered, 1)
        self.assertAlmostEqual(mem.confidence, 0.25, places=6)

    def test_cut_scales_with_concept_plasticity(self) -> None:
        # L16: same base penalty, a sticky (low-plasticity) belief cuts its
        # supporting memory less than a fluid (high-plasticity) one.
        sticky_mem = _Mem(11, _BELIEF, confidence=0.7)
        ms1 = _MemoryStore([sticky_mem])
        cs1 = _ConceptStore([_Edge("memory", "11")])
        rv1 = _reviser(cs1, ms1, ollama=_Ollama('{"resolution": "INACCURATE"}'))
        rv1.revise(_concept(plasticity=0.0), _verdict(), now=_NOW)

        plastic_mem = _Mem(11, _BELIEF, confidence=0.7)
        ms2 = _MemoryStore([plastic_mem])
        cs2 = _ConceptStore([_Edge("memory", "11")])
        rv2 = _reviser(cs2, ms2, ollama=_Ollama('{"resolution": "INACCURATE"}'))
        rv2.revise(_concept(plasticity=1.0), _verdict(), now=_NOW)

        self.assertGreater(sticky_mem.confidence, plastic_mem.confidence)
        # p=0 -> 0.5x penalty: 0.7 - 0.1 = 0.6; p=1 -> 1x: 0.7 - 0.2 = 0.5.
        self.assertAlmostEqual(sticky_mem.confidence, 0.6, places=6)
        self.assertAlmostEqual(plastic_mem.confidence, 0.5, places=6)

    def test_superseded_reclassifies_without_confidence_cut(self) -> None:
        mem = _Mem(11, _BELIEF, confidence=0.7)
        ms = _MemoryStore([mem])
        cs = _ConceptStore([_Edge("memory", "11")])
        ollama = _Ollama('{"resolution": "SUPERSEDED", "reason": "changed"}')
        rv = _reviser(cs, ms, ollama=ollama)
        out = rv.revise(_concept(), _verdict(), now=_NOW)
        self.assertEqual(out.superseded, 1)
        self.assertEqual(out.lowered, 0)
        self.assertAlmostEqual(mem.confidence, 0.7, places=6)  # untouched
        self.assertEqual(mem.temporal_type, "past_event")
        self.assertIsNotNone(mem.relevance_until)
        self.assertEqual(len(ms.reclassify_calls), 1)
        self.assertEqual(mem.metadata.get("belief_revision"), "superseded")

    def test_keep_writes_nothing(self) -> None:
        mem = _Mem(11, _BELIEF, confidence=0.7)
        ms = _MemoryStore([mem])
        cs = _ConceptStore([_Edge("memory", "11")])
        ollama = _Ollama('{"resolution": "KEEP", "reason": "over-reach"}')
        rv = _reviser(cs, ms, ollama=ollama)
        out = rv.revise(_concept(), _verdict(), now=_NOW)
        self.assertEqual(out.kept, 1)
        self.assertEqual(out.lowered, 0)
        self.assertEqual(out.superseded, 0)
        self.assertEqual(len(ms.update_calls), 0)
        self.assertEqual(len(ms.reclassify_calls), 0)


class GuardrailTests(unittest.TestCase):
    def test_pinned_memory_never_touched(self) -> None:
        mem = _Mem(11, _BELIEF, confidence=0.7, pinned=True)
        ms = _MemoryStore([mem])
        cs = _ConceptStore([_Edge("memory", "11")])
        ollama = _Ollama('{"resolution": "INACCURATE"}')
        rl = _RateLimiter(True)
        rv = _reviser(cs, ms, ollama=ollama, rate_limiter=rl)
        out = rv.revise(_concept(), _verdict(), now=_NOW)
        self.assertEqual(out.skipped_pinned, 1)
        self.assertEqual(out.lowered, 0)
        self.assertEqual(ollama.calls, 0)  # never reached the LLM
        self.assertEqual(rl.calls, 0)
        self.assertEqual(len(ms.update_calls), 0)

    def test_compatible_memory_skips_llm(self) -> None:
        # No opposition signal vs the counter-evidence => classify_pair "no".
        mem = _Mem(11, "Jacob enjoys hiking on weekends", confidence=0.7)
        ms = _MemoryStore([mem])
        cs = _ConceptStore([_Edge("memory", "11")])
        ollama = _Ollama('{"resolution": "INACCURATE"}')
        rl = _RateLimiter(True)
        rv = _reviser(cs, ms, ollama=ollama, rate_limiter=rl)
        out = rv.revise(_concept(), _verdict(), now=_NOW)
        self.assertEqual(out.kept, 1)
        self.assertEqual(ollama.calls, 0)
        self.assertEqual(rl.calls, 0)
        self.assertEqual(len(ms.update_calls), 0)

    def test_rate_limited_defers_conflict(self) -> None:
        mem = _Mem(11, _BELIEF, confidence=0.7)
        ms = _MemoryStore([mem])
        cs = _ConceptStore([_Edge("memory", "11")])
        ollama = _Ollama('{"resolution": "INACCURATE"}')
        rl = _RateLimiter(False)
        rv = _reviser(cs, ms, ollama=ollama, rate_limiter=rl)
        out = rv.revise(_concept(), _verdict(), now=_NOW)
        self.assertEqual(out.deferred_rate_limit, 1)
        self.assertEqual(out.lowered, 0)
        self.assertEqual(rl.calls, 1)
        self.assertEqual(ollama.calls, 0)  # budget denied => no LLM call
        self.assertEqual(len(ms.update_calls), 0)

    def test_max_evidence_caps_work(self) -> None:
        mems = [_Mem(i, _BELIEF, confidence=0.7) for i in range(1, 6)]
        ms = _MemoryStore(mems)
        cs = _ConceptStore([_Edge("memory", str(i)) for i in range(1, 6)])
        ollama = _Ollama('{"resolution": "KEEP"}')
        rv = _reviser(cs, ms, ollama=ollama, max_evidence=2)
        out = rv.revise(_concept(), _verdict(), now=_NOW)
        self.assertEqual(out.checked, 2)

    def test_non_memory_evidence_ignored(self) -> None:
        mem = _Mem(11, _BELIEF, confidence=0.7)
        ms = _MemoryStore([mem])
        cs = _ConceptStore([
            _Edge("cluster", "topic-3"),
            _Edge("memory", "11"),
        ])
        ollama = _Ollama('{"resolution": "INACCURATE"}')
        rv = _reviser(cs, ms, ollama=ollama)
        out = rv.revise(_concept(), _verdict(), now=_NOW)
        self.assertEqual(out.checked, 1)  # only the memory edge
        self.assertEqual(out.lowered, 1)

    def test_notify_fires_on_write(self) -> None:
        mem = _Mem(11, _BELIEF, confidence=0.7)
        ms = _MemoryStore([mem])
        cs = _ConceptStore([_Edge("memory", "11")])
        seen: list[dict] = []
        ollama = _Ollama('{"resolution": "INACCURATE"}')
        rv = _reviser(cs, ms, ollama=ollama, notify=seen.append)
        rv.revise(_concept(), _verdict(), now=_NOW)
        self.assertEqual(seen, [{"memory_id": 11}])

    def test_empty_counter_text_is_noop(self) -> None:
        mem = _Mem(11, _BELIEF, confidence=0.7)
        ms = _MemoryStore([mem])
        cs = _ConceptStore([_Edge("memory", "11")])
        ollama = _Ollama('{"resolution": "INACCURATE"}')
        rv = _reviser(cs, ms, ollama=ollama)
        out = rv.revise(_concept(), _verdict(snippet=""), now=_NOW)
        self.assertEqual(out.checked, 0)
        self.assertEqual(ollama.calls, 0)


if __name__ == "__main__":
    unittest.main()
