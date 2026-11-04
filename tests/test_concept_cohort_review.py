import sqlite3

import pytest

from scripts.review_concept_cohort import _source, collect, score_annotations, select_sample


def test_sampling_is_reproducible_and_covers_subject_kind_status():
    rows = [{"id": i, "subject": "user" if i < 8 else "aiko", "kind": "value",
             "status": "active"} for i in range(10)]
    picked = select_sample(rows, limit=4)
    assert picked == select_sample(list(reversed(rows)), limit=4)
    assert {row["subject"] for row in picked} == {"user", "aiko"}
    assert len({row["id"] for row in picked}) == 4


def test_unjudged_or_outside_sample_annotations_do_not_inflate_quality():
    result = score_annotations([{"id": 1}, {"id": 2}], {
        "1": {"supported": False}, "999": {"supported": True},
    })
    assert result["supported"] == {"judged": 1, "passed": 0, "rate": 0}
    assert result["distinct"]["rate"] is None
    with pytest.raises(ValueError):
        score_annotations([{"id": 1}], {"1": {"supported": "yes"}})


def test_review_keeps_claim_and_original_speaker_separate_and_never_writes(tmp_path):
    from app.core.infra.chat_database import ChatDatabase

    path = tmp_path / "review.db"
    db = ChatDatabase(path)
    db.execute_commit(
        "INSERT INTO concepts(id,label,rationale,kind,subject,status,created_at,updated_at) "
        "VALUES(1,'Coffee tonight is a permanent rule','Interpretation','value','user',"
        "'active','2026-11-01','2026-11-01')",
    )
    before = path.read_bytes()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        report = collect(conn, since="2026-10-28")
    finally:
        conn.close()
    assert report["sample"][0]["review_flags"] == ["relative_time"]
    assert report["sample"][0]["evidence"] == []
    assert report["scoreboard"]["supported"]["judged"] == 0
    assert path.read_bytes() == before


def test_cluster_edges_resolve_representative_ids_not_database_cluster_ids(tmp_path):
    from app.core.infra.chat_database import ChatDatabase

    path = tmp_path / "clusters.db"
    db = ChatDatabase(path)
    db.kv_set("aiko.topic_label.42", '{"label":"shared coffee"}')
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        source = _source(conn, "cluster", "42")
    finally:
        conn.close()
    assert source["label"] == "shared coffee"
    assert source["representative"]["missing"] is True
    assert source["historical_membership"] == "not_reconstructed"
