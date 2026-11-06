from types import SimpleNamespace

import pytest

from app.core.memory.hypothesis_memory import answer_payload, world_support
from app.core.memory.memory_origin import claim_source_refusal, memory_origin


def payload(**kw):
    args = dict(evidence="not always, only when I ask", question="Is it always helpful?",
                verdict="correct", subject="user", target_id=7, target_type="hypothesis",
                event={"session_id": "s", "user_message_id": 2,
                       "observed_at": "2026-11-06T10:00:00+00:00"})
    args.update(kw)
    return answer_payload("The broad unsupported proposed interpretation", args["evidence"], **args)


@pytest.mark.parametrize("verdict", ["confirm", "correct", "deny"])
def test_only_literal_reply_is_indexed_and_verdict_is_preserved(verdict):
    value = payload(verdict=verdict)
    assert "not always, only when I ask" in value["content"]
    assert "unsupported proposed interpretation" not in value["content"]
    assert "Question context (not a claim)" in value["content"]
    assert value["metadata"]["hypothesis_answer"]["verdict"] == verdict
    assert value["event_time"] is None


def test_answer_receipt_cannot_support_an_unrelated_general_claim():
    value = payload(subject="aiko")
    row = SimpleNamespace(kind="fact", metadata=value["metadata"])
    assert memory_origin(row).subject == "aiko"
    assert claim_source_refusal(row, subject="user", kind="preference") == "answer_requires_target"


def test_relative_quote_has_explicit_recording_anchor_but_no_invented_event_date():
    value = payload(evidence="I finished it last year")
    assert "2026-11-06T10:00:00+00:00" in value["content"]
    assert value["event_time"] is None
    assert value["metadata"]["hypothesis_answer"]["occurrence_time"] is None


def test_a_missing_source_or_wrong_speaker_is_not_accepted():
    assert payload(db=SimpleNamespace(get_message_row=lambda mid: None)) is None
    row = SimpleNamespace(role="assistant", content="not always, only when I ask", session_id="s")
    assert payload(db=SimpleNamespace(get_message_row=lambda mid: row)) is None


def test_oversized_quote_is_refused_whole_instead_of_losing_qualifiers():
    assert payload(evidence="a" * 1001 + " only when invited") is None


def test_world_anchor_requires_target_linked_confirmation_receipts():
    row = SimpleNamespace(answer_memory_ids=[1], hypothesis_id=7)
    answer = SimpleNamespace(metadata={"hypothesis_answer": {"verdict": "deny"}})
    assert world_support(row, SimpleNamespace(get=lambda mid: answer)) is None


def test_real_answer_support_and_world_anchor_follow_source_edits(tmp_path):
    import numpy as np
    from app.core.infra.chat_database import ChatDatabase
    from app.core.memory.memory_store import MemoryStore
    from app.core.memory.observation_receipts import longitudinal_sources_current
    from app.core.session.answer_event import current_answer_event
    from app.core.session.hypothesis_resolve_mixin import HypothesisResolveMixin

    path = tmp_path / "hypothesis.db"
    db = ChatDatabase(path)
    store = MemoryStore(path)
    host = SimpleNamespace(
        _chat_db=db, _memory_store=store, session_key="s",
        _embedder=SimpleNamespace(embed=lambda text: np.array([1., 0.])),
        _notify_memory_added=lambda row: None,
    )
    try:
        ids = []
        for text in ("Coarse grinding can cause that.", "I saw that with a coarse grind."):
            mid = db.add_message("s", "user", text)
            event = {"user_message_id": mid, "session_id": "s",
                     "observed_at": db.get_message_row(mid).created_at}
            token = current_answer_event.set(event)
            try:
                ids.append(HypothesisResolveMixin._store_hypothesis_answer(
                    host, "Coarse grinding causes channeling", text, evidence=text,
                    confirming=True, verdict="confirm", subject="world", target_id=7,
                ))
            finally:
                current_answer_event.reset(token)
        assert all(ids)
        row = SimpleNamespace(answer_memory_ids=ids, hypothesis_id=7,
                              statement="Coarse grinding causes channeling")
        anchor = HypothesisResolveMixin._anchor_world_hypothesis(host, row)
        assert anchor is not None
        saved = store.get(anchor)
        assert memory_origin(saved).subject == "world"
        assert memory_origin(saved).mode == "interpretation"
        assert longitudinal_sources_current(saved, store)
        host._hypothesis_store = SimpleNamespace(get=lambda hid: row)
        def edit_target(text):
            row.statement = "A broader untested interpretation"
            return np.array([1., 0.])
        host._embedder = SimpleNamespace(embed=edit_target)
        count = store.count()
        assert HypothesisResolveMixin._anchor_world_hypothesis(host, row) is None
        assert store.count() == count
        row.statement = "A broader untested interpretation"
        assert world_support(row, store) is None
        db.execute_commit("UPDATE messages SET content=? WHERE id=?", ("That was mistaken", mid))
        assert not longitudinal_sources_current(saved, store)
        assert world_support(row, store) is None
    finally:
        store.close()
