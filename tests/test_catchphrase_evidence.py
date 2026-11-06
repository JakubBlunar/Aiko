from types import SimpleNamespace

import numpy as np
import pytest

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.catchphrase_miner import (
    CatchphraseMiner, bless_inside_joke, detect_inside_joke_birth,
)
from app.core.memory.memory_store import MemoryStore
from app.core.memory.observation_receipts import longitudinal_sources_current
from app.core.proactive.voice_adoption_worker import VoiceAdoptionWorker
from app.core.relationship.shared_moments import SharedMomentsStore


PHRASE = "fish-shaped cookie energy"


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "phrases.db"
    db = ChatDatabase(path)
    store = MemoryStore(path)
    embed = SimpleNamespace(embed=lambda text: np.array([1., 0.]))
    try:
        yield db, store, embed
    finally:
        store.close()


def test_mined_phrase_retains_occurrences_and_cannot_claim_original_authorship(setup):
    db, store, embed = setup
    ids = [db.add_message("s", role, PHRASE) for role in ("user", "assistant", "user")]
    worker = CatchphraseMiner(chat_db=db, memory_store=store, embedder=embed,
                              min_seconds_between=0, min_new_user_turns=0)
    assert worker.maybe_run(session_key="s") == 1
    row = store.iter_by_kind("catchphrase")[0]
    receipt = row.metadata["catchphrase_evidence"]
    assert [s["message_id"] for s in receipt["sources"]] == ids
    assert receipt["count"] == 3 and receipt["user_count"] == 2
    assert row.metadata["first_observed_speaker"] == "user"
    assert row.metadata["origin"] == "unknown"
    host = SimpleNamespace(_origin_resolver=lambda phrase: "user")
    assert VoiceAdoptionWorker._origin_of(host, row, PHRASE) is None
    assert longitudinal_sources_current(row, store)
    db.execute_commit("UPDATE messages SET content=? WHERE id=?", ("Corrected wording", ids[0]))
    assert not longitudinal_sources_current(row, store)


def test_birth_keeps_both_sides_and_uses_echo_time_not_worker_time(setup):
    db, store, embed = setup
    origin = db.add_message("s", "assistant", "Classic " + PHRASE)
    echo = db.add_message("s", "user", "lol " + PHRASE)
    db.execute_commit("UPDATE messages SET created_at=? WHERE id=?",
                      ("2026-11-04T07:00:00+00:00", origin))
    db.execute_commit("UPDATE messages SET created_at=? WHERE id=?",
                      ("2026-11-04T07:01:00+00:00", echo))
    birth = detect_inside_joke_birth(user_text="lol " + PHRASE,
                                      origins=[(origin, "Classic " + PHRASE)])
    out = bless_inside_joke(birth, memory_store=store, embedder=embed,
                            moments_store=SharedMomentsStore(memory_store=store, embedder=embed),
                            session_key="s", source_message_id=echo, chat_db=db)
    assert out["catchphrase_id"] and out["moment_id"]
    row = store.get(out["moment_id"])
    assert row.metadata["source_message_ids"] == [origin, echo]
    assert row.event_time == "2026-11-04T07:01:00+00:00"
    assert row.metadata["when_basis"] == "cited_exchange_time"
    assert "became a running bit" not in row.content
    assert longitudinal_sources_current(row, store)
    db.execute_commit("UPDATE messages SET content=? WHERE id=?", ("Edited echo", echo))
    assert not longitudinal_sources_current(row, store)


def test_stale_source_during_embedding_cannot_mint_a_phrase(setup):
    db, store, _ = setup
    for role in ("user", "assistant", "user"):
        db.add_message("s", role, PHRASE)
    def embed(text):
        db.execute_commit("UPDATE messages SET content=?", ("Revised source",))
        return np.array([1., 0.])
    worker = CatchphraseMiner(chat_db=db, memory_store=store,
                              embedder=SimpleNamespace(embed=embed), min_seconds_between=0,
                              min_new_user_turns=0)
    assert worker.maybe_run(session_key="s") == 0
    assert store.count() == 0


def test_missing_echo_source_does_not_invent_exchange_time_or_write(setup):
    db, store, embed = setup
    origin = db.add_message("s", "assistant", "Classic " + PHRASE)
    birth = detect_inside_joke_birth(user_text="lol " + PHRASE,
                                      origins=[(origin, "Classic " + PHRASE)])
    assert bless_inside_joke(birth, memory_store=store, embedder=embed, session_key="s",
                             source_message_id=999, chat_db=db) == {
                                 "catchphrase_id": None, "moment_id": None}
    assert store.count() == 0
