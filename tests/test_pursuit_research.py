import json
import random
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.memory.memory_store import MemoryStore
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.hobby_worker import HobbyWorker, KV_CURRENT_HOBBY, load_hobby
from app.core.proactive.pursuit_research import PursuitResearch
from app.core.proactive.cue_store import CueStore
from app.core.session.cue_pool_mixin import CuePoolMixin


def state():
    return {
        "key": "basil", "artifact": "basil light requirements", "kind": "learning",
        "label": "learning about basil", "started_at": timephrase.utcnow().isoformat(),
        "progress": 0, "advances": 0,
    }


def embed(text):
    return np.array([1.0, 0.0], dtype=np.float32)


class Client:
    def chat_json(self, messages, **kwargs):
        return json.dumps({
            "researchable": True, "question": "How much light does basil need?",
        }), None


def research(tmp_path):
    ChatDatabase(tmp_path / "memory.db")
    store = MemoryStore(tmp_path / "memory.db")
    return store, PursuitResearch(
        store=store, embed=embed, client=Client(), model="test",
        enabled=lambda: True, user_name=lambda: "Jacob",
    )


def test_request_is_bounded_neutral_and_one_pending_per_project(tmp_path):
    store, worker = research(tmp_path)
    current = state()
    first = worker.request(current, datetime.now(timezone.utc))
    assert worker.request(current, datetime.now(timezone.utc)) == first
    question = store.get(first)
    assert question.metadata["curiosity_purpose"] == "research"
    assert question.metadata["hobby_key"] == "basil"
    assert len(store.iter_by_kind("open_question")) == 1


def test_result_matches_project_incarnation_not_just_name(tmp_path):
    store, worker = research(tmp_path)
    current = state()
    result = store.add(
        content="Basil needs bright light.", kind="curiosity_finding", embedding=embed(""),
        metadata={
            "hobby_key": "basil", "hobby_started_at": current["started_at"],
            "research_evidence": [{"snippet": "Bright light"}],
        },
        skip_dedupe=True,
    )
    assert worker.result(current).id == result.id
    assert worker.result({**current, "research_result_ids": [result.id]}) is None
    assert worker.result({**current, "started_at": "2026-10-02"}) is None


def test_hobby_waits_for_persisted_result_and_applies_it_once(tmp_path):
    store, researcher = research(tmp_path)
    data = {KV_CURRENT_HOBBY: json.dumps(state())}
    db = SimpleNamespace(kv_get=data.get, kv_set=lambda key, value: data.update({key: value}))
    worker = HobbyWorker(
        chat_db=db, agent_settings=SimpleNamespace(hobby_worker_enabled=True),
        memory_settings=SimpleNamespace(hobby_advance_min_hours=0, hobby_milestone_every=0),
        user_display_name_provider=lambda: "Jacob", research=researcher, rng=random.Random(1),
    )
    assert worker.run()["waiting"]
    assert load_hobby(db.kv_get)["progress"] == 0
    current = load_hobby(db.kv_get)
    saved = store.add(
        content="Basil needs bright light.", kind="curiosity_finding", embedding=embed(""),
        metadata={
            "hobby_key": "basil", "hobby_started_at": current["started_at"],
            "research_evidence": [{"snippet": "Bright light"}],
        },
        skip_dedupe=True,
    )
    assert worker.run()["research_memory_id"] == saved.id
    assert load_hobby(db.kv_get)["progress"] == 1
    assert worker.run()["waiting"]
    assert load_hobby(db.kv_get)["progress"] == 1


def test_hobby_without_research_cannot_earn_timer_progress():
    data = {KV_CURRENT_HOBBY: json.dumps(state())}
    db = SimpleNamespace(kv_get=data.get, kv_set=lambda key, value: data.update({key: value}))
    worker = HobbyWorker(
        chat_db=db, agent_settings=SimpleNamespace(hobby_worker_enabled=True),
        memory_settings=SimpleNamespace(hobby_advance_min_hours=0),
        user_display_name_provider=lambda: "Jacob",
    )
    assert worker.run()["reason"] == "research_unavailable"
    assert load_hobby(db.kv_get)["progress"] == 0
    assert not worker.is_ready(now=datetime.now(timezone.utc), last_run_at=None)


