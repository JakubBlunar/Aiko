from types import SimpleNamespace as NS

from app.core.concepts.evidence_projection import compact_basis, project_evidence


def memory(mid, source=10):
    return NS(id=mid, content="Prefers experiments when learning systems", kind="fact",
              provenance="stated", metadata={}, source_message_id=source,
              source_session="s", created_at="2026-10-01T12:00:00+00:00", event_time=None)


def project(rows, *, limit=3, viable=True):
    concept = NS(concept_id=1, confidence=.8, distinct_source_count=2, updated_at="r1")
    edges = [NS(src_type="memory", src_id=str(i), polarity=1, strength=1) for i in rows]
    store = NS(get=lambda _: concept, evidence_of=lambda _: edges,
               dependencies=NS(assess=lambda *a, **k: {"tracked": True, "viable": viable}))
    return project_evidence(store, NS(get=rows.get), None, concept, limit=limit)


def test_paraphrases_share_a_source_and_do_not_establish_a_pattern():
    packet = project({1: memory(1), 2: memory(2)})
    assert packet["support"]["known_support_groups"] == 1
    assert "one episode" in packet["uncertainty"]
    assert packet["evidence"][0]["authority"] == "testimony"
    assert packet["evidence"][0]["source_message_id"] == 10
    assert "2026-10-01" in compact_basis(packet)


def test_source_revision_changes_and_missing_sources_are_not_recovered():
    row = memory(1)
    before = project({1: row})["evidence"][0]["revision"]
    row.content = "Prefers demonstrations when learning systems"
    assert project({1: row})["evidence"][0]["revision"] != before
    packet = project({1: None}, viable=False)
    assert packet["evidence"] == []
    assert "no longer sufficient" in packet["uncertainty"]


def test_full_packet_has_a_hard_cap_and_explicit_truncation():
    packet = project({i: memory(i, source=i) for i in range(1, 100)}, limit=1000)
    assert len(packet["evidence"]) == 16
    assert packet["truncated"]


def test_negative_edges_never_supply_the_supporting_excerpt():
    concept = NS(concept_id=1)
    store = NS(get=lambda _: concept, evidence_of=lambda _: [
        NS(src_type="memory", src_id="1", polarity=-1, strength=1)])
    assert project_evidence(store, NS(get=lambda _: memory(1)), None, concept)["evidence"] == []
