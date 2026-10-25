"""L52 support loss revisits conclusions without turning absence into negation."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.concepts.concept_dependencies import MAX_MEMBERS
from app.core.concepts.concept_edge_reconciler import ConceptEdgeReconciler
from app.core.concepts.concept_event_store import ConceptEventStore
from app.core.concepts.concept_lifecycle_worker import ConceptLifecycleWorker
from app.core.concepts.concept_store import Concept, ConceptEdge, ConceptStore
from app.core.concepts.concept_view import ConceptView
from app.core.infra.chat_database import ChatDatabase

NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


@pytest.fixture
def model(tmp_path):
    db = ChatDatabase(tmp_path / "dependencies.db")
    store = ConceptStore(db)

    def concept(label, *, kind="identity", evidence_model="set", status="active"):
        row = Concept(label=label, kind=kind, evidence_model=evidence_model,
                      status=status, confidence=0.85, distinct_source_count=3,
                      evidence_count=3, embedding=np.ones(3, dtype=np.float32),
                      first_evidence_at=(NOW - timedelta(days=10)).isoformat(),
                      last_lifecycle_at=(NOW - timedelta(hours=1)).isoformat())
        store.add(row)
        return row

    def memory(ident):
        db._get_conn().execute(
            "INSERT INTO memories (id, content, kind, embedding, created_at, confidence) "
            "VALUES (?, ?, 'fact', ?, ?, 0.9)",
            (ident, f"Observation {ident}", np.ones(3, dtype=np.float32).tobytes(),
             NOW.isoformat()),
        )
        db._get_conn().commit()
        return ("memory", str(ident))

    settings = SimpleNamespace(concept_lifecycle_enabled=True,
                               concept_lifecycle_batch_size=100,
                               concept_plasticity_drift_enabled=False)
    events = ConceptEventStore(db)
    worker = ConceptLifecycleWorker(concept_store=store, memory_settings=settings,
                                    agent_settings=SimpleNamespace(concepts_enabled=True),
                                    concept_event_store=events, clock=lambda: NOW)
    return SimpleNamespace(db=db, store=store, concept=concept, memory=memory,
                           worker=worker, events=events)


def node(concept):
    return ("concept", str(concept.concept_id))


def test_alternative_support_survives_failed_conjunction_and_l3(model):
    a, b, c = [model.concept(label) for label in ("a", "b", "c")]
    child = model.concept("conclusion", kind="generalization", evidence_model="meta")
    model.store.dependencies.add(child.concept_id, [[node(a), node(b)], [node(c)]])
    a.status = "contradicted"
    model.store.update(a)

    report = model.store.dependencies.assess(child.concept_id)
    assert report["viable"] and len(report["valid_sets"]) == 1
    assert report["invalid_sets"]
    model.worker.run()
    assert child.status == "active"
    assert model.store.is_assertable(child.concept_id)


def test_transitive_retraction_filters_warm_retrieval_before_lifecycle(model):
    a, child, grandchild, other = [model.concept(label) for label in
                                  ("premise", "child", "grandchild", "other")]
    model.store.dependencies.add(child.concept_id, [[node(a)]])
    model.store.dependencies.add(grandchild.concept_id, [[node(child)]])
    query = np.ones(3, dtype=np.float32)
    assert len(model.store.nearest(query, k=4)) == 4  # warm the matrix
    a.status = "retired"
    model.store.update(a)
    assert child.status == grandchild.status == "active"  # L3 owns status
    assert [row.concept_id for row, _ in model.store.nearest(query, k=1)] == [other.concept_id]
    assert model.store.list_by(status="active") == [other]
    assert not ConceptView(model.store).activated([], seed_concept_ids=[a.concept_id])

    model.worker.run()
    assert child.status == grandchild.status == "candidate"
    assert child.confidence > 0.6  # absence did not become counter-evidence
    assert model.store.is_lifecycle_due(grandchild.concept_id) is False
    report = model.store.dependencies.assess(grandchild.concept_id)
    assert not report["viable"]


@pytest.mark.parametrize("mutation", [
    "delete", "edit", "supersede", "confidence", "inaccurate", "belief_superseded",
])
def test_memory_invalidation_and_alternative_observation(model, mutation):
    first, second = model.memory(1), model.memory(2)
    supported, unsupported = model.concept("supported"), model.concept("unsupported")
    model.store.dependencies.add(supported.concept_id, [[first], [second]])
    model.store.dependencies.add(unsupported.concept_id, [[first]])
    conn = model.db._get_conn()
    if mutation == "delete":
        conn.execute("DELETE FROM memories WHERE id = 1")
    elif mutation == "edit":
        conn.execute("UPDATE memories SET content = 'Correction' WHERE id = 1")
    elif mutation == "supersede":
        conn.execute("UPDATE memories SET metadata = ? WHERE id = 1",
                     (json.dumps({"superseded_by": 2}),))
    elif mutation == "confidence":
        conn.execute("UPDATE memories SET confidence = 0.2 WHERE id = 1")
    else:
        conn.execute("UPDATE memories SET metadata = ? WHERE id = 1",
                     (json.dumps({"belief_revision": mutation.removeprefix("belief_")}),))
    conn.commit()
    if mutation == "delete":
        ConceptEdgeReconciler(model.store).on_memory_deleted(1)
        assert model.store.is_lifecycle_due(unsupported.concept_id)
    assert model.store.is_assertable(supported.concept_id)
    assert not model.store.is_assertable(unsupported.concept_id)
    if mutation in {"delete", "edit"}:
        expected = "source_missing" if mutation == "delete" else "source_revision_changed"
        failures = model.store.dependencies.assess(unsupported.concept_id)["invalid_sets"]
        assert failures[0]["failures"][0]["reason"] == expected
    model.worker.run()
    assert supported.status == "active"
    assert unsupported.status == "candidate"


def test_readmission_reconsiders_revised_evidence_without_reversing_belief(model):
    source = model.memory(1)
    child = model.concept("child", kind="boundary")
    model.store.dependencies.add(child.concept_id, [[source]])
    model.db._get_conn().execute("UPDATE memories SET content = 'Revised' WHERE id = 1")
    model.db._get_conn().commit()
    model.worker.run()
    assert child.status == "candidate"
    model.store.dependencies.add(child.concept_id, [[source]])
    model.worker.run()
    assert child.status == "active"


def test_deleted_concept_leaves_content_free_failed_receipt_across_restart(model):
    parent, child = model.concept("private source text"), model.concept("child")
    model.store.dependencies.add(child.concept_id, [[node(parent)]])
    model.store.delete(parent.concept_id)
    assert model.store.is_lifecycle_due(child.concept_id)
    reloaded = ConceptStore(model.db)
    reloaded.load_all()
    assert not reloaded.is_assertable(child.concept_id)
    assert reloaded.is_lifecycle_due(child.concept_id)
    report = reloaded.dependencies.assess(child.concept_id)
    assert "private source text" not in json.dumps(report)
    assert report["support_sets"][0][0][1] == str(parent.concept_id)


def test_alias_merge_preserves_dependency_and_refuses_cycle_before_mutation(model):
    canonical, absorbed, child = [model.concept(label) for label in ("a", "alias", "child")]
    model.store.dependencies.add(child.concept_id, [[node(absorbed)]])
    assert model.store.merge_into(canonical_id=canonical.concept_id,
                                  absorbed_id=absorbed.concept_id)
    assert model.store.is_assertable(child.concept_id)
    members = model.store.dependencies.records[child.concept_id][0]
    assert members[0][1] == str(canonical.concept_id)
    assert not model.store.merge_into(canonical_id=canonical.concept_id,
                                      absorbed_id=child.concept_id)
    assert model.store.get(child.concept_id) is child


def test_merge_cannot_add_dependency_depth(model):
    leaf, deep, alias, child = [model.concept(label) for label in ("leaf", "deep", "alias", "c")]
    model.store.dependencies.add(deep.concept_id, [[node(leaf)]])
    model.store.dependencies.add(child.concept_id, [[node(alias)]])
    assert not model.store.merge_into(canonical_id=deep.concept_id,
                                      absorbed_id=alias.concept_id)
    assert model.store.get(alias.concept_id) is alias


def test_cycle_and_work_budget_fail_closed(model):
    first, second = model.concept("first"), model.concept("second")
    model.store.dependencies.add(second.concept_id, [[node(first)]])
    with pytest.raises(ValueError, match="cycle"):
        model.store.dependencies.add(first.concept_id, [[node(second)]])
    report = model.store.dependencies.assess(second.concept_id, max_nodes=1)
    assert not report["viable"]
    assert report["visited"] == 1
    assert report["invalid_sets"][0]["failures"][0]["reason"] == "traversal_limit"
    with pytest.raises(ValueError, match="size"):
        model.store.dependencies.add(first.concept_id,
                                      [[("memory", str(i)) for i in range(MAX_MEMBERS + 1)]])


def test_disproof_invalidates_still_active_premise(model):
    parent, child = model.concept("parent"), model.concept("child")
    model.store.dependencies.add(child.concept_id, [[node(parent)]])
    model.store.support.remember_disproof(parent.concept_id, 100)
    assert parent.status == "active"
    assert not model.store.is_assertable(child.concept_id)


def test_removed_dependency_edge_does_not_shrink_conjunction(model):
    parent, child = model.concept("parent"), model.concept("child")
    model.store.dependencies.add(child.concept_id, [[node(parent), model.memory(1)]])
    edge = model.store.edges_from("concept", parent.concept_id)[0]
    model.store.delete_edge(edge.edge_id)
    assert not model.store.is_assertable(child.concept_id)


def test_structural_admission_sets_and_history_exception(model):
    nodes = [model.memory(1), model.memory(2)]
    independent = model.concept("observations")
    sequence = model.concept("sequence", evidence_model="sequence")
    historical = model.concept("history", kind="self_correction", evidence_model="meta")
    for row in (independent, sequence, historical):
        model.store.dependencies.admit(row, nodes)
    assert len(model.store.dependencies.records[independent.concept_id]) == 2
    assert len(model.store.dependencies.records[sequence.concept_id]) == 1
    assert historical.concept_id not in model.store.dependencies.records


def test_legacy_rows_are_explicitly_untracked_until_admission(model):
    legacy = model.concept("legacy")
    report = model.store.dependencies.assess(legacy.concept_id)
    assert report["reason"] == "legacy_untracked"
    assert model.store.is_assertable(legacy.concept_id)
    for source in (model.memory(1), model.memory(2)):
        model.store.add_edge(ConceptEdge(*source, "concept", str(legacy.concept_id), "evidence"))
    model.store.dependencies.capture_legacy(legacy)
    assert model.store.dependencies.assess(legacy.concept_id)["tracked"]


def test_message_deletion_blocks_materialized_source_and_candidate_recall(model):
    child = model.concept("candidate", status="candidate")
    source = model.memory(1)
    # Recorded lineage whose message was erased cannot certify the conclusion.
    model.db._get_conn().execute("UPDATE memories SET metadata = ? WHERE id = 1",
                                 (json.dumps({"source_message_ids": [999]}),))
    model.db._get_conn().commit()
    model.store.dependencies.add(child.concept_id, [[source]])
    report = model.store.dependencies.assess(child.concept_id)
    assert report["invalid_sets"][0]["failures"][0]["reason"] == "source_message_deleted"
    assert not model.store.nearest(np.ones(3), status="candidate")
    assert not model.store.list_by(status="candidate")


def test_merge_does_not_launder_stale_source_revision(model):
    canonical, absorbed, child = [model.concept(label) for label in ("a", "alias", "child")]
    model.store.dependencies.add(child.concept_id, [[node(absorbed)]])
    absorbed.label = "a different premise"
    model.store.update(absorbed)
    assert not model.store.is_assertable(child.concept_id)
    assert model.store.merge_into(canonical_id=canonical.concept_id,
                                  absorbed_id=absorbed.concept_id)
    assert not model.store.is_assertable(child.concept_id)


def test_l2_creation_and_complete_reinforcement_record_real_sources(model):
    from app.core.concepts.proposers.base import CandidateProposal
    from test_concept_synthesis_worker import WorkerHarness

    harness = WorkerHarness(lambda system, user: {"concepts": []})
    harness.worker._concept_store = model.store
    nodes = [model.memory(1), model.memory(2)]
    proposal = CandidateProposal(label="A grounded claim", rationale="Observations",
                                 confidence=0.7, evidence=nodes, kind="identity", subject="user")
    stats = {"added": 0, "reinforced": 0, "by_subject": {}}
    harness.worker._persist(proposal, stats)
    assert stats["added"] == 1
    claim = model.store.all()[0]
    assert len(model.store.dependencies.records[claim.concept_id]) == 2
    proposal.evidence = [model.memory(3)]
    proposal.reinforces_id = claim.concept_id
    harness.worker._persist(proposal, stats)
    assert stats["reinforced"] == 1
    assert len(model.store.dependencies.records[claim.concept_id]) == 3


def test_latest_assessment_is_stable_and_records_failed_premise(model):
    parent, child = model.concept("parent"), model.concept("child")
    model.store.dependencies.add(child.concept_id, [[node(parent)]])
    parent.status = "contradicted"
    model.store.update(parent)
    model.worker.run()
    key = f"concept.dependency_review:{child.concept_id}"
    receipt = model.db.kv_get(key)
    assert "premise_contradicted" in receipt
    model.store.dependencies.reconsider(child.concept_id)
    assert model.db.kv_get(key) == receipt


@pytest.mark.parametrize("evidence_model", ["meta", "set"])
def test_partial_l2_reinforcement_preserves_support_semantics(model, monkeypatch, evidence_model):
    from app.core.concepts.concept_evidence_admission import Admission, Refusal
    from app.core.concepts.proposers.base import CandidateProposal
    from test_concept_synthesis_worker import WorkerHarness

    harness = WorkerHarness(lambda system, user: {"concepts": []})
    harness.worker._concept_store = model.store
    a, b, c, d = [model.concept(label) for label in ("a", "b", "c", "d")]
    kind = "generalization" if evidence_model == "meta" else "identity"
    child = model.concept("derived", kind=kind, evidence_model=evidence_model)
    model.store.dependencies.add(child.concept_id, [[node(a), node(b)]])
    before = model.store.dependencies.records[child.concept_id]
    monkeypatch.setattr("app.core.concepts.concept_synthesis_worker.admit",
                        lambda *args, **kwargs: Admission(
                            kept=[node(c)], refused=[Refusal(node(d), "offtopic")], admitted=1,
                        ))
    proposal = CandidateProposal(label=child.label, rationale="Alternative explanation",
                                 confidence=0.8, evidence=[node(c), node(d)],
                                 kind=kind, subject="user", evidence_model=evidence_model,
                                 reinforces_id=child.concept_id)
    stats = {"added": 0, "reinforced": 0, "by_subject": {}}
    harness.worker._persist(proposal, stats)
    if evidence_model == "meta":
        assert model.store.dependencies.records[child.concept_id] == before
    a.status = "contradicted"
    model.store.update(a)
    assert model.store.is_assertable(child.concept_id) is (evidence_model == "set")


def test_superseded_scoped_premise_does_not_certify_dependent(model):
    parent = model.concept("scoped style", kind="communication_style")
    child = model.concept("derived")
    model.store.dependencies.add(child.concept_id, [[node(parent)]])
    model.store.applicability.record(parent.concept_id, {
        "version": 1, "contexts": [{"name": "learning"}], "superseded_by": 999,
    })
    assert not model.store.is_assertable(child.concept_id)


def test_new_alternative_cannot_restore_a_removed_old_dependency(model):
    a, b, child = [model.concept(label) for label in ("a", "b", "child")]
    model.store.dependencies.add(child.concept_id, [[node(a)]])
    model.store.delete_edge(model.store.edges_from("concept", a.concept_id)[0].edge_id)
    model.store.dependencies.add(child.concept_id, [[node(b)]])
    assert model.store.is_assertable(child.concept_id)
    b.status = "contradicted"
    model.store.update(b)
    assert not model.store.is_assertable(child.concept_id)


def test_alias_merge_cannot_launder_disproved_identical_premise(model):
    canonical, absorbed = model.concept("same label"), model.concept("same label")
    child = model.concept("child")
    model.store.dependencies.add(child.concept_id, [[node(absorbed)]])
    absorbed.status = "contradicted"
    model.store.update(absorbed)
    assert model.store.merge_into(canonical_id=canonical.concept_id,
                                  absorbed_id=absorbed.concept_id)
    assert not model.store.is_assertable(child.concept_id)


def test_in_place_premise_revision_queues_review_without_resetting_decay_anchor(model):
    parent, child = model.concept("parent"), model.concept("child")
    model.store.dependencies.add(child.concept_id, [[node(parent)]])
    model.worker.run()
    assert not model.store.is_lifecycle_due(child.concept_id)
    anchor = child.last_lifecycle_at
    parent.label = "revised premise"
    model.store.update(parent)
    assert model.store.is_lifecycle_due(child.concept_id)
    assert child.last_lifecycle_at == anchor
    assert not model.store.is_assertable(child.concept_id)


def test_failed_conjunctions_leave_budget_to_discover_valid_alternative(model):
    invalid = model.concept("withdrawn premise")
    common = [node(model.concept(f"common {index}")) for index in range(22)]
    alternatives = [[node(invalid), *common, node(model.concept(f"alternative {index}"))]
                    for index in range(8)]
    alternatives.append([*common, node(model.concept("valid a")), node(model.concept("valid b"))])
    child = model.concept("derived", kind="generalization", evidence_model="meta")
    model.store.dependencies.add(child.concept_id, alternatives)
    invalid.status = "contradicted"
    model.store.update(invalid)
    report = model.store.dependencies.assess(child.concept_id)
    assert report["viable"]
    assert report["valid_sets"] == [8]
    assert report["visited"] <= 256


def test_source_edit_during_generation_rejects_stale_l2_proposal(model):
    from app.core.concepts.proposers.base import CandidateProposal, ProposerContext, ProposerSpec
    from test_concept_synthesis_worker import WorkerHarness

    harness = WorkerHarness(lambda system, user: {"concepts": []})
    harness.worker._concept_store = model.store
    nodes = [model.memory(1), model.memory(2)]
    stats = {"added": 0, "reinforced": 0, "by_subject": {}}
    harness.worker._routing_begin(stats)

    def generate(ctx, *, memories):
        model.db._get_conn().execute("UPDATE memories SET content = 'Corrected' WHERE id = 1")
        model.db._get_conn().commit()
        return [CandidateProposal(label="Stale claim", rationale="Old observations", confidence=0.8,
                                  evidence=nodes, kind="identity", subject="user")]

    spec = ProposerSpec("identity", "user", "set", "aiko_memories", generate, "race_test")
    captured = harness.worker._capture_proposer(spec)
    proposals = captured.propose(ProposerContext(call_llm=lambda system, user: []), memories=[
        SimpleNamespace(id=ident, content=f"Observation {ident}", kind="fact") for ident in (1, 2)
    ])
    assert proposals[0].dependency_revisions
    harness.worker._persist(proposals[0], stats)
    assert stats["dependency_source_changed"] == 1
    assert not model.store.all()


def test_admission_checks_frozen_revision_again(model):
    source = model.memory(1)
    revisions = {source: model.store.dependencies.fingerprint(source)}
    model.db._get_conn().execute("UPDATE memories SET content = 'Corrected' WHERE id = 1")
    model.db._get_conn().commit()
    child = model.concept("child")
    with pytest.raises(ValueError, match="changed during generation"):
        model.store.dependencies.add(child.concept_id, [[source]], revisions=revisions)


def test_memory_change_filters_unchanged_cached_matrix_before_top_k(model):
    unsupported, other = model.concept("unsupported"), model.concept("other")
    model.store.dependencies.add(unsupported.concept_id, [[model.memory(1)]])
    query = np.ones(3, dtype=np.float32)
    assert len(model.store.nearest(query, k=2)) == 2
    # No concept update or reconciliation dirties the active embedding cache.
    model.db._get_conn().execute("UPDATE memories SET confidence = 0.2 WHERE id = 1")
    model.db._get_conn().commit()
    assert unsupported.status == "active"
    assert [row.concept_id for row, _ in model.store.nearest(query, k=1)] == [other.concept_id]


@pytest.mark.parametrize("consumer", ["assertable", "list", "nearest", "candidate"])
def test_hot_reads_stop_after_one_complete_valid_support_set(model, consumer):
    child = model.concept("supported", status="candidate" if consumer == "candidate" else "active")
    model.store.dependencies.add(child.concept_id, [[model.memory(i)] for i in range(1, 17)])
    queries = []
    conn = model.db._get_conn()
    conn.set_trace_callback(queries.append)
    try:
        if consumer == "assertable":
            assert model.store.is_assertable(child.concept_id)
        elif consumer == "list":
            assert model.store.list_by(status="active") == [child]
        else:
            status = "candidate" if consumer == "candidate" else "active"
            assert [row for row, _ in model.store.nearest(np.ones(3), status=status)] == [child]
    finally:
        conn.set_trace_callback(None)
    memory_reads = [query for query in queries if "FROM memories WHERE id =" in query]
    edge_reads = [query for query in queries if "FROM concept_edges" in query]
    assert len(memory_reads) == 1
    assert len(edge_reads) == 1


def test_dependency_membership_does_not_materialize_source_fanout(model, monkeypatch):
    source = model.memory(1)
    child = model.concept("supported")
    model.store.dependencies.add(child.concept_id, [[source]])
    conn = model.db._get_conn()
    conn.executemany(
        "INSERT INTO concept_edges "
        "(src_type, src_id, dst_type, dst_id, relation, created_at) "
        "VALUES ('memory', '1', 'concept', ?, 'evidence', ?)",
        [(str(1000 + i), NOW.isoformat()) for i in range(1000)],
    )
    conn.commit()

    def no_fanout(*args):
        pytest.fail("dependency membership must use an exact indexed existence query")

    monkeypatch.setattr(model.store, "edges_from", no_fanout)
    assert model.store.dependencies.assess(child.concept_id)["viable"]
    assert model.store.is_assertable(child.concept_id)


@pytest.mark.parametrize("mutation", ["delete", "negative", "wrong_relation", "wrong_target"])
def test_boolean_support_gate_rechecks_exact_edge_on_every_read(model, mutation):
    source = model.memory(1)
    child = model.concept("supported")
    model.store.dependencies.add(child.concept_id, [[source]])
    assert model.store.is_assertable(child.concept_id)
    conn = model.db._get_conn()
    if mutation == "delete":
        conn.execute("DELETE FROM concept_edges WHERE relation = 'depends_on'")
    elif mutation == "negative":
        conn.execute("UPDATE concept_edges SET polarity = -1 WHERE relation = 'depends_on'")
    elif mutation == "wrong_relation":
        conn.execute("UPDATE concept_edges SET relation = 'evidence' WHERE relation = 'depends_on'")
    else:
        conn.execute("UPDATE concept_edges SET dst_id = '999' WHERE relation = 'depends_on'")
    conn.commit()
    assert not model.store.is_assertable(child.concept_id)
    report = model.store.dependencies.assess(child.concept_id)
    assert report["invalid_sets"][0]["failures"][0]["reason"] == "dependency_edge_removed"


def test_short_circuit_requires_every_conjunct_and_keeps_work_budget(model):
    a, b, c, d = [model.concept(label) for label in ("a", "b", "c", "d")]
    child = model.concept("supported")
    model.store.dependencies.add(child.concept_id, [[node(a), node(b)], [node(c), node(d)]])
    b.status = "retired"
    model.store.update(b)
    assert model.store.dependencies.is_viable(child.concept_id)
    assert not model.store.dependencies.is_viable(child.concept_id, max_nodes=1)
    d.status = "retired"
    model.store.update(d)
    assert not model.store.dependencies.is_viable(child.concept_id)


def test_full_assessment_still_reports_all_sets_and_strongest_confidence(model):
    first, second = model.concept("first"), model.concept("second")
    first.confidence, second.confidence = 0.6, 0.95
    model.store.update(first)
    model.store.update(second)
    child = model.concept("supported")
    model.store.dependencies.add(child.concept_id, [[node(first)], [node(second)]])
    assert model.store.dependencies.is_viable(child.concept_id)
    report = model.store.dependencies.assess(child.concept_id)
    assert report["valid_sets"] == [0, 1]
    assert report["confidence_bound"] == 0.95
