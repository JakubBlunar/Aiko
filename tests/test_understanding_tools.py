"""New brain reads expose scoped interpretations and historical understanding."""
from datetime import timedelta
import json

import numpy as np
import pytest

from app.core.concepts.concept_learning_event_store import ConceptLearningEventStore, LearningEvent
from app.core.concepts.concept_store import Concept, ConceptStore
from app.core.concepts.evolution_diary_store import DiaryEntry, EvolutionDiaryStore
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.relationship.belief_store import BeliefStore
from app.core.session.understanding_reads_mixin import UnderstandingReadsMixin
from app.llm.tools.base import ToolRegistry
from app.llm.tools.belief_reads import RecallBeliefsTool
from app.llm.tools.builtins import RecallSelfHistoryTool


@pytest.fixture
def env(tmp_path):
    db = ChatDatabase(tmp_path / "understanding.db")
    host = UnderstandingReadsMixin()
    host._user_id, host.session_key, host._chat_db = "u", "u:s", db
    host._belief_store = BeliefStore(db)
    host._concept_store = ConceptStore(db)
    host._concept_learning_store = ConceptLearningEventStore(db)
    host._evolution_diary_store = EvolutionDiaryStore(db)
    registry = ToolRegistry()
    registry.register(RecallBeliefsTool(host.recall_beliefs))
    registry.register(RecallSelfHistoryTool(None, host.understanding_history))
    return host, db, registry


def belief(host, db, *, topic="Tokyo trip", session="u:s", user="u", hours=1):
    now = timephrase.utcnow()
    text = "I am excited about the Tokyo trip, but unsure how much to plan."
    mid = db.add_message(session, "user", text)
    return host._belief_store.upsert(
        user_id=user, kind="mood", topic=topic,
        predicted_state="They may want shared planning while the amount remains uncertain.",
        source="worker", source_message_id=mid, topic_embedding=np.array([1., 0.]),
        metadata={"interpretation_version": 2, "session_key": session, "scope": "situation",
                  "expires_at": (now + timedelta(hours=hours)).isoformat(),
                  "situation": "Discussing the Tokyo trip", "reason": "They mentioned excitement",
                  "uncertainty": "Excitement alone does not request help",
                  "revise_when": "They decline planning together",
                  "evidence": [{"message_id": mid, "quote": "excited about the Tokyo trip"}]})


def history(host, *, owner="u", subject="relationship", label="Quiet company mattered"):
    at = (timephrase.utcnow() - timedelta(days=30)).isoformat()
    cid = host._concept_store.add(Concept(label, subject=subject, user_id=owner,
        status="active", embedding=np.array([1., 0.]), first_evidence_at=at))
    eid = host._concept_learning_store.add(LearningEvent(
        shape="emergence", concept_id=cid, subject=subject, new_label=label,
        because="The recorded exchanges changed my interpretation.", created_at=at,
        fingerprint=f"event-{cid}"))
    did = host._evolution_diary_store.add(DiaryEntry(
        entry="My understanding of quiet company changed during the recorded period.",
        period_start=at, period_end=at, concept_ids=(cid,), learning_event_ids=(eid,)))
    return cid, eid, did, at


def test_current_interpretation_preserves_uncertainty_expiry_and_source_context(env):
    host, db, registry = env
    row = belief(host, db)
    result = registry.dispatch("recall_beliefs", {"query": "Tokyo trip"})
    assert result.ok
    payload = json.loads(result.content)
    item = payload["beliefs"][0]
    assert item["belief_id"] == row.id and "does not request" in item["uncertainty"]
    assert item["expires_at"] == row.metadata["expires_at"]
    assert "unsure how much" in item["evidence"][0]["original_text"]
    assert not item["evidence"][0]["full_revision_tracked"]
    db.execute_commit("UPDATE messages SET content=? WHERE id=?",
                      ("I retract that", row.source_message_id))
    payload = json.loads(registry.dispatch("recall_beliefs", {"query": "Tokyo trip"}).content)
    assert payload["beliefs"] == [] and payload["withheld"] == 1


