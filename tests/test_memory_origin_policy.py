"""Source role, not topical similarity, decides what a memory can establish."""
from dataclasses import asdict
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.concepts.proposers import CandidateProposal
from app.core.concepts.concept_support import memory_revision
from app.core.memory.memory_origin import MemoryOrigin, claim_source_refusal, memory_origin
from app.core.memory.memory_store import MemoryStore
from app.core.infra.chat_database import ChatDatabase
from tests.test_concept_synthesis_worker import ClusterStub, MemStub, WorkerHarness


def test_origin_contract_keeps_legacy_revisions_stable_but_tracks_new_semantic_changes():
    row = SimpleNamespace(content="Noah prefers detailed explanations", kind="preference",
                          event_time=None, created_at="2026-10-01T12:00:00+00:00",
                          metadata={"source_message_ids": [42]})
    assert memory_revision(row) == "98d7da633e390a81ffdd4286017a5a8d"
    row.metadata["memory_origin"] = asdict(MemoryOrigin("user", "observation", "extractor"))
    first = memory_revision(row)
    row.metadata["memory_origin"]["subject"] = "aiko"
    assert memory_revision(row) != first


@pytest.mark.parametrize("kind,subject,accepted", [
    ("pre_thought", "user", False), ("knowledge", "user", False),
    ("curiosity_finding", "user", False), ("goal_progress", "user", False),
    ("open_question", "user", False), ("pursuit_note", "user", False),
    ("fact", "user", True), ("self", "aiko", True),
    ("goal_progress", "aiko", True), ("pursuit_note", "aiko", True),
    ("shared_moment", "relationship", True),
])
def test_source_policy_guards_actual_persistence(kind, subject, accepted):
    rows = [MemStub(i, "A source with a specific observation", kind, .6) for i in (201, 202, 203)]
    h = WorkerHarness(lambda *_: {"concepts": []}, clusters=[], self_memories=[],
                      shared_moments=rows)
    proposal = CandidateProposal("A specific interpretation", "source-based", .8,
                                 [("memory", str(r.id)) for r in rows], "identity", subject)
    stats = {"added": 0, "reinforced": 0, "by_subject": {}}
    h.worker._persist(proposal, stats)
    assert bool(h.store.count()) == accepted
    if not accepted:
        assert sum(stats["source_policy_refusals"].values()) == 1
    assert all(h.mem.get(r.id) is r for r in rows)


def test_intention_is_not_a_completed_narrative_but_can_support_aspiration():
    row = MemStub(1, "A promised future lesson", "promise", .6,
                  metadata={"promise_who": "user"})
    assert claim_source_refusal(row, subject="user", kind="narrative")
    assert claim_source_refusal(row, subject="user", kind="aspiration") is None


def test_future_event_is_not_a_completed_story_even_with_an_explicit_observation_label():
    row = MemStub(1, "A booked trip", "event", .6, metadata={
        "memory_origin": asdict(MemoryOrigin("user", "observation", "extractor")),
    })
    row.temporal_type = "future_plan"
    assert claim_source_refusal(row, subject="user", kind="narrative")


def test_goal_cluster_routes_to_aiko_and_world_cluster_has_no_personal_subject():
    rows = [MemStub(10, "My own developing aim", "goal_progress", .6),
            MemStub(20, "Public information", "knowledge", .6)]
    clusters = [ClusterStub(m.id, "topic", 1, (m.kind,), member_ids=[m.id]) for m in rows]
    h = WorkerHarness(lambda *_: {"concepts": []}, self_memories=[],
                      shared_moments=rows, clusters=clusters)
    assert [c[0] for c in h.worker._dominant_clusters("aiko")] == [10]
    assert not h.worker._dominant_clusters("user")


def test_explicit_actor_wins_over_legacy_kind_and_invalid_contract_fails_closed():
    row = MemStub(1, "I have my own preference", "self_tagged", .6,
                  metadata={"memory_origin": asdict(MemoryOrigin("aiko", "declaration", "pulse"))})
    assert claim_source_refusal(row, subject="user", kind="boundary")
    assert claim_source_refusal(row, subject="aiko", kind="boundary") is None
    row.metadata["memory_origin"] = {"version": 99}
    assert memory_origin(row).subject == "unknown"
    assert claim_source_refusal(row, subject="user", kind="identity")


def test_validated_first_person_user_testimony_is_not_mistaken_for_aiko():
    row = MemStub(1, "I prefer detailed explanations", "self_tagged", .6,
                  metadata={"admission": {"subject": "user", "writer": "inline"}})
    assert memory_origin(row).subject == "user"
    assert claim_source_refusal(row, subject="user", kind="communication_style") is None


def test_cluster_cannot_hide_a_hypothetical_member():
    rows = [MemStub(100, "A direct user observation", "fact", .6),
            MemStub(101, "If the user asks", "pre_thought", .6)]
    h = WorkerHarness(lambda *_: {"concepts": []}, self_memories=[], shared_moments=rows,
                      clusters=[ClusterStub(100, "topic", 2, ("fact", "pre_thought"),
                                            member_ids=[100, 101])])
    proposal = CandidateProposal("A personal event", "rationale", .8,
                                 [("cluster", "100")], "identity", "user")
    assert h.worker._claim_source_refusal(proposal) == "hypothetical"
    h.mem._by_id.pop(101)
    assert h.worker._claim_source_refusal(proposal) == "missing_cluster_sources"


def test_new_rows_record_origin_without_relabelling_existing_rows(tmp_path):
    db = ChatDatabase(tmp_path / "memories.db")
    store = MemoryStore(tmp_path / "memories.db")
    try:
        row = store.add("A public fact about cookies", "knowledge", np.array([1., 0.]))
        assert row.metadata["memory_origin"]["subject"] == "world"
        assert row.metadata["memory_origin"]["mode"] == "world_fact"
        assert row.provenance == "inferred"
        assert memory_origin(row).domain == "external"
    finally:
        store.close()
        db._get_conn().close()
