"""Both authors share durable notes in a protected bookshelf fixture."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.infra.log_context import reset_turn_id, set_turn_id
from app.core.session.world_mixin import WorldMixin
from app.core.world.keepsake_box import KEEPSAKE_BOX_SLUG, ProtectedKeepsakeBox
from app.core.world.world_store import WorldStore
from app.llm.tools.keepsake_box import LeaveKeepsakeNoteTool, ReadKeepsakeNotesTool
from app.web.rest.memory_world_routes import register

NOW = datetime(2026, 11, 20, 20, tzinfo=timezone.utc)


@pytest.fixture
def room(tmp_path, monkeypatch):
    monkeypatch.setattr(timephrase, "utcnow", lambda: NOW)
    path = tmp_path / "keepsake-box.db"
    ChatDatabase(path)
    store = WorldStore(path)
    store.seed_default()
    yield store, path
    store.close()


def test_existing_apartment_gets_one_box_without_overwriting_belongings(room):
    store, _ = room
    shelf = store.get_location("bookshelf")
    original_items = [(item.id, item.name) for item in store.list_items()
                      if item.slug != KEEPSAKE_BOX_SLUG]
    # Simulate the room of an installation before this fixture was introduced.
    with store._get_conn() as conn:
        conn.execute("DELETE FROM world_items WHERE slug=?", (KEEPSAKE_BOX_SLUG,))
    store._reload_mirror()

    assert store.ensure_keepsake_box()
    box = store.keepsake_box_item()
    assert box.location_id == box.home_location_id == shelf.id
    assert not store.ensure_keepsake_box()
    assert [(item.id, item.name) for item in store.list_items() if item.id != box.id] == (
        original_items
    )
    assert store.read_keepsake_notes() == {"notes": [], "next_before_id": None}


def test_protected_box_stays_on_shelf_and_notes_survive_reset_and_reopen(room):
    store, path = room
    store.ensure_home_lifecycle()
    box = store.keepsake_box_item()
    saved = store.add_keepsake_note("Our first note.", author="user")
    assert not any(action["item_id"] == box.id for action in
                   store.available_home_actions()["actions"])
    with pytest.raises(ProtectedKeepsakeBox):
        store.remove_item(box.id)
    with pytest.raises(ProtectedKeepsakeBox):
        store.update_item(box.id, kind="food", state={})
    with pytest.raises(ProtectedKeepsakeBox):
        store.consume_item(box.id)
    assert store.get_item(box.id) == box
    store.seed_default(force=True)
    reopened = WorldStore(path)
    try:
        restored = reopened.keepsake_box_item()
        assert restored.location_id == reopened.get_location("bookshelf").id
        assert restored.state["keepsake_latest_note_id"] == saved["id"]
        assert reopened.read_keepsake_notes()["notes"] == [saved]
    finally:
        reopened.close()


def test_web_and_chat_share_notes_with_real_authorship_and_safe_retries(room):
    store, _ = room
    session = MagicMock()
    session.world_store = session._world_store = store
    session.update_world_item = lambda item_id, **kw: WorldMixin.update_world_item(
        session, item_id, **kw,
    )
    session.delete_world_item = lambda item_id: WorldMixin.delete_world_item(session, item_id)
    session.consume_world_item = lambda item_id, **kw: WorldMixin.consume_world_item(
        session, item_id, **kw,
    )
    app = FastAPI()
    register(app, session, MagicMock(), MagicMock(), None)
    item_path = f"/api/world/items/{store.keepsake_box_item().id}"
    notes_path = f"{item_path}/keepsake-notes"
    with TestClient(app) as client:
        payload = {"text": "I remember laughing.\nThat was a good evening.", "author": "aiko",
                   "request_key": "my-note"}
        response = client.post(notes_path, json=payload)
        assert response.status_code == 200
        user_note = response.json()["note"]
        assert user_note["author"] == "user"
        assert user_note["created_at"] == NOW.isoformat()
        assert client.post(notes_path, json=payload).json()["note"] == user_note
        assert client.post(notes_path, json={**payload, "text": "different"}).status_code == 400
        assert client.post(notes_path, json={"text": " "}).status_code == 400
        assert client.post(notes_path, json={"text": "x" * 601}).status_code == 400
        assert client.get("/api/world/items/0/keepsake-notes").status_code == 404
        assert client.patch(item_path, json={"state": {}}).status_code == 409
        assert client.delete(item_path).status_code == 409
        assert client.post(f"{item_path}/consume").status_code == 409
        assert json.loads(ReadKeepsakeNotesTool(session).run({}))["notes"] == [user_note]

        token = set_turn_id("keepsake-test")
        try:
            tool = LeaveKeepsakeNoteTool(session)
            arguments = {"text": "I remember what made us laugh.", "author": "user"}
            receipt = json.loads(tool.run(arguments))
            assert json.loads(tool.run(arguments)) == receipt
        finally:
            reset_turn_id(token)
        aiko_note, existing = client.get(notes_path).json()["notes"]
        assert existing == user_note
        assert aiko_note["id"] == receipt["note_id"]
        assert aiko_note["author"] == "aiko"
        assert aiko_note["text"] == arguments["text"]
        assert session._notify_world.call_args.args[0]["item"]["state"][
            "keepsake_latest_note_id"
        ] == aiko_note["id"]
        assert aiko_note["text"] not in json.dumps(store.snapshot())


def test_parallel_writes_remain_available_through_older_pages(room):
    store, _ = room

    def write(index):
        try:
            return store.add_keepsake_note(f"Slip {index}", author="user")
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        saved = list(executor.map(write, range(12)))
    latest = store.read_keepsake_notes()
    older = store.read_keepsake_notes(before_id=latest["next_before_id"])
    assert latest["notes"] + older["notes"] == sorted(
        saved, key=lambda note: note["id"], reverse=True,
    )
    assert len(latest["notes"]) == 10
    assert older["next_before_id"] is None


def test_followup_receipts_count_both_turns_without_opening_slips(room):
    store, _ = room
    session = MagicMock(_world_store=store)
    write = LeaveKeepsakeNoteTool(session)
    read = ReadKeepsakeNotesTool(session)
    for turn, slips in (("first", ["Harbor"]), ("second", ["Railway", "Lantern", "Lantern"])):
        token = set_turn_id(turn)
        try:
            for text in slips:
                receipt = json.loads(write.run({"text": text}))
        finally:
            reset_turn_id(token)
    assert receipt["box_counts"] == {"total": 3, "aiko": 3, "user": 0}
    store.add_keepsake_note("Another perspective", author="user")
    expected = {"total": 4, "aiko": 3, "user": 1}
    assert json.loads(read.run({"counts_only": True})) == {"box_counts": expected}
    older = json.loads(read.run({"before_id": receipt["note_id"]}))
    assert len(older["notes"]) == 2 and older["box_counts"] == expected
