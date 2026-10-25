from __future__ import annotations

import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.summary_worker import SummaryWorker
from app.core.proactive.thread_resummary_worker import ThreadResummaryWorker
from app.core.rag.message_indexer import MessageIndexer
from app.core.rag.rag_retriever import RagRetriever
from app.core.rag.rag_store import RagStore
from app.core.session.chat_turn_mixin import ChatTurnMixin
from app.core.session.message_history import (
    HistoryConflict,
    HistoryMessageNotFound,
    history_lock,
)
from app.core.session.task_orchestration_mixin import TaskOrchestrationMixin
from app.core.session.turn_runner import TurnResult
from app.web.rest.message_history_routes import register
from app.web.server import _spawn_chat_turn, create_web_app
from app.llm.ollama_client import OllamaUsage
from web_fake_session import FakeSession


@pytest.fixture
def db(tmp_path):
    database = ChatDatabase(tmp_path / "chat.db")
    yield database
    database._get_conn().close()


def make_session(db, cls=FakeSession):
    return cls(
        session_key="u:s", _chat_db=db, _remember_history=True,
        _turn_in_progress=False, _rag_store=MagicMock(),
        _prompt_assembler=MagicMock(), _rag_prefetcher=MagicMock(),
    )


def add_pair(db, session_id="u:s", attachments=None):
    user = db.add_message(session_id, "user", "Try this again", attachments=attachments)
    reply = db.add_message(session_id, "assistant", "< | DSML | calls> broken output")
    return user, reply


def test_delete_is_selective_persistent_and_invalidates_derived_history(db):
    session = make_session(db)
    user, reply = add_pair(db)
    other_user, other_reply = add_pair(db, "u:other")
    db.save_summary("u:s", "bad summary", 3, 2)
    db.save_thread_note("u:s", "bad title", "bad note", 2)
    db.save_summary("u:other", "keep this", 3, 2)
    before = db.get_history_head("u:s")
    events = []
    session.add_history_listener(events.append)

    assert session.delete_chat_message("u:s", reply) == [reply]
    assert db.get_message_row(reply) is None
    assert db.get_message_row(user) is not None
    assert db.get_existing_message_ids([user, reply, other_user, other_reply]) == {
        user, other_user, other_reply,
    }
    assert db.get_latest_summary("u:s") is None
    assert db.get_thread_note("u:s") is None
    assert db.get_latest_summary("u:other").summary == "keep this"
    assert db.get_history_head("u:s") != before
    session._rag_store.delete_messages.assert_called_once_with("u:s", [reply])
    session._rag_prefetcher.reset.assert_called_once()
    session._prompt_assembler.reset_slice_cache.assert_called_once_with("u:s")
    assert events == [{"type": "messages_deleted", "session": "u:s", "message_ids": [reply]}]


