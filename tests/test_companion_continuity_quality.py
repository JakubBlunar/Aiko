"""Regression contrasts from the 17 November ordinary-use review."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.infra import timephrase
from app.core.memory.worker_sources import transcript_packet
from app.core.proactive.diary_worker import source_snapshot
from tests.test_diary_worker import _FakeLLM, _make_worker
from tests.test_idle_curiosity_worker import _build_world
from tests.test_pursuit_research import research, state


def test_aging_that_exposes_more_old_messages_cannot_reopen_diary(monkeypatch):
    base = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    clock = [base + timedelta(hours=3)]
    monkeypatch.setattr(timephrase, "_now_provider", lambda: clock[0])
    rows = [SimpleNamespace(id=i, session_id="s", role="user", content="x" * 140,
                            created_at=base.isoformat()) for i in range(14)]
    def snapshot():
        return source_snapshot(rows, user_name="Noah", session_key="s")
    first = snapshot()
    worker = _make_worker()
    worker._source_snapshot_provider = snapshot
    monkeypatch.setattr("app.core.memory.worker_sources.current", lambda *args: True)
    assert worker.run()["fired"] == 1
    clock[0] = base + timedelta(days=3)
    assert len(snapshot()["message_sources"]) > len(first["message_sources"])
    assert worker.run()["reason"] == "no_new_conversation"
    assert len(worker._ollama.calls) == 2
    rows[-1].content = "A new thought about a different subject."
    assert worker.run()["fired"] == 1
    assert len(worker._ollama.calls) == 4


def test_diary_rejects_identical_entry_even_with_new_dialogue():
    worker = _make_worker()
    entry = worker._ollama.entry
    worker._memory_store.list_recent = lambda **kwargs: [SimpleNamespace(content=entry)]
    worker._context_key_provider = lambda: "genuinely-new-dialogue"
    assert worker.run()["reason"] == "duplicate_entry"
    assert worker._memory_store.added == []
    assert worker.run()["reason"] == "no_new_conversation"


@pytest.mark.parametrize("source,expected", [
    ("I felt closer after our conversation.", "unsupported_clock"),
    ("At 03:01 I joined you under the blanket.", None),
])
def test_diary_clock_needs_testimony_rather_than_a_message_timestamp(monkeypatch, source, expected):
    row = SimpleNamespace(id=1, session_id="s", role="user", content=source,
                          created_at="2026-11-16T03:01:00+00:00")
    worker = _make_worker(llm=_FakeLLM("The quiet closeness at 03:01 stayed with me."))
    worker._source_snapshot_provider = lambda: source_snapshot(
        [row], user_name="Noah", session_key="s")
    monkeypatch.setattr("app.core.memory.worker_sources.current", lambda *args: True)
    result = worker.run()
    assert result.get("reason") == expected
    assert len(worker._memory_store.added) == (0 if expected else 1)


def test_project_scope_is_retained_locally_with_the_question(tmp_path):
    store, worker = research(tmp_path)
    try:
        project = state()
        mid = worker.request(project, timephrase.utcnow())
        question = store.get(mid)
        assert question.metadata["research_project"]["artifact"] == project["artifact"]
        assert question.metadata["research_project"]["kind"] == "learning"
    finally:
        store.close()


def test_pending_legacy_question_acquires_scope_without_rewriting_it(tmp_path):
    store, worker = research(tmp_path)
    try:
        project = state()
        question = store.add(content="How much light does basil need?", kind="open_question",
            embedding=worker._embed("basil"), skip_dedupe=True,
            metadata={"hobby_key": project["key"], "hobby_started_at": project["started_at"],
                      "curiosity_purpose": "research"})
        assert worker.request(project, timephrase.utcnow()) == question.id
        saved = store.get(question.id)
        assert saved.content == "How much light does basil need?"
        assert saved.metadata["research_project"]["artifact"] == project["artifact"]
        assert len(store.iter_by_kind("open_question")) == 1
    finally:
        store.close()


@pytest.mark.parametrize("verdict,accepted", [(True, True), (False, False), (None, False),
                                            ("true", False), (1, False)])
def test_scoped_research_must_pass_relevance_before_saving_or_resolving(verdict, accepted):
    output = {"answer": "Use a shared horizon for the roof's vanishing points.",
              "confidence": .9, "source_indices": [0], "project_relevant": verdict}
    env = _build_world(answer_json=output)
    store = env["memory_store"]
    scope = {"artifact": "a skyline sketch", "description": "rooftop perspective",
             "kind": "making"}
    try:
        question = store.add(content="How can roof perspective be sketched?", kind="open_question",
            embedding=env["embedder"].embed("roof"), skip_dedupe=True,
            metadata={"curiosity_purpose": "research", "already_neutral": True,
                      "research_origin": "pursuit", "research_project": scope,
                      "hobby_key": "skyline", "hobby_started_at": "2026-11-01"})
        result = env["worker"].run()
        findings = store.iter_by_kind("curiosity_finding")
        assert bool(findings) is accepted
        assert bool(store.get(question.id).metadata.get("curiosity_resolved_at")) is accepted
        assert result["outcome"] == ("resolved" if accepted else "inconclusive")
        prompt = env["ollama"].chat_calls[-1]["messages"][-1]["content"]
        assert "a skyline sketch" in prompt
        assert "structural shell" in env["ollama"].chat_calls[-1]["messages"][0]["content"]
        assert "a skyline sketch" not in json.dumps(env["web_search"].calls)
        if accepted:
            assert findings[0].metadata["research_project"] == scope
            assert findings[0].metadata["project_relevant"] is True
    finally:
        store.close()


def test_project_edit_during_research_prevents_a_stale_finding():
    env = _build_world()
    store = env["memory_store"]
    try:
        question = store.add(content="How can roof perspective be sketched?", kind="open_question",
            embedding=env["embedder"].embed("roof"),
            metadata={"research_project": {"artifact": "skyline sketch"},
                      "already_neutral": True})
        def generate(*args, **kwargs):
            store.update(question.id, metadata={"research_project": {"artifact": "roof design"}})
            yield json.dumps({"answer": "Use two vanishing points on a shared horizon.",
                              "confidence": .9, "source_indices": [0], "project_relevant": True})
        env["ollama"].chat_stream = generate
        assert env["worker"].run()["outcome"] == "question_changed"
        assert not store.iter_by_kind("curiosity_finding")
    finally:
        store.close()


def test_snapshot_sources_still_only_name_rows_offered_to_the_model():
    rows = [SimpleNamespace(id=1, session_id="s", role="user", content="x" * 3000,
                            created_at="2026-11-01T12:00:00Z")]
    assert source_snapshot(rows, user_name="Noah", session_key="s")["message_sources"] == []
    assert transcript_packet(rows)["text"] == ""


@pytest.mark.parametrize("verdict,reason", [
    ({"supported": False, "reason": "unsupported_claim"}, "unsupported_claim"),
    ({"supported": True, "reason": "unsupported_claim"}, "grounding_unavailable"),
    ({"supported": "true", "reason": "supported"}, "grounding_unavailable"),
    ({"entry": "a malformed review"}, "grounding_unavailable"),
])
def test_diary_review_cannot_save_rejected_or_ambiguous_claims(monkeypatch, verdict, reason):
    worker = _make_worker()
    worker._source_snapshot_provider = lambda: {
        "key": "source-1", "text": "Noah: I added context to the memories.",
        "session_key": "s", "message_sources": [],
    }
    original = worker._ollama.chat_json
    def generate(*args, **kwargs):
        if kwargs["surface"] == "diary_grounding":
            return json.dumps(verdict), None
        return original(*args, **kwargs)
    worker._ollama.chat_json = generate
    monkeypatch.setattr("app.core.memory.worker_sources.current", lambda *args: True)
    assert worker.run()["reason"] == reason
    assert not worker._memory_store.added
    if reason == "unsupported_claim":
        assert worker.run()["reason"] == "no_new_conversation"
