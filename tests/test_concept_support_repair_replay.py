"""Replay a reviewed diary premise and a separate, explicit agreement."""

import json
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.concepts.concept_evidence_lineage import resolve_support
from app.core.concepts.concept_store import Concept, ConceptEdge, ConceptStore
from app.core.concepts.concept_synthesis_worker import ConceptSynthesisWorker
from app.core.concepts.proposers.base import CandidateProposal
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore
from app.core.memory.worker_sources import current, receipt, transcript_packet
from app.core.rag.rag_retriever import RagRetriever
from app.llm.tools.builtins import RecallConceptTool


_AMBIGUOUS = (
    ("user", "I'm too tired to joke during this episode."),
    ("assistant", "Let's settle in and watch quietly."),
)
_EXPLICIT = (
    ("user", "Can we agree to pause the teasing until this episode ends?"),
    ("assistant", "Yes. Let's pause the teasing until the episode ends."),
)
_QUERY = "What agreement did we make about teasing during the show?"
_MATCH = np.array([1.0, 0.0], dtype=np.float32)
_ALTERNATIVE = np.array([0.8, 0.6], dtype=np.float32)


@pytest.fixture
def replay(tmp_path):
    path = tmp_path / "concept-repair.db"
    db = ChatDatabase(path)
    memories = MemoryStore(path)
    try:
        yield db, memories, ConceptStore(db)
    finally:
        memories.close()
        db._get_conn().close()


def _append(db, session, exchange):
    return [db.add_message(session, role, text) for role, text in exchange]


def _observations(db, memories):
    ambiguous, explicit = "replay:ambiguous", "replay:explicit"
    ambiguous_ids = _append(db, ambiguous, _AMBIGUOUS)
    explicit_ids = _append(db, explicit, _EXPLICIT)
    packet = transcript_packet(
        [db.get_message_row(mid) for mid in ambiguous_ids], session_key=ambiguous,
    )
    diary = memories.add(
        "We agreed to pause teasing whenever we watch together.", "diary", _MATCH,
        source_session=ambiguous, metadata={"reflective_derivation": receipt(packet)},
        temporal_type="past_event", confidence=0.9, skip_dedupe=True,
    )
    explicit_note = memories.add(
        "Both agreed to pause teasing until the end of that episode.", "fact", _MATCH,
        source_session=explicit, source_message_id=explicit_ids[0],
        metadata={"source_message_ids": explicit_ids},
        provenance="stated", confidence=0.9, skip_dedupe=True,
    )
    assert diary is not None and explicit_note is not None
    return diary, explicit_note, ambiguous_ids, explicit_ids, packet


def _concept(store, label, vector, *alternatives):
    row = Concept(
        user_id="replay", label=label, status="active", confidence=0.9, kind="boundary",
        subject="aiko", embedding=vector, evidence_count=len(alternatives),
        distinct_source_count=len(alternatives),
    )
    store.add(row)
    concept_id = str(row.concept_id)
    nodes = []
    for memory in alternatives:
        node = ("memory", str(memory.id))
        assert store.add_edge(ConceptEdge(*node, "concept", concept_id, "evidence"))
        nodes.append([node])
    store.dependencies.add(row.concept_id, nodes)
    return row


def _recall(db, memories, store):
    retriever = RagRetriever(
        None, SimpleNamespace(embed=lambda _: _MATCH), memory_store=memories, chat_db=db,
    )
    retriever.set_concept_store(store)
    return RecallConceptTool(retriever, user_id="replay")