def test_database_delete_rolls_back_all_rows_and_summary_on_failure(db):
    user, reply = add_pair(db)
    db.save_summary("u:s", "retained", 2, 2)
    db._get_conn().execute(
        f"CREATE TEMP TRIGGER reject_reply BEFORE DELETE ON messages "
        f"WHEN OLD.id = {reply} BEGIN SELECT RAISE(ABORT, 'blocked'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="blocked"):
        db.delete_message_rows("u:s", [user, reply])
    assert db.get_existing_message_ids([user, reply]) == {user, reply}
    assert db.get_latest_summary("u:s").summary == "retained"


def test_delete_user_keeps_other_messages_and_advances_latest_user(db):
    old, _ = add_pair(db)
    latest, reply = add_pair(db)
    session = make_session(db)
    session.delete_chat_message("u:s", latest)
    assert db.get_latest_user_message_id("u:s") == old
    assert db.get_message_row(reply) is not None


def test_production_web_app_registers_message_deletion(db):
    session = make_session(db)
    user, reply = add_pair(db)
    client = TestClient(create_web_app(session))
    response = client.delete(f"/api/sessions/u:s/messages/{reply}")
    assert response.status_code == 200
    assert response.json() == {"session": "u:s", "message_ids": [reply]}
    assert db.get_message_row(reply) is None
    assert db.get_message_row(user) is not None


@pytest.mark.parametrize("operation", ["delete", "retry"])
def test_routes_reject_foreign_missing_changed_session_and_busy_requests(db, operation):
    session = make_session(db)
    user, _ = add_pair(db)
    foreign, _ = add_pair(db, "other:s")
    app = FastAPI()
    register(app, session, MagicMock())
    client = TestClient(app)

    def request(mid, key="u:s"):
        path = f"/api/sessions/{key}/messages/{mid}"
        return client.delete(path) if operation == "delete" else client.post(path + "/retry")

    assert request(foreign).status_code == 404
    assert request(999999).status_code == 404
    assert request(user, "other:s").status_code == 409
    session._turn_in_progress = True
    assert request(user).status_code == 409
    assert db.get_message_row(user) is not None


def test_delete_rejects_a_turn_that_holds_the_history_lock(db):
    session = make_session(db)
    _, reply = add_pair(db)
    held = threading.Event()
    release = threading.Event()

    def hold():
        with history_lock(session):
            held.set()
            assert release.wait(3)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert held.wait(3)
        with pytest.raises(HistoryConflict):
            session.delete_chat_message("u:s", reply)
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert db.get_message_row(reply) is not None


def test_retry_only_accepts_latest_user_and_reserves_against_double_clicks(db):
    session = make_session(db)
    old, _ = add_pair(db)
    latest, reply = add_pair(db)
    app = FastAPI()
    with patch("app.web.server._spawn_chat_turn") as spawn:
        register(app, session, MagicMock())
        client = TestClient(app)
        assert client.post(f"/api/sessions/u:s/messages/{old}/retry").status_code == 409
        assert client.post(f"/api/sessions/u:s/messages/{reply}/retry").status_code == 409
        result = client.post(f"/api/sessions/u:s/messages/{latest}/retry")
        assert result.status_code == 202
        assert result.json() == {"session": "u:s", "message_id": latest}
        assert client.post(f"/api/sessions/u:s/messages/{latest}/retry").status_code == 409
        assert client.delete(f"/api/sessions/u:s/messages/{reply}").status_code == 409
        assert spawn.call_args.kwargs == {"retry_message_id": latest}
    assert db.get_message_row(reply) is not None  # Removed only when the queued turn runs.
    session.finish_chat_retry(latest)
    assert session.reserve_chat_retry("u:s", latest).id == latest


@pytest.mark.parametrize("change", ["new_user", "switch", "deleted_user", "attachments"])
def test_retry_revalidates_before_removing_any_reply(db, change):
    session = make_session(db)
    user, reply = add_pair(db, attachments='[{"id":"photo","kind":"image"}]')
    session.reserve_chat_retry("u:s", user)
    if change == "new_user":
        db.add_message("u:s", "user", "Actually a different question")
    elif change == "switch":
        session.session_key = "u:new"
    elif change == "deleted_user":
        db.delete_message_rows("u:s", [user])
    else:
        db._get_conn().execute("UPDATE messages SET attachments = '{}' WHERE id = ?", (user,))
        db._get_conn().commit()
    expected = (
        ValueError if change == "attachments" else
        HistoryMessageNotFound if change == "deleted_user" else HistoryConflict
    )
    with pytest.raises(expected):
        with history_lock(session):
            session.prepare_chat_retry(user)
    assert db.get_message_row(reply) is not None
    session.finish_chat_retry(user)
    assert session.__dict__["_pending_chat_retry"] is None


class _ChatHost(TaskOrchestrationMixin, ChatTurnMixin, FakeSession):
    pass


def chat_host(db, *, queued):
    host = make_session(db, _ChatHost)
    host._settings = SimpleNamespace(tts=SimpleNamespace(enabled=False))
    host._compactions_total = 0
    host._context_window = 2048
    host._context_source = "test"
    host._metrics_history = []
    host._sleep_store = None
    host._fact_check_cancel = None
    host._task_orchestration_enabled = queued
    host._brain_loop = (
        SimpleNamespace(enqueue=host._on_user_message_event) if queued else None
    )
    host.get_last_metrics = lambda: dict(host._last_metrics)
    return host


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("remember", [False, True])
def test_retry_runs_normal_turn_without_duplicate_user_or_lost_attachments(db, queued, remember):
    host = chat_host(db, queued=queued)
    host._remember_history = remember
    storage_key = "u:s" if remember else "u:s:noremember"
    raw_attachments = (
        '[{"id":"photo","filename":"photo.png","kind":"image","rel_path":"photo.png"}]'
    )
    user, old_reply = add_pair(db, storage_key, attachments=raw_attachments)
    original = db.get_message_row(user)
    events = []
    host.add_history_listener(events.append)

    def generate(key, text, **kwargs):
        assert key == storage_key
        assert text == original.content
        assert kwargs["resume_user_message_id"] == user
        assert db.get_message_row(old_reply) is None
        assert host._active_turn_attachments == [
            {"id": "photo", "filename": "photo.png", "kind": "image", "rel_path": "photo.png"},
        ]
        reply_id = db.add_message(key, "assistant", "A clean replacement")
        return TurnResult(
            text="A clean replacement", reaction="neutral", assistant_message_id=reply_id,
        )

    host._turn_runner.run.side_effect = generate
    host.reserve_chat_retry("u:s", user)
    reply = host.enqueue_user_message(
        text=original.content, mode="typed", retry_message_id=user, wait_for_reply=True,
    )
    host.finish_chat_retry(user)
    assert reply == "A clean replacement"
    assert db.get_message_row(user) == original
    rows = db.get_messages(storage_key)
    assert [(r.role, r.content) for r in rows] == [
        ("user", original.content), ("assistant", "A clean replacement"),
    ]
    assert host._last_metrics["user_message_id"] == user
    assert host._last_metrics["assistant_message_id"] == rows[-1].id
    assert {"type": "reply_replacing", "session": "u:s", "user_message_id": user} in events
    assert any(e["type"] == "user_message_saved" and e["session"] == "u:s" for e in events)


@pytest.mark.parametrize("fail", [False, True])
def test_spawn_retry_uses_queue_without_user_echo_and_releases_reservation(db, fail):
    host = chat_host(db, queued=True)
    user, old_reply = add_pair(db)
    host.reserve_chat_retry("u:s", user)
    if fail:
        host._turn_runner.run.side_effect = RuntimeError("provider unavailable")
    else:
        host._turn_runner.run.return_value = TurnResult(text="replacement", reaction="neutral")
    hub = MagicMock()
    done = threading.Event()
    with patch("app.web.server.offer_text_delivery", return_value=None):
        _spawn_chat_turn(host, hub, "Try this again", done, retry_message_id=user)
        assert done.wait(5)
    assert host.__dict__["_pending_chat_retry"] is None
    assert db.get_message_row(user) is not None
    assert db.get_message_row(old_reply) is None
    assert not any(
        call.args[0].startswith("You") for call in host._notify_message.call_args_list
    )
    events = [call.args[0] for call in hub.broadcast.call_args_list]
    assert events[0] == {"type": "turn_started", "session": "u:s"}
    assert events[-1]["type"] == ("error" if fail else "turn_done")
    assert events[-1]["session"] == "u:s"


def test_indexer_cannot_resurrect_a_queued_or_in_flight_deleted_message(db):
    _, reply = add_pair(db)
    row = db.get_message_row(reply)
    rag = MagicMock()
    rag.has_message.return_value = False
    embedder = MagicMock()
    embedder.embed.side_effect = lambda text: db.delete_message_rows("u:s", [reply]) or [1.0]
    indexer = MessageIndexer(db, rag, embedder)
    try:
        indexer._index_one(row)
        rag.add_message.assert_not_called()
        indexer._index_one(row, attempt=1)
        assert embedder.embed.call_count == 1
    finally:
        indexer.stop()


def test_indexer_cleans_up_a_deletion_committed_during_lance_write(db):
    _, reply = add_pair(db)
    row = db.get_message_row(reply)
    rag = MagicMock()
    rag.has_message.return_value = False
    rag.add_message.side_effect = lambda **kwargs: db.delete_message_rows("u:s", [reply])
    indexer = MessageIndexer(db, rag, MagicMock())
    try:
        indexer._index_one(row)
        rag.delete_messages.assert_called_once_with("u:s", [reply])
        assert indexer.stats()["indexed"] == 0
    finally:
        indexer.stop()


def test_search_deletion_is_selective_and_sqlite_filters_stale_hits(db, tmp_path):
    user, reply = add_pair(db)
    other, _ = add_pair(db, "u:other")
    rag = RagStore(tmp_path / "rag", embedding_model="test", vector_dim=4)
    vector = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    try:
        for mid in (user, reply, other):
            row = db.get_message_row(mid)
            rag.add_message(
                session_id=row.session_id, message_id=mid, role=row.role,
                content=row.content, embedding=vector, created_at=row.created_at,
            )
        db.delete_message_rows("u:s", [reply])
        hits = rag.search_messages(vector, top_k=10, min_score=0)
        assert {h.record.message_id for h in hits} == {user, reply, other}
        retriever = RagRetriever(
            rag, MagicMock(), chat_db=db, include_documents=False, score_threshold=0,
        )
        retriever._drop_deleted_message_hits(hits)
        assert {h.record.message_id for h in hits} == {user, other}
        retrieved = retriever.retrieve("broken output", _embedding=vector, _mark_used=False)
        assert retrieved
        assert all(hit.record.message_id != reply for hit in retrieved)
        rag.delete_messages("u:s", [reply, other])
        assert rag.has_message("u:s", user)
        assert not rag.has_message("u:s", reply)
        assert rag.has_message("u:other", other)
    finally:
        rag.close()


def test_indexer_retries_cleanup_if_a_deleted_rows_lance_removal_failed(db):
    _, reply = add_pair(db)
    row = db.get_message_row(reply)
    rag = MagicMock()
    rag.has_message.return_value = False
    rag.add_message.side_effect = lambda **kwargs: db.delete_message_rows("u:s", [reply])
    rag.delete_messages.side_effect = [RuntimeError("temporary Lance failure"), None]
    indexer = MessageIndexer(db, rag, MagicMock())
    try:
        indexer._index_one(row)
        assert len(indexer._retry_timers) == 1
        timer = next(iter(indexer._retry_timers))
        timer.cancel()
        timer.function()
        retry_row, attempt = indexer._queue.get_nowait()
        indexer._index_one(retry_row, attempt)
        assert rag.delete_messages.call_count == 2
        assert rag.add_message.call_count == 1
        assert not indexer._retry_timers
    finally:
        indexer.stop()


def test_stale_derived_history_cannot_be_saved_after_a_delete(db):
    _, reply = add_pair(db)
    revision = db.get_history_edit_revision("u:s")
    db.delete_message_rows("u:s", [reply])
    assert not db.save_summary(
        "u:s", "old quote", 2, 2, expected_history_revision=revision,
    )
    assert not db.save_thread_note(
        "u:s", "old title", "old quote", 2, expected_history_revision=revision,
    )
    assert db.get_latest_summary("u:s") is None
    assert db.get_thread_note("u:s") is None
    current = db.get_history_edit_revision("u:s")
    assert current > revision
    assert db.save_summary("u:s", "fresh", 1, 1, expected_history_revision=current)
    assert db.save_thread_note(
        "u:s", "fresh title", "fresh", 1, expected_history_revision=current,
    )


def test_summary_generation_cannot_resurrect_deleted_text(db):
    _, reply = add_pair(db)
    client = MagicMock()

    def generate(*args, **kwargs):
        db.delete_message_rows("u:s", [reply])
        return "stale quote from deleted reply", OllamaUsage()

    client.chat_json.side_effect = generate
    worker = SummaryWorker(
        db, client, model="test", is_busy=lambda: False,
        min_unsummarized_messages=1, context_window=lambda: 4096,
    )
    assert not worker.compact_now("u:s")
    assert worker.compactions_total() == 0
    assert db.get_latest_summary("u:s") is None


def test_thread_note_generation_cannot_resurrect_deleted_text(db):
    _, reply = add_pair(db)
    notify = MagicMock()
    worker = ThreadResummaryWorker(
        chat_db=db, ollama=MagicMock(), chat_model="test", cancel_event=threading.Event(),
        agent_settings=SimpleNamespace(
            thread_resummary_enabled=True, thread_resummary_min_messages=1,
        ),
        memory_settings=SimpleNamespace(), rate_limiter=MagicMock(),
        session_key_provider=lambda: "u:s", notify_thread_note=notify,
    )

    def draft(**kwargs):
        db.delete_message_rows("u:s", [reply])
        return "stale title", "stale quote"

    worker._draft = draft
    assert worker.run() == {"wrote": False, "reason": "history_changed"}
    assert db.get_thread_note("u:s") is None
    notify.assert_not_called()


def test_new_messages_do_not_discard_an_otherwise_valid_background_summary(db):
    add_pair(db)
    client = MagicMock()

    def generate(*args, **kwargs):
        db.add_message("u:s", "user", "A newer message")
        return "valid summary of the first pair", OllamaUsage()

    client.chat_json.side_effect = generate
    worker = SummaryWorker(
        db, client, model="test", is_busy=lambda: False,
        min_unsummarized_messages=1, context_window=lambda: 4096,
    )
    assert worker.compact_now("u:s")
    assert db.get_latest_summary("u:s").messages_summarized == 2
