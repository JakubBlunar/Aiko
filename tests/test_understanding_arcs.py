from types import SimpleNamespace as NS

import numpy as np
import pytest

from app.core.concepts.concept_store import Concept, ConceptStore
from app.core.concepts.concept_event_store import ConceptEvent, ConceptEventStore
from app.core.concepts.concept_learning_event_store import (
    ConceptAlias, ConceptLearningEventStore, LearningEvent,
)
from app.core.concepts.understanding_arcs import (
    record_learning_sources, render_arc, requested_subjects, retrospective_request,
    understanding_arc,
)
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore
from app.core.session.context_budget_selector import ContextCandidate
from app.core.session.pattern_context import prepare_arcs


@pytest.fixture
def history(tmp_path):
    db = ChatDatabase(tmp_path / "history.db")
    memories = MemoryStore(tmp_path / "history.db")
    store = ConceptStore(db)
    learning, lifecycle = ConceptLearningEventStore(db), ConceptEventStore(db)
    yield db, memories, store, learning, lifecycle
    memories.close()
    db._get_conn().close()


def make_arc(history, subject, *, cluster_sources=False):
    db, memories, store, learning, lifecycle = history
    concepts = []
    labels = ["Learning to name a preference", "Distinguishing when it fits",
              "Keeping its exceptions"]
    for label in labels:
        concept = Concept(label=label, subject=subject, status="active")
        concept.concept_id = store.add(concept)
        concepts.append(concept)
    source_ids = []
    graph = NS(clusters=[]) if cluster_sources else None
    if graph is not None:
        graph.topic_clusters = lambda: graph.clusters
    for index in range(2):
        memory = memories.add(f"Explicit qualification number {index}", kind="fact",
                              embedding=np.array([1., index + 1.]), skip_dedupe=True)
        source_ids.append(memory.id)
        if graph is not None:
            graph.clusters.append(NS(representative_id=memory.id, member_ids=[memory.id],
                                     summary="Recorded basis"))
        at = f"2026-10-{index + 1:02d}T12:00:00+00:00"
        proof = ConceptEvent(event_type="promoted", subject=subject,
                             concept_id=concepts[index + 1].concept_id,
                             label=labels[index + 1], created_at=at)
        lifecycle.add(proof)
        event = LearningEvent(shape="succession", subject=subject,
                              concept_id=concepts[index + 1].concept_id,
                              prior_concept_id=concepts[index].concept_id,
                              old_label=labels[index], new_label=labels[index + 1],
                              because="A more specific qualification was recorded.",
                              decisive_event_id=proof.event_id,
                              evidence_refs=(("cluster" if graph else "memory", str(memory.id)),),
                              fingerprint=f"{subject}-{index}", created_at=at)
        learning.add(event)
        record_learning_sources(db, event, store, memories, graph)
    kwargs = dict(store=store, learning=learning, lifecycle=lifecycle, db=db,
                  memories=memories, graph=graph)
    return concepts, source_ids, kwargs


@pytest.mark.parametrize("subject", ["user", "aiko", "relationship"])
def test_chronological_arc_covers_every_subject(history, subject):
    concepts, _, kwargs = make_arc(history, subject)
    packet = understanding_arc(concepts[-1], **kwargs)
    assert packet["status"] == "supported"
    assert packet["subject"] == subject
    assert [s["at"] for s in packet["steps"]] == sorted(s["at"] for s in packet["steps"])
    assert packet["steps"][0]["concept_id"] == concepts[1].concept_id
    assert packet["steps"][-1]["later"] == concepts[-1].label
    assert "not proof a person changed" in render_arc(packet)


def test_deleted_revised_sources_and_missing_lifecycle_proof_block_a_story(history):
    concepts, source_ids, kwargs = make_arc(history, "relationship")
    assert understanding_arc(concepts[-1], **kwargs)["status"] == "supported"
    assert understanding_arc(concepts[-1], **{**kwargs, "lifecycle": NS(list=lambda **k: [])})[
        "status"] == "thin_record"
    memories = history[1]
    memories.update(source_ids[0], content="Retracted old qualification")
    assert understanding_arc(concepts[-1], **kwargs)["status"] == "thin_record"
    memories.delete(source_ids[-1])
    assert not understanding_arc(concepts[-1], **kwargs)["steps"]


def test_aliases_preserve_continuity_but_label_revisions_need_a_new_event(history):
    concepts, _, kwargs = make_arc(history, "aiko")
    old = concepts[1].concept_id
    history[3].record_alias(ConceptAlias(absorbed_id=old, canonical_id=concepts[0].concept_id,
                                        subject="aiko", kind="identity"))
    assert understanding_arc(concepts[-1], **kwargs)["status"] == "supported"
    concepts[-1].label = "An unrecorded new version"
    assert understanding_arc(concepts[-1], **kwargs)["status"] == "thin_record"


def test_legacy_or_candidate_history_is_explicitly_thin(history):
    concepts, _, kwargs = make_arc(history, "user")
    history[0].kv_set("concept.learning_sources:1", "")
    assert understanding_arc(concepts[-1], **kwargs)["status"] == "thin_record"
    concepts[-1].status = "candidate"
    assert not understanding_arc(concepts[-1], **kwargs)["steps"]


def test_retrospective_context_is_subject_scoped_and_costed_before_selection(history):
    concepts, _, kwargs = make_arc(history, "aiko")
    host = NS(_understanding_arc=lambda c: understanding_arc(c, **kwargs))
    candidate = ContextCandidate(source="concept", relevance=.8, tokens=20, order=0,
                                 payload=concepts[-1])
    assert prepare_arcs(host, [candidate], "hello", lambda s: len(s) // 4) == {}
    assert prepare_arcs(host, [candidate], "Has our relationship changed?",
                        lambda s: len(s) // 4) == {}
    result = prepare_arcs(host, [candidate], "How have you changed?", lambda s: len(s) // 4)
    assert result and candidate.tokens > 20
    assert requested_subjects("How have you changed?") == {"aiko"}
    assert requested_subjects("Has our relationship changed?") == {"relationship"}
    assert retrospective_request("What changed in your understanding?")
    assert not retrospective_request("I changed the setting and it works")


def test_new_cluster_members_do_not_rewrite_the_recorded_basis(history):
    concepts, source_ids, kwargs = make_arc(history, "relationship", cluster_sources=True)
    extra = history[1].add("A separate later observation", kind="fact",
                           embedding=np.array([1., 3.]), skip_dedupe=True)
    kwargs["graph"].clusters[0].member_ids.append(extra.id)
    assert understanding_arc(concepts[-1], **kwargs)["status"] == "supported"
    history[1].delete(source_ids[0])
    assert understanding_arc(concepts[-1], **kwargs)["status"] == "thin_record"


def test_edits_to_original_dialogue_invalidate_the_history_even_if_memory_is_unchanged(history):
    concepts, source_ids, kwargs = make_arc(history, "aiko")
    source = NS(id=10, role="assistant", session_id="s", content="An explicit self observation")
    db = history[0]
    reader = NS(kv_get=db.kv_get, kv_set=db.kv_set, get_message_row=lambda _: source)
    history[1].get(source_ids[-1]).source_message_id = source.id
    event = history[3].list(limit=1)[0]
    record_learning_sources(reader, event, history[2], history[1], None)
    assert understanding_arc(concepts[-1], **{**kwargs, "db": reader})["status"] == "supported"
    source.content = "I retract the earlier observation."
    assert understanding_arc(concepts[-1], **{**kwargs, "db": reader})["status"] == "thin_record"