def test_review_withdraws_diary_premise_and_recall_uses_the_explicit_alternative(replay):
    db, memories, store = replay
    diary, explicit, ambiguous_ids, explicit_ids, packet = _observations(db, memories)
    overbroad = _concept(store, diary.content, _MATCH, diary)
    scoped = _concept(store, explicit.content, _ALTERNATIVE, diary, explicit)
    recall = _recall(db, memories, store)

    assert current(packet, db)
    assert store.is_assertable(overbroad.concept_id)
    assert store.is_assertable(scoped.concept_id)
    original = json.loads(recall.run({"query": _QUERY}))
    assert original["concept"]["concept_id"] == overbroad.concept_id
    assert original["evidence"][0]["authority"] == "model_reflection"

    diary_node = ("memory", str(diary.id))
    for row in (overbroad, scoped):
        store.dependencies.withdraw_premise(row.concept_id, diary_node)
        store.dependencies.admit(row, [diary_node])

    assert not store.is_assertable(overbroad.concept_id)
    assert store.is_assertable(scoped.concept_id)
    report = store.dependencies.reconsider(overbroad.concept_id)
    assert report["invalid_sets"][0]["failures"][0]["reason"] == "reviewed_premise_withdrawn"

    reloaded = ConceptStore(db)
    reloaded.load_all()
    worker = ConceptSynthesisWorker(
        concept_store=reloaded, topic_graph=None, memory_store=memories, embedder=None,
        ollama=None, chat_model="unused", cancel_event=Event(), agent_settings=None,
        memory_settings=None, kv_get=db.kv_get, kv_set=db.kv_set,
    )
    stats = {"reinforced": 0}
    worker._reinforce(reloaded.get(overbroad.concept_id), CandidateProposal(
        label="A general agreement to stop teasing on viewing nights",
        rationale="A reinterpretation of the reviewed diary", confidence=0.99,
        evidence=[diary_node], kind="boundary", subject="aiko",
        reinforces_id=overbroad.concept_id,
    ), stats)
    assert stats["reinforced"] == 0
    for view in (recall, _recall(db, memories, reloaded)):
        result = json.loads(view.run({"query": _QUERY}))
        assert result["concept"]["concept_id"] == scoped.concept_id
        assert result["support_validity"]["viable"]
        assert [row["memory_id"] for row in result["evidence"]] == [explicit.id]
        earlier = json.loads(view.run({"concept_id": overbroad.concept_id}))
        assert earlier["concept"]["concept_id"] == overbroad.concept_id
        assert not earlier["support_validity"]["viable"]
        assert [row["memory_id"] for row in earlier["evidence"]] == [diary.id]

    for ids, exchange in ((ambiguous_ids, _AMBIGUOUS), (explicit_ids, _EXPLICIT)):
        rows = [db.get_message_row(mid) for mid in ids]
        assert tuple((row.role, row.content) for row in rows) == exchange
    assert memories.get(diary.id).content == diary.content
    assert current(packet, db)


def test_premise_withdrawal_survives_concept_merge_and_is_local_to_the_claim(replay):
    db, memories, store = replay
    diary, explicit, _, _, _ = _observations(db, memories)
    rejected = _concept(store, "We always pause teasing when watching", _MATCH, diary)
    surviving = _concept(store, "We agreed to pause teasing for an episode", _MATCH, explicit)
    unrelated = _concept(store, "I reflected on watching quietly", _MATCH, diary)
    node = ("memory", str(diary.id))
    with pytest.raises(ValueError, match="existing premise"):
        store.dependencies.withdraw_premise(rejected.concept_id, ("memory", explicit.id))
    store.dependencies.withdraw_premise(rejected.concept_id, node)
    assert store.merge_into(canonical_id=surviving.concept_id, absorbed_id=rejected.concept_id)
    reloaded = ConceptStore(db)
    reloaded.load_all()
    assert reloaded.dependencies.withdrawn_premises(surviving.concept_id) == {node}
    report = reloaded.dependencies.assess(surviving.concept_id)
    assert report["viable"]
    assert report["invalid_sets"][0]["failures"][0]["reason"] == "reviewed_premise_withdrawn"
    assert reloaded.is_assertable(unrelated.concept_id)


def test_paraphrases_keep_one_observation_group_and_diary_adds_no_vote(replay):
    db, memories, store = replay
    diary, explicit, ambiguous_ids, _, _ = _observations(db, memories)
    copies = [
        memories.add(
            content, "fact", _MATCH, source_session="replay:ambiguous",
            source_message_id=ambiguous_ids[0], provenance="stated", skip_dedupe=True,
        )
        for content in ("User was too tired to joke.", "User lacked energy for jokes.")
    ]
    assert all(copies)
    same_episode = _concept(store, "Energy shapes playfulness", _MATCH, diary, *copies)
    with_contrast = _concept(
        store, "There are different reasons to pause teasing", _ALTERNATIVE,
        diary, *copies, explicit,
    )
    repeated = resolve_support(store, memories, None, same_episode.concept_id)
    separate = resolve_support(store, memories, None, with_contrast.concept_id)
    assert repeated["known_support_groups"] == 1
    assert repeated["excluded_reflections"] == 1
    assert separate["known_support_groups"] == 2
