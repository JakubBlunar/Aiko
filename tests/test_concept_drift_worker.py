"""L17: the drift worker -- relabel pipeline plus learning-event capture."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from app.core.concepts.concept_drift_worker import (
    DRIFT_PENDING_KEY,
    DRIFT_SWEEP_KEY,
    DRIFT_WATERMARK_KEY,
    SWEEP_DONE,
    ConceptDriftWorker,
)
from app.core.concepts.concept_event_store import ConceptEvent, ConceptEventStore
from app.core.concepts.concept_learning_event_store import (
    ConceptLearningEventStore,
)
from app.core.concepts.concept_store import Concept, ConceptEdge, ConceptStore
from app.core.infra.chat_database import ChatDatabase


NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


@dataclass
class Settings:
    concept_drift_enabled: bool = True
    concept_drift_interval_seconds: int = 3600
    concept_drift_max_concepts: int = 120
    concept_drift_trace_anchor: int = 20
    concept_drift_trace_recent: int = 60
    concept_drift_min_salience: float = 0.35
    concept_drift_min_age_days: float = 3.0
    concept_drift_min_confidence_delta: float = 0.15
    concept_drift_max_findings: int = 12
    concept_drift_succession_min_cosine: float = 0.55
    concept_drift_succession_max_cosine: float = 0.86
    concept_drift_succession_min_overlap: float = 0.25
    concept_drift_succession_window_days: float = 120.0
    concept_relabel_enabled: bool = True
    concept_relabel_min_cosine: float = 0.80
    concept_relabel_cooldown_days: float = 21.0
    concept_relabel_max_per_run: int = 3
    concept_relabel_scan_limit: int = 40
    concept_drift_relabel_min_tokens: int = 1
    concept_reflection_min_salience: float = 0.6
    concept_drift_pending_cap: int = 3
    concept_drift_sweep_enabled: bool = True
    concept_drift_sweep_page: int = 60
    concept_drift_sweep_max_findings: int = 24


@dataclass
class Agent:
    concepts_enabled: bool = True


class FakeEmbedder:
    """Deterministic embeddings: same first token -> near-identical vector."""

    def __init__(self, table: dict[str, list[float]] | None = None) -> None:
        self.table = table or {}
        self.calls: list[str] = []

    def embed(self, text: str):
        self.calls.append(text)
        vec = self.table.get(text)
        if vec is None:
            vec = [1.0, 0.0, 0.0]
        return np.array(vec, dtype=np.float32)


class FakeOllama:
    def __init__(self, verdict: bool = True, fail: bool = False) -> None:
        self.verdict = verdict
        self.fail = fail
        self.calls = 0

    def chat_stream(self, messages, **kwargs):
        self.calls += 1
        if self.fail:
            raise RuntimeError("model down")
        yield '{"better": %s, "reason": "sharper"}' % (
            "true" if self.verdict else "false"
        )


@dataclass
class FakeLimiter:
    allowed: bool = True
    seen: int = 0

    def allow(self, _now) -> bool:
        self.seen += 1
        return self.allowed


@dataclass
class Harness:
    db: ChatDatabase
    store: ConceptStore
    events: ConceptEventStore
    learning: ConceptLearningEventStore
    settings: Settings
    kv: dict = field(default_factory=dict)


def _harness() -> Harness:
    tmp = tempfile.mkdtemp()
    db = ChatDatabase(Path(tmp) / "test.db")
    return Harness(
        db=db,
        store=ConceptStore(db),
        events=ConceptEventStore(db),
        learning=ConceptLearningEventStore(db),
        settings=Settings(),
    )


def _worker(
    h: Harness,
    *,
    embedder=None,
    ollama=None,
    limiter=None,
    evidence=None,
) -> ConceptDriftWorker:
    return ConceptDriftWorker(
        concept_store=h.store,
        concept_event_store=h.events,
        learning_store=h.learning,
        memory_settings=h.settings,
        agent_settings=Agent(),
        embedder=embedder,
        ollama=ollama,
        rate_limiter=limiter,
        kv_get=h.kv.get,
        kv_set=lambda k, v: h.kv.__setitem__(k, v),
        evidence_labels_provider=evidence,
        clock=lambda: NOW,
    )


def _concept(h: Harness, label: str, vec: list[float], **kw) -> int:
    base = dict(
        kind="identity",
        subject="user",
        status="active",
        confidence=0.8,
        plasticity=0.3,
        first_evidence_at=_iso(90),
        # A belief that was genuinely held: evidence landed on it after
        # promotion. The ``loss`` gate reads this pair, so a fixture
        # without it models the one-shot inference whose fade is not
        # learning -- pass ``last_reinforced_at=""`` to get that.
        promoted_at=_iso(80),
        last_reinforced_at=_iso(70),
    )
    base.update(kw)
    return h.store.add(
        Concept(
            label=label,
            embedding=np.array(vec, dtype=np.float32),
            **base,  # type: ignore[arg-type]
        )
    )


def _event(h: Harness, cid: int, kind: str, days: float, **kw) -> int:
    payload = dict(
        concept_id=cid,
        event_type=kind,
        created_at=_iso(days),
        confidence=0.5,
    )
    payload.update(kw)
    return h.events.add(ConceptEvent(**payload))  # type: ignore[arg-type]


class DemandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = _harness()

    def test_disabled_reports_no_pressure(self) -> None:
        self.h.settings.concept_drift_enabled = False
        signal = _worker(self.h).demand(now=NOW, last_run_at=None)
        assert signal is not None
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "disabled")

    def test_empty_timeline_reports_no_pressure(self) -> None:
        signal = _worker(self.h).demand(now=NOW, last_run_at=None)
        assert signal is not None
        self.assertEqual(signal.pressure, 0.0)

    def test_new_events_create_pressure(self) -> None:
        cid = _concept(self.h, "a", [1.0, 0.0])
        _event(self.h, cid, "promoted", 1)
        signal = _worker(self.h).demand(now=NOW, last_run_at=None)
        assert signal is not None
        self.assertGreater(signal.pressure, 0.0)
        self.assertFalse(signal.needs_llm)

    def test_watermark_suppresses_seen_events(self) -> None:
        cid = _concept(self.h, "a", [1.0, 0.0])
        last = _event(self.h, cid, "promoted", 1)
        self.h.kv[DRIFT_WATERMARK_KEY] = str(last)
        self.h.kv[DRIFT_SWEEP_KEY] = str(SWEEP_DONE)
        signal = _worker(self.h).demand(now=NOW, last_run_at=None)
        assert signal is not None
        self.assertEqual(signal.pressure, 0.0)

    def test_unfinished_sweep_keeps_the_worker_admitted(self) -> None:
        # Nothing new on the timeline, but history still to backfill.
        cid = _concept(self.h, "a", [1.0, 0.0])
        last = _event(self.h, cid, "promoted", 1)
        self.h.kv[DRIFT_WATERMARK_KEY] = str(last)
        signal = _worker(self.h).demand(now=NOW, last_run_at=None)
        assert signal is not None
        self.assertGreater(signal.pressure, 0.0)
        self.assertIn("sweep", signal.reason)
        self.assertFalse(signal.needs_llm)

    def test_empty_store_reports_nothing_to_sweep(self) -> None:
        # No timeline at all: the backfill must not manufacture pressure.
        signal = _worker(self.h).demand(now=NOW, last_run_at=None)
        assert signal is not None
        self.assertEqual(signal.pressure, 0.0)

    def test_relabel_proposal_marks_the_llm_lane(self) -> None:
        cid = _concept(self.h, "likes depth", [1.0, 0.0])
        _event(
            self.h, cid, "relabel_proposed", 1,
            label="prefers calibrated depth",
        )
        signal = _worker(self.h).demand(now=NOW, last_run_at=None)
        assert signal is not None
        self.assertTrue(signal.needs_llm)
        self.assertEqual(signal.lane, "llm")

    def test_demand_never_touches_the_cosine_mirror(self) -> None:
        # The probe must stay a single indexed aggregate: the crash that
        # took down consolidation came from restacking a matrix here.
        cid = _concept(self.h, "a", [1.0, 0.0])
        _event(self.h, cid, "promoted", 1)
        calls: list[object] = []
        self.h.store.matrix_snapshot = (  # type: ignore[method-assign]
            lambda *a, **k: calls.append(1) or ([], np.zeros((0, 0)))
        )
        self.h.store.nearest = (  # type: ignore[method-assign]
            lambda *a, **k: calls.append(1) or []
        )
        _worker(self.h).demand(now=NOW, last_run_at=None)
        self.assertEqual(calls, [])

    def test_probe_failure_degrades_quietly(self) -> None:
        def boom() -> int:
            raise RuntimeError("db gone")

        self.h.events.max_event_id = boom  # type: ignore[method-assign]
        signal = _worker(self.h).demand(now=NOW, last_run_at=None)
        assert signal is not None
        self.assertEqual(signal.pressure, 0.0)


class RelabelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = _harness()
        self.embedder = FakeEmbedder(
            {"prefers depth calibrated to the topic": [0.98, 0.2, 0.0]}
        )
        self.cid = _concept(self.h, "likes detailed answers", [1.0, 0.0, 0.0])

    def _propose(self, label: str, days: float = 1.0, reason: str = "") -> int:
        return _event(
            self.h, self.cid, "relabel_proposed", days,
            label=label, reason=reason,
        )

    def _run(self, **kw) -> dict:
        worker = _worker(
            self.h,
            embedder=kw.pop("embedder", self.embedder),
            ollama=kw.pop("ollama", FakeOllama(True)),
            limiter=kw.pop("limiter", FakeLimiter()),
        )
        return worker.run()

    def test_accepted_proposal_rewrites_label_and_rationale(self) -> None:
        self._propose(
            "prefers depth calibrated to the topic",
            reason="he asked for shorter answers about ops",
        )
        stats = self._run()
        self.assertEqual(stats["relabel_applied"], 1)
        concept = self.h.store.get(self.cid)
        assert concept is not None
        self.assertEqual(concept.label, "prefers depth calibrated to the topic")
        self.assertEqual(
            concept.rationale, "he asked for shorter answers about ops"
        )

    def test_embedding_moves_with_the_wording(self) -> None:
        before = self.h.store.get(self.cid).embedding.copy()  # type: ignore
        self._propose("prefers depth calibrated to the topic")
        self._run()
        after = self.h.store.get(self.cid).embedding  # type: ignore
        self.assertFalse(np.allclose(before, after))
        # And the cached active matrix must have been invalidated.
        self.assertTrue(self.h.store._active_dirty)

    def test_relabeled_event_records_the_previous_wording(self) -> None:
        self._propose("prefers depth calibrated to the topic")
        self._run()
        [event] = [
            e for e in self.h.events.list(concept_id=self.cid)
            if e.event_type == "relabeled"
        ]
        self.assertEqual(event.label, "prefers depth calibrated to the topic")
        self.assertIn("likes detailed answers", event.reason)

    def test_cosmetic_proposal_is_refused(self) -> None:
        self._propose("Likes detailed answers.")
        stats = self._run()
        self.assertEqual(stats["relabel_applied"], 0)
        self.assertEqual(
            self.h.store.get(self.cid).label,  # type: ignore
            "likes detailed answers",
        )

    def test_previously_held_wording_is_refused(self) -> None:
        # The concept already wore this phrasing at some point.
        _event(
            self.h, self.cid, "promoted", 40,
            label="prefers depth calibrated to the topic",
        )
        self._propose("prefers depth calibrated to the topic")
        stats = self._run()
        self.assertEqual(stats["relabel_applied"], 0)

    def test_cooldown_blocks_a_second_rewrite(self) -> None:
        _event(self.h, self.cid, "relabeled", 2, label="likes detailed answers")
        self._propose("prefers depth calibrated to the topic")
        stats = self._run()
        self.assertEqual(stats["relabel_applied"], 0)

    def test_expired_cooldown_allows_a_rewrite(self) -> None:
        _event(
            self.h, self.cid, "relabeled", 60, label="an older wording"
        )
        self._propose("prefers depth calibrated to the topic")
        stats = self._run()
        self.assertEqual(stats["relabel_applied"], 1)

    def test_distant_wording_is_refused_as_a_different_belief(self) -> None:
        embedder = FakeEmbedder({"enjoys long walks": [0.0, 1.0, 0.0]})
        self._propose("enjoys long walks")
        stats = self._run(embedder=embedder)
        self.assertEqual(stats["relabel_applied"], 0)
        self.assertEqual(
            self.h.store.get(self.cid).label,  # type: ignore
            "likes detailed answers",
        )

    def test_adjudicator_veto_is_respected(self) -> None:
        self._propose("prefers depth calibrated to the topic")
        stats = self._run(ollama=FakeOllama(verdict=False))
        self.assertEqual(stats["relabel_applied"], 0)

    def test_adjudicator_failure_is_not_an_acceptance(self) -> None:
        self._propose("prefers depth calibrated to the topic")
        stats = self._run(ollama=FakeOllama(fail=True))
        self.assertEqual(stats["relabel_applied"], 0)

    def test_exhausted_budget_defers_rather_than_applying(self) -> None:
        self._propose("prefers depth calibrated to the topic")
        stats = self._run(limiter=FakeLimiter(allowed=False))
        self.assertEqual(stats["relabel_applied"], 0)

    def test_no_model_wired_falls_back_to_the_cheap_gates(self) -> None:
        self._propose("prefers depth calibrated to the topic")
        worker = _worker(self.h, embedder=self.embedder, ollama=None)
        stats = worker.run()
        self.assertEqual(stats["relabel_applied"], 1)

    def test_rejection_is_cached_so_the_budget_is_not_respent(self) -> None:
        self._propose("prefers depth calibrated to the topic")
        ollama = FakeOllama(verdict=False)
        worker = _worker(
            self.h, embedder=self.embedder, ollama=ollama,
            limiter=FakeLimiter(),
        )
        worker.run()
        self.h.kv.pop(DRIFT_WATERMARK_KEY, None)
        worker.run()
        self.assertEqual(ollama.calls, 1)

    def test_per_run_cap_is_enforced(self) -> None:
        self.h.settings.concept_relabel_max_per_run = 1
        for i in range(3):
            cid = _concept(self.h, f"belief number {i}", [1.0, 0.0, 0.0])
            _event(
                self.h, cid, "relabel_proposed", 1,
                label=f"a sharper belief number {i}",
            )
        stats = self._run()
        self.assertLessEqual(stats["relabel_applied"], 1)

    def test_newest_proposal_per_concept_wins(self) -> None:
        self._propose("an older sharper wording", days=5)
        self._propose("prefers depth calibrated to the topic", days=1)
        self._run()
        self.assertEqual(
            self.h.store.get(self.cid).label,  # type: ignore
            "prefers depth calibrated to the topic",
        )

    def test_retired_concepts_are_not_relabelled(self) -> None:
        concept = self.h.store.get(self.cid)
        assert concept is not None
        concept.status = "retired"
        self.h.store.update(concept)
        self._propose("prefers depth calibrated to the topic")
        stats = self._run()
        self.assertEqual(stats["relabel_applied"], 0)

    def test_disabled_relabel_leaves_labels_alone(self) -> None:
        self.h.settings.concept_relabel_enabled = False
        self._propose("prefers depth calibrated to the topic")
        stats = self._run()
        self.assertEqual(stats["relabel_applied"], 0)

    def test_missing_embedder_blocks_the_rewrite(self) -> None:
        self._propose("prefers depth calibrated to the topic")
        worker = _worker(self.h, embedder=None, ollama=FakeOllama(True))
        stats = worker.run()
        self.assertEqual(stats["relabel_applied"], 0)

    def test_relabel_never_touches_l3_owned_fields(self) -> None:
        before = self.h.store.get(self.cid)
        assert before is not None
        snapshot = (before.confidence, before.plasticity, before.status)
        self._propose("prefers depth calibrated to the topic")
        self._run()
        after = self.h.store.get(self.cid)
        assert after is not None
        self.assertEqual(
            (after.confidence, after.plasticity, after.status), snapshot
        )


class ClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = _harness()

    def test_loss_becomes_a_learning_event(self) -> None:
        cid = _concept(
            self.h, "likes detailed answers", [1.0, 0.0], status="retired"
        )
        _event(self.h, cid, "promoted", 90, confidence=0.85)
        _event(self.h, cid, "retired", 2, confidence=0.2)
        stats = _worker(self.h).run()
        self.assertGreaterEqual(stats["recorded"], 1)
        [event] = self.h.learning.list()
        self.assertEqual(event.shape, "loss")
        self.assertEqual(event.concept_id, cid)

    def test_a_never_reinforced_fade_records_nothing(self) -> None:
        # The shape the L22 sweep produces in bulk: promoted once on a
        # single inference, nothing ever confirmed it, then parked. She
        # did not change her mind, so the timeline stays quiet.
        cid = _concept(
            self.h,
            "a guess that never came up again",
            [1.0, 0.0],
            status="dormant",
            last_reinforced_at="",
        )
        _event(self.h, cid, "promoted", 90, confidence=0.85)
        _event(self.h, cid, "dormant", 2, confidence=0.2)
        stats = _worker(self.h).run()
        self.assertEqual(stats["recorded"], 0)
        self.assertEqual(self.h.learning.list(), [])

    def test_succession_pairs_across_rows(self) -> None:
        # Cosine ~0.82: inside the succession band, below the dedupe bar
        # these two rows would never have coexisted above.
        old = _concept(
            self.h, "likes detailed answers", [1.0, 0.7, 0.0],
            status="retired", confidence=0.2,
        )
        new = _concept(
            self.h, "prefers depth calibrated to the topic", [1.0, 0.0, 0.0],
            status="active", confidence=0.85,
        )
        for cid in (old, new):
            for mid in ("1", "2"):
                self.h.store.add_edge(
                    ConceptEdge(
                        src_type="memory", src_id=mid,
                        dst_type="concept", dst_id=str(cid),
                        relation="evidence",
                    )
                )
        _event(self.h, old, "promoted", 90, confidence=0.8)
        _event(self.h, old, "retired", 5, confidence=0.2)
        _event(self.h, new, "discovered", 30, confidence=0.3)
        _event(self.h, new, "promoted", 8, confidence=0.85)

        stats = _worker(self.h).run()
        self.assertGreaterEqual(stats["succession_pairs"], 1)
        shapes = {e.shape for e in self.h.learning.list()}
        self.assertIn("succession", shapes)
        [succession] = [
            e for e in self.h.learning.list() if e.shape == "succession"
        ]
        self.assertEqual(succession.prior_concept_id, old)
        self.assertEqual(succession.concept_id, new)

    def test_second_run_is_idempotent(self) -> None:
        cid = _concept(
            self.h, "a settled belief", [1.0, 0.0], status="retired"
        )
        _event(self.h, cid, "promoted", 90, confidence=0.85)
        _event(self.h, cid, "retired", 2, confidence=0.2)
        worker = _worker(self.h)
        worker.run()
        first = self.h.learning.count()
        # Rewind the watermark: the same history must not be re-recorded.
        self.h.kv.pop(DRIFT_WATERMARK_KEY, None)
        worker.run()
        self.assertEqual(self.h.learning.count(), first)

    def test_watermark_advances_past_the_processed_window(self) -> None:
        cid = _concept(self.h, "a", [1.0, 0.0], status="retired")
        _event(self.h, cid, "promoted", 90, confidence=0.85)
        last = _event(self.h, cid, "retired", 2, confidence=0.2)
        _worker(self.h).run()
        self.assertGreaterEqual(int(self.h.kv[DRIFT_WATERMARK_KEY]), last)

    def test_no_new_events_skips(self) -> None:
        cid = _concept(self.h, "a", [1.0, 0.0])
        last = _event(self.h, cid, "promoted", 1)
        self.h.kv[DRIFT_WATERMARK_KEY] = str(last)
        self.h.kv[DRIFT_SWEEP_KEY] = str(SWEEP_DONE)
        self.assertEqual(
            _worker(self.h).run(), {"skipped": True, "reason": "no new events"}
        )

    def test_disabled_worker_skips(self) -> None:
        self.h.settings.concept_drift_enabled = False
        self.assertEqual(
            _worker(self.h).run(), {"skipped": True, "reason": "disabled"}
        )

    def test_evidence_labels_are_snapshotted(self) -> None:
        cid = _concept(self.h, "a belief", [1.0, 0.0], status="retired")
        _event(self.h, cid, "promoted", 90, confidence=0.85)
        _event(self.h, cid, "retired", 2, confidence=0.2)
        _worker(
            self.h, evidence=lambda _cid: ["the evening he explained it"]
        ).run()
        [event] = self.h.learning.list()
        self.assertEqual(
            event.evidence_labels, ("the evening he explained it",)
        )

    def test_a_failing_evidence_provider_is_survivable(self) -> None:
        cid = _concept(self.h, "a belief", [1.0, 0.0], status="retired")
        _event(self.h, cid, "promoted", 90, confidence=0.85)
        _event(self.h, cid, "retired", 2, confidence=0.2)

        def boom(_cid: int) -> list[str]:
            raise RuntimeError("resolution failed")

        stats = _worker(self.h, evidence=boom).run()
        self.assertGreaterEqual(stats["recorded"], 1)

    def test_pending_snapshot_holds_only_salient_changes(self) -> None:
        cid = _concept(
            self.h, "a hard-won value", [1.0, 0.0], status="retired",
            kind="value", plasticity=0.2,
        )
        _event(self.h, cid, "promoted", 90, confidence=0.9)
        _event(self.h, cid, "retired", 2, confidence=0.1)
        self.h.settings.concept_reflection_min_salience = 0.0
        _worker(self.h).run()
        payload = self.h.kv.get(DRIFT_PENDING_KEY)
        self.assertIsNotNone(payload)
        self.assertIn("because", payload)
        # No machinery leaks into what a prompt block could read.
        self.assertNotIn("decisive_event_id", payload)
        self.assertNotIn("concept_id", payload)

    def test_high_salience_floor_empties_the_snapshot(self) -> None:
        cid = _concept(self.h, "a belief", [1.0, 0.0], status="retired")
        _event(self.h, cid, "promoted", 90, confidence=0.85)
        _event(self.h, cid, "retired", 2, confidence=0.2)
        self.h.settings.concept_reflection_min_salience = 0.99
        _worker(self.h).run()
        self.assertEqual(self.h.kv.get(DRIFT_PENDING_KEY), "[]")

    def _count_snapshots(self) -> list[int]:
        real = self.h.store.matrix_snapshot
        calls: list[int] = []

        def counting(ids=None):
            calls.append(1)
            return real(ids)

        self.h.store.matrix_snapshot = counting  # type: ignore[method-assign]
        return calls

    def test_run_stacks_the_matrix_at_most_twice(self) -> None:
        # Once for the faded side, once for the risen side -- never per
        # concept, which is the crash pattern the plan calls out.
        self.h.settings.concept_drift_sweep_enabled = False
        old = _concept(self.h, "old belief", [1.0, 0.2], status="retired")
        _concept(self.h, "new belief", [1.0, 0.0], status="active")
        _event(self.h, old, "promoted", 90, confidence=0.8)
        _event(self.h, old, "retired", 5, confidence=0.2)
        calls = self._count_snapshots()
        _worker(self.h).run()
        self.assertLessEqual(len(calls), 2)

    def test_the_sweep_pass_holds_the_same_bound(self) -> None:
        # The backfill reads far more concepts than a live tick, so it is
        # the pass most able to reintroduce a per-concept stack.
        old = _concept(self.h, "old belief", [1.0, 0.2], status="retired")
        _concept(self.h, "new belief", [1.0, 0.0], status="active")
        _event(self.h, old, "promoted", 90, confidence=0.8)
        last = _event(self.h, old, "retired", 5, confidence=0.2)
        self.h.kv[DRIFT_WATERMARK_KEY] = str(last)
        calls = self._count_snapshots()
        _worker(self.h).run()
        self.assertLessEqual(len(calls), 2)


class ColdStartSweepTests(unittest.TestCase):
    """The backfill: history that predates the worker must still land."""

    def setUp(self) -> None:
        self.h = _harness()

    def _retired_arc(self, label: str) -> int:
        cid = _concept(self.h, label, [1.0, 0.0], status="retired")
        _event(self.h, cid, "promoted", 90, confidence=0.85)
        _event(self.h, cid, "retired", 2, confidence=0.2)
        return cid

    def test_history_beyond_the_forward_page_still_gets_classified(
        self,
    ) -> None:
        # The forward pass only ever sees ``max_concepts`` ids and then
        # burns the watermark; without the sweep the later arcs are lost.
        self.h.settings.concept_drift_max_concepts = 1
        ids = [self._retired_arc(f"belief number {i}") for i in range(3)]
        worker = _worker(self.h)
        for _ in range(6):
            worker.run()
        recorded = {e.concept_id for e in self.h.learning.list()}
        self.assertEqual(recorded, set(ids))

    def test_the_cursor_pages_through_the_id_space(self) -> None:
        self.h.settings.concept_drift_sweep_page = 1
        ids = [self._retired_arc(f"belief number {i}") for i in range(3)]
        worker = _worker(self.h)
        worker.run()
        self.assertEqual(int(self.h.kv[DRIFT_SWEEP_KEY]), ids[0])
        worker.run()
        self.assertEqual(int(self.h.kv[DRIFT_SWEEP_KEY]), ids[1])

    def test_the_sweep_retires_itself_when_it_runs_out(self) -> None:
        self._retired_arc("a settled belief")
        worker = _worker(self.h)
        worker.run()
        stats = worker.run()
        self.assertTrue(stats.get("sweep_done"))
        self.assertEqual(int(self.h.kv[DRIFT_SWEEP_KEY]), SWEEP_DONE)
        # And it stays retired rather than restarting on the next tick.
        self.assertEqual(
            worker.run(), {"skipped": True, "reason": "no new events"}
        )

    def test_the_sweep_is_idempotent_against_the_forward_pass(self) -> None:
        self._retired_arc("a settled belief")
        worker = _worker(self.h)
        worker.run()
        before = self.h.learning.count()
        self.assertGreaterEqual(before, 1)
        # Rewind both cursors: the same arcs must not be recorded twice.
        self.h.kv.pop(DRIFT_WATERMARK_KEY, None)
        self.h.kv.pop(DRIFT_SWEEP_KEY, None)
        worker.run()
        self.assertEqual(self.h.learning.count(), before)

    def test_the_sweep_never_queues_a_reflection(self) -> None:
        # Backfilled findings are months old; Aiko must not boot up and
        # start voicing them as fresh realisations.
        self.h.settings.concept_reflection_min_salience = 0.0
        last = _event(
            self.h, self._retired_arc("a settled belief"), "reinforced", 1
        )
        self.h.kv[DRIFT_WATERMARK_KEY] = str(last)
        stats = _worker(self.h).run()
        self.assertGreaterEqual(stats.get("sweep_recorded", 0), 1)
        self.assertIsNone(self.h.kv.get(DRIFT_PENDING_KEY))

    def test_disabling_the_sweep_restores_forward_only_behaviour(self) -> None:
        self.h.settings.concept_drift_sweep_enabled = False
        self.h.settings.concept_drift_max_concepts = 1
        self._retired_arc("belief one")
        self._retired_arc("belief two")
        worker = _worker(self.h)
        for _ in range(4):
            worker.run()
        self.assertNotIn(DRIFT_SWEEP_KEY, self.h.kv)
        self.assertLessEqual(self.h.learning.count(), 1)

    def test_a_page_with_nothing_salient_still_advances(self) -> None:
        # A young, unmoved belief yields no finding; the cursor must not
        # stall on it or the sweep never reaches the interesting arcs.
        cid = _concept(self.h, "brand new", [1.0, 0.0], first_evidence_at=_iso(1))
        _event(self.h, cid, "discovered", 1, confidence=0.3)
        stats = _worker(self.h).run()
        self.assertEqual(self.h.learning.count(), 0)
        self.assertEqual(stats.get("sweep_cursor"), cid)


class SweepPagingTests(unittest.TestCase):
    """The store-level read the sweep pages with."""

    def setUp(self) -> None:
        self.h = _harness()

    def test_after_concept_id_walks_forwards(self) -> None:
        ids = []
        for i in range(3):
            cid = _concept(self.h, f"belief {i}", [1.0, 0.0])
            _event(self.h, cid, "discovered", 1)
            ids.append(cid)
        first = self.h.events.concepts_with_events_after(
            0, limit=1, after_concept_id=0
        )
        self.assertEqual(first, [ids[0]])
        second = self.h.events.concepts_with_events_after(
            0, limit=1, after_concept_id=ids[0]
        )
        self.assertEqual(second, [ids[1]])

    def test_the_end_of_the_id_space_is_empty(self) -> None:
        cid = _concept(self.h, "only belief", [1.0, 0.0])
        _event(self.h, cid, "discovered", 1)
        self.assertEqual(
            self.h.events.concepts_with_events_after(
                0, limit=10, after_concept_id=cid
            ),
            [],
        )

    def test_omitting_the_cursor_preserves_the_forward_read(self) -> None:
        cid = _concept(self.h, "a belief", [1.0, 0.0])
        _event(self.h, cid, "discovered", 1)
        self.assertEqual(
            self.h.events.concepts_with_events_after(0, limit=10), [cid]
        )


class MatrixSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = _harness()

    def test_returns_ids_and_rows_in_step(self) -> None:
        a = _concept(self.h, "a", [1.0, 0.0])
        b = _concept(self.h, "b", [0.0, 1.0])
        ids, mat = self.h.store.matrix_snapshot([a, b])
        self.assertEqual(ids, [a, b])
        self.assertEqual(mat.shape, (2, 2))

    def test_minority_dimensions_are_dropped(self) -> None:
        a = _concept(self.h, "a", [1.0, 0.0])
        b = _concept(self.h, "b", [0.0, 1.0])
        odd = _concept(self.h, "odd", [1.0, 0.0, 0.0])
        ids, mat = self.h.store.matrix_snapshot([a, b, odd])
        self.assertEqual(sorted(ids), sorted([a, b]))
        self.assertEqual(mat.shape[0], len(ids))

    def test_unknown_and_empty_ids_are_safe(self) -> None:
        ids, mat = self.h.store.matrix_snapshot([999])
        self.assertEqual(ids, [])
        self.assertEqual(mat.size, 0)
        ids, mat = self.h.store.matrix_snapshot([])
        self.assertEqual(ids, [])

    def test_none_means_the_whole_store(self) -> None:
        _concept(self.h, "a", [1.0, 0.0])
        _concept(self.h, "b", [0.0, 1.0])
        ids, _mat = self.h.store.matrix_snapshot()
        self.assertEqual(len(ids), 2)


if __name__ == "__main__":
    unittest.main()
