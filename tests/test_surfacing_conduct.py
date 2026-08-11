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


def test_the_top_gap_bar_is_tunable() -> None:
    # Two clusters that both run hot: the leader clears every other bar, and
    # only the gap to the runner-up decides. On live data this was the gate
    # that declined, and it was the one nobody could move.
    stats = {
        1: ClusterTaste(1, surfaced=40, settled=30, engaged=20),
        2: ClusterTaste(2, surfaced=36, settled=26, engaged=16),
        3: ClusterTaste(3, surfaced=6, settled=4, engaged=2),
    }
    reps = {1: (101, "servers"), 2: (102, "music"), 3: (103, "books")}
    counts = {1: 2, 2: 30, 3: 8}
    assert detect_concentration(stats, counts, reps, min_top_gap=0.10) is None
    assert detect_concentration(stats, counts, reps, min_top_gap=0.05) is not None


def test_a_declining_detector_says_which_bar_it_missed() -> None:
    stats = {
        1: ClusterTaste(1, surfaced=40, settled=30, engaged=20),
        2: ClusterTaste(2, surfaced=36, settled=26, engaged=16),
        3: ClusterTaste(3, surfaced=6, settled=4, engaged=2),
    }
    reps = {1: (101, "servers"), 2: (102, "music"), 3: (103, "books")}
    reading: dict[str, object] = {}
    assert detect_concentration(
        stats, {1: 2, 2: 30, 3: 8}, reps, reading=reading,
    ) is None
    assert reading["outcome"] == "declined"
    assert reading["declined_on"] == "top_gap"
    # The bar and the measurement sit side by side, so the distance is a
    # read rather than a re-derivation.
    assert reading["min_top_gap"] == 0.10
    assert 0.0 < float(reading["top_gap"]) < 0.10


def test_a_reading_records_the_best_the_data_offered_even_when_short() -> None:
    stats = {1: ClusterTaste(1, surfaced=70, settled=60, engaged=20)}
    reading: dict[str, object] = {}
    assert detect_concentration(
        stats, {1: 40}, {1: (101, "servers")}, reading=reading,
    ) is None
    assert reading["declined_on"] == "no_cluster_clears_the_bars"
    assert reading["best_share"] == 1.0
    assert float(reading["best_excess"]) <= 0.0


def test_fixation_reports_how_far_off_baseline_the_top_concept_ran() -> None:
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
        1: ItemStats(surfaced=18, settled=12, engaged=10),
        2: ItemStats(surfaced=5, settled=5, engaged=4),
        3: ItemStats(surfaced=3, settled=3, engaged=2),
    }
    reading: dict[str, object] = {}
    assert detect_fixation(
        concepts,
        stats,
        engaged_baseline=0.6,
        core_kinds=frozenset(),
        reading=reading,
    ) is None
    assert reading["declined_on"] == "gates"
    # Engaged with more than usual, not less: the shape is genuinely absent
    # rather than the bars being wrong, and the reading shows which.
    assert float(reading["top_engaged_rate"]) > float(
        reading["engaged_rate_ceiling"]
    )


def test_a_firing_detector_says_so() -> None:
    stats = {
        1: ClusterTaste(1, surfaced=42, settled=40, engaged=25),
        2: ClusterTaste(2, surfaced=12, settled=10, engaged=7),
        3: ClusterTaste(3, surfaced=4, settled=3, engaged=2),
    }
    reps = {1: (101, "servers"), 2: (102, "music"), 3: (103, "books")}
    reading: dict[str, object] = {}
    assert detect_concentration(
        stats, {1: 3, 2: 12, 3: 5}, reps, reading=reading,
    ) is not None
    assert reading["outcome"] == "fired"
    assert "declined_on" not in reading


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


# ── the latch: an empty proposal pass must not retire the finding ───────
#
# L42 sat at zero conduct rows for its entire life while its detector was
# working correctly. The detector found a valid neglect finding, the LLM
# naming pass returned nothing, and the pass wrote the "already handled
# these findings" fingerprint anyway — so that finding could never be
# proposed again. These cover both halves of the repair: the proposer no
# longer needs the model to produce something, and the pass no longer
# closes the latch on a pass that produced nothing.


def _conduct_finding(**over) -> ConductFinding:
    base = dict(
        shape="neglect",
        key="concepts:7,8",
        summary="I hold parts of this quietly",
        second_person="You hold parts of this quietly",
        evidence=(("concept", 7), ("concept", 8)),
        score=0.7,
    )
    base.update(over)
    return ConductFinding(**base)


def test_conduct_proposer_falls_back_when_the_model_returns_nothing() -> None:
    ctx = ProposerContext(
        call_llm=lambda _system, _user: [],
        min_sources=2,
        user_name="Ben",
        assistant_name="Aiko",
    )
    proposals = propose_conduct_aiko(ctx, findings=[_conduct_finding()])
    assert len(proposals) == 1
    assert proposals[0].label == "You hold parts of this quietly"
    assert proposals[0].kind == "conduct"
    assert proposals[0].subject == "aiko"
    assert proposals[0].evidence == [("concept", "7"), ("concept", "8")]
    # Below the LLM path's 0.65 default: nothing judged it worth saying.
    assert proposals[0].confidence < 0.65


def test_conduct_proposer_falls_back_when_the_model_returns_junk() -> None:
    # Well-formed JSON that names a finding_key nobody asked about still
    # leaves us with zero proposals, which is the same loss.
    ctx = ProposerContext(
        call_llm=lambda _system, _user: [{"finding_key": "nope", "label": "x"}],
        min_sources=2,
        user_name="Ben",
        assistant_name="Aiko",
    )
    proposals = propose_conduct_aiko(ctx, findings=[_conduct_finding()])
    assert len(proposals) == 1
    assert proposals[0].label == "You hold parts of this quietly"


