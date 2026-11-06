"""Brain returns keep source meaning, uncertainty and inspectable identities."""
from dataclasses import asdict
import json
from types import SimpleNamespace as NS

import numpy as np
import pytest

from app.core.concepts.concept_store import Concept, ConceptStore
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_origin import MemoryOrigin
from app.core.memory.memory_store import MemoryStore
from app.core.rag.rag_retriever import RagRetriever
from app.core.session.memory_reads_mixin import MemoryReadsMixin
from app.llm.tools.base import ToolRegistry
from app.llm.tools.builtins import RecallConceptTool, RecallTool, RecallTopicTool
from app.llm.tools.memory_inspection import InspectMemoryTool


@pytest.fixture
def env(tmp_path):
    db = ChatDatabase(tmp_path / "recall.db")
    store = MemoryStore(tmp_path / "recall.db")
    session = NS(_memory_store=store, _chat_db=db, _user_id="u")
    registry = ToolRegistry()
    registry.register(InspectMemoryTool(
        lambda **kw: MemoryReadsMixin.inspect_memory(session, **kw)))
    try:
        yield db, store, session, registry
    finally:
        store.close()


def save(store, text, **kw):
    return store.add(text, "event", np.array([1., 0.]), source_session="u:s",
                     skip_dedupe=True, **kw)


def retriever(store, db, rows, *, embed=None):
    vectors = NS(search_memories=lambda *a, **kw: [
        RagRetriever._memory_to_hit(row, score=.9) for row in rows],
        search_messages=lambda *a, **kw: [], search_documents=lambda *a, **kw: [])
    graph = NS(best_clusters_for=lambda *a, **kw: [(1, "A topic", .9)],
               cluster_member_ids=lambda cid: [row.id for row in rows], topic_clusters=lambda: [])
    return RagRetriever(vectors, NS(embed=embed or (lambda text: np.array([1., 0.]))),
                        memory_store=store, chat_db=db, topic_graph=graph,
                        include_messages=False, include_documents=False,
                        topic_expansion_enabled=False, cluster_diversity_enabled=False)


def test_whole_virtual_memory_and_dates_survive_both_fast_reads(env):
    db, store, _, _ = env
    text = "Imagining a scene in detail. " * 25 + "It did not happen physically."
    row = save(store, text, metadata={"memory_origin": asdict(MemoryOrigin(
        "aiko", "virtual_event", "test", "virtual"))})
    rag = retriever(store, db, [row])
    for tool, args in ((RecallTool(rag, "u"), {"query": "scene"}),
                       (RecallTopicTool(rag, "u"), {"topic": "scene"})):
        item = json.loads(tool.run(args))["hits"][0]
        assert item["text"] == text and item["text_complete"]
        assert item["subject"] == "aiko" and item["domain"] == "virtual"
        assert item["memory_id"] == row.id and item["created_at"] == row.created_at
        assert item["event_time"] is None
    assert store.get(row.id).use_count == 0


def test_long_content_is_omitted_then_read_in_revision_bound_pages(env):
    db, store, _, registry = env
    text = "A possible scene. " * 450 + "Except this was declined."
    row = save(store, text)
    payload = json.loads(RecallTool(retriever(store, db, [row]), "u").run({"query": "scene"}))
    assert payload["hits"][0]["text"] is None
    assert payload["hits"][0]["inspection_required"]
    first = registry.dispatch("inspect_memory", {"memory_id": row.id})
    page = json.loads(first.content)["memory"]
    assert page["is_fragment"] and not page["text_complete"]
    second = registry.dispatch("inspect_memory", {
        "memory_id": row.id, "content_offset": page["next_offset"], "revision": page["revision"],
    })
    assert page["text"] + json.loads(second.content)["memory"]["text"] == text
    store.update(row.id, content="The revised account", embedding=np.array([1., 0.]))
    assert not registry.dispatch("inspect_memory", {
        "memory_id": row.id, "content_offset": page["next_offset"], "revision": page["revision"],
    }).ok


def test_topic_paging_withdrawal_and_source_revision(env):
    db, store, _, _ = env
    rows = [save(store, f"Distinct record {i} about a musical scene") for i in range(4)]
    store.update(rows[0].id, metadata={"superseded_by": rows[1].id}, metadata_merge=True)
    tool = RecallTopicTool(retriever(store, db, rows), "u")
    first = json.loads(tool.run({"topic": "scene", "limit": 1}))
    assert first["total"] == 3 and first["withheld"] == 1 and first["has_more"]
    second = json.loads(tool.run({"topic": "scene", "limit": 1,
                                 "offset": first["next_offset"], "revision": first["revision"]}))
    assert first["hits"][0]["memory_id"] != second["hits"][0]["memory_id"]
    historical = json.loads(tool.run({"topic": "scene", "include_historical": True}))
    assert historical["hits"][0]["source_state"] == "withdrawn"
    store.update(rows[2].id, content="An edited interpretation", embedding=np.array([1., 0.]))
    from app.llm.tools.base import ToolError
    with pytest.raises(ToolError, match="restart"):
        tool.run({"topic": "scene", "offset": 1, "revision": first["revision"]})


