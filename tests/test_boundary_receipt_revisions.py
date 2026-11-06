"""Receipt-only changes matter without rewriting legacy support fingerprints."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.core.concepts.concept_support import memory_revision
from app.core.memory.memory_integrity import compatible_memories
from app.core.memory.reflective_evidence import revision


CONTRACTS = ("hypothesis_answer", "hypothesis_support", "image_evidence",
             "pursuit_evidence", "catchphrase_evidence", "gap_answer")


def row(metadata=None):
    return SimpleNamespace(content="A qualified source.", kind="fact", event_time=None,
                           created_at="2026-11-05T10:00:00Z", metadata=metadata or {},
                           temporal_type="durable", provenance="stated")


def test_legacy_support_fingerprint_stays_identical():
    assert memory_revision(row()) == "641a3a1b3ca4f9c49b77ce44066fc470"
    legacy = row({"hypothesis_answer": {"mode": "direct", "verdict": "confirm"}})
    assert memory_revision(legacy) == "641a3a1b3ca4f9c49b77ce44066fc470"


@pytest.mark.parametrize("contract", CONTRACTS)
def test_changed_receipt_changes_both_concept_and_queued_source_revision(contract):
    before = row({contract: {"version": 1, "sources": [{"message_id": 1}]}})
    after = deepcopy(before)
    after.metadata[contract]["sources"][0]["message_id"] = 2
    assert memory_revision(before) != memory_revision(after)
    assert revision(before) != revision(after)


@pytest.mark.parametrize("contract", CONTRACTS)
def test_equal_wording_with_different_question_or_episode_receipts_cannot_merge(contract):
    before = row({contract: {"version": 1, "scope_id": 1}})
    after = row({contract: {"version": 1, "scope_id": 2}})
    assert not compatible_memories(before, after)
    assert not compatible_memories(before, after, consolidation=True)


def test_recall_budget_accounts_for_whole_qualified_note_and_source_labels():
    from app.core.rag.rag_retriever import RagRetriever
    from app.core.rag.rag_store import MemoryRecord, RagHit
    from app.core.memory.memory_origin import MemoryOrigin
    from tests.test_context_budget import RegionBuilderTests

    host, _ = RegionBuilderTests()._host()
    host._concept_store = None
    host._topic_graph = None
    text = "A detailed virtual scene. " * 60 + "Only imagined; no physical event occurred."
    record = MemoryRecord("1", text, "pursuit_note", .5, None, None,
                          "2026-11-05T10:00:00Z", None, 0)
    hit = RagHit("memory", .9, record,
                 memory_origin=MemoryOrigin("aiko", "virtual_event", "test", "virtual"))
    host._rag_retriever = SimpleNamespace(
        candidates=lambda *args, **kw: [hit],
        format_hits=lambda hits, **kw: RagRetriever.format_block(hits, **kw),
    )
    small = host.build_relevant_context(user_text="scene", recent_turns=[], session_key="s",
                                        budget_tokens=100, degrade_level=0)
    assert not small.text
    enough = host.build_relevant_context(user_text="scene", recent_turns=[], session_key="s",
                                         budget_tokens=2000, degrade_level=0)
    assert "Only imagined; no physical event occurred" in enough.text
    assert "subject=aiko" in enough.text
