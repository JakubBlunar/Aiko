from datetime import datetime, timezone

from app.core.memory.research_evidence import (
    evidence_metadata,
    researchable_question,
    source_indices,
)


def test_citation_points_to_supporting_result_not_first_hit():
    rows = [
        {"title": "Wrong topic", "url": "https://example.org/one", "snippet": "Unrelated"},
        {"title": "Supporting", "url": "https://example.org/two", "snippet": "Evidence"},
    ]
    metadata = evidence_metadata(rows, (1,), datetime.now(timezone.utc))
    assert metadata["source_url"] == "https://example.org/two"
    assert metadata["supporting_source_indices"] == [1]
    assert metadata["research_evidence"] == rows


def test_legacy_completion_does_not_invent_primary_source():
    rows = [{"url": "https://example.org/one"}, {"url": "https://example.org/two"}]
    metadata = evidence_metadata(rows, (), datetime.now(timezone.utc))
    assert metadata["source_url"] == ""
    assert metadata["evidence_support"] == "unattributed"


def test_evidence_is_bounded_and_indices_validated():
    row = {"title": "x" * 300, "url": "https://example.org", "snippet": "x" * 900}
    metadata = evidence_metadata([row] * 8, (3, 8), datetime.now(timezone.utc))
    assert len(metadata["research_evidence"]) == 4
    assert len(metadata["research_evidence"][0]["title"]) == 120
    assert len(metadata["research_evidence"][0]["snippet"]) == 400
    assert metadata["supporting_source_indices"] == [3]
    assert source_indices([True, 1, 1, -1, "2", 7]) == (1,)


def test_question_purpose_is_not_inferred_from_public_character_pronouns():
    assert researchable_question("Why does she leave in chapter three?", {})
    assert not researchable_question("Maybe ask Jacob about his favourite chapter", {})
    assert not researchable_question("A feeling to reflect on", {"curiosity_purpose": "reflection"})
