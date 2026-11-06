from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.knowledge_gap_extractor import KnowledgeGapStore
from app.core.memory.memory_origin import memory_origin
from app.core.memory.memory_store import MemoryStore
from app.core.session.memory_facade_mixin import MemoryFacadeMixin
from app.web.rest.memory_world_routes import register


@pytest.fixture
def setup_gap(tmp_path):
    path = tmp_path / "gaps.db"
    ChatDatabase(path)
    store = MemoryStore(path)
    embedder = SimpleNamespace(embed=lambda text: np.array([1., 0.]))
    gaps = KnowledgeGapStore(store, embedder)
    gap = gaps.add_gap(topic="astronomy", question="What makes the sky blue?")
    host = SimpleNamespace(_memory_store=store, _knowledge_gap_store=gaps, _embedder=embedder,
                           session_key="s", _notify_memory_added=lambda row: None,
                           _notify_knowledge_gap=lambda row: None)
    try:
        yield host, store, gap
    finally:
        store.close()


def test_manual_world_answer_retains_context_without_becoming_a_user_fact(setup_gap):
    host, store, gap = setup_gap
    result = MemoryFacadeMixin.resolve_knowledge_gap(host, gap.id, answer="Rayleigh scattering")
    answer = store.get(result["metadata"]["resolved_by_memory_id"])
    assert "What makes the sky blue?" in answer.content
    assert "Rayleigh scattering" in answer.content
    assert answer.metadata["resolved_gap_id"] == gap.id
    assert memory_origin(answer).subject == "unknown"
    assert answer.provenance == "stated"
    assert answer.event_time is None


def test_deliberate_blank_dismissal_does_not_need_an_embedder(setup_gap):
    host, store, gap = setup_gap
    host._embedder = None
    result = MemoryFacadeMixin.resolve_knowledge_gap(host, gap.id, answer=" ")
    assert result["metadata"]["resolved_by"] == "manual_dismissal"
    assert store.count() == 1


@pytest.mark.parametrize("failure", ["offline", "exception", "no_write"])
def test_answer_failure_keeps_gap_open(setup_gap, failure):
    host, store, gap = setup_gap
    def boom(text):
        raise OSError("offline")
    host._embedder = None if failure == "offline" else SimpleNamespace(embed=boom)
    with patch.object(store, "add", return_value=None):
        if failure == "no_write":
            host._embedder = SimpleNamespace(embed=lambda text: np.array([1., 0.]))
        with pytest.raises(RuntimeError, match="remains open"):
            MemoryFacadeMixin.resolve_knowledge_gap(host, gap.id, answer="An answer")
    assert not store.get(gap.id).metadata.get("resolved_at")
    assert store.count() == 1


def test_invalid_or_non_gap_id_cannot_create_an_orphan_answer(setup_gap):
    host, store, gap = setup_gap
    assert MemoryFacadeMixin.resolve_knowledge_gap(host, 999, answer="An answer") is None
    other = store.add("A different memory", "fact", np.array([0., 1.]), skip_dedupe=True)
    assert MemoryFacadeMixin.resolve_knowledge_gap(host, other.id, answer="An answer") is None
    assert store.count() == 2


def test_edited_question_during_embedding_does_not_receive_the_old_answer(setup_gap):
    host, store, gap = setup_gap
    def embed(text):
        store.update(gap.id, metadata={"question": "A changed question"}, metadata_merge=True)
        return np.array([1., 0.])
    host._embedder = SimpleNamespace(embed=embed)
    with pytest.raises(RuntimeError, match="question changed"):
        MemoryFacadeMixin.resolve_knowledge_gap(host, gap.id, answer="An old answer")
    assert store.count() == 1
    assert not store.get(gap.id).metadata.get("resolved_at")


def test_rest_reports_answer_save_failure_as_retryable():
    app, session = FastAPI(), MagicMock()
    session.resolve_knowledge_gap.side_effect = RuntimeError("Answer could not be saved")
    register(app, session, MagicMock(), MagicMock(), MagicMock())
    with TestClient(app) as client:
        response = client.post("/api/knowledge-gaps/1/resolve", json={"answer": "An answer"})
    assert response.status_code == 503
    assert "could not be saved" in response.json()["detail"]
