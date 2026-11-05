import json
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore
from app.core.memory.memory_integrity import conflict_scope_compatible
from app.core.memory.memory_relations import (
    admit_relation, incoming_relations, reference_revision, relation_current, review_state,
)


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "contexts.db"
    db = ChatDatabase(path)
    mid = db.add_message("s", "user", "I prefer brief replies when tired.")
    store = MemoryStore(path)
    prior = store.add("Noah prefers brief replies when tired", "preference", np.ones(4),
                      source_message_id=mid, temporal_type="preference")
    yield db, store, prior
    store.close()


def relation(setup, text="I prefer detailed replies during debugging.", kind="different_context",
             role="user", offered=True):
    db, store, prior = setup
    mid = db.add_message("s", role, text)
    value = {"prior_memory_id": prior.id, "relation": kind,
             "report": {"message_id": mid, "quote": text}}
    choices = {prior.id: {"revision": reference_revision(prior)}} if offered else {}
    return admit_relation(value, [db.get_message_row(mid)], choices, store,
                          session_key="s", subject="user")


def save(setup, value, content="Noah prefers detailed replies during debugging"):
    _, store, _ = setup
    return store.add(content, "preference", np.ones(4),
                     source_message_id=value["report"]["message_id"],
                     metadata={"contextual_relation": value}, temporal_type="preference",
                     skip_dedupe=True)


def test_different_context_and_counterexample_preserve_prior_without_global_reversal(setup):
    _, store, prior = setup
    value = relation(setup)
    saved = save(setup, value)
    assert relation_current(value, store)
    assert incoming_relations(prior, store)[0][0]["memory_id"] == saved.id
    assert not review_state(prior, store)["review_required"]
    assert prior.content == "Noah prefers brief replies when tired"
    assert not prior.metadata.get("superseded_by")
    example = relation(setup, "I prefer brief replies, but this time detailed replies helped.",
                       "counterexample")
    save(setup, example, "Noah found detailed replies useful on a particular occasion")
    assert not review_state(prior, store)["review_required"]


def test_explicit_correction_requests_review_instead_of_rewriting_history(setup):
    _, store, prior = setup
    value = relation(setup, "I was wrong about preferring brief replies when tired.", "correction")
    save(setup, value)
    assert review_state(prior, store)["review_required"]
    assert prior.content == "Noah prefers brief replies when tired"
    assert prior.metadata.get("belief_revision") is None


def test_explicit_changed_preference_defers_the_linked_basis_only(setup):
    _, store, prior = setup
    value = relation(setup, "I now prefer detailed replies when tired.", "changed_preference")
    save(setup, value, "Noah now prefers detailed replies when tired")
    assert review_state(prior, store)["review_required"]
    other = store.add("Noah prefers brief replies while travelling", "preference", np.ones(4),
                      skip_dedupe=True)
    assert not review_state(other, store)["review_required"]
    assert store.get(prior.id).content == "Noah prefers brief replies when tired"


@pytest.mark.parametrize("kwargs", [
    {"offered": False}, {"role": "assistant"},
    {"text": "I prefer detailed replies during debugging.", "kind": "correction"},
    {"text": "I was wrong about river drawings.", "kind": "correction"},
    {"text": "Imagine I prefer detailed replies during debugging."},
])
def test_unoffered_wrong_actor_unstated_change_and_topic_only_link_are_refused(setup, kwargs):
    with pytest.raises(ValueError):
        relation(setup, **kwargs)


def test_changed_original_or_report_invalidates_the_relation(setup):
    db, store, prior = setup
    value = relation(setup)
    save(setup, value)
    db.execute_commit("UPDATE messages SET content=? WHERE id=?",
                      ("A corrected utterance", value["report"]["message_id"]))
    assert not relation_current(value, store)
    assert not incoming_relations(prior, store)[0][0]["current"]


def test_context_qualifiers_prevent_false_contradictions():
    def memory(quote):
        return SimpleNamespace(kind="preference", content="A contextual preference",
                               temporal_type="preference", metadata={"observation": {
                                   "version": 1, "context": {"quote": quote}, "exceptions": [],
                               }})
    assert not conflict_scope_compatible(memory("when tired"), memory("during debugging"))
    assert conflict_scope_compatible(memory("when tired"), memory("when tired"))


def test_extractor_offers_reference_ids_and_retains_both_contexts(setup):
    from app.core.memory.memory_extractor import MemoryExtractor
    from tests.test_memory_extractor_watermark import _FakeOllama, _FakeEmbedder

    db, store, prior = setup
    text = "I prefer detailed replies during debugging."
    mid = db.add_message("s", "user", text)
    candidate = {"content": "Noah prefers detailed replies during debugging", "kind": "preference",
                 "temporal_type": "preference", "evidence": [{"message_id": mid, "quote": text}],
                 "supersedes_memory_id": prior.id,
                 "contextual_relation": {"prior_memory_id": prior.id,
                                         "relation": "different_context",
                                         "report": {"message_id": mid, "quote": text}}}
    model = _FakeOllama([json.dumps({"memories": [candidate]})])
    worker = MemoryExtractor(db, store, _FakeEmbedder(), model, model="fake", min_window_messages=2,
                             context_window=lambda: 16384)
    assert worker.extract_for_session("s") == 1
    assert f"[memory:{prior.id}]" in model.prompts[0]
    newer = store.list_recent()[0]
    assert newer.id != prior.id and store.get(prior.id) is not None
    assert newer.metadata["admission"]["supersedes_memory_id"] is None


def test_context_links_use_their_index(setup):
    db, _, prior = setup
    rows = db.execute_fetchall("EXPLAIN QUERY PLAN SELECT id FROM memories "
        "WHERE json_valid(metadata) AND "
        "json_extract(metadata, '$.contextual_relation.prior_memory_id')=?", (prior.id,))
    assert any("idx_memories_contextual_relation" in str(row) for row in rows)


def test_dependency_reader_defers_a_corrected_basis_without_marking_it_false(setup):
    from app.core.concepts.concept_store import Concept, ConceptStore

    db, _, prior = setup
    concepts = ConceptStore(db)
    concept = Concept(label="A reading about brief replies", kind="identity", subject="user",
                      status="active", confidence=.8, embedding=np.ones(4))
    concepts.add(concept)
    concepts.dependencies.add(concept.concept_id, [[("memory", str(prior.id))]])
    assert concepts.is_assertable(concept.concept_id)
    corrected = relation(setup, "I was wrong about preferring brief replies when tired.",
                         "correction")
    save(setup, corrected)
    assert not concepts.is_assertable(concept.concept_id)
    assert concept.status == "active"  # Lifecycle owns status; this is a review gate.
    assert prior.content == "Noah prefers brief replies when tired"