def test_first_result_replaces_unsupported_legacy_counters(tmp_path):
    store, researcher = research(tmp_path)
    current = {**state(), "progress": 10, "advances": 10, "unit": "lesson"}
    saved = store.add(
        content="Basil needs bright light.", kind="curiosity_finding", embedding=embed(""),
        metadata={
            "hobby_key": "basil", "hobby_started_at": current["started_at"],
            "research_evidence": [{"snippet": "Bright light"}],
        }, skip_dedupe=True,
    )
    data = {KV_CURRENT_HOBBY: json.dumps(current)}
    db = SimpleNamespace(kv_get=data.get, kv_set=lambda key, value: data.update({key: value}))
    worker = HobbyWorker(
        chat_db=db, agent_settings=SimpleNamespace(hobby_worker_enabled=True),
        memory_settings=SimpleNamespace(hobby_advance_min_hours=0),
        user_display_name_provider=lambda: "Jacob", research=researcher,
    )
    assert worker.run()["research_memory_id"] == saved.id
    current = load_hobby(db.kv_get)
    assert (current["progress"], current["advances"], current["unit"]) == (1, 1, "finding")


def test_diary_and_optional_cue_survive_progress_retry_without_duplicates(tmp_path):
    store, researcher = research(tmp_path)
    cues = CueStore(ChatDatabase(tmp_path / "memory.db"))
    current = {**state(), "progress": 2, "advances": 2, "progress_source": "research"}
    saved = store.add(
        content="Basil needs bright light.", kind="curiosity_finding", embedding=embed(""),
        metadata={
            "hobby_key": "basil", "hobby_started_at": current["started_at"],
            "research_evidence": [{"snippet": "Bright light"}],
            "source_query": "basil light requirements",
        }, skip_dedupe=True,
    )
    data = {KV_CURRENT_HOBBY: json.dumps(current)}
    failures = []

    def persist(key, value):
        if not failures:
            failures.append(True)
            raise OSError("disk full")
        data[key] = value

    db = SimpleNamespace(kv_get=data.get, kv_set=persist)
    worker = HobbyWorker(
        chat_db=db, agent_settings=SimpleNamespace(hobby_worker_enabled=True),
        memory_settings=SimpleNamespace(hobby_advance_min_hours=0, hobby_milestone_every=3),
        user_display_name_provider=lambda: "Jacob", research=researcher,
        cue_store_provider=lambda: cues,
    )
    with pytest.raises(OSError, match="disk full"):
        worker.run()
    assert load_hobby(db.kv_get)["progress"] == 2
    assert worker.run()["research_memory_id"] == saved.id
    assert load_hobby(db.kv_get)["progress"] == 3
    assert len(store.iter_by_kind("diary")) == 1
    assert store.iter_by_kind("diary")[0].metadata["research_memory_id"] == saved.id
    assert cues.count_pending("research_result") == 1

    host = CuePoolMixin()
    host._cue_store = cues
    host._surfaced_pool_cues = []
    row = host.take_pool_cue("research_result")
    assert row.payload["research_memory_id"] == saved.id
    host._settle_pool_cues(user_text="Dinner?", assistant_text="We could have noodles.")
    assert cues.count_pending("research_result") == 1
    host.take_pool_cue("research_result")
    host._settle_pool_cues(
        user_text="What did you find?", assistant_text="The excerpts suggested basil likes light.",
    )
    assert cues.count_pending("research_result") == 0


