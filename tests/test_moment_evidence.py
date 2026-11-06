from datetime import timedelta
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore
from app.core.memory.memory_origin import memory_origin
from app.core.relationship.moment_evidence import admit
from app.core.relationship.shared_moment_extractor import SharedMomentCandidate
from app.core.relationship.shared_moments import SharedMomentsStore
from app.core.relationship.anniversary import pick_anniversary


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "moments.db"
    db = ChatDatabase(path)
    mid = db.add_message("s", "user", "The cookie jar joke made me laugh.")
    other = db.add_message("s", "assistant", "An unrelated later message.")
    store = MemoryStore(path)
    embedder = SimpleNamespace(embed=lambda _: np.ones(4))
    yield db, store, SharedMomentsStore(memory_store=store, embedder=embedder), mid, other
    store.close()


def candidate(mid, text="The cookie jar joke made me laugh.", scope="exchange"):
    return SharedMomentCandidate("An unchecked extra detail", "playful", source="llm", evidence={
        "claims": [{"text": "We laughed about the cookie jar.",
                    "evidence": [{"message_id": mid, "quote": text}]}], "event_scope": scope,
    })


def test_only_supporting_lines_establish_exchange_time_and_stored_claims(setup):
    db, store, moments, mid, other = setup
    db.execute_commit("UPDATE messages SET created_at=? WHERE id=?",
                      ("2026-10-01T12:00:00Z", mid))
    value = candidate(mid)
    assert admit(value, [db.get_message_row(mid), db.get_message_row(other)])
    assert value.source_message_ids == [mid]
    assert value.when.startswith("2026-10-01T12:00:00")
    assert "unchecked" not in value.summary
    saved = moments.add_from_candidate(value)
    assert saved.when_basis == "cited_exchange_time"
    assert store.get(saved.id).metadata["moment_evidence"]["sources"][0]["message_id"] == mid


def test_recalled_event_can_have_unknown_time_without_a_false_anniversary(setup):
    db, store, moments, mid, _ = setup
    value = candidate(mid, scope="recalled_event")
    assert admit(value, [db.get_message_row(mid)]) and value.when is None
    saved = moments.add_from_candidate(value)
    assert saved.when_basis == "unknown_occurrence"
    assert store.get(saved.id).event_time is None
    assert pick_anniversary([saved], now=timephrase.utcnow() + timedelta(days=365)) is None


def test_recalled_date_is_resolved_from_a_literal_user_phrase_not_the_last_message(setup):
    db, _, _, mid, other = setup
    text = "The museum visit on September 30 was funny."
    db.execute_commit("UPDATE messages SET content=?,created_at=? WHERE id=?",
                      (text, "2026-10-01T12:00:00Z", mid))
    value = candidate(mid, text, scope="recalled_event")
    value.evidence["claims"][0]["text"] = "We recalled the funny museum visit."
    value.evidence["event_time_evidence"] = {"message_id": mid, "quote": "September 30"}
    assert admit(value, [db.get_message_row(mid), db.get_message_row(other)])
    assert value.when.startswith("2026-09-30") and value.evidence["precision"] == "day"


def test_fabricated_quote_or_unoffered_id_is_refused_and_virtual_scope_survives(setup):
    db, store, moments, mid, _ = setup
    assert not admit(candidate(999), [db.get_message_row(mid)])
    assert not admit(candidate(mid, "An invented utterance"), [db.get_message_row(mid)])
    value = candidate(mid, scope="shared_virtual")
    assert admit(value, [db.get_message_row(mid)])
    saved = moments.add_from_candidate(value)
    assert memory_origin(store.get(saved.id)).domain == "virtual"


def test_source_edit_before_persistence_defers_the_moment(setup):
    db, _, moments, mid, _ = setup
    value = candidate(mid)
    assert admit(value, [db.get_message_row(mid)])
    db.execute_commit("UPDATE messages SET content=? WHERE id=?", ("A corrected message", mid))
    assert moments.add_from_candidate(value) is None


def test_manual_revision_does_not_keep_claim_citations_for_different_wording(setup):
    db, store, moments, mid, _ = setup
    value = candidate(mid)
    assert admit(value, [db.get_message_row(mid)])
    saved = moments.add_from_candidate(value)
    moments.update(saved.id, summary="A deliberately revised description of the moment")
    metadata = store.get(saved.id).metadata
    assert "moment_evidence" not in metadata
    assert metadata["prior_moment_evidence"]


def test_recording_time_alone_never_creates_an_anniversary_and_same_evidence_can_dedupe(setup):
    db, store, moments, mid, _ = setup
    manual = moments.add(summary="A manually preserved memory with unknown occurrence time",
                         vibe="warm")
    assert manual.when_basis == "recording_time_only"
    assert pick_anniversary([manual], now=timephrase.utcnow() + timedelta(days=365)) is None
    first = candidate(mid)
    assert admit(first, [db.get_message_row(mid)])
    saved = moments.add_from_candidate(first)
    second = candidate(mid)
    second.evidence["claims"][0]["text"] = "We shared a laugh about the cookie jar."
    assert admit(second, [db.get_message_row(mid)])
    assert moments.add_from_candidate(second) is None
    assert store.get(saved.id) is not None
    assert len(store.iter_by_kind("shared_moment")) == 2  # One manual, one cited exchange.
