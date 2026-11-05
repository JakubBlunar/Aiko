"""Contrastive checks for source authority during review, not just admission."""
from dataclasses import asdict

import pytest

from app.core.memory.memory_origin import MemoryOrigin, memory_origin
from app.core.memory.memory_consolidator import _cluster_memories
from tests.test_concept_contradiction import _Mem, _Hit, _MemoryStore, _concept, _detector, _IN_BAND
from tests.test_concept_belief_reviser import (
    _Mem as RevisionMemory, _MemoryStore as RevisionStore, _ConceptStore, _Edge,
    _reviser, _verdict, _concept as revision_concept, _BELIEF, _COUNTER, _Ollama,
)
from tests.test_memory_conflict_worker import _build_world as conflicts, _add_fact
from tests.test_idle_fact_checker import _build_world as checker
from tests.test_memory_consolidation_worker import (
    _FakeMemory as MergeMemory, _FakeStore as MergeStore, _FakeOllama as MergeModel,
    _make_worker as merger,
)


@pytest.mark.parametrize("kind", ["knowledge", "pre_thought", "goal_progress", "self", "diary"])
def test_world_drafts_and_aiko_notes_cannot_disprove_a_user_belief(kind):
    row = _Mem(1, "Noah hates spicy food deeply and often", _IN_BAND)
    row.kind = kind
    assert _detector(_MemoryStore([_Hit(row)])).detect(
        _concept("Noah loves spicy food deeply and often")) is None


def test_confidence_alone_keeps_a_conflict_open():
    world = conflicts()
    store, embed = world["memory_store"], world["embedder"]
    first = _add_fact(store, embed, "Noah loves spicy food deeply and often", confidence=.95)
    second = _add_fact(store, embed, "Noah hates spicy food deeply and often", confidence=.4)
    result = world["worker"].run()
    assert result["auto_resolved"] == 0
    assert result["opened"] == 1
    assert store.get(first).tier == store.get(second).tier == "long_term"


def test_exact_vector_and_neutral_bridge_cannot_collapse_opposite_claims():
    world = conflicts()
    store = world["memory_store"]
    first_text = "Noah loves spicy food deeply and often"
    vector = world["embedder"].embed(first_text)
    first = store.add(first_text, "fact", vector)
    opposite = store.add("Noah hates spicy food deeply and often", "fact", vector)
    assert opposite is not None and opposite.id != first.id
    bridge = store.add("Noah discussed spicy food deeply and often", "fact", vector,
                       skip_dedupe=True)
    groups = _cluster_memories([first, bridge, opposite], similarity=.9, min_size=2)
    assert groups
    assert all(not {first.id, opposite.id} <= {m.id for m in group} for group in groups)


@pytest.mark.parametrize("merged", [None, 37, "Today I changed my mind"])
def test_merge_falls_back_to_its_source_for_nontext_or_unanchored_output(merged):
    original = MergeMemory(1, "Noah enjoys quiet walks", confidence=.9)
    store = MergeStore([original, MergeMemory(2, "Noah enjoys quiet outings")])
    result = merger(store, ollama=MergeModel(merged=merged)).run()
    assert result["merged"] == 1
    assert original.content == "Noah enjoys quiet walks"
    assert original.confidence == .9


def test_explicit_user_correction_beats_inference_but_never_demotes_a_pin():
    world = conflicts()
    store, embed = world["memory_store"], world["embedder"]
    prior = _add_fact(store, embed, "Noah loves spicy food deeply and often", confidence=.95)
    changed = _add_fact(store, embed, "Noah hates spicy food deeply and often", confidence=.5,
                        provenance="stated", source_message_id=42)
    store.update(changed, metadata={"supersedes_memory_ids": [prior]}, metadata_merge=True)
    plan = world["worker"]._plan_resolution(store.get(prior), store.get(changed), .3)
    assert plan.auto_resolve and plan.winner.id == changed
    store.set_pinned(prior, True)
    pinned_plan = world["worker"]._plan_resolution(store.get(prior), store.get(changed), .3)
    assert not pinned_plan.auto_resolve


@pytest.mark.parametrize("scope", ["actor", "event_time"])
def test_different_actor_or_episode_is_not_a_conflict(scope):
    world = conflicts()
    store, embed = world["memory_store"], world["embedder"]
    first = _add_fact(store, embed, "Noah loves spicy food deeply and often")
    second = _add_fact(store, embed, "Noah hates spicy food deeply and often")
    if scope == "actor":
        store.update(second, metadata={"memory_origin": asdict(
            MemoryOrigin("aiko", "declaration", "synthetic"))})
    else:
        for mid, day in ((first, "01"), (second, "02")):
            store.update(mid, temporal_type="past_event", event_time=f"2026-10-{day}T12:00:00Z")
    result = world["worker"].run()
    assert result["opened"] == result["auto_resolved"] == 0


@pytest.mark.parametrize("kind,provenance,pinned,citations", [
    ("fact", "stated", False, [0]), ("knowledge", "inferred", True, [0]),
    ("knowledge", "inferred", False, []),
])
def test_search_cannot_rewrite_private_curated_or_unattributed_evidence(
        kind, provenance, pinned, citations):
    world = checker(verdict_json={"verdict": "contradict", "delta": -.3,
        "rewrite": "Python 3.12 shipped in October 2023", "supporting_source_indices": citations})
    store = world["memory_store"]
    original = "Python 3.12 was released in 2022"
    row = store.add(original, kind, world["embedder"].embed(original),
                    confidence=.9, provenance=provenance, pinned=pinned)
    world["queue"].enqueue(memory_id=row.id, claim_text=original, claim_kind="year")
    world["worker"].run()
    assert row.content == original
    assert row.confidence == .9
    assert row.metadata["fact_check_evidence"]["research_evidence"]


@pytest.mark.parametrize("answer,scope,indices", [
    (None, "world", [0]), ("Noah played violin for ten years", "personal", [0]),
    ("Python 3.12 shipped in October 2023", "world", []),
])
def test_gap_needs_a_public_attributed_answer(answer, scope, indices):
    world = checker(verdict_json={"verdict": "support", "delta": .2, "rewrite": answer,
                                 "scope": scope, "supporting_source_indices": indices})
    gap = world["knowledge_gap_store"].add_gap(
        topic="python", question="When did Python 3.12 ship?")
    world["queue"].enqueue(memory_id=gap.id, claim_text="Python 3.12 release date",
                            claim_kind="knowledge_gap")
    world["worker"].run()
    assert not gap.metadata.get("resolved_at")
    assert not list(world["memory_store"].iter_by_kind("knowledge"))


def test_concept_revision_preserves_testimony_and_requires_a_resolvable_personal_counter_source():
    original = RevisionMemory(11, _BELIEF)
    original.provenance = "stated"
    store = RevisionStore([original])
    llm = _Ollama('{"resolution": "INACCURATE"}')
    worker = _reviser(_ConceptStore([_Edge("memory", "11")]), store, ollama=llm)
    assert worker.revise(revision_concept(), _verdict()).kept == 1
    assert llm.calls == 0 and not store.update_calls
    original.provenance = "inferred"
    counter = store.get(99)
    counter.content = _COUNTER
    counter.kind = "knowledge"
    assert memory_origin(counter).subject == "world"
    assert worker.revise(revision_concept(), _verdict()).skipped_source_scope == 1
    assert llm.calls == 0
