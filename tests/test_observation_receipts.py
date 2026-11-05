from types import SimpleNamespace
import json

import numpy as np
import pytest

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore
from app.core.memory.memory_admission import admit_memory
from app.core.memory.memory_origin import origin_annotation
from app.core.memory.observation_receipts import longitudinal_sources_current


TEXT = "Shorter replies helped when I was tired, except during debugging."


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
