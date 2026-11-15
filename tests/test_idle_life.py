"""Behavioral history against disposable state, including crashes and real retention."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.idle_worker_scheduler import IdleWorkerScheduler
from app.core.world.home_actions_mixin import RECEIPTS_KEY
from app.core.world.idle_life_store import IdleLifeStore
from app.core.world.idle_life_trace import current_run, emit, execute_traced, item_changes
from app.core.world.world_store import WorldStore
from app.web.rest.idle_life_routes import register

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)


@pytest.fixture
def life(tmp_path, monkeypatch):
    monkeypatch.setattr(timephrase, "real_now", lambda: NOW)
    monkeypatch.setattr(timephrase, "utcnow", lambda: NOW)
    db = ChatDatabase(tmp_path / "life.db")
    return IdleLifeStore(db), db


def test_real_retention_and_restart_despite_debug_clock(life, monkeypatch):
    store, db = life
    first = store.record("choice", "away_activity", {"key": "rest"})
    monkeypatch.setattr(timephrase, "utcnow", lambda: NOW + timedelta(days=900))
    assert store.history()["events"][0]["id"] == first
    # Retention uses capture time, not the movable narrative clock.
    monkeypatch.setattr(timephrase, "real_now", lambda: NOW + timedelta(days=13))
    resumed = IdleLifeStore(db)
    assert any(row["id"] == first for row in resumed.history(hours=336)["events"])
    monkeypatch.setattr(timephrase, "real_now", lambda: NOW + timedelta(days=15))
    fresh = IdleLifeStore(db)
    assert all(row["id"] != first for row in fresh.history(hours=336)["events"])


def test_cursor_filters_dont_lose_or_duplicate_events(life):
    store, _ = life
    expected = [store.record("choice", "away_activity", {"key": str(n)}) for n in range(7)]
    store.record("choice", "hobby", {})
    ids, cursor = [], None
    while True:
        page = store.history(worker="away_activity", kind="choice", limit=2, before_id=cursor)
        ids.extend(row["id"] for row in page["events"])
        cursor = page["next_before_id"]
        if cursor is None:
            break
    assert ids == expected[::-1]
    assert store.history(limit=9999)["requested_hours"] == 72


def test_intervals_compress_and_do_not_bridge_unobserved_gaps(life):
    store, _ = life
    with patch("time.monotonic", side_effect=[0, 15, 30, 61, 170, 471]):
        for _ in range(6):
            store.observe("away_activity", "shared_hold", {"generation": 3})
    rows = store.history(kind="admission")["events"]
    assert len(rows) == 3  # gaps >90s and five-minute rotations are separate.
    assert rows[-1]["observations"] == 4
    store.observe("away_activity", "allowed", {"generation": 4})
    assert store.history(kind="admission")["events"][0]["payload"]["reason"] == "allowed"


def test_atomic_event_rolls_back_with_effect_and_deduplicates(life):
    store, db = life
    conn = db._get_conn()
    conn.execute("BEGIN IMMEDIATE")
    store.record("action_effect", "home_actions", {"action": "sip"}, conn=conn,
                 event_key="token:1")
    conn.rollback()
    assert store.history(kind="action_effect")["events"] == []
    store.record("action_effect", "home_actions", {}, event_key="token:1")
    store.record("action_effect", "home_actions", {}, event_key="token:1")
    assert len(store.history(kind="action_effect")["events"]) == 1


def test_failed_writes_report_missing_evidence_and_atomic_writes_raise(life):
    store, _ = life
    class BrokenConnection:
        def execute(self, *args):
            raise RuntimeError("unavailable")
        def rollback(self):
            pass
    with patch.object(store._db, "_get_conn", return_value=BrokenConnection()):
        assert store.record("choice", "hobby", {}) is None
    assert store.health()["failed_writes_since_boot"] == 1
    with pytest.raises(RuntimeError):
        store.record("action_effect", "home_actions", {}, conn=BrokenConnection())


def test_partial_run_keeps_steps_and_clears_task_local_scope(life):
    store, _ = life
    def fail():
        emit("step_completed", {"key": "reading", "receipt": {"item_id": 7}})
        raise ValueError("second step failed")
    with pytest.raises(ValueError):
        execute_traced(store, "away_activity", fail)
    rows = store.history(worker="away_activity")["events"]
    assert {r["kind"] for r in rows} == {
        "run_started", "step_completed", "run_failed", "run_finished"}
    assert len({r["run_id"] for r in rows}) == 1
    assert current_run() is None


def test_resources_exclude_creation_text_and_track_same_drink(life):
    before = [{"id": 1, "quantity": 1, "state": {
        "home_vessel": {"drink": {"amount_ml": 250, "prepared_at": NOW.isoformat()}},
        "creation": {"text": "private fiction"}}}]
    after = [{"id": 1, "quantity": 1, "state": {
        "home_vessel": {"drink": {"amount_ml": 175, "prepared_at": NOW.isoformat()}},
        "creation": {"text": "other fiction"}}}]
    delta = item_changes(before, after)
    assert delta[0]["fields"]["state.home_vessel.drink.amount_ml"] == {
        "before": 250, "after": 175}
    assert "fiction" not in json.dumps(delta)


def test_household_trace_is_transactional_and_outlives_retry_ring(life):
    trace, db = life
    world = WorldStore(db._db_path)
    world.seed_default()
    world.ensure_home_drinks()
    world.idle_life_store = trace
    offered = next(row for row in world.available_home_actions()["actions"]
                   if row["action"] == "prepare_drink" and row["recipe"] == "tea")
    receipt = world.perform_home_action(offered["token"], relocate=True)
    assert world.perform_home_action(offered["token"])["already_applied"]
    rows = trace.history(kind="action_effect")["events"]
    assert len(rows) == 1
    assert rows[0]["payload"]["token"] == receipt["token"]
    assert rows[0]["actor"] == "conversation"
    assert rows[0]["payload"]["drink_instance"].endswith(NOW.isoformat())
    # Evict the bounded retry ring; time-retained evidence stays available.
    db.kv_set(RECEIPTS_KEY, "[]")
    assert len(trace.history(kind="action_effect")["events"]) == 1
    action = next(row for row in world.available_home_actions()["actions"]
                  if row["action"] == "sip_drink" and row["item_id"] == receipt["item_id"])
    before = world.get_item(receipt["item_id"]).state.copy()
    with patch.object(trace, "record", side_effect=RuntimeError("trace unavailable")):
        with pytest.raises(RuntimeError):
            world.perform_home_action(action["token"], relocate=True)
    world._reload_mirror()
    assert world.get_item(receipt["item_id"]).state == before
    world.close()


def test_scheduler_records_hold_and_not_a_completed_activity(life):
    store, _ = life
    worker = SimpleNamespace(name="away_activity", interval_seconds=1,
                             is_ready=lambda **kwargs: True,
                             demand=lambda **kwargs: None,
                             run=lambda: {"fired": 0, "skipped_cooldown": True})
    scheduler = IdleWorkerScheduler(trace_store=store, is_quiet_callback=lambda: False)
    scheduler.register(worker)
    scheduler._tick()
    row = store.history(kind="admission", worker="away_activity")["events"][0]
    assert row["payload"]["reason"] == "user_active"
    assert not store.history(kind="run_result")["events"]
    scheduler.force_run("away_activity")
    row = store.history(kind="run_result")["events"][0]
    assert row["payload"]["fired"] == 0
    assert row["actor"] == "direct_worker"


def test_read_only_api_and_mcp_use_same_bounded_store(life):
    store, _ = life
    eid = store.record("choice", "away_activity", {"key": "rest"}, run_id="r")
    app = FastAPI()
    register(app, SimpleNamespace(_idle_life_store=store))
    with TestClient(app) as client:
        response = client.get("/api/world/idle-life?hours=999&kind=choice")
        assert response.json()["requested_hours"] == 336
        assert response.json()["events"][0]["id"] == eid
        assert client.get(f"/api/world/idle-life/events/{eid}").json()["id"] == eid
        assert client.get("/api/world/idle-life/events/999999").status_code == 404
        assert client.get("/api/world/idle-life/decisions/r").json()["events"][0]["id"] == eid
        assert client.get("/api/world/idle-life/summary").json()["counts"]
    from app.mcp.server_tools.idle_life_tools import register as register_tools
    tools = {}
    class FakeMCP:
        def tool(self):
            def add(fn):
                tools[fn.__name__] = fn
                return fn
            return add
    register_tools(FakeMCP(), SimpleNamespace(_idle_life_store=store))
    assert json.loads(tools["get_idle_life_decision"]("r"))["events"][0]["id"] == eid
    assert json.loads(tools["get_idle_life_decision"](event_id=eid))["events"][0]["id"] == eid


def test_projection_failure_cannot_fail_an_activity_or_leak_trace_scope(life):
    store, _ = life
    with patch("app.core.world.idle_life_trace.item_changes", side_effect=ValueError("projection")):
        assert execute_traced(store, "away_activity", lambda: {"fired": 1}) == {"fired": 1}
    assert current_run() is None
    assert store.history(kind="capture_failed")["events"]
    assert store.history(kind="run_finished")["events"][0]["payload"]["snapshot_unavailable"]


def test_trace_connection_failure_is_visible_and_nonfatal(life):
    store, _ = life
    with patch.object(store._db, "_get_conn", side_effect=RuntimeError("closed")):
        assert store.record("choice", "hobby", {}) is None
    assert store.health()["failed_writes_since_boot"] == 1


def test_lifecycle_outcomes_are_preserved_without_copying_generated_body(life):
    store, _ = life
    result = {"fired": 1, "completed_in_progress": True, "resumed": False,
              "interrupted": True, "status": "awake", "promoted": 2,
              "research_question_id": 7, "generated_body": "private output"}
    execute_traced(store, "away_activity", lambda: result)
    payload = store.history(kind="run_result")["events"][0]["payload"]
    assert payload["completed_in_progress"] and payload["interrupted"]
    assert payload["resumed"] is False and payload["status"] == "awake"
    assert payload["research_question_id"] == 7 and "generated_body" not in payload