def test_expired_foreign_and_other_session_readings_are_not_current_context(env):
    host, db, registry = env
    own = belief(host, db)
    belief(host, db, topic="Tokyo budget", hours=-1)
    belief(host, db, topic="Tokyo flight", session="u:elsewhere")
    belief(host, db, topic="Tokyo hotel", user="other", session="other:s")
    payload = json.loads(registry.dispatch("recall_beliefs", {"query": "Tokyo trip"}).content)
    assert [item["belief_id"] for item in payload["beliefs"]] == [own.id]


def test_relationship_timeline_and_exact_change_diary_remain_distinct(env):
    host, _, registry = env
    cid, eid, did, at = history(host)
    payload = json.loads(registry.dispatch("recall_self_history", {
        "subject": "relationship", "query": "Quiet company", "concept_id": cid,
    }).content)
    assert payload["subject"] == "relationship" and payload["thin_record"]
    assert payload["eras"][0]["entries"][0]["at"] == at
    page = json.loads(registry.dispatch("recall_self_history", {
        "mode": "diary", "subject": "relationship",
    }).content)
    assert page["entries"][0]["entry_id"] == did
    paragraph = json.loads(registry.dispatch("recall_self_history", {
        "mode": "diary_entry", "entry_id": did,
    }).content)
    assert paragraph["record_type"] == "evolution_diary"
    assert paragraph["record"]["period_start"] == at
    assert paragraph["record"]["learning_event_ids"] == [eid]
    event = json.loads(registry.dispatch("recall_self_history", {
        "mode": "learning_event", "event_id": eid,
    }).content)
    assert event["record"]["created_at"] == at
    assert "interpretation" in event["record"]["because"]


def test_history_owner_filters_precede_output_and_exact_id_reads(env):
    host, _, registry = env
    own, _, _, _ = history(host)
    foreign, event, entry, _ = history(host, owner="other", label="Private history")
    timeline = json.loads(registry.dispatch("recall_self_history", {
        "subject": "relationship",
    }).content)
    ids = [item["concept_id"] for era in timeline["eras"] for item in era["entries"]]
    assert ids == [own] and foreign not in ids
    for args in ({"mode": "learning_event", "event_id": event},
                 {"mode": "diary_entry", "entry_id": entry}):
        payload = json.loads(registry.dispatch("recall_self_history", args).content)
        assert payload["status"] == "not_found" and "record" not in payload


def test_unavailable_history_and_interpretations_do_not_look_like_empty_records(env):
    host, _, registry = env
    host._belief_store = None
    assert not registry.dispatch("recall_beliefs", {"query": "Tokyo"}).ok
    host._concept_store = None
    assert not registry.dispatch("recall_self_history", {"subject": "relationship"}).ok


def test_exact_history_ids_and_paging_arguments_are_validated_by_action(env):
    _, _, registry = env
    for args in ({"mode": "diary_entry"}, {"mode": "learning_event"},
                 {"mode": "diary", "entry_id": 1}, {"content_offset": 2},
                 {"mode": "diary_entry", "entry_id": True}):
        assert not registry.dispatch("recall_self_history", args).ok


def test_long_historical_paragraph_retains_its_final_qualification(env):
    host, _, registry = env
    cid, eid, _, at = history(host)
    body = "An earlier interpretation. " * 300 + "It did not establish a change in the user."
    did = host._evolution_diary_store.add(DiaryEntry(
        entry=body, period_start=at, period_end=at, concept_ids=(cid,), learning_event_ids=(eid,)))
    first = json.loads(registry.dispatch("recall_self_history", {
        "mode": "diary_entry", "entry_id": did,
    }).content)
    assert first["is_fragment"] and first["record"] is None
    second = json.loads(registry.dispatch("recall_self_history", {
        "mode": "diary_entry", "entry_id": did, "content_offset": first["next_offset"],
        "revision": first["revision"],
    }).content)
    assert json.loads(first["details_fragment"] + second["details_fragment"])["entry"] == body