def test_inspection_returns_originals_and_rechecks_edited_sources(env):
    db, store, _, registry = env
    mid = db.add_message("u:s", "user", "I imagined it, but did not do it.")
    row = save(store, "The user imagined the scene.", source_message_id=mid,
               metadata={"admission": {"evidence": [{"message_id": mid,
                         "quote": "I imagined it, but did not do it."}]}})
    payload = json.loads(registry.dispatch("inspect_memory", {"memory_id": row.id}).content)
    assert payload["original_messages"][0]["text"] == "I imagined it, but did not do it."
    db.execute_commit("UPDATE messages SET content=? WHERE id=?", ("I retract that", mid))
    payload = json.loads(registry.dispatch("inspect_memory", {"memory_id": row.id}).content)
    assert payload["memory"]["source_state"] == "changed"
    topic = RecallTopicTool(retriever(store, db, [row]), "u")
    assert json.loads(topic.run({"topic": "scene"}))["hits"] == []


def test_exact_memory_inspection_is_user_scoped(env):
    db, store, _, registry = env
    row = store.add("Another user's private account", "fact", np.array([1., 0.]),
                    source_session="other:s")
    assert json.loads(registry.dispatch("inspect_memory", {"memory_id": row.id}).content)[
        "status"] == "not_found"
    assert json.loads(RecallTool(retriever(store, db, [row]), "u").run({"query": "account"}))[
        "hits"] == []


def test_lookup_failure_does_not_claim_absence_and_other_sources_survive(env):
    db, store, _, _ = env
    def unavailable(text):
        raise RuntimeError("offline")
    rag = retriever(store, db, [], embed=unavailable)
    registry = ToolRegistry()
    for tool, args in ((RecallTool(rag, "u"), {"query": "x"}),
                       (RecallTopicTool(rag, "u"), {"topic": "x"})):
        registry.register(tool)
        assert not registry.dispatch(tool.schema().name, args).ok
    row = save(store, "A source that remains readable")
    rag = retriever(store, db, [row])
    rag._include_documents = True
    rag._store.search_documents = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("offline"))
    payload = json.loads(RecallTool(rag, "u").run({"query": "source"}))
    assert payload["status"] == "partial" and payload["hits"]


def test_concept_subject_and_exact_id_with_no_embedding_call(env):
    db, store, _, _ = env
    concepts = ConceptStore(db)
    ids = [concepts.add(Concept("Enjoys music", subject=subject, status="active",
                               embedding=np.array([1., 0.]))) for subject in ("aiko", "user")]
    rag = retriever(store, db, [])
    rag.set_concept_store(concepts)
    tool = RecallConceptTool(rag, "u")
    payload = json.loads(tool.run({"query": "music", "subject": "user"}))
    assert payload["concept"]["concept_id"] == ids[1]
    rag._embedder.embed = lambda text: (_ for _ in ()).throw(AssertionError("no model needed"))
    payload = json.loads(tool.run({"concept_id": ids[0]}))
    assert payload["concept"]["subject"] == "aiko" and payload["score"] is None


def test_failed_memory_hydration_preserves_available_message_results(env):
    from app.core.rag.rag_store import MessageRecord, RagHit

    db, store, _, _ = env
    mid = db.add_message("u:s", "user", "A source that remains readable.")
    row = save(store, "A memory whose source join becomes unavailable")
    memory_hit = RagRetriever._memory_to_hit(row, score=.9)
    message_hit = RagHit("message", .8, MessageRecord(
        "u:s:1", "u:s", mid, "user", "A source that remains readable.", row.created_at))
    rag = retriever(store, db, [])
    rag.retrieve = lambda *a, **kw: [memory_hit, message_hit]
    def unavailable(mid):
        raise RuntimeError("memory mirror unavailable")
    rag._memory_store = NS(get=unavailable)
    payload = json.loads(RecallTool(rag, "u").run({"query": "source"}))
    assert payload["status"] == "partial"
    assert payload["hits"][0]["source"] == "message"
    assert payload["unavailable_sources"] == ["memory"]
