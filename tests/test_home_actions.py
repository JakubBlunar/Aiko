"""Home actions against isolated SQLite, including chat/idle parity and recovery."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.world.home_actions_mixin import RECEIPTS_KEY
from app.core.world.home_drinks import drink_view, temperature
from app.core.world.idle_activity_worker import IdleAwayActivityWorker, load_journal
from app.core.world.world_store import WorldStore
from app.llm.tools.base import ToolError
from app.llm.tools.home_actions import AvailableHomeActionsTool, PerformHomeActionTool
from app.llm.tools.world import InspectItemTool

NOW = datetime(2026, 11, 14, 14, tzinfo=timezone.utc)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(timephrase, "utcnow", lambda: NOW)
    path = tmp_path / "home.db"
    db = ChatDatabase(path)
    store = WorldStore(path)
    store.seed_default()
    store.ensure_home_drinks()
    store.set_state(location_id=store.get_location("kitchenette").id)
    yield store, db, path
    store.close()


def find_action(store, action, *, recipe=None, item_id=None, now=NOW):
    return next(row for row in store.available_home_actions(now=now)["actions"]
                if row["action"] == action
                and (recipe is None or row["recipe"] == recipe)
                and (item_id is None or row["item_id"] == item_id))


def perform(store, action, **kwargs):
    now = kwargs.get("now", NOW)
    offered = find_action(store, action, **kwargs)
    return store.perform_home_action(offered["token"], now=now)


def test_household_seed_preserves_existing_world_and_does_not_resurrect(home):
    store, _, path = home
    station = store.find_item("household drink station")
    assert station.given_by == "household"
    existing = {item.id: item.to_dict() for item in store.list_items()}
    assert store.ensure_home_drinks() is False
    assert existing == {item.id: item.to_dict() for item in store.list_items()}
    mug = store.find_item("ceramic mug")
    store.remove_item(mug.id)
    reopened = WorldStore(path)
    try:
        assert reopened.ensure_home_drinks() is False
        assert reopened.get_item(mug.id) is None
    finally:
        reopened.close()


@pytest.mark.parametrize("recipe", ["tea", "coffee", "hot_chocolate", "water", "juice"])
def test_recipes_share_reusable_vessel_lifecycle(home, recipe):
    store, _, _ = home
    receipt = perform(store, "prepare_drink", recipe=recipe)
    iid = receipt["item_id"]
    item = store.get_item(iid)
    assert drink_view(item, now=NOW)["remaining_ml"] == 250
    assert item.consumable is False
    for _ in range(4):
        perform(store, "sip_drink", item_id=iid)
    assert store.get_item(iid) is not None
    view = drink_view(store.get_item(iid), now=NOW)
    assert view["remaining_ml"] == 0 and not view["clean"]
    assert not any(row["action"] == "prepare_drink" and row["item_id"] == iid
                   for row in store.available_home_actions(now=NOW)["actions"])
    perform(store, "wash_vessel", item_id=iid)
    assert drink_view(store.get_item(iid), now=NOW)["clean"]
    assert find_action(store, "prepare_drink", recipe=recipe, item_id=iid)


def test_cooling_lid_and_reheating_preserve_remaining_drink(home):
    store, _, _ = home
    receipt = perform(store, "prepare_drink", recipe="coffee")
    iid = receipt["item_id"]
    perform(store, "sip_drink", item_id=iid)
    plain = deepcopy(store.get_item(iid).state["home_vessel"])
    lidded = {**deepcopy(plain), "has_lid": True, "lid_closed": True}
    later = NOW + timedelta(minutes=30)
    assert temperature(lidded, later) > temperature(plain, later) > 22
    reheated = perform(store, "reheat_drink", item_id=iid, now=later)
    view = drink_view(store.get_item(iid), now=later)
    assert view["remaining_ml"] == 175 and view["temperature_band"] == "hot"
    assert reheated["mode"] == "simulated_home_action"


def test_cold_drink_warms_and_is_not_reheatable(home):
    store, _, _ = home
    receipt = perform(store, "prepare_drink", recipe="juice")
    vessel = store.get_item(receipt["item_id"]).state["home_vessel"]
    assert 8 < temperature(vessel, NOW + timedelta(hours=2)) < 22
    assert not any(row["action"] == "reheat_drink"
                   for row in store.available_home_actions(now=NOW + timedelta(hours=2))["actions"])


def test_temperature_and_inspection_are_read_only_and_age_after_restart(home, monkeypatch):
    store, _, path = home
    receipt = perform(store, "prepare_drink", recipe="coffee")
    iid = receipt["item_id"]
    saved = deepcopy(store.get_item(iid).state)
    monkeypatch.setattr(timephrase, "utcnow", lambda: NOW + timedelta(hours=8))
    reopened = WorldStore(path)
    try:
        session = SimpleNamespace(_world_store=reopened)
        result = json.loads(InspectItemTool(session).run({"item": "ceramic mug"}))
        assert result["home_drink"]["temperature_band"] == "room temperature"
        assert reopened.get_item(iid).state == saved
    finally:
        reopened.close()


def test_missing_equipment_supplies_and_hot_incompatible_glass_are_not_offered(home):
    store, _, _ = home
    station = store.find_item("household drink station")
    state = deepcopy(station.state)
    state["home_drink_station"]["equipment"].remove("coffee_press")
    state["home_drink_station"]["supplies"]["tea"] = 0
    store.update_item(station.id, state=state)
    offered = store.available_home_actions(now=NOW)["actions"]
    assert not any(row["recipe"] in {"tea", "coffee"} for row in offered)
    glass = store.find_item("drinking glass")
    assert {row["recipe"] for row in offered if row["item_id"] == glass.id} == {"water", "juice"}


def test_restock_is_bounded_and_not_clock_backwards_repeatable(home):
    store, _, _ = home
    station = store.find_item("household drink station")
    state = deepcopy(station.state)
    state["home_drink_station"]["supplies"]["coffee"] = 0
    store.update_item(station.id, state=state)
    perform(store, "restock_drinks")
    state = deepcopy(store.get_item(station.id).state)
    state["home_drink_station"]["supplies"]["coffee"] = 0
    store.update_item(station.id, state=state)
    for at in (NOW - timedelta(hours=1), NOW + timedelta(hours=23)):
        assert not any(row["action"] == "restock_drinks"
                       for row in store.available_home_actions(now=at)["actions"])
    assert find_action(store, "restock_drinks", now=NOW + timedelta(hours=24))


def test_stale_selection_and_wrong_location_do_not_move_actor_or_consume_supplies(home):
    store, _, _ = home
    token = find_action(store, "prepare_drink", recipe="coffee")["token"]
    mug = store.find_item("ceramic mug")
    station_before = deepcopy(store.find_item("household drink station").state)
    store.update_item(mug.id, description="A user edit", state={**mug.state, "user_edit": True})
    with pytest.raises(ValueError, match="stale"):
        store.perform_home_action(token, autonomous=True)
    assert store.find_item("household drink station").state == station_before
    token = find_action(store, "prepare_drink", recipe="coffee")["token"]
    store.set_state(location_id=store.get_location("bed").id)
    with pytest.raises(ValueError, match="Move"):
        store.perform_home_action(token)
    with pytest.raises(ValueError, match="blocked"):
        store.perform_home_action(token, autonomous=True, admit=lambda: False)
    assert store.get_state().location_id == store.get_location("bed").id


def test_other_scenes_and_remote_vessels_are_not_used(home):
    store, _, _ = home
    mug = store.find_item("ceramic mug")
    store.update_item(mug.id, location_id=store.get_location("bed").id)
    offered = store.available_home_actions(now=NOW)["actions"]
    assert not any(row["item_id"] == mug.id for row in offered)
    scene = store.add_scene(name="Other room", description="A different place")
    store.set_state(scene_id=scene.id)
    assert not store.available_home_actions(now=NOW)["actions"]


def test_drink_can_be_carried_and_sipped_away_from_kitchen(home):
    store, _, _ = home
    receipt = perform(store, "prepare_drink", recipe="coffee")
    iid = receipt["item_id"]
    taken = store.take_into_hands(iid)
    assert taken is not None
    store.set_state(location_id=store.get_location("window_seat").id)
    perform(store, "sip_drink", item_id=iid)
    assert drink_view(store.get_item(iid), now=NOW)["remaining_ml"] == 175
    assert store.get_item(iid).location_id is None


def test_receipt_and_mutations_rollback_together_on_storage_failure(home):
    store, _, _ = home
    token = find_action(store, "prepare_drink", recipe="coffee")["token"]
    before = store.snapshot()
    conn = store._get_conn()
    conn.execute(
        "CREATE TRIGGER reject_receipt BEFORE INSERT ON kv_meta "
        "WHEN NEW.key='home_actions.receipts.v1' BEGIN SELECT RAISE(ABORT,'disk failure'); END",
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        store.perform_home_action(token)
    assert store.snapshot() == before
    conn.execute("DROP TRIGGER reject_receipt")
    conn.commit()
    assert store.perform_home_action(token)["already_applied"] is False


def test_retries_and_concurrent_dispatch_apply_once_across_restart(home):
    store, _, path = home
    token = find_action(store, "prepare_drink", recipe="coffee")["token"]

    def run(_):
        try:
            return store.perform_home_action(token)
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(run, range(2)))
    assert sorted(row["already_applied"] for row in receipts) == [False, True]
    assert store.find_item("household drink station").state[
        "home_drink_station"]["supplies"]["coffee"] == 7
    reopened = WorldStore(path)
    try:
        assert reopened.perform_home_action(token)["already_applied"] is True
        saved = reopened._get_conn().execute(
            "SELECT value FROM kv_meta WHERE key=?", (RECEIPTS_KEY,),
        ).fetchone()
        assert len(json.loads(saved[0])) == 1
    finally:
        reopened.close()


def test_chat_and_idle_use_same_actions_and_save_only_success(home):
    store, db, _ = home
    patches = []
    session = SimpleNamespace(_world_store=store, _notify_world=patches.append)
    packet = json.loads(AvailableHomeActionsTool(session).run({}))
    token = next(row["token"] for row in packet["actions"] if row["recipe"] == "coffee")
    chat = json.loads(PerformHomeActionTool(session).run({"token": token}))
    assert chat["action"] == "prepare_drink"
    assert patches[-1]["snapshot"]["items"]
    worker = IdleAwayActivityWorker(
        world_store=store, kv_get=db.kv_get, kv_set=db.kv_set,
        user_display_name_provider=lambda: "Test user", cooldown_seconds=0,
        notify=patches.append,
    )
    snapshot = worker._build_candidates("Test user", NOW)
    assert "tea" not in snapshot.candidates
    assert "home_sip_drink" in snapshot.candidates
    worker.force_activity("home_sip_drink")
    result = worker.run()
    assert result["fired"] == 1
    assert load_journal(db.kv_get)[-1]["key"] == "home_sip_drink"
    assert drink_view(store.get_item(chat["item_id"]), now=NOW)["remaining_ml"] == 175
    assert patches[-1]["snapshot"]
    with pytest.raises(ToolError):
        PerformHomeActionTool(session).run({"token": "made up"})


def test_worker_model_is_taught_live_tokens_and_rejects_invented_capabilities(home):
    store, db, _ = home
    action = find_action(store, "prepare_drink", recipe="coffee")
    prompts = []

    class Model:
        def chat_json(self, messages, **kwargs):
            prompts.append(messages[-1]["content"])
            return json.dumps({"home_action_token": action["token"], "creation": None}), {}

    worker = IdleAwayActivityWorker(
        world_store=store, kv_get=db.kv_get, kv_set=db.kv_set,
        user_display_name_provider=lambda: "Test user", ollama=Model(), model="test",
    )
    plan = worker._compose_plan_llm("Test user", store.list_locations(), store.list_items())
    assert plan.home_action_token == action["token"]
    assert "coffee" in prompts[0] and "home_action_token" in prompts[0]
    action["token"] = "invented"
    assert worker._compose_plan_llm(
        "Test user", store.list_locations(), store.list_items(),
    ) is None


def test_malformed_user_vessel_does_not_break_world_inspection(home):
    store, _, _ = home
    mug = store.find_item("ceramic mug")
    store.update_item(mug.id, state={"home_vessel": {"schema": 1, "drink": 7}})
    assert "home_drink" not in store.get_item(mug.id).to_dict()
    assert store.available_home_actions(now=NOW)


def test_custom_lidded_vessel_is_usable_and_opening_lid_preserves_heat(home):
    store, _, _ = home
    kitchen = store.get_location("kitchenette")
    mug, _ = store.add_item(name="insulated mug with lid", kind="vessel",
                            location_id=kitchen.id, given_by="user")
    perform(store, "prepare_drink", recipe="coffee", item_id=mug.id)
    later = NOW + timedelta(minutes=10)
    before = temperature(store.get_item(mug.id).state["home_vessel"], later)
    perform(store, "toggle_lid", item_id=mug.id, now=later)
    assert temperature(store.get_item(mug.id).state["home_vessel"], later) == before
    assert not any(row["action"] == "sip_drink" and row["item_id"] == mug.id
                   for row in store.available_home_actions(now=later)["actions"])
    perform(store, "toggle_lid", item_id=mug.id, now=later)
    perform(store, "sip_drink", item_id=mug.id, now=later)
    assert drink_view(store.get_item(mug.id), now=later)["remaining_ml"] == 175


def test_explicit_world_reset_restores_kit_and_clears_old_action_receipts(home):
    from app.core.session.world_mixin import WorldMixin

    store, _, _ = home
    receipt = perform(store, "prepare_drink", recipe="coffee")

    class Session(WorldMixin):
        _world_store = store
        _world_listeners = []
        user_display_name = "Test user"

    result = Session().reseed_world(force=True)
    assert any(item["slug"] == "home_ceramic_mug" for item in result["items"])
    assert not store._get_conn().execute(
        "SELECT value FROM kv_meta WHERE key=?", (RECEIPTS_KEY,),
    ).fetchone()
    with pytest.raises(ValueError, match="stale"):
        store.perform_home_action(receipt["token"])
