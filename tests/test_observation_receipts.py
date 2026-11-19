from types import SimpleNamespace
import json

import numpy as np
import pytest

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore
from app.core.memory.memory_admission import admit_memory, admit_source_note
from app.core.memory.memory_origin import origin_annotation
from app.core.memory.observation_receipts import longitudinal_sources_current


TEXT = "Shorter replies helped when I was tired, except during debugging."
NEED = "Teasing is hard for me to recognize while concentrating, except teasing about games."
RESPONSE = "Please give me a heads-up before teasing."


def row(text=TEXT, *, role="user", mid=1):
    return SimpleNamespace(id=mid, session_id="s", role=role, content=text,
                           created_at="2026-11-05T10:00:00Z")


def candidate():
    def cite(quote):
        return {"message_id": 1, "quote": quote}
    return {"content": "Noah benefits from shorter replies when tired except during debugging.",
            "kind": "preference", "evidence": [cite(TEXT)], "observation": {
                "context": cite("when I was tired"), "outcome": cite(TEXT),
                "exceptions": [cite("except during debugging")],
            }}


def scoped_candidate(need_id=1, report_id=3, response=RESPONSE):
    report = {"message_id": report_id, "quote": response}
    return {
        "content": (
            "Noah prefers a heads-up before teasing while concentrating, except about games."
        ),
        "kind": "preference", "evidence": [report], "observation": {
            "context": {"message_id": need_id, "quote": NEED}, "action": report.copy(),
            "exceptions": [{"message_id": need_id, "quote": "except teasing about games"}],
        },
    }


@pytest.mark.parametrize("case", [
    "unoffered", "wrong_speaker", "bare_assent", "wrong_topic", "unbound_report",
    "older_outcome", "lost_exception",
])
def test_earlier_qualifiers_need_a_matching_new_owner_report(case):
    earlier = [row(NEED, role="assistant" if case == "wrong_speaker" else "user")]
    if case == "unoffered":
        earlier = []
    response = {"bare_assent": "Yes, that helped.",
                "wrong_topic": "Please make the fonts larger."}.get(case, RESPONSE)
    fresh = row(response, mid=3)
    value = scoped_candidate(response=response)
    if case == "unbound_report":
        fresh.content += " I saw the episode."
        value["evidence"][0]["quote"] = "I saw the episode."
    if case == "older_outcome":
        value["observation"]["outcome"] = value["observation"].pop("context")
    if case == "lost_exception":
        value["content"] = "Noah prefers a heads-up before teasing while concentrating."
    result = admit_memory(value, [fresh], session_key="s", writer="extractor", context_rows=earlier)
    assert not result.accepted
    assert result.metadata["claim_fidelity_refusal"] == "unsupported_observation"


def test_literal_fallback_keeps_valid_observation_facets():
    value = candidate()
    value["content"] = "Noah benefits from shorter replies."
    saved, admission = admit_source_note(value, [row()], session_key="s", writer="extractor")
    assert admission.accepted
    assert TEXT in saved["content"]
    assert admission.metadata["source_note_fallback"] == "lost_condition"
    assert admission.metadata["observation"]["outcome"]["quote"] == TEXT


def test_literal_facets_keep_conditions_negation_source_date_and_actor():
    result = admit_memory(candidate(), [row()], session_key="s", writer="extractor")
    assert result.accepted
    observation = result.metadata["observation"]
    assert observation["subject"] == "user"
    assert observation["context"]["quote"] == "when I was tired"
    assert observation["outcome"]["source_at"] == "2026-11-05T10:00:00Z"
    assert observation["exceptions"][0]["quote"] == "except during debugging"
    memory = SimpleNamespace(kind="preference", metadata=result.metadata, provenance="inferred")
    assert "not causal proof" in origin_annotation(memory)
    assert "except during debugging" in origin_annotation(memory)