def test_diary_does_not_store_stale_relative_time(tmp_path):
    store, researcher = research(tmp_path)
    saved = store.add(
        content="Basil is flowering today.", kind="curiosity_finding", embedding=embed(""),
        skip_dedupe=True,
    )
    current = {**state(), "research_result_ids": [saved.id]}
    diary_id = researcher.write_diary(current, datetime.now(timezone.utc))
    assert "today" not in store.get(diary_id).content
    assert store.get(diary_id).metadata["research_memory_id"] == saved.id


def test_neutral_question_still_cannot_contain_private_contact_data(tmp_path):
    store, researcher = research(tmp_path)
    researcher._client = SimpleNamespace(chat_json=lambda *_args, **_kwargs: (
        json.dumps({"researchable": True, "question": "What does jacob@example.com grow?"}), None,
    ))
    assert researcher.request(state(), datetime.now(timezone.utc)) is None
    assert store.iter_by_kind("open_question") == []


def test_resolved_question_cannot_be_reissued_as_new_progress(tmp_path):
    store, researcher = research(tmp_path)
    current = state()
    first = researcher.request(current, timephrase.utcnow())
    store.update(first, metadata={**store.get(first).metadata,
                                  "curiosity_resolved_at": timephrase.utcnow().isoformat()})
    assert researcher.request(current, timephrase.utcnow()) is None
    assert len(store.iter_by_kind("open_question")) == 1


def test_reworded_finding_does_not_advance_hobby_but_new_answer_does(tmp_path):
    store, researcher = research(tmp_path)
    current = state()
    meta = {"hobby_key": current["key"], "hobby_started_at": current["started_at"],
            "research_evidence": [{"snippet": "Example source"}]}
    old = store.add("Basil plants need six hours of direct sunlight for healthy growth.",
                    "curiosity_finding", embed(""), metadata=meta, skip_dedupe=True)
    store.add("For healthy growth basil plants need six hours of direct sunlight.",
              "curiosity_finding", embed(""), metadata=meta, skip_dedupe=True)
    current["research_result_ids"] = [old.id]
    assert researcher.result(current) is None
    new = store.add("Basil should be watered when the top layer of soil dries out.",
                    "curiosity_finding", embed(""), metadata=meta, skip_dedupe=True)
    assert researcher.result(current).id == new.id


def test_research_context_keeps_project_and_multiple_dated_findings(tmp_path):
    store, researcher = research(tmp_path)
    current = state()
    for content in ["Earlier result about leaf shape.", "Later result about sunlight."]:
        store.add(content, "curiosity_finding", embed(""), skip_dedupe=True,
                  metadata={"hobby_key": current["key"],
                            "hobby_started_at": current["started_at"]})
    calls = []
    def compose(messages, **kwargs):
        calls.extend(messages)
        return json.dumps({"researchable": False}), None
    researcher._client = SimpleNamespace(chat_json=compose)
    assert researcher.request(current, timephrase.utcnow()) is None
    prompt = calls[0]["content"]
    assert "basil light requirements" in prompt
    assert "Earlier result about leaf shape." in prompt
    assert "Later result about sunlight." in prompt
    assert "cannot redefine it" in prompt


@pytest.mark.parametrize("other", [
    "Basil plants need 8 hours of direct sunlight for healthy growth.",
    "Basil plants do not need 6 hours of direct sunlight for healthy growth.",
    "Basil plants don't need 6 hours of direct sunlight for healthy growth.",
    "Basil plants don\u2019t need 6 hours of direct sunlight for healthy growth.",
])
def test_changed_number_or_negation_is_not_a_repeat(other):
    from app.core.proactive.pursuit_research import _near_repeat

    assert not _near_repeat("Basil plants need 6 hours of direct sunlight for healthy growth.",
                            other)


def test_number_words_in_findings_are_preserved():
    from app.core.proactive.pursuit_research import _near_repeat

    assert not _near_repeat("Basil plants need six hours of direct sunlight for healthy growth.",
                            "Basil plants need eight hours of direct sunlight for healthy growth.")
