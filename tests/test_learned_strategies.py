from types import SimpleNamespace as NS
import json

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore
from app.core.memory.memory_extractor import MemoryExtractor
from app.core.memory.learned_strategies import admit_strategy, strategy_hits
from app.core.rag.rag_retriever import RagRetriever


def episode():
    rows = [NS(id=1, role="user", session_id="s",
               content="diagnose connection failures in local environment except production"),
            NS(id=2, role="assistant", session_id="s", content="isolate one service"),
            NS(id=3, role="user", session_id="s", content="That worked and fixed the connection.")]

    def cite(index, text):
        return {"message_id": rows[index].id, "quote": rows[index].content, "text": text}

    candidate = {"kind": "strategy", "content": "a strategy", "salience": .6,
                 "strategy": {"goal": cite(0, "diagnose connection failures"),
                              "approach": cite(1, "isolate one service"),
                              "constraints": [cite(0, "local environment")],
                              "exceptions": [cite(0, "production")],
                              "outcome": {**cite(2, "worked and fixed the connection"),
                                          "success": True}}}
    return rows, candidate


def test_strategy_requires_an_actual_user_outcome_and_explicit_conditions():
    rows, candidate = episode()
    assert admit_strategy(candidate, rows, session_key="s", owner="u")
    for content in ("Sounds good", "That would have worked", "Imagine that worked", "worked?"):
        rows[2].content = content
        candidate["strategy"]["outcome"].update(text=content, quote=content)
        assert admit_strategy(candidate, rows, session_key="s", owner="u") is None
    rows, candidate = episode()
    candidate["strategy"]["constraints"] = []
    assert admit_strategy(candidate, rows, session_key="s", owner="u") is None


def test_matching_goal_conditions_exception_and_retraction(tmp_path):
    db = ChatDatabase(tmp_path / "memory.db")
    store = MemoryStore(tmp_path / "memory.db")
    rows, candidate = episode()
    messages = {r.id: r for r in rows}
    reader = NS(get_message_row=messages.get)
    value = admit_strategy(candidate, rows, session_key="s", owner="u")
    try:
        stored = store.add(value["content"], kind="strategy", embedding=np.array([1., 0.]),
                           metadata=value["metadata"], source_message_id=3, skip_dedupe=True)
        query = "Please diagnose intermittent connection failures in local environment"
        assert len(strategy_hits(store, reader, RagRetriever, user_text=query, owner="u")) == 1
        for turn in ("explain connection failures in local environment",
                     "diagnose connection failures", query + " production", "hello"):
            assert not strategy_hits(store, reader, RagRetriever, user_text=turn, owner="u")
        assert not strategy_hits(store, reader, RagRetriever, user_text=query, owner="other")
        correction = NS(id=4, role="user", session_id="s", content="isolate one service failed")
        messages[4] = correction
        revised = {"strategy": {"revises_memory_id": stored.id,
                                "outcome": {"text": correction.content,
                                            "quote": correction.content, "message_id": 4,
                                            "success": False}}}
        failed = admit_strategy(revised, [correction], session_key="s", owner="u",
                                memories=store, db=reader)
        assert failed
        store.add(failed["content"], kind="strategy", embedding=np.array([1., 0.]),
                  metadata=failed["metadata"], source_message_id=4, skip_dedupe=True)
        assert not strategy_hits(store, reader, RagRetriever, user_text=query, owner="u")
        messages.pop(4)
        assert not strategy_hits(store, reader, RagRetriever, user_text=query, owner="u")
    finally:
        store.close()
        db._get_conn().close()


def test_a_revised_or_deleted_original_source_invalidates_transfer(tmp_path):
    db = ChatDatabase(tmp_path / "memory.db")
    store = MemoryStore(tmp_path / "memory.db")
    rows, candidate = episode()
    messages = {r.id: r for r in rows}
    value = admit_strategy(candidate, rows, session_key="s", owner="u")
    try:
        store.add(value["content"], kind="strategy", embedding=np.array([1., 0.]),
                  metadata=value["metadata"], source_message_id=3, skip_dedupe=True)
        reader = NS(get_message_row=messages.get)
        query = "diagnose connection failures in local environment"
        rows[2].content = "Actually that did not work."
        assert not strategy_hits(store, reader, RagRetriever, user_text=query, owner="u")
        messages.pop(2)
        assert not strategy_hits(store, reader, RagRetriever, user_text=query, owner="u")
    finally:
        store.close()
        db._get_conn().close()


def test_strategy_is_extracted_in_the_existing_single_model_call(tmp_path):
    db = ChatDatabase(tmp_path / "memory.db")
    store = MemoryStore(tmp_path / "memory.db")
    rows, candidate = episode()
    for row in rows:
        row.created_at = "2026-10-01T12:00:00+00:00"
    messages, kv, calls = {r.id: r for r in rows}, {}, []
    reader = NS(kv_get=kv.get, kv_set=lambda k, v: kv.update({k: v}),
                get_messages=lambda *a, **k: rows, get_message_row=messages.get)

    def chat(prompt, **kwargs):
        calls.append(prompt)
        return json.dumps({"memories": [candidate]}), NS(
            prompt_tokens=30, completion_tokens=50, done_reason="stop")

    worker = MemoryExtractor(reader, store, NS(embed=lambda _: np.array([1., 0.])),
                             NS(chat_json=chat), model="test", think=False,
                             min_window_messages=2, context_window=lambda: 8192,
                             user_id_provider=lambda: "u")
    try:
        assert worker.extract_for_session("s") == 1
        assert len(calls) == 1
        assert store.list_recent(kind="strategy")[0].metadata["learned_strategy"]["success"]
    finally:
        store.close()
        db._get_conn().close()
