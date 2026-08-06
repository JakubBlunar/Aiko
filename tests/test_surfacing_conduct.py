from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np

from app.core.concepts.concept_store import Concept
from app.core.concepts.concept_synthesis_worker import ConceptSynthesisWorker
from app.core.concepts.proposers.base import ExistingConcept, ProposerContext
from app.core.concepts.proposers.conduct_aiko import propose_conduct_aiko
from app.core.concepts.surfacing_conduct import (
    ConductFinding,
    detect_concentration,
    detect_fixation,
    detect_neglect,
    load_conduct_snapshot,
    map_user_topic_counts,
    save_conduct_snapshot,
)
from app.core.memory.surfacing_outcome_store import ClusterTaste, ItemStats


def test_concentration_normalizes_against_user_topic_mix_and_stays_cold() -> None:
    stats = {
        1: ClusterTaste(1, surfaced=42, settled=40, engaged=25),
        2: ClusterTaste(2, surfaced=12, settled=10, engaged=7),
        3: ClusterTaste(3, surfaced=4, settled=3, engaged=2),
    }
    reps = {1: (101, "servers"), 2: (102, "music"), 3: (103, "books")}
    assert detect_concentration(
        stats, {1: 3, 2: 12, 3: 5}, reps, min_total_settled=100,
    ) is None
    finding = detect_concentration(stats, {1: 3, 2: 12, 3: 5}, reps)
    assert finding is not None
    assert finding.shape == "concentration"
    assert finding.key == "cluster:1"
    assert len(finding.evidence) >= 2
    assert finding.observed_share is not None
    assert finding.expected_share is not None
    assert finding.observed_share > finding.expected_share


def test_user_topic_mapping_skips_unmapped_vectors() -> None:
    class _Graph:
        def best_clusters_for(self, vector, **_kwargs):
            return [(1, "one", 0.9)] if float(vector[0]) > 0 else []

    counts, mapped = map_user_topic_counts(
        [
            ("2026-01-01", np.array([1.0], dtype=np.float32)),
            ("2026-01-02", np.array([-1.0], dtype=np.float32)),
        ],
        _Graph(),
    )
    assert counts == {1: 1}
    assert mapped == 1


def test_neglect_requires_several_old_non_profile_concepts() -> None:
    now = datetime(2026, 7, 1, tzinfo=timezone.utc)
    old = (now - timedelta(days=30)).isoformat()
    concepts = [
        Concept(
            concept_id=i,
            label=f"idea {i}",
            kind="identity" if i == 1 else "affective",
            subject="user" if i == 1 else "aiko",
            status="active",
            confidence=0.9,
            created_at=old,
        )
        for i in range(1, 5)
    ]
    finding = detect_neglect(concepts, {}, now=now, min_candidates=3)
    assert finding is not None
    assert finding.shape == "neglect"
    assert all(item_id != 1 for _kind, item_id in finding.evidence)


def test_fixation_is_flex_only_outlier_below_relationship_baseline() -> None:
    concepts = [
        Concept(
            concept_id=i,
            label=f"reading {i}",
            kind="affective",
            subject="aiko",
            status="active",
        )
        for i in range(1, 4)
    ]
    stats = {
        1: ItemStats(surfaced=18, settled=12, engaged=2),
        2: ItemStats(surfaced=5, settled=5, engaged=4),
        3: ItemStats(surfaced=3, settled=3, engaged=2),
    }
    finding = detect_fixation(
        concepts,
        stats,
        engaged_baseline=0.6,
        core_kinds=frozenset({"identity", "value"}),
    )
    assert finding is not None
    assert finding.shape == "fixation"
    assert finding.key == "concept:1"
    assert detect_fixation(
        concepts,
        {1: ItemStats(surfaced=18, settled=12, engaged=10), **{
            key: value for key, value in stats.items() if key != 1
        }},
        engaged_baseline=0.6,
        core_kinds=frozenset(),
    ) is None


def test_snapshot_is_bounded_and_rejects_malformed_rows() -> None:
    kv: dict[str, str] = {}
    findings = [
        ConductFinding(
            shape="neglect",
            key=f"k{i}",
            summary="quiet",
            evidence=(("concept", 1), ("concept", 2)),
            score=0.7,
        )
        for i in range(4)
    ]
    save_conduct_snapshot(kv.__setitem__, findings, cap=2)
    assert len(load_conduct_snapshot(kv.get)) == 2
    kv["concept.surfacing_conduct"] = '[{"shape":"bogus"},null]'
    assert load_conduct_snapshot(kv.get) == []


def test_neglect_malformed_dates_fail_silent() -> None:
    concept = SimpleNamespace(
        concept_id=1,
        kind="affective",
        subject="aiko",
        confidence=0.9,
        created_at="not-a-date",
    )
    assert detect_neglect(
        [concept, concept, concept],
        {},
        now=datetime.now(timezone.utc),
    ) is None


def test_weekly_cadence_probe_is_kv_only() -> None:
    now = datetime(2026, 7, 8, tzinfo=timezone.utc)
    worker = object.__new__(ConceptSynthesisWorker)
    worker._agent_settings = SimpleNamespace(surfacing_conduct_enabled=True)
    worker._memory_settings = SimpleNamespace(conduct_cadence_seconds=604800)
    kv = {"concept.surfacing_conduct.last_run": now.isoformat()}
    worker._kv_get = kv.get
    assert worker._conduct_due(now) is False
    assert worker._conduct_due(now + timedelta(days=7)) is True
    worker._agent_settings.surfacing_conduct_enabled = False
    assert worker._conduct_due(now + timedelta(days=30)) is False


def test_conduct_proposer_uses_detector_evidence_and_can_reinforce() -> None:
    finding = ConductFinding(
        shape="fixation",
        key="concept:7",
        summary="I keep returning to one reading",
        evidence=(("concept", 7), ("concept", 8)),
        score=0.8,
    )
    ctx = ProposerContext(
        call_llm=lambda _system, _user: [{
            "finding_key": "concept:7",
            "reinforces_id": 42,
            "rationale": "still fits",
        }],
        min_sources=2,
        user_name="Ben",
        assistant_name="Aiko",
    )
    proposals = propose_conduct_aiko(
        ctx,
        findings=[finding],
        existing=[ExistingConcept(id=42, label="I repeat myself")],
    )
    assert len(proposals) == 1
    assert proposals[0].reinforces_id == 42
    assert proposals[0].evidence == [("concept", "7"), ("concept", "8")]