def test_conduct_fallback_still_honours_the_evidence_floor() -> None:
    ctx = ProposerContext(
        call_llm=lambda _system, _user: [],
        min_sources=3,
        user_name="Ben",
        assistant_name="Aiko",
    )
    assert propose_conduct_aiko(ctx, findings=[_conduct_finding()]) == []


def test_every_detector_shape_can_be_named_without_the_model() -> None:
    # A shape with no second-person rendering would silently lose its
    # findings on the fallback path, which is the bug in miniature.
    from app.core.concepts.surfacing_conduct import CONDUCT_SHAPES

    now = datetime(2026, 7, 8, tzinfo=timezone.utc)
    old = (now - timedelta(days=200)).isoformat()
    stats = {
        1: ClusterTaste(1, surfaced=42, settled=40, engaged=25),
        2: ClusterTaste(2, surfaced=12, settled=10, engaged=7),
        3: ClusterTaste(3, surfaced=4, settled=3, engaged=2),
    }
    reps = {1: (101, "servers"), 2: (102, "music"), 3: (103, "books")}
    produced: dict[str, ConductFinding] = {}
    concentration = detect_concentration(
        stats, {1: 1, 2: 30, 3: 30}, reps, min_excess=0.05,
    )
    if concentration is not None:
        produced["concentration"] = concentration
    neglect = detect_neglect(
        [
            SimpleNamespace(
                concept_id=i,
                kind="affective",
                subject="aiko",
                confidence=0.9,
                created_at=old,
                label=f"belief {i}",
            )
            for i in range(1, 5)
        ],
        {},
        now=now,
    )
    if neglect is not None:
        produced["neglect"] = neglect
    fixation = detect_fixation(
        [
            Concept(
                concept_id=i,
                label=f"reading {i}",
                kind="affective",
                subject="aiko",
                status="active",
            )
            for i in range(1, 4)
        ],
        {
            1: ItemStats(surfaced=18, settled=12, engaged=2),
            2: ItemStats(surfaced=5, settled=5, engaged=4),
            3: ItemStats(surfaced=3, settled=3, engaged=2),
        },
        engaged_baseline=0.6,
        core_kinds=frozenset({"identity", "value"}),
    )
    if fixation is not None:
        produced["fixation"] = fixation
    assert set(produced) == CONDUCT_SHAPES, (
        "a detector shape produced nothing; adjust the fixtures"
    )
    for shape, finding in produced.items():
        assert finding.second_person.strip(), f"{shape} cannot be named offline"
        assert not finding.second_person.startswith("I "), shape


def _conduct_worker(kv: dict[str, str], proposals):
    """A ``ConceptSynthesisWorker`` with only what ``_run_conduct_pass``
    touches, so the test is about the latch and nothing else."""
    worker = object.__new__(ConceptSynthesisWorker)
    worker._agent_settings = SimpleNamespace(surfacing_conduct_enabled=True)
    worker._memory_settings = SimpleNamespace(conduct_cadence_seconds=604800)
    worker._clock = lambda: datetime(2026, 7, 8, tzinfo=timezone.utc)
    worker._kv_get = kv.get
    worker._kv_set = kv.__setitem__
    worker._surfacing_outcome_store_provider = lambda: SimpleNamespace(
        engaged_rate_by_cluster=lambda **_kw: {},
        stats_for=lambda *_a, **_kw: {},
    )
    worker._concept_store = SimpleNamespace(
        list_by=lambda **_kw: [],
        edges_into=lambda *_a, **_kw: [],
    )
    worker._topic_graph = SimpleNamespace(topic_clusters=lambda: [])
    worker._user_vector_rows_provider = None
    worker._existing_for = lambda _spec, **_kw: []
    spec = SimpleNamespace(
        propose=lambda _ctx, **_kw: list(proposals),
        sig_key="concept_synth.conduct_sig.aiko",
    )
    return worker, spec


def test_an_empty_proposal_pass_leaves_the_finding_proposable(monkeypatch) -> None:
    from app.core.concepts import concept_synthesis_worker as module

    monkeypatch.setattr(
        module, "detect_conduct", lambda **_kw: [_conduct_finding()]
    )
    kv: dict[str, str] = {}
    stats: dict[str, object] = {}
    worker, spec = _conduct_worker(kv, proposals=[])
    ctx = ProposerContext(
        call_llm=lambda _s, _u: [],
        min_sources=2,
        user_name="Ben",
        assistant_name="Aiko",
    )

    assert worker._run_conduct_pass(ctx, spec, stats) == []
    assert "concept_synth.conduct_sig.aiko" not in kv, (
        "an empty pass wrote the already-handled fingerprint, which retires "
        "the finding permanently"
    )
    assert stats["conduct_latch_held_open"] is True


def test_a_productive_pass_does_record_the_fingerprint(monkeypatch) -> None:
    from app.core.concepts import concept_synthesis_worker as module

    monkeypatch.setattr(
        module, "detect_conduct", lambda **_kw: [_conduct_finding()]
    )
    kv: dict[str, str] = {}
    worker, spec = _conduct_worker(kv, proposals=["a proposal"])
    ctx = ProposerContext(
        call_llm=lambda _s, _u: [],
        min_sources=2,
        user_name="Ben",
        assistant_name="Aiko",
    )

    assert worker._run_conduct_pass(ctx, spec, {}) == ["a proposal"]
    assert "concept_synth.conduct_sig.aiko" in kv
    # ...and a second run with the same findings is correctly a no-op.
    worker2, spec2 = _conduct_worker(kv, proposals=["another"])
    kv.pop("concept.surfacing_conduct.last_run", None)
    assert worker2._run_conduct_pass(ctx, spec2, {}) == []
