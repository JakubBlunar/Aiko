"""Offline surfacing receipts preserve attribution and expose missing coverage."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core.memory.surfacing_trace_store import MAX_TURNS, PREFIX, compact_trace, record_trace
from app.core.memory.turn_prompt_block_store import TurnPromptBlockStore
from app.core.session.post_turn_helpers_mixin import PostTurnHelpersMixin
from scripts.hypothesis_surface_report import collect


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "diagnostics.db")
    conn.executescript(
        "CREATE TABLE kv_meta (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);"
        "CREATE TABLE turn_prompt_blocks (assistant_message_id INTEGER, block TEXT, "
        "chars INTEGER, created_at TEXT);"
        "CREATE TABLE cue_pool (id INTEGER PRIMARY KEY, cue_type TEXT, subject TEXT, payload TEXT);"
    )
    yield SimpleNamespace(_get_conn=lambda: conn)
    conn.close()


def attempt(identity, state, *, secret=False):
    return {
        "attempt_id": identity,
        "system_prompt": "private transcript" if secret else "",
        "optional_choice": {"winner": "concept_hypothesis_block",
                            "focused_block": "concept_hypothesis_block"},
        "claims": [{"cue_id": 12, "cue_type": "concept_hypothesis", "state": state,
                    "blocks": ["concept_hypothesis_block"], "text": "private cue"}],
        "providers": {"concept_hypothesis": {"state": "included", "message": "secret"}},
    }


def test_recorder_persists_current_turn_final_attempt_without_text(db):
    host = PostTurnHelpersMixin()
    host._turn_prompt_block_store = TurnPromptBlockStore(db)
    host._record_prompt_blocks(
        assistant_message_id=21,
        telemetry=SimpleNamespace(block_chars={"persona": 100}, surfacing_trace=[
            attempt("first", "overflow_retry", secret=True), attempt("final", "committed"),
        ]),
    )
    raw, = db._get_conn().execute("SELECT value FROM kv_meta").fetchone()
    receipt = json.loads(raw)
    assert receipt["assistant_message_id"] == 21
    assert [item["claims"][0]["state"] for item in receipt["attempts"]] == [
        "overflow_retry", "committed",
    ]
    assert not any(word in raw for word in ("private", "secret", "system_prompt", '"text"'))


def test_retention_is_global_bounded_and_duplicate_turns_replace(db):
    db._get_conn().execute("INSERT INTO kv_meta VALUES ('unrelated', 'preserve', '')")
    db._get_conn().commit()
    for mid in range(1, MAX_TURNS + 3):
        assert record_trace(db, mid, [attempt("one", "committed")])
    assert record_trace(db, MAX_TURNS + 2, [attempt("replacement", "omitted")])
    rows = db._get_conn().execute(
        "SELECT key,value FROM kv_meta WHERE key LIKE ? ORDER BY key", (PREFIX + "%",),
    ).fetchall()
    assert len(rows) == MAX_TURNS
    assert json.loads(rows[0][1])["assistant_message_id"] == 3
    assert json.loads(rows[-1][1])["attempts"][0]["attempt_id"] == "replacement"
    assert db._get_conn().execute("SELECT value FROM kv_meta WHERE key='unrelated'").fetchone()


def test_missing_trace_is_not_recorded_as_an_empty_success(db):
    store = TurnPromptBlockStore(db)
    assert store.add_turn(30, {"persona": 100}) == 1
    assert not db._get_conn().execute("SELECT value FROM kv_meta").fetchall()
    assert not record_trace(db, 0, [attempt("bad", "committed")])
    assert not record_trace(db, "invalid", [attempt("bad", "committed")])


def test_invalid_diagnostic_cannot_undo_prompt_block_write(db):
    store = TurnPromptBlockStore(db)
    assert store.add_turn(30, {"persona": 100}, surfacing_trace=[None]) == 1
    assert db._get_conn().execute("SELECT assistant_message_id FROM turn_prompt_blocks").fetchone()
    assert not db._get_conn().execute("SELECT value FROM kv_meta").fetchall()


def test_retention_failure_rolls_back_only_the_diagnostic_transaction(db):
    conn = db._get_conn()
    conn.set_authorizer(lambda action, table, *args: sqlite3.SQLITE_DENY
                        if action == sqlite3.SQLITE_DELETE and table == "kv_meta"
                        else sqlite3.SQLITE_OK)
    try:
        assert TurnPromptBlockStore(db).add_turn(
            30, {"persona": 100}, surfacing_trace=[attempt("one", "committed")],
        ) == 1
    finally:
        conn.set_authorizer(None)
    assert conn.execute("SELECT assistant_message_id FROM turn_prompt_blocks").fetchone() == (30,)
    assert not conn.execute("SELECT value FROM kv_meta").fetchall()


def test_attempt_and_claim_truncation_are_reported():
    trace = attempt("large", "committed")
    trace["claims"] *= 70
    compact = compact_trace([trace, trace, trace])
    assert compact["attempts_omitted"] == 1
    assert all(row["claims_omitted"] == 6 for row in compact["attempts"])
    assert all(len(row["claims"]) == 64 for row in compact["attempts"])


def test_read_only_report_joins_verified_ask_to_final_commit_and_keeps_unknowns(db):
    conn = db._get_conn()
    payload = {"target_type": "hypothesis", "label": "Walking is thinking time",
               "asked_message_id": 21, "asked_text": "Is walking your thinking time?",
               "ask_verified": True,
               "feedback": [{"state": "accepted_answer"}, {"state": "update_applied"}]}
    conn.execute("INSERT INTO cue_pool VALUES (12, 'concept_hypothesis', '', ?)",
                 (json.dumps(payload),))
    legacy = {**payload, "target_type": "concept", "ask_verified": False,
              "asked_text": "Walking is thinking time.", "feedback": []}
    conn.execute("INSERT INTO cue_pool VALUES (13, 'concept_hypothesis', '', ?)",
                 (json.dumps(legacy),))
    conn.commit()
    record_trace(db, 21, [attempt("first", "overflow_retry"), attempt("final", "committed")])
    before = conn.total_changes
    report = collect(conn)
    assert conn.total_changes == before
    assert report["families"]["hypothesis"]["verified_asks_joined_to_final_commit"] == 1
    assert report["families"]["hypothesis"]["cues_with_update_applied"] == 1
    assert report["families"]["concept"]["unverified_ask_receipts"] == 1
    assert report["families"]["hypothesis"]["accepted_answers_with_unverified_ask"] == 0
    assert report["families"]["hypothesis"]["cues_without_reported_delivery"] == 1
    assert report["families"]["concept"]["cues_without_final_trace"] == 1
    assert len(report["cues"][0]["final_claims"]) == 1
    assert "Walking" not in json.dumps(report)


def test_corrupt_receipts_and_unknown_versions_are_visible(db):
    conn = db._get_conn()
    conn.executemany("INSERT INTO kv_meta VALUES (?, ?, '')", [
        (PREFIX + "1", "invalid"), (PREFIX + "2", '{"version":99,"attempts":[]}'),
    ])
    conn.execute("INSERT INTO cue_pool VALUES (12, 'concept_hypothesis', '', 'invalid')")
    report = collect(conn)
    assert report["coverage"]["unreadable_turn_traces"] == 2
    assert report["coverage"]["unreadable_cue_payloads"] == 1
