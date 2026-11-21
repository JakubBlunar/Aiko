"""Isolated cross-topic evidence comparison through archive and concept recall."""

import json
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.concepts.concept_evidence_lineage import resolve_support
from app.core.concepts.concept_store import Concept, ConceptEdge, ConceptStore
from app.core.conversation.occasion_review import compare_accounts, reported_account, review_pair
from app.core.conversation.topic_evidence import topic_evidence_profile
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore
from app.core.rag.rag_retriever import RagRetriever
from app.llm.tools.builtins import RecallConceptTool
from app.llm.tools.conversation_history import ConversationHistoryTool


_VECTOR = np.array([1.0, 0.0], dtype=np.float32)
_FIRST = "On Tuesday, my bike broke down. The chain slipped near the bridge."
_VARIANTS = [
    ("It was that same Tuesday breakdown that made me late for work.", "same", 1),
    ("The bike broke down a second time on Friday and made me late for work.", "distinct", 2),
    ("The chain slipped. I was late for work.", None, None),
]


@pytest.fixture
def replay(tmp_path):
    path = tmp_path / "occasion-review.db"
    db = ChatDatabase(path)
    memories = MemoryStore(path)
    try:
        yield db, memories, ConceptStore(db)
    finally:
        memories.close()
        db._get_conn().close()


def _write_report(db, memories, topic, statement, *, role="user"):
    session = f"replay:{topic}"
    mid = db.add_message(session, role, statement)
    note = memories.add(
        statement, "fact", _VECTOR, source_message_id=mid,
        source_session=session, provenance="stated", temporal_type="past_event",
        skip_dedupe=True,
    )
    assert note is not None
    return note


@pytest.mark.parametrize(("later_text", "relation", "count"), _VARIANTS)
def test_retelling_keeps_both_topics_and_counts_only_reviewed_occasions(
    replay, later_text, relation, count,
):
    db, memories, store = replay
    first_note = _write_report(db, memories, "repair", _FIRST)
    later_note = _write_report(db, memories, "work", later_text)
    notes = [first_note, later_note]
    topics = [
        SimpleNamespace(representative_id=note.id, member_ids=[note.id]) for note in notes
    ]
    for topic in topics:
        assert topic_evidence_profile(topic, memories)["recorded_observation_groups"] == 1

    accounts = [
        reported_account(note, topic.representative_id, db)
        for topic, note in zip(topics, notes, strict=True)
    ]
    assert all(accounts)
    reviewed = review_pair(*accounts, relation) if relation else None
    comparison = compare_accounts(*accounts, reviewed)
    assert comparison["account_count"] == 2
    assert comparison["occasion_count"] == count
    assert comparison["accounts"][1].statement == later_text

    archive = ConversationHistoryTool(db, "replay")
    for account in comparison["accounts"]:
        result = json.loads(archive.run({"mode": "message", "message_id": account.message_id}))
        assert result["message"]["text"] == account.statement
        assert result["message"]["session_id"] == account.session_id
        assert result["message"]["created_at"] == account.recorded_at

    concept = Concept(
        user_id="replay", kind="narrative", subject="user", status="active",
        label="Bike commute notes", confidence=0.9, embedding=_VECTOR,
        evidence_count=2, distinct_source_count=2,
    )
    store.add(concept)
    nodes = [("memory", str(note.id)) for note in notes]
    for node in nodes:
        assert store.add_edge(ConceptEdge(*node, "concept", str(concept.concept_id), "evidence"))
    store.dependencies.add(concept.concept_id, [nodes])
    retriever = RagRetriever(None, None, memory_store=memories, chat_db=db)
    retriever.set_concept_store(store)
    result = json.loads(RecallConceptTool(retriever, "replay").run(
        {"concept_id": concept.concept_id},
    ))
    assert {item["memory_id"] for item in result["evidence"]} == {
        account.memory_id for account in comparison["accounts"]
    }
    assert resolve_support(store, memories, None, concept.concept_id)["known_support_groups"] == 2


def test_source_edit_expires_the_relationship_and_reopens_frequency(replay):
    db, memories, _ = replay
    note = _write_report(db, memories, "repair", _FIRST)
    later = _write_report(db, memories, "work", _VARIANTS[0][0])
    first_account = reported_account(note, 1, db)
    review = review_pair(first_account, reported_account(later, 2, db), "same")
    assert db.update_message_content(later.source_message_id, _VARIANTS[2][0])
    profile = compare_accounts(first_account, reported_account(later, 2, db), review)
    assert profile["review_stale"]
    assert profile["occasion_count"] is None
    assert [account.statement for account in profile["accounts"]] == [_FIRST, _VARIANTS[2][0]]
    assert memories.get(later.id).content == _VARIANTS[0][0]


def test_review_requires_two_distinct_user_reports(replay):
    db, memories, _ = replay
    note = _write_report(db, memories, "repair", _FIRST)
    assistant = _write_report(db, memories, "work", _VARIANTS[0][0], role="assistant")
    assert reported_account(assistant, 2, db) is None
    with pytest.raises(ValueError, match="two chronological accounts"):
        review_pair(reported_account(note, 1, db), reported_account(note, 2, db), "same")
