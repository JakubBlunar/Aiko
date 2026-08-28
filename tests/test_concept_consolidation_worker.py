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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from app.core.concepts.concept_consolidation_worker import (
    ConceptConsolidationWorker,
)
from app.core.concepts.concept_dedupe import DEDUPE_COS
from app.core.concepts.concept_event_store import ConceptEventStore
from app.core.concepts.concept_store import Concept, ConceptEdge, ConceptStore
from app.core.infra.chat_database import ChatDatabase
from app.core.infra.memory_settings import MemorySettings
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
    evidence_model: str = "set",
    embedding: np.ndarray | None = None,
) -> Concept:
    c = Concept(
        label=label,
        kind=kind,
        subject=subject,
        status=status,
        evidence_model=evidence_model,
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
        # Stated rather than inherited: the adjudication tests below use
        # ~0.98 twin fixtures, so if the shipped default ever moves off
        # "disabled" they would silently route down the zero-LLM path and
        # assert nothing. :class:`AutoMergeTests` sets its own bar.
        concept_consolidation_auto_merge_cosine=1.0,
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
    kv=True,
    now=None,
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
        kv_get=db.kv_get if kv else None,
        kv_set=db.kv_set if kv else None,
        clock=(now or (lambda: _NOW)),
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


class AutoMergeTests(unittest.TestCase):
    """L46: a pair at or above ``auto_merge_cosine`` merges with no
    adjudication -- a path that ships **disabled**.

    It was planned at ``DEDUPE_COS``, reasoning that the creation guard fuses
    at that cosine without asking anyone. The reasoning does not transfer,
    because the two cases fail in opposite directions: at creation a false
    positive reinforces a row, here it destroys a belief. See
    ``memory_settings`` for the measurement that settled it. These tests
    therefore set the bar explicitly, and the mechanism is exercised so it
    stays correct for a graph that wants it.
    """

    def _settings_auto(self, **over):
        # Candidate bar low, auto bar at the cosine L46 originally proposed.
        base = dict(
            concept_consolidation_merge_cosine=0.80,
            concept_consolidation_auto_merge_cosine=0.86,
        )
        base.update(over)
        return _settings(**base)

    def test_the_auto_bar_ships_disabled(self) -> None:
        """Pinned because the shipped default reverses the plan. On the live
        graph, 14 of the 18 pairs above ``DEDUPE_COS`` were template
        collisions rather than twins, so auto-merging there would delete
        distinct beliefs -- and cosine is the only signal, token overlap
        having failed to separate the groups."""
        auto = MemorySettings().concept_consolidation_auto_merge_cosine
        self.assertAlmostEqual(auto, 1.0)
        self.assertGreater(auto, DEDUPE_COS)

    def test_merges_above_the_bar_without_the_llm(self) -> None:
        _db, store, ev = _new_store()
        a = _add(store, label="A", confidence=0.9,
                 embedding=_vec(1.0, 0.0, 0.0))
        b = _add(store, label="B", confidence=0.7,
                 embedding=_vec(0.98, 0.2, 0.0))  # cos ~0.98
        worker, ollama = _worker(
            store, ev, _db,
            responder={"same": False, "reason": "would have refused"},
            settings=self._settings_auto(),
        )
        out = worker.run()
        self.assertEqual(out["auto_merged"], 1)
        self.assertEqual(out["adjudicated"], 0)
        self.assertEqual(ollama.calls, 0)
        # Stronger row survives, exactly as on the adjudicated path.
        self.assertIsNotNone(store.get(a.concept_id))
        self.assertIsNone(store.get(b.concept_id))

    def test_auto_merge_costs_no_rate_limiter_budget(self) -> None:
        """The starvation this fixes was budget, so an auto-merge must not
        touch it -- otherwise clearing the backlog would still be capped at
        thirty a day."""
        _db, store, ev = _new_store()
        _add(store, label="A", confidence=0.9, embedding=_vec(1.0, 0.0, 0.0))
        _add(store, label="B", confidence=0.7, embedding=_vec(0.98, 0.2, 0.0))
        worker, _ollama = _worker(
            store, ev, _db,
            responder={"same": True, "reason": "x"},
            settings=self._settings_auto(),
            per_hour_cap=0,  # no adjudication possible at all
        )
        out = worker.run()
        self.assertEqual(out["auto_merged"], 1)
        self.assertFalse(out["rate_limited"])

    def test_between_the_bars_still_adjudicates(self) -> None:
        _db, store, ev = _new_store()
        # cos ~0.848: over the candidate bar, under the auto bar.
        a = _add(store, label="A", confidence=0.9, embedding=_vec(1.0, 0.0))
        b = _add(store, label="B", confidence=0.7,
                 embedding=_vec(0.848, 0.53))
        worker, ollama = _worker(
            store, ev, _db,
            responder={"same": False, "reason": "template collision"},
            settings=self._settings_auto(),
        )
        out = worker.run()
        self.assertEqual(out["auto_merged"], 0)
        self.assertEqual(out["adjudicated"], 1)
        self.assertEqual(ollama.calls, 1)
        self.assertIsNotNone(store.get(a.concept_id))
        self.assertIsNotNone(store.get(b.concept_id))

    def test_a_merged_row_is_not_reused_by_a_later_pair(self) -> None:
        """Three mutually-similar rows yield three pairs; the first merge
        deletes one of them, and the pairs naming it must be dropped rather
        than sent to ``merge_into`` (or, worse, to the adjudicator) against a
        row that no longer exists."""
        _db, store, ev = _new_store()
        _add(store, label="A", confidence=0.9, embedding=_vec(1.0, 0.0, 0.0))
        _add(store, label="B", confidence=0.8,
             embedding=_vec(0.99, 0.14, 0.0))
        _add(store, label="C", confidence=0.7,
             embedding=_vec(0.98, 0.2, 0.0))
        worker, ollama = _worker(
            store, ev, _db,
            responder={"same": True, "reason": "x"},
            settings=self._settings_auto(),
        )
        out = worker.run()
        self.assertEqual(out["pairs_considered"], 3)
        # Two merges collapse three rows into one; the third pair named a
        # row already gone.
        self.assertEqual(out["auto_merged"], 2)
        self.assertEqual(ollama.calls, 0)
        self.assertEqual(len(store.list_by(status="active")), 1)


class VerdictCacheTests(unittest.TestCase):
    """L46: rejections persist. They used to live in a dict that died with
    the process, so a restart re-litigated the same stable template
    collisions out of the same thirty-a-day budget."""

    def _pair(self, store):
        a = _add(store, label="A energizes him", confidence=0.9,
                 embedding=_vec(1.0, 0.0))
        b = _add(store, label="B energizes him", confidence=0.7,
                 embedding=_vec(0.9, 0.436))  # cos ~0.9, under a 1.0 auto bar
        return a, b

    def test_a_verdict_survives_a_new_worker_on_the_same_db(self) -> None:
        db, store, ev = _new_store()
        self._pair(store)
        first, ollama1 = _worker(
            db=db, store=store, events=ev,
            responder={"same": False, "reason": "different specifics"},
        )
        first.run()
        self.assertEqual(ollama1.calls, 1)

        # A fresh worker object over the same database: a restart.
        second, ollama2 = _worker(
            db=db, store=store, events=ev,
            responder={"same": False, "reason": "different specifics"},
        )
        out = second.run()
        self.assertEqual(out["adjudicated"], 0)
        self.assertEqual(ollama2.calls, 0)

    def test_without_kv_the_verdict_is_still_honoured_in_process(self) -> None:
        """No kv wired (lean / test deployments) must degrade to the old
        in-memory behaviour rather than re-asking every tick."""
        db, store, ev = _new_store()
        self._pair(store)
        worker, ollama = _worker(
            db=db, store=store, events=ev, kv=False,
            responder={"same": False, "reason": "n"},
        )
        worker.run()
        worker.run()
        self.assertEqual(ollama.calls, 1)

    def test_a_relabel_reopens_the_question(self) -> None:
        """The verdict is about two *statements*. L17 moves labels, and that
        is exactly the case that produced the above-bar backlog, so a verdict
        keyed on ids alone would go on suppressing a pair whose wording no
        longer matches what the adjudicator was shown."""
        db, store, ev = _new_store()
        _a, b = self._pair(store)
        worker, ollama = _worker(
            db=db, store=store, events=ev,
            responder={"same": False, "reason": "different specifics"},
        )
        worker.run()
        self.assertEqual(ollama.calls, 1)

        b.label = "A energizes him, restated"
        store.update(b)

        worker2, ollama2 = _worker(
            db=db, store=store, events=ev,
            responder={"same": False, "reason": "still no"},
        )
        out = worker2.run()
        self.assertEqual(out["adjudicated"], 1)
        self.assertEqual(ollama2.calls, 1)

    def test_an_expired_verdict_is_re_asked(self) -> None:
        db, store, ev = _new_store()
        self._pair(store)
        worker, _o1 = _worker(
            db=db, store=store, events=ev,
            responder={"same": False, "reason": "n"},
        )
        worker.run()

        later = _NOW + timedelta(days=31)
        worker2, ollama2 = _worker(
            db=db, store=store, events=ev,
            responder={"same": False, "reason": "n"},
            now=lambda: later,
        )
        out = worker2.run()
        self.assertEqual(out["adjudicated"], 1)
        self.assertEqual(ollama2.calls, 1)

    def test_an_unreadable_stamp_re_asks_rather_than_suppressing(self) -> None:
        """A timestamp nothing can parse must not freeze a pair forever.

        The signature is the *correct* one, so the only thing that can let
        this pair through is the unreadable expiry being treated as expired.
        """
        db, store, ev = _new_store()
        a, b = self._pair(store)
        key = ":".join(
            str(i) for i in sorted((a.concept_id, b.concept_id))
        )
        db.kv_set(
            "concept_consolidation.verdicts",
            json.dumps({
                key: {
                    "at": "not-a-date",
                    "sig": ConceptConsolidationWorker._pair_sig(a, b),
                },
            }),
        )
        worker, ollama = _worker(
            db=db, store=store, events=ev,
            responder={"same": False, "reason": "n"},
        )
        out = worker.run()
        self.assertEqual(out["adjudicated"], 1)
        self.assertEqual(ollama.calls, 1)


class GlobalDiscoveryTests(unittest.TestCase):
    """L46: discovery is a global scan, worst-first.

    It used to walk ``list_stalest(batch_size)`` keeping one neighbour per
    seed -- so a tick saw at most ``batch_size`` pairs, and its cursor was
    ``last_lifecycle_at``, a column only the L3 worker writes. Consolidation
    could not advance its own position, and re-derived roughly the same forty
    pairs every fifteen minutes while the backlog grew to 147.
    """

    def test_sees_more_than_one_pair_per_concept(self) -> None:
        _db, store, ev = _new_store()
        # Three mutually-similar rows: three pairs, where the old one-per-seed
        # rule could only ever have reported the strongest neighbour of each.
        _add(store, label="A", embedding=_vec(1.0, 0.0, 0.0))
        _add(store, label="B", embedding=_vec(0.99, 0.14, 0.0))
        _add(store, label="C", embedding=_vec(0.98, 0.2, 0.0))
        worker, _o = _worker(
            store, ev, _db, responder={"same": False, "reason": "n"},
            settings=_settings(concept_consolidation_merge_cosine=0.9),
            per_hour_cap=0,
        )
        out = worker.run()
        self.assertEqual(out["pairs_considered"], 3)

    def test_ignores_the_l3_cursor(self) -> None:
        """Every row here has a fresh ``last_lifecycle_at``, so under the old
        stalest-first fetch with a small batch the twins could be invisible.
        Discovery must not depend on a column it cannot write."""
        _db, store, ev = _new_store()
        stamp = "2026-07-01T11:00:00+00:00"

        def one_hot(slot: int) -> np.ndarray:
            xs = [0.0] * 8
            xs[slot] = 1.0
            return _vec(*xs)

        # Six mutually-orthogonal fillers, then the one real twin pair. All
        # eight share a dim so none is dropped as a minority-dim row.
        vectors = [one_hot(i + 2) for i in range(6)]
        vectors.append(one_hot(0))
        twin = [0.0] * 8
        twin[0], twin[1] = 0.98, 0.2
        vectors.append(_vec(*twin))
        for i, vec in enumerate(vectors):
            c = _add(store, label=f"c{i}", embedding=vec)
            c.last_lifecycle_at = stamp
            store.update(c)
        worker, _o = _worker(
            store, ev, _db, responder={"same": False, "reason": "n"},
            settings=_settings(
                concept_consolidation_batch_size=1,
                concept_consolidation_merge_cosine=0.9,
            ),
            per_hour_cap=0,
        )
        out = worker.run()
        self.assertEqual(out["scanned"], 8)
        self.assertEqual(out["pairs_considered"], 1)

    def test_adjudicates_the_most_similar_pair_first(self) -> None:
        """With a budget of one, the token goes to the strongest candidate."""
        _db, store, ev = _new_store()
        # Weak pair (cos ~0.91) and strong pair (cos ~0.995), far apart.
        _add(store, label="weak-a", embedding=_vec(1.0, 0.0, 0.0, 0.0))
        _add(store, label="weak-b", embedding=_vec(0.91, 0.415, 0.0, 0.0))
        _add(store, label="strong-a", embedding=_vec(0.0, 0.0, 1.0, 0.0))
        _add(store, label="strong-b", embedding=_vec(0.0, 0.0, 0.995, 0.1))
        seen: list[str] = []

        def responder(messages):
            seen.append(messages[1]["content"])
            return {"same": False, "reason": "n"}

        worker, _o = _worker(
            store, ev, _db, responder=responder,
            settings=_settings(concept_consolidation_merge_cosine=0.9),
            per_hour_cap=1,
        )
        out = worker.run()
        self.assertEqual(out["pairs_considered"], 2)
        self.assertEqual(out["adjudicated"], 1)
        self.assertTrue(out["rate_limited"])
        self.assertEqual(len(seen), 1)
        self.assertIn("strong-", seen[0])
        self.assertNotIn("weak-", seen[0])

    def test_pairs_are_capped_by_batch_size(self) -> None:
        """``batch_size`` became the cap on pairs *acted on*, not on seeds
        scanned -- the whole backlog is visible every tick now, so this is
        what keeps one tick from trying to work all of it."""
        _db, store, ev = _new_store()
        for i in range(5):
            _add(store, label=f"c{i}",
                 embedding=_vec(1.0, 0.02 * i, 0.0))
        worker, ollama = _worker(
            store, ev, _db, responder={"same": False, "reason": "n"},
            settings=_settings(
                concept_consolidation_batch_size=3,
                concept_consolidation_merge_cosine=0.9,
            ),
            per_hour_cap=50, per_day_cap=50,
        )
        out = worker.run()
        self.assertEqual(out["pairs_considered"], 10)  # 5 choose 2
        # ...but only three carried into the run, budget notwithstanding.
        self.assertEqual(out["adjudicated"], 3)
        self.assertEqual(ollama.calls, 3)
        self.assertFalse(out["rate_limited"])

    def test_pairs_never_cross_subject_or_kind(self) -> None:
        """``merge_into`` refuses a cross-kind / cross-subject merge, so
        offering one to the adjudicator would spend a token on a pair that
        could not be acted on either way."""
        _db, store, ev = _new_store()
        shared = _vec(1.0, 0.0)
        _add(store, label="u-identity", subject="user", kind="identity",
             embedding=shared)
        _add(store, label="a-identity", subject="aiko", kind="identity",
             embedding=shared)
        _add(store, label="u-value", subject="user", kind="value",
             embedding=shared)
        worker, _o = _worker(
            store, ev, _db, responder={"same": True, "reason": "x"},
        )
        out = worker.run()
        self.assertEqual(out["scanned"], 3)
        self.assertEqual(out["pairs_considered"], 0)


class CandidateBandTests(unittest.TestCase):
    """H16: each block nominates its own worst offenders below the bar.

    A flat ``merge_cosine`` assumes every kind's similarity distribution
    sits in the same place. In the live graph 0.84 is above the 99th
    percentile for 17 of 19 blocks and admits *zero* of
    ``tension/relationship``'s 406 pairs, so the twins there were never
    reaching the adjudicator that exists to judge them.
    """

    def _band_settings(self, **over):
        base = dict(
            concept_consolidation_merge_cosine=0.90,
            concept_consolidation_candidate_floor=0.78,
            concept_consolidation_block_top_n=1,
        )
        base.update(over)
        return _settings(**base)

    def test_a_block_under_the_bar_still_contributes(self) -> None:
        _db, store, ev = _new_store()
        # cos ~0.85: under the 0.90 bar, over the 0.78 floor. The old flat
        # rule saw nothing here.
        _add(store, label="A", embedding=_vec(1.0, 0.0))
        _add(store, label="B", embedding=_vec(0.85, 0.527))
        worker, _o = _worker(
            store, ev, _db, responder={"same": False, "reason": "n"},
            settings=self._band_settings(),
        )
        out = worker.run()
        self.assertEqual(out["pairs_considered"], 1)
        self.assertEqual(out["banded"], 1)
        self.assertEqual(out["adjudicated"], 1)

    def test_the_floor_still_holds(self) -> None:
        """The band widens the bar; it does not remove it. Below the floor
        the pairs stop being restatements and start being different
        subjects sharing a sentence template."""
        _db, store, ev = _new_store()
        _add(store, label="A", embedding=_vec(1.0, 0.0))
        _add(store, label="B", embedding=_vec(0.7, 0.714))  # cos ~0.7
        worker, ollama = _worker(
            store, ev, _db, responder={"same": True, "reason": "x"},
            settings=self._band_settings(),
        )
        out = worker.run()
        self.assertEqual(out["pairs_considered"], 0)
        self.assertEqual(ollama.calls, 0)

    def test_top_n_bounds_what_one_block_can_spend(self) -> None:
        """The band is what keeps a wide floor affordable: without a cap,
        one templated kind would eat the whole day's budget."""
        _db, store, ev = _new_store()
        # Equiangular trio: every pair sits at cos 0.85, inside the band.
        for i in range(3):
            xs = [0.9220, 0.0, 0.0, 0.0]
            xs[i + 1] = 0.3873
            _add(store, label=f"c{i}", embedding=_vec(*xs))
        worker, _o = _worker(
            store, ev, _db, responder={"same": False, "reason": "n"},
            settings=self._band_settings(concept_consolidation_block_top_n=2),
            per_hour_cap=50, per_day_cap=50,
        )
        out = worker.run()
        # Three pairs are in the band; only two are bought.
        self.assertEqual(out["banded"], 2)
        self.assertEqual(out["pairs_considered"], 2)

    def test_zero_top_n_restores_the_flat_bar(self) -> None:
        _db, store, ev = _new_store()
        _add(store, label="A", embedding=_vec(1.0, 0.0))
        _add(store, label="B", embedding=_vec(0.85, 0.527))
        worker, ollama = _worker(
            store, ev, _db, responder={"same": True, "reason": "x"},
            settings=self._band_settings(
                concept_consolidation_block_top_n=0
            ),
        )
        out = worker.run()
        self.assertEqual(out["pairs_considered"], 0)
        self.assertEqual(ollama.calls, 0)

    def test_each_block_gets_its_own_allowance(self) -> None:
        """The point of ranking *within* a block: a compressed kind is not
        crowded out by a kind whose labels happen to score higher."""
        _db, store, ev = _new_store()
        _add(store, label="i-a", kind="identity",
             embedding=_vec(1.0, 0.0, 0.0, 0.0))
        _add(store, label="i-b", kind="identity",
             embedding=_vec(0.89, 0.456, 0.0, 0.0))
        _add(store, label="v-a", kind="value",
             embedding=_vec(0.0, 0.0, 1.0, 0.0))
        _add(store, label="v-b", kind="value",
             embedding=_vec(0.0, 0.0, 0.80, 0.6))
        worker, _o = _worker(
            store, ev, _db, responder={"same": False, "reason": "n"},
            settings=self._band_settings(),
            per_hour_cap=50, per_day_cap=50,
        )
        out = worker.run()
        self.assertEqual(out["banded"], 2)

    def test_a_shared_base_outranks_a_higher_cosine(self) -> None:
        """Two tensions standing on the same belief are the same friction
        however differently they are worded -- the judgement cosine cannot
        make on labels this templated."""
        _db, store, ev = _new_store()
        base = _add(store, label="shared base", kind="value",
                    embedding=_vec(0.0, 1.0, 0.0, 0.0))
        # The pair that shares a base scores *lower* than the pair that
        # does not, so cosine alone would spend the single slot elsewhere.
        near_a = _add(store, label="t-near-a", kind="tension",
                      evidence_model="meta",
                      embedding=_vec(1.0, 0.0, 0.0, 0.0))
        near_b = _add(store, label="t-near-b", kind="tension",
                      evidence_model="meta",
                      embedding=_vec(0.89, 0.456, 0.0, 0.0))
        far_a = _add(store, label="t-far-a", kind="tension",
                     evidence_model="meta",
                     embedding=_vec(0.0, 0.0, 1.0, 0.0))
        far_b = _add(store, label="t-far-b", kind="tension",
                     evidence_model="meta",
                     embedding=_vec(0.0, 0.0, 0.80, 0.6))
        for meta in (far_a, far_b):
            store.add_edge(
                ConceptEdge(
                    src_type="concept", src_id=str(base.concept_id),
                    dst_type="concept", dst_id=str(meta.concept_id),
                    relation="evidence",
                )
            )
        seen: list[str] = []

        def responder(messages):
            seen.append(messages[1]["content"])
            return {"same": False, "reason": "n"}

        worker, _o = _worker(
            store, ev, _db, responder=responder,
            settings=self._band_settings(),
            per_hour_cap=50, per_day_cap=50,
        )
        worker.run()
        self.assertEqual(len(seen), 1)
        self.assertIn("t-far-", seen[0])
        self.assertNotIn("t-near-", seen[0])
        del near_a, near_b


class BandReachesTheBatchTests(unittest.TestCase):
    """H12: the band was nominated and then thrown away, for its whole life.

    Every test in :class:`BandTests` above builds a graph in which the
    banded pairs are the *only* pairs, so all of them pass against a
    ``run`` that discards the band entirely -- they assert on what
    ``_collect_pairs`` returns, and the defect was in its consumer. On the
    live graph the 65 banded nominations held global ranks 440-504 against
    a ``batch_size`` of 40 and not one had ever been adjudicated.

    So every fixture here gives the band *competition*: enough over-bar
    pairs to fill the batch on their own, which is the condition the old
    code could not survive and the one the real graph always meets.
    """

    def _graph(self, store, *, over_pairs: int) -> None:
        """``over_pairs`` twins above the bar, plus one banded pair.

        The over-bar pairs are their own ``(subject, kind)`` block each, so
        they compete with the band without also crowding out each other.
        """
        for i in range(over_pairs):
            base = np.zeros(3 + over_pairs, dtype=np.float32)
            base[0] = 1.0
            twin = np.zeros(3 + over_pairs, dtype=np.float32)
            twin[0], twin[1] = 0.99, 0.141
            _add(store, label=f"over-{i}-a", kind=f"k{i}", embedding=base)
            _add(store, label=f"over-{i}-b", kind=f"k{i}", embedding=twin)
        # cos ~0.85: inside the 0.78-0.90 band, below every pair above.
        band_a = np.zeros(3 + over_pairs, dtype=np.float32)
        band_a[2] = 1.0
        band_b = np.zeros(3 + over_pairs, dtype=np.float32)
        band_b[2], band_b[1] = 0.85, 0.527
        _add(store, label="band-a", kind="banded", embedding=band_a)
        _add(store, label="band-b", kind="banded", embedding=band_b)

    def _settings_for(self, **over):
        base = dict(
            concept_consolidation_merge_cosine=0.90,
            concept_consolidation_candidate_floor=0.78,
            concept_consolidation_block_top_n=1,
            concept_consolidation_batch_size=4,
            concept_consolidation_band_reserve=1,
        )
        base.update(over)
        return _settings(**base)

    def _run(self, **over):
        _db, store, ev = _new_store()
        self._graph(store, over_pairs=over.pop("over_pairs", 6))
        seen: list[str] = []

        def responder(messages):
            seen.append(messages[1]["content"])
            return {"same": False, "reason": "n"}

        worker, _o = _worker(
            store, ev, _db, responder=responder,
            settings=self._settings_for(**over),
            per_hour_cap=99, per_day_cap=99,
        )
        return worker.run(), seen

    def test_a_banded_pair_survives_a_full_batch_of_stronger_pairs(
        self,
    ) -> None:
        # The regression. Six over-bar pairs against a batch of four: the
        # old global cosine sort filled every slot before reaching a pair
        # that is, by construction, beneath all of them.
        out, seen = self._run()
        self.assertEqual(out["banded"], 1)
        self.assertEqual(out["banded_in_batch"], 1)
        self.assertTrue(
            any("band-" in text for text in seen),
            "the banded pair never reached the adjudicator",
        )

    def test_the_reserve_does_not_shrink_the_batch(self) -> None:
        # A reserve wider than the band must hand the slack back rather
        # than leave the batch short of over-bar work.
        out, _seen = self._run(concept_consolidation_band_reserve=3)
        self.assertEqual(out["banded_in_batch"], 1)
        self.assertEqual(len(out and _seen), 4)

    def test_zero_reserve_restores_the_old_behaviour(self) -> None:
        # Kept switchable, and worth pinning: this is the exact shape of
        # the bug, so it should only ever be reachable on purpose.
        out, seen = self._run(concept_consolidation_band_reserve=0)
        self.assertEqual(out["banded"], 1)
        self.assertEqual(out["banded_in_batch"], 0)
        self.assertFalse(any("band-" in text for text in seen))

    def test_the_reserve_is_spread_through_the_batch(self) -> None:
        """Appending it would be cut a second time by the rate limiter.

        ``batch_size`` is not the real budget -- the per-day cap is -- so
        a reserve parked at the end of the batch is only reached on runs
        that were never going to exhaust the budget anyway.
        """
        _db, store, ev = _new_store()
        self._graph(store, over_pairs=6)
        seen: list[str] = []

        def responder(messages):
            seen.append(messages[1]["content"])
            return {"same": False, "reason": "n"}

        worker, _o = _worker(
            store, ev, _db, responder=responder,
            settings=self._settings_for(
                concept_consolidation_batch_size=6,
                concept_consolidation_band_reserve=2,
            ),
            # Runs out mid-batch. Interleaved, the band's one pair sits at
            # slot 3 of 6 and is reached; appended it would sit at slot 5
            # and be cut, which is how the batch used to lose it.
            per_hour_cap=4, per_day_cap=4,
        )
        out = worker.run()
        self.assertTrue(out["rate_limited"])
        self.assertTrue(
            any("band-" in text for text in seen),
            "the band was appended, not interleaved: the rate limiter cut "
            "it exactly as the batch used to",
        )

    def test_every_block_is_offered_before_any_block_repeats(self) -> None:
        """The reserve is round-robin, not another cosine ranking.

        Ordering the reserve by cosine would reintroduce the cross-block
        comparison the band exists precisely because it is invalid, just
        inside a shorter list.
        """
        _db, store, ev = _new_store()
        # Two banded blocks. ``high`` beats ``low`` on cosine in both of
        # its pairs, so a cosine-ordered reserve would take both of its
        # before touching ``low`` at all.
        for kind, (x, y) in (("high", (0.88, 0.475)), ("low", (0.80, 0.6))):
            for idx in range(2):
                anchor = np.zeros(6, dtype=np.float32)
                anchor[0 if kind == "high" else 3] = 1.0
                twin = np.zeros(6, dtype=np.float32)
                twin[0 if kind == "high" else 3] = x
                twin[(1 if kind == "high" else 4) + idx % 2] = y
                _add(store, label=f"{kind}-{idx}-a", kind=kind,
                     embedding=anchor)
                _add(store, label=f"{kind}-{idx}-b", kind=kind,
                     embedding=twin)
        seen: list[str] = []

        def responder(messages):
            seen.append(messages[1]["content"])
            return {"same": False, "reason": "n"}

        worker, _o = _worker(
            store, ev, _db, responder=responder,
            settings=self._settings_for(
                concept_consolidation_block_top_n=2,
                concept_consolidation_batch_size=2,
                concept_consolidation_band_reserve=2,
            ),
            per_hour_cap=99, per_day_cap=99,
        )
        worker.run()
        self.assertTrue(
            any("low-" in text for text in seen),
            "the quieter block never got a turn",
        )

    def test_a_reserve_narrower_than_the_graph_rotates(self) -> None:
        """The turn goes to the least-served block, not the first by name.

        On the live numbers the reserve is *always* narrower than the
        graph -- 12 slots against 22 blocks -- so depth 0 alone overruns
        it and whatever order blocks are visited in is the whole
        allocation. Visiting by name gave the alphabetical head a
        permanent turn and the tail none, which starved
        ``relationship/value``: the block H12 created, and the reason any
        of this was measured.
        """
        _db, store, ev = _new_store()
        # Four single-pair blocks, named so that plain alphabetical order
        # would serve "aaa" and "bbb" forever and never reach the rest.
        for name in ("aaa", "bbb", "ccc", "ddd"):
            anchor = np.zeros(3, dtype=np.float32)
            anchor[0] = 1.0
            twin = np.zeros(3, dtype=np.float32)
            twin[0], twin[1] = 0.85, 0.527
            _add(store, label=f"{name}-a", kind=name, embedding=anchor)
            _add(store, label=f"{name}-b", kind=name, embedding=twin)
        seen: list[str] = []

        def responder(messages):
            seen.append(messages[1]["content"])
            return {"same": False, "reason": "n"}

        worker, _o = _worker(
            store, ev, _db, responder=responder,
            settings=self._settings_for(
                concept_consolidation_batch_size=2,
                concept_consolidation_band_reserve=2,
            ),
            per_hour_cap=99, per_day_cap=99,
        )
        worker.run()
        worker.run()
        served = {
            name
            for name in ("aaa", "bbb", "ccc", "ddd")
            if any(f"{name}-" in text for text in seen)
        }
        self.assertEqual(
            served, {"aaa", "bbb", "ccc", "ddd"},
            "two runs of a two-wide reserve must cover four blocks; "
            f"only {sorted(served)} got a turn",
        )


class AnsweredPairsFreeTheirSlotTests(unittest.TestCase):
    """A rejection has to remove the pair from *selection*, not just skip it.

    The batch is a fixed cosine-sorted prefix, so a pair the adjudicator
    already answered kept its place in that prefix forever. Once the top
    ``batch_size`` were answered, every later run re-selected the same
    forty, skipped all forty for free, and never reached the forty-first
    -- which on the live graph is 400 candidates the worker could not
    reach by any amount of waiting.
    """

    def _chain(self, store, n: int) -> None:
        # ``n`` pairs, each its own block, in descending cosine order.
        for i in range(n):
            a = np.zeros(n + 2, dtype=np.float32)
            a[0] = 1.0
            b = np.zeros(n + 2, dtype=np.float32)
            b[0], b[i + 1] = 0.99 - 0.01 * i, 0.1 + 0.01 * i
            _add(store, label=f"p{i}-a", kind=f"k{i}", embedding=a)
            _add(store, label=f"p{i}-b", kind=f"k{i}", embedding=b)

    def test_the_run_after_a_rejection_reaches_new_pairs(self) -> None:
        _db, store, ev = _new_store()
        self._chain(store, 4)
        seen: list[str] = []

        def responder(messages):
            seen.append(messages[1]["content"])
            return {"same": False, "reason": "n"}

        worker, _o = _worker(
            store, ev, _db, responder=responder,
            settings=_settings(
                concept_consolidation_merge_cosine=0.80,
                concept_consolidation_block_top_n=0,
                concept_consolidation_batch_size=2,
            ),
            per_hour_cap=99, per_day_cap=99,
        )
        first = worker.run()
        self.assertEqual(first["adjudicated"], 2)
        self.assertEqual(first["already_answered"], 0)
        first_seen = list(seen)

        second = worker.run()
        self.assertEqual(
            second["already_answered"], 2,
            "the answered pairs were still being nominated",
        )
        self.assertEqual(
            second["adjudicated"], 2,
            "the second run spent its budget re-skipping the first run's "
            "work instead of advancing into the backlog",
        )
        self.assertTrue(
            set(seen[len(first_seen):]).isdisjoint(first_seen),
            "the second run re-examined pairs it had already answered",
        )

    def test_an_answered_pair_does_not_eat_a_blocks_band_slot(self) -> None:
        """A block's ``top_n`` is ``top_n`` *questions*, not ``top_n`` rows.

        Filtering after the cut would let one stale verdict permanently
        retire a block's only slot.
        """
        _db, store, ev = _new_store()
        # One block, two banded pairs; the block may nominate one per run.
        _add(store, label="b-anchor", kind="tension",
             embedding=_vec(1.0, 0.0, 0.0))
        _add(store, label="b-near", kind="tension",
             embedding=_vec(0.86, 0.51, 0.0))
        _add(store, label="b-far", kind="tension",
             embedding=_vec(0.82, 0.0, 0.572))
        seen: list[str] = []

        def responder(messages):
            seen.append(messages[1]["content"])
            return {"same": False, "reason": "n"}

        worker, _o = _worker(
            store, ev, _db, responder=responder,
            settings=_settings(
                concept_consolidation_merge_cosine=0.90,
                concept_consolidation_candidate_floor=0.78,
                concept_consolidation_block_top_n=1,
                concept_consolidation_batch_size=4,
                concept_consolidation_band_reserve=4,
            ),
            per_hour_cap=99, per_day_cap=99,
        )
        worker.run()
        self.assertEqual(len(seen), 1)
        worker.run()
        self.assertEqual(
            len(seen), 2,
            "the block's one slot stayed spent on a question already "
            "answered",
        )

    def test_the_auto_merge_bar_is_exempt(self) -> None:
        """A pair above the auto bar needs no adjudication, so a stale
        verdict must not veto its merge."""
        _db, store, ev = _new_store()
        a = _add(store, label="A", confidence=0.9,
                 embedding=_vec(1.0, 0.0, 0.0))
        b = _add(store, label="B", confidence=0.7,
                 embedding=_vec(0.995, 0.0999, 0.0))
        worker, ollama = _worker(
            store, ev, _db, responder={"same": False, "reason": "n"},
            settings=_settings(
                concept_consolidation_merge_cosine=0.90,
                concept_consolidation_auto_merge_cosine=0.95,
            ),
            per_hour_cap=99, per_day_cap=99,
        )
        worker._remember_rejection(a, b, _NOW)
        out = worker.run()
        self.assertEqual(out["auto_merged"], 1)
        self.assertEqual(out["already_answered"], 0)
        self.assertEqual(ollama.calls, 0)


class MetaMergeTests(unittest.TestCase):
    """A meta's base set has an arity that means something.

    ``tension`` declares ``meta_min_active_bases=None`` -- lose any one
    base and the row is moot. Importing an absorbed twin's bases would
    leave the survivor standing on three or four, so consolidation would
    quietly retire the frictions it had just decided were worth keeping.
    """

    def _tension_on(self, store, label: str, bases: list[Concept]) -> Concept:
        meta = _add(
            store, label=label, kind="tension", evidence_model="meta"
        )
        for base in bases:
            store.add_edge(
                ConceptEdge(
                    src_type="concept", src_id=str(base.concept_id),
                    dst_type="concept", dst_id=str(meta.concept_id),
                    relation="evidence",
                )
            )
        return meta

    def test_merging_two_tensions_does_not_widen_the_base_set(self) -> None:
        _db, store, _ev = _new_store()
        shared = _add(store, label="shared", kind="value")
        only_canonical = _add(store, label="his side", kind="value")
        only_absorbed = _add(store, label="her side", kind="value")
        canonical = self._tension_on(
            store, "friction A", [shared, only_canonical]
        )
        absorbed = self._tension_on(
            store, "friction A restated", [shared, only_absorbed]
        )
        self.assertTrue(
            store.merge_into(
                canonical_id=canonical.concept_id,
                absorbed_id=absorbed.concept_id,
            )
        )
        bases = {
            int(e.src_id)
            for e in store.evidence_of(canonical.concept_id)
            if e.src_type == "concept"
        }
        self.assertEqual(
            bases, {shared.concept_id, only_canonical.concept_id}
        )
        self.assertIsNone(store.get(absorbed.concept_id))
        # The orphaned base is a belief in its own right; it survives.
        self.assertIsNotNone(store.get(only_absorbed.concept_id))

    def test_a_base_kind_still_absorbs_everything(self) -> None:
        """The guard is about meta arity, so ordinary rows are unaffected."""
        _db, store, _ev = _new_store()
        canonical = _add(store, label="A", confidence=0.9)
        absorbed = _add(store, label="B", confidence=0.7)
        _evidence(store, "memory", "11", absorbed.concept_id)
        _evidence(store, "cluster", "5", absorbed.concept_id)
        store.merge_into(
            canonical_id=canonical.concept_id,
            absorbed_id=absorbed.concept_id,
        )
        srcs = {
            (e.src_type, e.src_id)
            for e in store.evidence_of(canonical.concept_id)
        }
        self.assertEqual(srcs, {("memory", "11"), ("cluster", "5")})

    def test_a_meta_still_takes_non_concept_evidence(self) -> None:
        _db, store, _ev = _new_store()
        base = _add(store, label="base", kind="value")
        canonical = self._tension_on(store, "friction", [base])
        absorbed = _add(
            store, label="twin", kind="tension", evidence_model="meta"
        )
        _evidence(store, "memory", "42", absorbed.concept_id)
        store.merge_into(
            canonical_id=canonical.concept_id,
            absorbed_id=absorbed.concept_id,
        )
        srcs = {
            (e.src_type, e.src_id)
            for e in store.evidence_of(canonical.concept_id)
        }
        self.assertIn(("memory", "42"), srcs)
        self.assertIn(("concept", str(base.concept_id)), srcs)


class DemandTests(unittest.TestCase):
    def test_auto_mergeable_pairs_do_not_claim_the_llm(self) -> None:
        """``needs_llm`` gates this worker behind the scheduler's LLM lane.
        A run that will only auto-merge calls nothing, so claiming otherwise
        parks free work behind a budget it never spends."""
        _db, store, ev = _new_store()
        _add(store, label="A", confidence=0.9, embedding=_vec(1.0, 0.0, 0.0))
        _add(store, label="B", confidence=0.7, embedding=_vec(0.98, 0.2, 0.0))
        worker, _o = _worker(
            store, ev, _db, responder={"same": True, "reason": "x"},
            settings=_settings(
                concept_consolidation_merge_cosine=0.80,
                concept_consolidation_auto_merge_cosine=0.86,
            ),
        )
        signal = worker.demand(now=_NOW, last_run_at=None)
        self.assertGreater(signal.pressure, 0.0)
        self.assertFalse(signal.needs_llm)

    def test_an_ambiguous_pair_does_claim_the_llm(self) -> None:
        _db, store, ev = _new_store()
        _add(store, label="A", confidence=0.9, embedding=_vec(1.0, 0.0))
        _add(store, label="B", confidence=0.7, embedding=_vec(0.848, 0.53))
        worker, _o = _worker(
            store, ev, _db, responder={"same": True, "reason": "x"},
            settings=_settings(
                concept_consolidation_merge_cosine=0.80,
                concept_consolidation_auto_merge_cosine=0.86,
            ),
        )
        signal = worker.demand(now=_NOW, last_run_at=None)
        self.assertTrue(signal.needs_llm)

    def test_a_fully_adjudicated_graph_reports_no_pressure(self) -> None:
        _db, store, ev = _new_store()
        _add(store, label="A energizes him", confidence=0.9,
             embedding=_vec(1.0, 0.0))
        _add(store, label="B energizes him", confidence=0.7,
             embedding=_vec(0.9, 0.436))
        worker, _o = _worker(
            store, ev, _db, responder={"same": False, "reason": "n"},
        )
        worker.run()
        signal = worker.demand(now=_NOW, last_run_at=None)
        self.assertEqual(signal.pressure, 0.0)
        self.assertFalse(signal.needs_llm)


if __name__ == "__main__":
    unittest.main()
