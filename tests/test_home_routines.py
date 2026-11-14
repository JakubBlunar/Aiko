"""Bathroom state, interruption, and idle continuation against isolated SQLite."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.world.home_routines import bathroom_view
from app.core.world.idle_activity_worker import IdleAwayActivityWorker, load_journal
from app.core.world.world_store import WorldStore

NOW = datetime(2026, 11, 14, 18, tzinfo=timezone.utc)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(timephrase, "utcnow", lambda: NOW)
    path = tmp_path / "routines.db"
    db = ChatDatabase(path)
    store = WorldStore(path)
    store.seed_default()
    store.ensure_home_drinks()
    store.ensure_home_bathroom()
    store.set_state(location_id=store.get_location("bathroom").id)
    yield store, db, path
    store.close()


def action(store, name, now=NOW):
    return next(row for row in store.available_home_actions(now=now)["actions"]
                if row["action"] == name)


def test_bathroom_additive_seed_preserves_home_and_is_not_resurrected(home):
    store, _, _ = home
    assert store.get_location("bathroom").locked
    existing = deepcopy(store.snapshot())
    assert store.ensure_home_bathroom() is False
    assert store.snapshot() == existing
    fixture = store.find_item("bathroom fixtures")
    store.remove_item(fixture.id)
    assert store.ensure_home_bathroom() is False
    assert store.get_item(fixture.id) is None


def test_brushing_uses_supplies_and_an_elapsed_gate_without_need_penalties(home):
    store, _, _ = home
    first = action(store, "brush_teeth")
    store.perform_home_action(first["token"], now=NOW)
    state = store.find_item("bathroom fixtures").state["home_bathroom"]
    assert state["supplies"]["toothpaste"] == 19
    assert state["last_brushed_teeth_at"] == NOW.isoformat()
    assert not any(row["action"] == "brush_teeth"
                   for row in store.available_home_actions(now=NOW)["actions"])
    assert action(store, "brush_teeth", NOW + timedelta(hours=8))
    assert not any("penalty" in key or "health" in key for key in state)


def test_bath_start_is_not_completion_and_end_can_be_early(home):
    store, _, _ = home
    start = action(store, "start_bath")
    receipt = store.perform_home_action(start["token"], now=NOW)
    assert "settled in" in receipt["summary"]
    assert store.get_state().posture == "lying"
    assert not any(row["action"] == "finish_bath"
                   for row in store.available_home_actions(now=NOW)["actions"])
    store.perform_home_action(action(store, "drain_bath")["token"], now=NOW)
    fixture = store.find_item("bathroom fixtures")
    view = bathroom_view(fixture, now=NOW)
    assert view["bath_phase"] == "empty" and view["towel"] == "drying"
    assert bathroom_view(fixture, now=NOW + timedelta(hours=4))["towel"] == "dry"


def test_bath_restart_and_elapsed_time_do_not_complete_it(home):
    store, _, path = home
    store.perform_home_action(action(store, "start_bath")["token"], now=NOW)
    reopened = WorldStore(path)
    try:
        later = NOW + timedelta(hours=4)
        fixture = reopened.find_item("bathroom fixtures")
        before = deepcopy(fixture.state)
        view = bathroom_view(fixture, now=later)
        assert view["bath_ready_to_finish"] and view["water_temperature_band"] == "cool"
        assert fixture.state == before and view["bath_phase"] == "soaking"
        reopened.perform_home_action(action(reopened, "finish_bath", later)["token"], now=later)
        assert bathroom_view(reopened.get_item(fixture.id), now=later)["bath_phase"] == "empty"
    finally:
        reopened.close()


def test_idle_finishes_due_bath_without_waiting_general_cooldown(home, monkeypatch):
    store, db, _ = home
    worker = IdleAwayActivityWorker(
        world_store=store, kv_get=db.kv_get, kv_set=db.kv_set,
        user_display_name_provider=lambda: "Test user", cooldown_seconds=5400,
    )
    worker.force_activity("home_start_bath")
    assert worker.run()["fired"] == 1
    monkeypatch.setattr(timephrase, "utcnow", lambda: NOW + timedelta(minutes=10))
    assert worker.run()["skipped_cooldown"]
    monkeypatch.setattr(timephrase, "utcnow", lambda: NOW + timedelta(minutes=21))
    result = worker.run()
    assert result["fired"] == 1 and result["key"] == "home_finish_bath"
    assert load_journal(db.kv_get)[-1]["key"] == "home_finish_bath"
    assert bathroom_view(store.find_item("bathroom fixtures"))["bath_phase"] == "empty"


def test_shared_scene_or_sleep_guard_still_blocks_due_bath(home, monkeypatch):
    store, db, _ = home
    from app.core.world.world_mutation_guard import WorldMutationGuard

    guard = WorldMutationGuard(kv_get=db.kv_get, situation_snapshot_provider=lambda: None,
                               intentional_hold_seconds=0,
                               sleep_state_provider=lambda: {"status": "asleep"})
    store.perform_home_action(action(store, "start_bath")["token"], now=NOW)
    monkeypatch.setattr(timephrase, "utcnow", lambda: NOW + timedelta(minutes=30))
    worker = IdleAwayActivityWorker(
        world_store=store, kv_get=db.kv_get, kv_set=db.kv_set,
        user_display_name_provider=lambda: "Test user", mutation_guard=guard,
    )
    assert worker.run()["skipped_aiko_asleep"]
    assert bathroom_view(store.find_item("bathroom fixtures"))["bath_phase"] == "soaking"


def test_missing_supplies_and_leaving_bathroom_invalidate_continuation(home):
    store, _, _ = home
    fixture = store.find_item("bathroom fixtures")
    state = deepcopy(fixture.state)
    state["home_bathroom"]["supplies"] = {"toothpaste": 0, "soap": 0}
    store.update_item(fixture.id, state=state)
    offered = store.available_home_actions(now=NOW)["actions"]
    assert not any(row["action"] in {"brush_teeth", "wash_face", "start_bath"} for row in offered)
    store.perform_home_action(action(store, "restock_bathroom")["token"], now=NOW)
    store.perform_home_action(action(store, "start_bath")["token"], now=NOW)
    store.set_state(location_id=store.get_location("bed").id, activity="reading")
    assert store.home_activity_followup(now=NOW + timedelta(hours=1)) is None


def test_short_shower_uses_soap_and_records_a_completed_step(home):
    store, _, _ = home
    store.perform_home_action(action(store, "take_shower")["token"], now=NOW)
    state = store.find_item("bathroom fixtures").state["home_bathroom"]
    assert state["last_showered_at"] == NOW.isoformat()
    assert state["supplies"]["soap"] == 19
    assert state["bath"]["phase"] == "empty"
