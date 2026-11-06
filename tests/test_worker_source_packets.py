import json
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore
from app.core.memory.worker_sources import transcript_packet
from app.core.proactive.diary_worker import DiaryWorker


def test_input_budget_keeps_a_contiguous_whole_suffix_and_its_qualifications():
    row = SimpleNamespace(role="user", content="A plan. " * 50 + "But I did not do it.")
    assert transcript_packet([row], max_chars=80)["text"] == ""
    packet = transcript_packet([row], max_chars=1000)
    assert "But I did not do it" in packet["text"]
    older = SimpleNamespace(role="user", content="The earlier plan sounded good.")
    assert transcript_packet([older, row], max_chars=80)["text"] == ""


@pytest.mark.parametrize("action", ["switch", "edit", "edit_during_embed"])
def test_diary_uses_original_session_and_rejects_source_edits(tmp_path, action):
    path = tmp_path / "diary.db"
    db = ChatDatabase(path)
    mid = db.add_message("old", "user", "I considered a plan but did not carry it out.")
    store = MemoryStore(path)
    state = {"session": "old", "name": "User"}
    kv = {}
    def snapshot():
        session = state["session"]
        return transcript_packet(db.get_messages(session), session_key=session,
                                 user_name=state["name"])
    def generate(messages, **kwargs):
        if action == "switch":
            state.update(session="new", name="Another person")
        if action == "edit":
            db.execute_commit("UPDATE messages SET content=? WHERE id=?", ("A correction", mid))
        return json.dumps({
            "entry": "I felt curious about the question and kept its uncertainty in mind.",
        }), None
    def embed(text):
        if action == "edit_during_embed":
            db.execute_commit("UPDATE messages SET content=? WHERE id=?", ("A correction", mid))
        return np.array([1., 0.])
    worker = DiaryWorker(memory_store=store, embed=embed,
        recent_context_provider=lambda: snapshot()["text"], source_snapshot_provider=snapshot,
        is_away_provider=lambda: True, user_display_name_provider=lambda: state["name"],
        source_session_provider=lambda: state["session"], kv_get=kv.get, kv_set=kv.__setitem__,
        ollama=SimpleNamespace(chat_json=generate), model="fake", cooldown_seconds=0,
        min_context_chars=1)
    try:
        result = worker.run()
        entries = store.iter_by_kind("diary")
        if action == "switch":
            assert result["fired"] == 1 and len(entries) == 1
            assert entries[0].source_session == "old"
            receipt = entries[0].metadata["reflective_derivation"]
            assert receipt["session_key"] == "old"
            assert receipt["message_sources"][0]["message_id"] == mid
            assert receipt["scope"] == "generation_context"
        else:
            assert not entries
    finally:
        store.close()


@pytest.mark.parametrize("overlong", [False, True])
def test_post_sleep_diary_keeps_output_whole_and_original_session(overlong):
    from app.core.proactive.post_sleep_diary_worker import PostSleepDiaryWorker

    episode = SimpleNamespace(
        id=4, started_at="2026-11-05T22:00:00+00:00", ended_at="2026-11-06T06:00:00+00:00",
        diary_memory_id=None, interruptions=[], reason_text="A recorded goodnight",
        reason_code="model_authored_goodnight", kind="overnight", outcome="awake",
    )
    state = {"session": "old"}
    written, finished = [], []
    def generate(*args, **kwargs):
        state["session"] = "new"
        return ("I rested after our goodnight. " * 20 + "It was not uninterrupted." if overlong
                else "I rested after the recorded goodnight on 5 November 2026.")
    def save(**kwargs):
        written.append(kwargs)
        return SimpleNamespace(id=42)
    worker = PostSleepDiaryWorker(
        sleep_store=SimpleNamespace(list_episodes=lambda **kw: [episode],
            reserve_episode_output=lambda *a, **kw: True,
            finish_episode_output=lambda *a, **kw: finished.append(kw)),
        memory_store=SimpleNamespace(add=save),
        embedder=SimpleNamespace(embed=lambda text: np.ones(2)),
        client=SimpleNamespace(chat=generate), model_provider=lambda: "fake",
        user_name_provider=lambda: "User", session_provider=lambda: state["session"],
    )
    result = worker.run()
    assert result["written"] is not overlong
    if overlong:
        assert written == [] and finished[-1]["memory_id"] is None
    else:
        assert written[0]["source_session"] == "old"
