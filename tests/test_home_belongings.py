"""Virtual purchases and disposal must preserve ownership, capacity and recovery."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.world.home_maintenance_worker import HomeMaintenanceWorker
from app.core.world.world_store import WorldStore
from app.web.rest.memory_world_routes import register

NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(timephrase, "utcnow", lambda: NOW)
    path = tmp_path / "belongings.db"
    db = ChatDatabase(path)
    store = WorldStore(path)
    store.seed_default()
    store.ensure_home_drinks()
    store.ensure_home_bathroom()
    store.ensure_home_lifecycle()
    store.set_state(location_id=store.get_location("desk").id)
    yield store, db, path
    store.close()


def option(store, name, *, now=NOW, item_id=None, product=None):
    return next(row for row in store.available_home_actions(now=now)["actions"]
                if row["action"] == name and (item_id is None or row["item_id"] == item_id)
                and (product is None or row.get("parameters", {}).get("product") == product))


def perform(store, name, **kwargs):
    return store.perform_home_action(option(store, name, **kwargs)["token"],
                                     now=kwargs.get("now", NOW), relocate=True)


def test_order_is_not_possession_and_unpacking_is_idempotent(home):
    store, _, _ = home
    original = len(store.list_items())
    perform(store, "place_home_order", product="lidded_mug")
    assert len(store.list_items()) == original
    assert store.home_household_snapshot(now=NOW)["orders"][-1]["status"] == "ordered"
    assert store.home_household_snapshot(now=NOW + timedelta(minutes=6))[
        "orders"][-1]["status"] == "dispatched"
    later = NOW + timedelta(minutes=31)
    assert store.home_household_snapshot(now=later)["orders"][-1]["status"] == "delivered"
    unpack = option(store, "unpack_parcel", now=later)
    result = store.perform_home_action(unpack["token"], now=later)
    assert len(result["created_item_ids"]) == 1
    assert store.perform_home_action(unpack["token"], now=later)["already_applied"]
    assert len(store.list_items()) == original + 1
    mug = store.get_item(result["created_item_ids"][0])
    assert mug.state["home_vessel"]["has_lid"] and mug.given_by == "aiko"
    assert mug.home_location_id == store.get_location("kitchenette").id


def test_order_pacing_cancel_and_missing_computer(home):
    store, _, _ = home
    perform(store, "place_home_order", product="cushion")
    perform(store, "cancel_home_order")
    assert not any(row["action"] == "place_home_order"
                   for row in store.available_home_actions(now=NOW)["actions"])
    assert option(store, "place_home_order", now=NOW + timedelta(days=1), product="frame")
    store.remove_item(store.find_item("dual monitors").id)
    assert not any(row["action"] == "place_home_order" for row in
                   store.available_home_actions(now=NOW + timedelta(days=1))["actions"])


def test_capacity_preserves_existing_items_and_revalidates_delivery(home):
    store, _, _ = home
    before = [item.id for item in store.list_items()]
    assert store.ensure_home_lifecycle() is False
    assert [item.id for item in store.list_items()] == before
    perform(store, "place_home_order", product="cushion")
    desk = store.get_location("desk").id
    for index in range(8):
        store.add_item(name=f"user display gift {index}", kind="decor", location_id=desk,
                       given_by="user")
    offered = store.available_home_actions(now=NOW + timedelta(days=1))["actions"]
    assert not any(row["action"] in {"place_home_order", "unpack_parcel"} for row in offered)
    assert all(store.get_item(iid) for iid in before)
    assert store.home_household_snapshot()["orders"][-1]["status"] == "ordered"


def test_bin_is_hidden_from_room_tools_idle_drinks_and_prompt_and_restorable(home):
    store, _, _ = home
    mug = store.find_item("ceramic mug")
    original = deepcopy(mug.state)
    perform(store, "discard_item", item_id=mug.id)
    assert store.find_item("ceramic mug") is None
    assert mug.id not in [row.id for row in store.list_items()]
    assert mug.id in [row.id for row in store.list_items(include_inactive=True)]
    assert "ceramic mug" not in store.render_block()
    assert not any(row["action"] == "prepare_drink" and row["item_id"] == mug.id
                   for row in store.available_home_actions()["actions"])
    perform(store, "restore_item", item_id=mug.id)
    assert store.find_item("ceramic mug").id == mug.id
    assert all(store.get_item(mug.id).state[key] == value for key, value in original.items())


def test_gifts_keepsakes_favorites_and_saved_art_are_protected(home):
    store, _, _ = home
    desk = store.get_location("desk").id
    gift, _ = store.add_item(name="gift cup", kind="vessel", location_id=desk, given_by="user")
    art, _ = store.add_item(name="my sketchbook", kind="book", location_id=desk,
                           state={"home_sketchbook": {"schema": 1, "pages": []}})
    mug = store.find_item("ceramic mug")
    perform(store, "keep_item", item_id=mug.id)
    discarded = {row["item_id"] for row in store.available_home_actions()["actions"]
                 if row["action"] == "discard_item"}
    assert not {gift.id, art.id, mug.id} & discarded
    assert not any(store.get_item(iid).kind == "keepsake" for iid in discarded)


def test_storage_is_bounded_and_retrieval_needs_active_space(home):
    store, _, _ = home
    desk = store.get_location("desk").id
    for index in range(8):
        item, _ = store.add_item(name=f"stored object {index}", kind="other", location_id=desk)
        perform(store, "store_item", item_id=item.id)
    assert len(store.home_household_snapshot()["stored"]) == 8
    assert not any(row["action"] == "store_item"
                   for row in store.available_home_actions()["actions"])
    perform(store, "retrieve_item", item_id=item.id)
    assert len(store.home_household_snapshot()["stored"]) == 7


def test_passive_expiry_preserves_protected_rows_and_records_disposal(home, monkeypatch):
    store, db, _ = home
    mug = store.find_item("ceramic mug")
    perform(store, "discard_item", item_id=mug.id)
    later = NOW + timedelta(hours=49)
    monkeypatch.setattr(timephrase, "utcnow", lambda: later)
    patches = []
    worker = HomeMaintenanceWorker(store, patches.append)
    assert worker.sleep_policy == "continue"
    assert worker.demand(now=later, last_run_at=None).pressure > 0
    assert worker.run()["discarded"] == 1
    assert store.get_item(mug.id) is None
    assert store.home_household_snapshot()["disposals"][-1]["name"] == "ceramic mug"
    assert db.kv_get("aiko.away_activities") is None
    assert patches[-1]["snapshot"]


def test_bin_undo_route_is_explicit_and_retry_safe(home):
    store, _, _ = home
    mug = store.find_item("ceramic mug")
    perform(store, "discard_item", item_id=mug.id)
    token = option(store, "restore_item", item_id=mug.id)["token"]
    session = MagicMock()
    session.world_store = store
    app = FastAPI()
    register(app, session, MagicMock(), MagicMock(), None)
    with TestClient(app) as client:
        assert client.get("/api/world/home").json()["bin"][0]["id"] == mug.id
        response = client.post("/api/world/home/action", json={"token": token})
        assert response.status_code == 200
        retried = client.post("/api/world/home/action", json={"token": token})
        assert retried.json()["already_applied"]
        assert client.post("/api/world/home/action", json={"token": "invented"}).status_code == 409


def test_expiry_only_clears_unprotected_rows_owned_by_this_bin(home):
    store, _, _ = home
    mug = store.find_item("ceramic mug")
    perform(store, "discard_item", item_id=mug.id)
    item = store.get_item(mug.id)
    store.update_item(item.id, state={**item.state, "home_favorite": True})
    foreign, _ = store.add_item(name="unowned hidden record", kind="other",
                                location_id=store.get_location("desk").id, state={
        "home_lifecycle": {"status": "discarded", "owner_id": -1,
                           "at": NOW.isoformat(), "expires_at": NOW.isoformat()},
    })
    assert store.maintain_home_lifecycle(now=NOW + timedelta(days=3))["discarded"] == 0
    assert store.get_item(mug.id) and store.get_item(foreign.id)
