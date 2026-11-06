from types import SimpleNamespace
from datetime import timedelta

import numpy as np
import pytest

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_origin import memory_origin
from app.core.memory.memory_store import MemoryStore
from app.core.memory.observation_receipts import longitudinal_sources_current
from app.core.memory.pursuit_notes import PursuitNoteWriter
from app.core.rag.rag_retriever import RagRetriever


AT = timephrase.parse_iso("2026-11-05T10:00:00Z")


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "pursuits.db"
    ChatDatabase(path)
    value = MemoryStore(path)
    try:
        yield value
    finally:
        value.close()


def writer(store, embed=None):
    return PursuitNoteWriter(store, SimpleNamespace(embed=embed or (
        lambda text: np.array([1., 0.]))))


def test_distinct_virtual_visits_have_distinct_times_and_same_episode_replay_is_skipped(store):
    notes = writer(store)
    a = notes.write("Watered the virtual garden.", source="away_beat", topic="garden", at=AT)
    b = notes.write("Watered the virtual garden.", source="away_beat", topic="garden",
                    at=AT + timedelta(days=1))
    assert a != b and a is not None and b is not None
    assert notes.write("Watered it again, differently phrased.", source="away_beat",
                       topic="garden", at=AT) is None
    assert store.count() == 2
    assert store.get(a).event_time != store.get(b).event_time
    assert memory_origin(store.get(a)).domain == "virtual"


def test_imagined_and_simulated_clauses_retain_their_modes_and_effect_receipts(store):
    mid = writer(store).write(
        "Imagined a cafe and then watered the virtual lettuce.", source="away_beat", at=AT,
        extra={"keys": ["outing", "garden"], "clauses": [
            {"activity": "outing", "mode": "imagined", "text": "Imagined a cafe"},
            {"activity": "garden", "mode": "simulated", "text": "Watered the lettuce"},
        ], "world_receipts": [{"item_id": 4, "effect": "watered"}]},
    )
    row = store.get(mid)
    receipt = row.metadata["pursuit_evidence"]
    assert [c["mode"] for c in receipt["clauses"]] == ["imagined", "simulated"]
    assert receipt["world_receipts"][0]["item_id"] == 4
    block = RagRetriever.format_block([RagRetriever._memory_to_hit(row, score=.8)])
    assert "domain=virtual" in block and "activity=virtual" in block
    assert "What you know about the user" not in block


def research(store):
    return store.add("A researched result about watering intervals.", "curiosity_finding",
                     np.array([0., 1.]), metadata={"research_evidence": {"sources": ["receipt"]}})


def test_research_wrapup_retains_source_revisions_without_claiming_physical_practice(store):
    source = research(store)
    mid = writer(store).write("Research thread closed with a recorded finding.",
                             source="hobby_wrapup", at=AT, extra={
                                 "progress_source": "research", "research_memory_ids": [source.id]})
    row = store.get(mid)
    assert memory_origin(row).mode == "reflection"
    assert memory_origin(row).domain == "conversation"
    assert row.metadata["pursuit_evidence"]["research_inputs"][0]["memory_id"] == source.id
    assert longitudinal_sources_current(row, store)
    store.update(source.id, content="A corrected research result", embedding=np.array([0., 1.]))
    assert not longitudinal_sources_current(row, store)


def test_research_change_during_embedding_defers_the_note(store):
    source = research(store)
    def embed(text):
        store.update(source.id, content="A corrected result", embedding=np.array([0., 1.]))
        return np.array([1., 0.])
    assert writer(store, embed).write("A reflection on the original research result.",
                                      source="hobby_milestone", at=AT, extra={
                                          "progress_source": "research",
                                          "research_memory_ids": [source.id]}) is None
    assert store.count() == 1


def test_unbacked_project_idea_remains_a_reflection_not_completed_activity(store):
    mid = writer(store).write("Left the project idea; no result-backed progress was recorded.",
                              source="hobby_wrapup", at=AT)
    row = store.get(mid)
    assert memory_origin(row).mode == "reflection"
    assert row.metadata["pursuit_evidence"]["activity_mode"] == "idea"


def test_long_note_is_refused_whole(store):
    assert writer(store).write("A long scene. " * 50 + "Only imagined, never physical.",
                               source="away_beat", at=AT) is None
    assert store.count() == 0
