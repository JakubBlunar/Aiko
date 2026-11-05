from types import SimpleNamespace

from app.core.conversation.topic_evidence import topic_evidence_profile, focus_evidence_context


def memory(mid, message, *, kind="fact", temporal="past_event", event="2026-10-01T10:00:00Z"):
    return SimpleNamespace(id=mid, content="A source note", kind=kind, metadata={},
                           source_message_id=message,
                           temporal_type=temporal, event_time=event, provenance="inferred",
                           created_at=f"2026-11-0{mid}T12:00:00Z")


def test_profile_keeps_note_count_roles_recording_dates_and_observation_groups_distinct():
    rows = {1: memory(1, 42), 2: memory(2, 42), 3: memory(3, 43, kind="diary"),
            4: memory(4, 44, temporal="future_plan", event="2027-01-01T10:00:00Z")}
    cluster = SimpleNamespace(member_ids=list(rows), representative_id=1)
    profile = topic_evidence_profile(cluster, SimpleNamespace(get=rows.get))
    assert profile["note_count"] == 4
    assert profile["recorded_observation_groups"] == 1
    assert profile["roles"]["aiko/reflection"] == 1
    assert profile["observed_event_span"]["to"].startswith("2026-10-01")
    assert profile["recorded_span"]["to"].startswith("2026-11-04")
    assert profile["planned_event_span"]["to"].startswith("2027-01-01")


def test_missing_unknown_and_capped_sources_remain_visible():
    rows = {1: memory(1, None), 2: memory(2, 42)}
    rows[2].metadata = {"consolidated_into": 1}
    profile = topic_evidence_profile(SimpleNamespace(member_ids=[1, 2, 3, 4]),
                                     SimpleNamespace(get=rows.get), limit=3)
    assert profile["unknown_lineage_notes"] == 1
    assert profile["excluded_notes"] == 1 and profile["missing_notes"] == 1
    assert profile["truncated"] and not profile["complete"]


def test_only_offered_focus_clusters_are_profiled():
    row = memory(1, 42)
    cluster = SimpleNamespace(member_ids=[1], representative_id=1)
    graph = SimpleNamespace(topic_clusters=lambda: [cluster])
    store = SimpleNamespace(get=lambda mid: row)
    assert focus_evidence_context([], graph, store) == ""
    text = focus_evidence_context([SimpleNamespace(rep=1)], graph, store)
    assert "not new evidence" in text and '"recorded_observation_groups":1' in text


def test_topic_dirtiness_tracks_semantic_changes_without_salience_churn():
    from app.core.conversation.topic_evidence import topic_source_fingerprint

    row = memory(1, 42)
    cluster = SimpleNamespace(member_ids=[1], representative_id=1)
    store = SimpleNamespace(get=lambda mid: row)
    before = topic_source_fingerprint(cluster, store)
    row.salience = .9
    assert topic_source_fingerprint(cluster, store) == before
    row.content = "A correction with a different context"
    assert topic_source_fingerprint(cluster, store) != before
