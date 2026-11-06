"""Source meaning survives both direct and associative recall."""
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from app.core.memory.memory_origin import MemoryOrigin
from app.core.rag.rag_retriever import RagRetriever
from app.core.rag.rag_store import MemoryRecord, RagHit


def memory(subject, mode, domain="conversation", **extra):
    return SimpleNamespace(
        id=1, content="A qualified memory, only when invited.", kind="fact",
        salience=0.5, source_session="s", source_message_id=2,
        created_at="2026-10-01T10:00:00+00:00", last_used_at=None, use_count=0,
        metadata={"memory_origin": asdict(MemoryOrigin(subject, mode, "test", domain))},
        provenance="inferred", temporal_type="past_event", event_time=None, **extra,
    )


@pytest.mark.parametrize("subject,mode,domain,header", [
    ("user", "observation", "conversation", "What you know about Jacob"),
    ("aiko", "intention", "conversation", "Things you've shared"),
    ("aiko", "virtual_event", "virtual", "Things you've shared"),
    ("relationship", "reflection", "conversation", "shared relationship"),
    ("world", "world_fact", "external", "Learned world knowledge"),
    ("unknown", "unknown", "conversation", "unresolved subject"),
])
def test_origin_routes_memory_without_reassigning_its_subject(subject, mode, domain, header):
    hit = RagRetriever._memory_to_hit(memory(subject, mode, domain), score=0.8)
    block = RagRetriever.format_block([hit], user_display_name="Jacob")
    assert header in block
    assert f"subject={subject}; source={mode}; domain={domain}" in block
    assert "only when invited" in block
    if subject != "user":
        assert "What you know about Jacob" not in block


def test_expansion_keeps_virtual_source_and_unknown_occurrence():
    row = memory("aiko", "virtual_event", "virtual")
    hit = RagRetriever._memory_to_hit(row, score=0.8, expansion=True)
    block = RagRetriever.format_block([hit])
    assert "Related notes from the same topic" in block
    assert "subject=aiko; source=virtual_event; domain=virtual" in block
    assert "noted" in block or "recorded" in block
    assert "only when invited" in block


def test_unhydrated_vector_kind_is_not_authority_for_subject():
    record = MemoryRecord("1", "A fact of unresolved origin.", "fact", 0.5,
                          None, None, "2026-10-01", None, 0)
    block = RagRetriever.format_block([RagHit("memory", 0.8, record)])
    assert "unresolved subject" in block
    assert "subject=unknown" in block
    assert "What you know about" not in block


def test_expansion_never_loses_end_qualification_to_prefix_truncation():
    row = memory("user", "observation")
    row.content = "Some detail. " * 35 + "Only in a fictional scene, never in real life."
    block = RagRetriever.format_block([
        RagRetriever._memory_to_hit(row, score=0.8, expansion=True),
    ])
    assert "never in real life" in block
