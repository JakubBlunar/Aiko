import json
import sqlite3

from app.core.memory.surfacing_trace_store import PREFIX
from app.core.proactive.cue_accounting import decisions_from_block_chars
from scripts.cue_delivery_report import collect


def test_alternate_route_counts_only_a_committed_included_claim():
    claim = {"cue_type": "forward_curiosity", "state": "committed",
             "blocks": ["initiative_block"]}
    decisions = decisions_from_block_chars(set(), {"initiative_block": 100},
                                           final_claims=[claim])
    assert decisions.rows() == [("forward_curiosity", "surfaced", "")]
    for state in ("omitted", "overflow_retry", "error:ValueError"):
        claim["state"] = state
        assert not decisions_from_block_chars(set(), {"initiative_block": 100},
                                               final_claims=[claim]).surfaced


def test_retry_commit_and_missing_coverage_are_not_final_successes():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE kv_meta(key TEXT,value TEXT);"
        "CREATE TABLE turn_prompt_blocks(assistant_message_id INTEGER,created_at TEXT);"
        "CREATE TABLE cue_decisions(cue TEXT,outcome TEXT,reason TEXT,created_at TEXT);"
        "CREATE TABLE cue_pool(cue_type TEXT,payload TEXT);"
        "INSERT INTO turn_prompt_blocks VALUES(1,'2026-11-04'),(2,'2026-11-04');"
    )
    trace = {"version": 1, "assistant_message_id": 1, "attempts": [
        {"claims": [{"cue_type": "forward_curiosity", "state": "committed"}]},
        {"claims": [{"cue_type": "forward_curiosity", "state": "omitted"}]},
    ]}
    conn.execute("INSERT INTO kv_meta VALUES(?,?)", (PREFIX + "1", json.dumps(trace)))
    report = collect(conn, since="2026-11-01")
    assert report["coverage"]["turns_without_retained_trace"] == 1
    assert report["families"]["forward_curiosity"] == {"final_claim:omitted": 1}
    assert report["committed_routes"] == {}
    assert "private" not in json.dumps(report)
    conn.close()
