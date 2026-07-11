"""Tests for L2 near-duplicate concept consolidation.

Two halves:

* :class:`MergeIntoTests` -- the :meth:`ConceptStore.merge_into` structural
  primitive (edge re-point + dedupe, union counts, dependent cascade,
  absorbed-row deletion, confidence untouched, refusal guards).
* :class:`ConsolidationWorkerTests` -- the
  :class:`ConceptConsolidationWorker` orchestration (enable / maturity
  gates, a genuine merge on a ``same`` verdict, a template-collision left
  intact on ``false`` + cached, the per-tick LLM cap, and the sub-threshold
  no-LLM path).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from app.core.concepts.concept_consolidation_worker import (
    ConceptConsolidationWorker,
)
from app.core.concepts.concept_event_store import ConceptEventStore
from app.core.concepts.concept_store import Concept, ConceptEdge, ConceptStore
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter

_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _vec(*xs: float) -> np.ndarray:
    return np.asarray(xs, dtype=np.float32)


def _new_store() -> tuple[ChatDatabase, ConceptStore, ConceptEventStore]:
    tmp = tempfile.mkdtemp()
    db = ChatDatabase(Path(tmp) / "test.db")
    return db, ConceptStore(db), ConceptEventStore(db)


def _add(
    store: ConceptStore,
    *,
    label: str,
    subject: str = "user",
    kind: str = "identity",
    status: str = "active",
    confidence: float = 0.8,
    distinct_source_count: int = 2,
    evidence_count: int = 2,
    embedding: np.ndarray | None = None,
) -> Concept:
    c = Concept(
        label=label,
        kind=kind,
        subject=subject,
        status=status,
        confidence=confidence,
        plasticity=0.5,
        evidence_count=evidence_count,
        distinct_source_count=distinct_source_count,
        embedding=(embedding if embedding is not None else _vec(1.0, 0.0)),
    )
    c.concept_id = store.add(c)
    return c


def _evidence(store: ConceptStore, src_type: str, src_id: str, dst_id: int):
    store.add_edge(
        ConceptEdge(
            src_type=src_type,
            src_id=src_id,
            dst_type="concept",
            dst_id=str(dst_id),
            relation="evidence",
        )
    )


class MergeIntoTests(unittest.TestCase):
    def test_repoints_and_dedupes_evidence(self) -> None:
        _db, store, _ev = _new_store()
        canonical = _add(store, label="A", confidence=0.9)
        absorbed = _add(store, label="B", confidence=0.7)
        # Shared source (memory:10) on both, plus distinct ones.
        _evidence(store, "memory", "10", canonical.concept_id)
        _evidence(store, "memory", "10", absorbed.concept_id)
        _evidence(store, "memory", "11", absorbed.concept_id)
        _evidence(store, "cluster", "5", absorbed.concept_id)

        ok = store.merge_into(
            canonical_id=canonical.concept_id,
            absorbed_id=absorbed.concept_id,
        )
        self.assertTrue(ok)
        # Absorbed row + its edges are gone.
        self.assertIsNone(store.get(absorbed.concept_id))
        self.assertEqual(store.edges_into("concept", absorbed.concept_id), [])
        # Canonical now carries the union of distinct sources: mem10 (deduped),
        # mem11, cluster5 => 3 distinct, 3 evidence edges.
        merged = store.get(canonical.concept_id)
        self.assertEqual(merged.distinct_source_count, 3)
        self.assertEqual(merged.evidence_count, 3)
        srcs = {(e.src_type, e.src_id) for e in store.evidence_of(
            canonical.concept_id)}
        self.assertEqual(
            srcs, {("memory", "10"), ("memory", "11"), ("cluster", "5")}
        )

    def test_confidence_untouched(self) -> None:
        _db, store, _ev = _new_store()
        canonical = _add(store, label="A", confidence=0.91)
        absorbed = _add(store, label="B", confidence=0.6)
        store.merge_into(
            canonical_id=canonical.concept_id,
            absorbed_id=absorbed.concept_id,
        )
        self.assertAlmostEqual(
            store.get(canonical.concept_id).confidence, 0.91, places=6
        )

    def test_repoints_dependents(self) -> None:
        _db, store, _ev = _new_store()
        canonical = _add(store, label="A")
        absorbed = _add(store, label="B")
        meta = _add(store, label="Meta", kind="tension")
        # ``absorbed`` is a base of the meta: absorbed -> meta (references).
        store.add_edge(
            ConceptEdge(
                src_type="concept",
                src_id=str(absorbed.concept_id),
                dst_type="concept",
                dst_id=str(meta.concept_id),
                relation="references",
            )
        )
        store.merge_into(
            canonical_id=canonical.concept_id,
            absorbed_id=absorbed.concept_id,
        )
        # The dependency now flows from the canonical instead.
        deps = store.dependents_of(canonical.concept_id)
        self.assertIn(meta.concept_id, deps)
        self.assertIsNone(store.get(absorbed.concept_id))

    def test_refuses_cross_kind_and_subject(self) -> None:
        _db, store, _ev = _new_store()
        a = _add(store, label="A", kind="identity")
        b = _add(store, label="B", kind="value")
        self.assertFalse(
            store.merge_into(
                canonical_id=a.concept_id, absorbed_id=b.concept_id
            )
        )
        c = _add(store, label="C", subject="user")
        d = _add(store, label="D", subject="aiko")
        self.assertFalse(
            store.merge_into(
                canonical_id=c.concept_id, absorbed_id=d.concept_id
            )
        )
        # All four survive.
        for x in (a, b, c, d):
            self.assertIsNotNone(store.get(x.concept_id))

    def test_refuses_same_id_and_missing(self) -> None:
        _db, store, _ev = _new_store()
        a = _add(store, label="A")
        self.assertFalse(
            store.merge_into(
                canonical_id=a.concept_id, absorbed_id=a.concept_id
            )
        )
        self.assertFalse(
            store.merge_into(canonical_id=a.concept_id, absorbed_id=999999)
        )

    def test_refuses_conflict_edge(self) -> None:
        _db, store, _ev = _new_store()
        a = _add(store, label="A")
        b = _add(store, label="B")
        store.add_edge(
            ConceptEdge(
                src_type="concept",
                src_id=str(a.concept_id),
                dst_type="concept",
                dst_id=str(b.concept_id),
                relation="contradicts",
            )
        )
        self.assertFalse(
            store.merge_into(
                canonical_id=a.concept_id, absorbed_id=b.concept_id
            )
        )
        self.assertIsNotNone(store.get(b.concept_id))


class _FakeOllama:
    """Minimal ``chat_stream`` stub: yields a JSON verdict per call."""

    def __init__(self, responder) -> None:
        self._responder = responder
        self.calls = 0

    def chat_stream(self, messages, **_kw):
        self.calls += 1
        resp = (
            self._responder(messages)
            if callable(self._responder)
            else self._responder
        )
        yield json.dumps(resp)


def _settings(**over) -> SimpleNamespace:
    base = dict(
        concept_consolidation_enabled=True,
        concept_consolidation_interval_seconds=900,
        concept_consolidation_batch_size=40,
        concept_consolidation_merge_cosine=0.88,
        concept_min_clusters=6,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _worker(
    store,
    events,
    db,
    *,
    responder,
    settings=None,
    per_hour_cap=10,
    per_day_cap=50,
    graph_mature=True,
    concepts_enabled=True,
):
    ollama = _FakeOllama(responder)
    limiter = FactCheckRateLimiter(
        db,
        per_hour_cap=per_hour_cap,
        per_day_cap=per_day_cap,
        state_key="test.consolidation.rate_state",
    )
    worker = ConceptConsolidationWorker(
        concept_store=store,
        memory_settings=settings or _settings(),
        agent_settings=SimpleNamespace(concepts_enabled=concepts_enabled),
        ollama=ollama,
        chat_model="m",
        rate_limiter=limiter,
        concept_event_store=events,
        graph_mature_provider=(lambda: graph_mature),
        clock=lambda: _NOW,
    )
    return worker, ollama


class ConsolidationWorkerTests(unittest.TestCase):
    def _twin_pair(self, store):
        # cos(A,B) ~ 0.98 (>= 0.88); a far-away decoy so nearest is stable.
        a = _add(store, label="values understanding systems",
                 confidence=0.9, distinct_source_count=5,
                 embedding=_vec(1.0, 0.0, 0.0))
        b = _add(store, label="values grasping how systems work",
                 confidence=0.7, distinct_source_count=2,
                 embedding=_vec(0.98, 0.2, 0.0))
        return a, b

    def test_disabled_noop(self) -> None:
        _db, store, ev = _new_store()
        self._twin_pair(store)
        worker, ollama = _worker(
            store, ev, _db,
            responder={"same": True, "reason": "x"},
            settings=_settings(concept_consolidation_enabled=False),
        )
        out = worker.run()
        self.assertTrue(out.get("skipped"))
        self.assertEqual(ollama.calls, 0)

    def test_immature_graph_noop(self) -> None:
        _db, store, ev = _new_store()
        self._twin_pair(store)
        worker, ollama = _worker(
            store, ev, _db,
            responder={"same": True, "reason": "x"},
            graph_mature=False,
        )
        out = worker.run()
        self.assertEqual(out.get("reason"), "immature_graph")
        self.assertEqual(ollama.calls, 0)

    def test_merges_on_same_verdict(self) -> None:
        _db, store, ev = _new_store()
        a, b = self._twin_pair(store)
        worker, ollama = _worker(
            store, ev, _db, responder={"same": True, "reason": "paraphrase"}
        )
        out = worker.run()
        self.assertEqual(out["merged"], 1)
        self.assertEqual(ollama.calls, 1)
        # Stronger row (a: conf 0.9) survives; b is absorbed.
        self.assertIsNotNone(store.get(a.concept_id))
        self.assertIsNone(store.get(b.concept_id))
        # A "merged" timeline event was recorded against the canonical.
        merged_events = ev.list(event_type="merged")
        self.assertEqual(len(merged_events), 1)
        self.assertEqual(merged_events[0].concept_id, a.concept_id)

    def test_false_verdict_left_intact_and_cached(self) -> None:
        _db, store, ev = _new_store()
        a, b = self._twin_pair(store)
        worker, ollama = _worker(
            store, ev, _db, responder={"same": False, "reason": "distinct"}
        )
        out = worker.run()
        self.assertEqual(out["merged"], 0)
        self.assertEqual(out["adjudicated"], 1)
        self.assertIsNotNone(store.get(a.concept_id))
        self.assertIsNotNone(store.get(b.concept_id))
        # Second run: the rejected pair is cached, so no new LLM call.
        out2 = worker.run()
        self.assertEqual(out2["adjudicated"], 0)
        self.assertEqual(ollama.calls, 1)

    def test_rate_cap_bounds_adjudications(self) -> None:
        _db, store, ev = _new_store()
        # Two independent near-dup pairs (orthogonal across pairs).
        _add(store, label="p1a", confidence=0.9,
             embedding=_vec(1.0, 0.0, 0.0, 0.0))
        _add(store, label="p1b", confidence=0.7,
             embedding=_vec(0.98, 0.2, 0.0, 0.0))
        _add(store, label="p2a", confidence=0.9,
             embedding=_vec(0.0, 0.0, 1.0, 0.0))
        _add(store, label="p2b", confidence=0.7,
             embedding=_vec(0.0, 0.0, 0.98, 0.2))
        worker, ollama = _worker(
            store, ev, _db,
            responder={"same": False, "reason": "n"},
            per_hour_cap=1,
        )
        out = worker.run()
        self.assertEqual(out["pairs_considered"], 2)
        self.assertEqual(out["adjudicated"], 1)
        self.assertEqual(ollama.calls, 1)

    def test_below_threshold_no_llm(self) -> None:
        _db, store, ev = _new_store()
        _add(store, label="x", embedding=_vec(1.0, 0.0))
        _add(store, label="y", embedding=_vec(0.7, 0.714))  # cos ~0.7
        worker, ollama = _worker(
            store, ev, _db, responder={"same": True, "reason": "x"}
        )
        out = worker.run()
        self.assertEqual(out["pairs_considered"], 0)
        self.assertEqual(out["merged"], 0)
        self.assertEqual(ollama.calls, 0)


if __name__ == "__main__":
    unittest.main()
