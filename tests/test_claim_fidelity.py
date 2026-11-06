from types import SimpleNamespace

import numpy as np
import pytest

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_admission import admit_memory
from app.core.memory.memory_store import MemoryStore
from app.core.memory.observation_receipts import longitudinal_sources_current


def admit(text, content, **extra):
    row = SimpleNamespace(id=1, role="user", session_id="s", content=text,
                          created_at="2026-10-01T12:00:00+00:00")
    value = {"kind": "event", "content": content, "temporal_type": "past_event",
             "evidence": [{"message_id": 1, "quote": text}], **extra}
    return admit_memory(value, [row], session_key="s", writer="extractor")


@pytest.mark.parametrize("source,content,accepted", [
    ("I planned to brush my teeth and I am back.", "Noah completed brushing his teeth", False),
    ("I brushed my teeth.", "Noah completed brushing his teeth", True),
    ("I finished the report.", "Noah completed the report", True),
    ("I have not finished the report.", "Noah completed the report", False),
    ("I will finish the report.", "Noah completed the report", False),
    ("I went for a walk.", "Noah often goes for walks", False),
    ("I often go for a walk.", "Noah often goes for walks", True),
    ("I prefer brief replies when tired.", "Noah prefers brief replies", False),
    ("I prefer brief replies when tired.", "Noah prefers brief replies when tired", True),
    ("I am back.", "Noah returned", True),
])
def test_source_fidelity_preserves_actual_actions_and_limits(source, content, accepted):
    assert admit(source, content).accepted == accepted


def test_inferred_meaning_is_tentative_and_keeps_its_basis():
    assert not admit("I am back.", "Noah completed the routine", basis="inferred").accepted
    value = admit("I am back.", "Noah may be ready to rest", basis="inferred")
    assert value.accepted and value.metadata["claim_fidelity"]["basis"] == "inferred"
    assert not admit("I am back.", "Noah returned", basis=[]).accepted


def test_event_time_requires_source_timing_and_does_not_inherit_model_precision():
    assert admit("I returned.", "Noah returned", event_time="2026-10-01T12:07:22Z"
                 ).metadata["claim_fidelity"]["event_time"] is None
    value = admit("I returned yesterday.", "Noah returned", event_time="2026-09-30T12:07:22Z",
                  event_time_evidence={"message_id": 1, "quote": "yesterday"})
    # The deadline helper intentionally supports a bounded vocabulary; the date
    # comes from the literal source and is explicitly day precision.
    receipt = value.metadata["claim_fidelity"]
    assert receipt["precision"] == "day"
    assert receipt["event_time"].startswith("2026-09-30T00:00:00")
    value = admit("I returned on September 30 at 18:30.", "Noah returned",
                  event_time_evidence={"message_id": 1, "quote": "September 30 at 18:30"})
    assert value.metadata["claim_fidelity"]["precision"] == "minute"
    assert "18:30" in value.metadata["claim_fidelity"]["event_time"]


def test_edited_testimony_invalidates_the_new_source_receipt(tmp_path):
    path = tmp_path / "sources.db"
    db = ChatDatabase(path)
    mid = db.add_message("s", "user", "I returned.")
    row = db.get_message_row(mid)
    value = admit_memory({"kind": "event", "content": "Noah returned",
        "evidence": [{"message_id": mid, "quote": row.content}]}, [row],
        session_key="s", writer="extractor")
    store = MemoryStore(path)
    try:
        memory = store.add("Noah returned", "event", np.ones(4), metadata=value.metadata)
        assert longitudinal_sources_current(memory, store)
        db.execute_commit("UPDATE messages SET content=? WHERE id=?", ("A correction", mid))
        assert not longitudinal_sources_current(memory, store)
        db.execute_commit("UPDATE messages SET content=?,created_at=? WHERE id=?",
                          (row.content, "2026-01-01T00:00:00Z", mid))
        assert not longitudinal_sources_current(memory, store)
        db.execute_commit("UPDATE messages SET created_at=? WHERE id=?", (row.created_at, mid))
        assert longitudinal_sources_current(memory, store)
    finally:
        store.close()
