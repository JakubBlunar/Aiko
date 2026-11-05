"""Adversarial source/scope checks with a real SQLite memory store."""
from dataclasses import asdict
from datetime import timedelta

import numpy as np
import pytest

from app.core.infra.chat_database import ChatDatabase
from app.core.infra import timephrase
from app.core.memory.memory_consolidator import MemoryConsolidator
from app.core.memory.memory_origin import MemoryOrigin, memory_origin
from app.core.memory.memory_store import MemoryStore


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "integrity.db"
    ChatDatabase(path)
    mem = MemoryStore(path)
    yield mem
    mem.close()


VECTOR = np.array([1., 0., 0.], dtype=np.float32)


def add(store, content="Noah enjoys quiet walks", **kwargs):
    return store.add(content, kwargs.pop("kind", "fact"), VECTOR, **kwargs)


@pytest.mark.parametrize("other", [
    {"kind": "self"}, {"kind": "knowledge"}, {"kind": "pre_thought"},
    {"provenance": "stated"},
    {"temporal_type": "past_event", "event_time": "2026-10-02T12:00:00+00:00"},
    {"metadata": {"memory_origin": asdict(MemoryOrigin("world", "world_fact", "search"))}},
])
def test_identical_vector_cannot_erase_different_source_scope(store, other):
    first = add(store)
    second = add(store, **other)
    assert second.id != first.id
    assert store.count() == 2


def test_true_duplicate_still_dedupes(store):
    add(store)
    assert add(store) is None
    assert store.count() == 1


def test_different_or_unknown_event_dates_do_not_collapse(store):
    for date in ("2026-10-01T12:00:00+00:00", "2026-10-02T12:00:00+00:00", None, None):
        add(store, "Noah took a quiet walk", temporal_type="past_event", event_time=date)
    assert store.count() == 4


def test_a_new_undated_plan_cannot_be_absorbed_by_an_old_plan(store):
    first = add(store, "Noah plans a quiet walk", temporal_type="future_plan")
    first.created_at = (timephrase.utcnow() - timedelta(days=2)).isoformat()
    second = add(store, "Noah plans a quiet walk", temporal_type="future_plan")
    assert second is not None and second.id != first.id


def test_rewrites_keep_original_but_dont_inherit_testimony(store):
    row = add(store, provenance="stated", source_message_id=42)
    store.update(row.id, salience=.9)
    assert row.provenance == "stated"
    store.update(row.id, content="Noah enjoys quiet walks near the river")
    assert row.provenance == "inferred"
    assert row.metadata["content_versions"][0]["content"] == "Noah enjoys quiet walks"
    assert row.metadata["content_versions"][0]["provenance"] == "stated"
    assert row.source_message_id == 42  # Basis retained, not a new literal quote.
    assert memory_origin(row).subject == "user"
    assert memory_origin(row).mode == "interpretation"
    for n in range(4):
        store.update(row.id, content=f"Noah enjoys quiet walks near river {n}")
    assert len(row.metadata["content_versions"]) == 2
    assert row.metadata["original_content"]["content"] == "Noah enjoys quiet walks"
    assert row.metadata["original_content"]["provenance"] == "stated"
    store.update(row.id, content="Noah prefers forest walks", provenance="stated")
    assert row.provenance == "stated"
    assert memory_origin(row).writer == "manual_edit"


def test_update_has_the_same_temporal_backstop_as_insert(store):
    row = add(store)
    store.update(row.id, content="Noah took a walk yesterday")
    assert row.temporal_type == "past_event"
    assert row.event_time is None
    assert row.relevance_until
    store.update(row.id, content="Noah plans a walk", temporal_type="past_event",
                 event_time="2099-01-01T12:00:00+00:00")
    assert row.temporal_type == "future_plan"


def test_merge_preserves_sources_without_fabricating_use_or_repointing_evidence(store):
    first = add(store, provenance="stated", salience=.8)
    second = add(store, "Noah likes walks near the river", provenance="stated", skip_dedupe=True)
    repoints = []
    worker = MemoryConsolidator(ollama=None, memory_store=store, chat_db=None, model="",
                               repoint_memory_edges=lambda *args: repoints.append(args))
    assert worker._apply_merge(first, [second], first.content) == 1
    assert store.get(second.id).content == "Noah likes walks near the river"
    assert second.tier == "archive"
    assert second.metadata["consolidated_into"] == first.id
    assert first.use_count == 0
    assert first.provenance == "stated"
    assert first.metadata["source_revisions"]
    assert not repoints


def test_merge_preserves_observation_roots_across_repeated_compaction_without_refreshing(store):
    from app.core.concepts.concept_evidence_lineage import memory_lineages, support_summary
    from app.core.concepts.concept_support import memory_revision

    first = add(store, source_message_id=42, provenance="stated", salience=.8)
    second = add(store, source_message_id=43, provenance="stated", skip_dedupe=True)
    worker = MemoryConsolidator(ollama=None, memory_store=store, chat_db=None, model="")
    before = memory_revision(first)
    assert worker._apply_merge(first, [second], first.content) == 1
    report = support_summary(memory_lineages(first))
    assert report["known_support_groups"] == 2
    assert report["root_count"] == 2
    assert memory_revision(first) == before  # Packaging old roots is not a new observation.
    third = add(store, source_message_id=44, provenance="stated", skip_dedupe=True)
    worker._apply_merge(first, [third], first.content)
    assert support_summary(memory_lineages(first))["root_count"] == 3
    assert store.get(second.id) is not None
    assert first.use_count == 0


def test_merge_defers_if_a_source_was_corrected_since_the_generation_snapshot(store):
    from app.core.memory.memory_integrity import merge_source_key

    first = add(store, salience=.8)
    second = add(store, skip_dedupe=True)
    revisions = {row.id: merge_source_key(row) for row in (first, second)}
    store.update(second.id, content="Noah prefers a different setting")
    worker = MemoryConsolidator(ollama=None, memory_store=store, chat_db=None, model="")
    assert worker._apply_merge(first, [second], "A stale generated merge",
                               source_revisions=revisions) is None
    assert first.content == "Noah enjoys quiet walks"
    assert second.tier == "long_term" and "consolidated_into" not in second.metadata