@pytest.mark.parametrize("change", [
    {"context": {"message_id": 1, "quote": "when secretly upset"}},
    {"context": {"message_id": True, "quote": "when I was tired"}},
    {"outcome": {"message_id": 1, "quote": "helped"}},
    {"outcome": {"message_id": 1, "quote": "Shorter replies"}},
    {"exceptions": [{"message_id": 1, "quote": "Shorter replies"}]},
    {"motive": "hidden motive"},
])
def test_unsupported_facets_do_not_enter_the_memory(change):
    value = candidate()
    value["observation"].update(change)
    assert not admit_memory(value, [row()], session_key="s", writer="extractor").accepted


def test_other_actor_and_predictions_cannot_supply_user_outcomes():
    value = candidate()
    assert not admit_memory(value, [row(role="assistant")],
                            session_key="s", writer="extractor").accepted
    text = "Shorter replies would have helped when I was tired."
    value["observation"] = {"outcome": {"message_id": 1, "quote": text}}
    assert not admit_memory(value, [row(text)], session_key="s", writer="extractor").accepted


def test_empty_facets_do_not_invent_context_for_a_valid_ordinary_memory():
    value = candidate()
    value["observation"] = {}
    result = admit_memory(value, [row()], session_key="s", writer="extractor")
    assert result.accepted and "observation" not in result.metadata


def test_scoped_memories_remain_separate_and_changed_quotes_fail_revalidation(tmp_path):
    path = tmp_path / "memory.db"
    db = ChatDatabase(path)
    mid = db.add_message("s", "user", TEXT, token_count=10)
    source = db.get_message_row(mid)
    value = candidate()
    for citation in [*value["evidence"], value["observation"]["context"],
                     value["observation"]["outcome"], *value["observation"]["exceptions"]]:
        citation["message_id"] = mid
    admission = admit_memory(value, [source], session_key="s", writer="extractor")
    store = MemoryStore(path)
    try:
        first = store.add(value["content"], "preference", np.ones(4), metadata=admission.metadata)
        second = store.add("Noah benefits from shorter replies", "preference", np.ones(4))
        assert first is not None and second is not None and first.id != second.id
        assert longitudinal_sources_current(first, store)
        db.execute_commit("UPDATE messages SET content=? WHERE id=?", ("A corrected report", mid))
        assert not longitudinal_sources_current(first, store)
    finally:
        store.close()


def test_actual_extractor_round_trip_preserves_receipts_and_rejects_stale_queued_sources(tmp_path):
    from app.core.memory.memory_extractor import MemoryExtractor
    from tests.test_memory_extractor_watermark import _FakeOllama, _FakeEmbedder

    path = tmp_path / "extract.db"
    db = ChatDatabase(path)
    mid = db.add_message("s", "user", TEXT)
    through = db.add_message("s", "assistant", "I will keep that distinction in mind.")
    value = candidate()
    for citation in [*value["evidence"], value["observation"]["context"],
                     value["observation"]["outcome"], *value["observation"]["exceptions"]]:
        citation["message_id"] = mid
    store = MemoryStore(path)
    try:
        model = _FakeOllama([json.dumps({"memories": [value]})])
        worker = MemoryExtractor(db, store, _FakeEmbedder(), model, model="fake",
                                 min_window_messages=1)
        assert worker.extract_for_session("s") == 1
        saved = store.list_recent()[0]
        assert saved.metadata["observation"]["context"]["quote"] == "when I was tired"
        assert saved.metadata["observation"]["outcome"]["quote"] == TEXT
        queued = {"through": through, "created_at": saved.created_at, "candidates": [{
            "content": "A stale contextual note", "kind": "preference", "salience": .5,
            "temporal_type": "preference", "metadata": saved.metadata,
        }]}
        db.execute_commit("UPDATE messages SET content=? WHERE id=?", ("Corrected wording", mid))
        assert worker._apply_batch("s", queued) == 0
        assert store.count() == 1
    finally:
        store.close()


def _scoped_batch(db):
    need = db.add_message("s", "user", NEED)
    proposal = db.add_message("s", "assistant", "I can do that with a spoken signal.")
    fresh = db.add_message("s", "user", RESPONSE)
    through = db.add_message("s", "assistant", "I'll keep the heads-up brief.")
    db.kv_set("memory.extractor.watermark:s", str(proposal))
    return need, fresh, through, json.dumps({"memories": [scoped_candidate(need, fresh)]})


def test_earlier_need_survives_extraction_and_recall_until_source_edit(tmp_path):
    from app.core.memory.memory_extractor import MemoryExtractor
    from app.core.rag.rag_retriever import RagRetriever
    from tests.test_memory_extractor_watermark import _FakeOllama, _FakeEmbedder
    from tests.test_rag_retriever_scoring import _StubStore

    path = tmp_path / "agreement.db"
    db = ChatDatabase(path)
    need, fresh, _, payload = _scoped_batch(db)
    store = MemoryStore(path)
    try:
        model = _FakeOllama([payload])
        embedder = _FakeEmbedder()
        worker = MemoryExtractor(db, store, embedder, model, model="fake",
                                 min_window_messages=1, context_messages=4)
        assert worker.extract_for_session("s") == 1
        assert len(model.prompts) == 1 and NEED in model.prompts[0]
        saved = store.list_recent()[0]
        assert saved.metadata["admission"]["source_message_ids"] == [fresh]
        receipt = saved.metadata["observation"]
        assert receipt["earlier_context"] is True
        assert receipt["context"]["quote"] == NEED
        assert receipt["context"]["context_only"] is True
        assert receipt["action"]["quote"] == RESPONSE
        assert "context_only" not in receipt["action"]
        assert {item["message_id"] for item in receipt["sources"]} == {need, fresh}

        vector = _StubStore(memories=[RagRetriever._memory_to_hit(saved, score=0.9)])
        retriever = RagRetriever(vector, embedder, memory_store=store, top_k=2)
        retriever.set_topic_graph(SimpleNamespace(
            best_clusters_for=lambda *_args, **_kwargs: [(1, "teasing", .9)],
            cluster_member_ids=lambda _cid: [saved.id],
        ))
        hits = retriever.retrieve("teasing during concentration")
        assert len(hits) == 1
        block = retriever.format_hits(hits, user_display_name="Noah")
        assert NEED in block and RESPONSE in block and "context_only" in block
        assert len(retriever.recall_topic("teasing")[1]) == 1

        assert db.update_message_content(need, "It is fine to joke while I'm concentrating.")
        assert retriever.retrieve("teasing during concentration") == []
        assert retriever.recall_topic("teasing") == ("teasing", [])
        assert store.get(saved.id) is not None
    finally:
        store.close()


@pytest.mark.parametrize("changed", ["earlier", "fresh"])
def test_editing_either_source_during_embedding_prevents_write(tmp_path, changed):
    from app.core.memory.memory_extractor import MemoryExtractor
    from tests.test_memory_extractor_watermark import _FakeOllama

    path = tmp_path / "edit.db"
    db = ChatDatabase(path)
    need, fresh, through, payload = _scoped_batch(db)

    def editing_embedder(_text):
        assert db.update_message_content(need if changed == "earlier" else fresh, "Correction.")
        return [0.0, 1.0]

    store = MemoryStore(path)
    try:
        model = _FakeOllama([payload])
        worker = MemoryExtractor(db, store, SimpleNamespace(embed=editing_embedder), model,
                                 model="fake", min_window_messages=1, context_messages=4)
        assert worker.extract_for_session("s") == 0
        assert store.count() == 0
        assert db.kv_get("memory.extractor.watermark:s") == str(through)
        assert worker.extract_for_session("s") == 0
        assert len(model.prompts) == 1
    finally:
        store.close()
