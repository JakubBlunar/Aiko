"""Saved drawing content, revisions, worker drafting and safe previews."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import MagicMock
from xml.etree import ElementTree as ET

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.world.home_sketches import inspect_sketch, render_sketch_svg, validate_drawing
from app.core.world.idle_activity_worker import IdleAwayActivityWorker, load_journal
from app.core.world.world_store import WorldStore
from app.llm.tools.world import InspectItemTool
from app.web.rest.memory_world_routes import register

NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
DRAWING = {"title": "Three rooftops", "strokes": [
    {"points": [[40, 220], [110, 140], [180, 220]], "width": 2, "shade": 0, "filled": False},
    {"points": [[140, 210], [220, 120], [290, 210]], "width": 2, "shade": 1, "filled": False},
], "note": "An original study of overlapping roof shapes."}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(timephrase, "utcnow", lambda: NOW)
    path = tmp_path / "sketches.db"
    db = ChatDatabase(path)
    store = WorldStore(path)
    store.seed_default()
    store.ensure_home_sketchbook()
    store.set_state(location_id=store.get_location("desk").id)
    yield store, db, path
    store.close()


def option(store, name="new_sketch", now=NOW):
    return next(row for row in store.available_home_actions(now=now)["actions"]
                if row["action"] == name)


def test_blank_materials_are_not_historical_drawing_or_readable_book(home):
    store, _, _ = home
    item = store.find_item("sketchbook")
    assert item.kind == "sketchbook" and inspect_sketch(item)["pages"] == []
    assert store.ensure_home_sketchbook() is False
    assert "fictional" in option(store)["sketch_context"]["reference"]
    assert "chapters" not in item.state


@pytest.mark.parametrize("bad", [None, {"title": "empty", "strokes": []},
    {"title": "markup", "svg": "<script>bad</script>"},
    {"title": "out of bounds", "strokes": [{"points": [[0, 0], [513, 0]]}]},
    {"title": "nonfinite", "strokes": [{"points": [[0, 0], [float('nan'), 0]]}]},
])
def test_invalid_or_missing_drawing_cannot_create_work_or_move_actor(home, bad):
    store, _, _ = home
    token = option(store)["token"]
    before = store.snapshot()
    with pytest.raises(ValueError):
        store.perform_home_action(token, drawing=bad)
    assert store.snapshot() == before


def test_saved_revisions_are_inspectable_immutable_and_retry_safe_after_restart(home):
    store, _, path = home
    first = option(store)
    receipt = store.perform_home_action(first["token"], drawing=DRAWING, now=NOW)
    assert receipt["drawing_sha256"]
    original = deepcopy(inspect_sketch(store.get_item(first["item_id"]))["drawing"])
    changed = {**deepcopy(DRAWING), "title": "Shadows between rooftops"}
    changed["strokes"].append({"points": [[110, 170], [150, 210], [130, 205]],
                               "width": 1, "shade": 2, "filled": True})
    with pytest.raises(ValueError, match="different drawing"):
        store.perform_home_action(first["token"], drawing=changed, now=NOW)
    revision = option(store, "revise_sketch")
    store.perform_home_action(revision["token"], drawing=changed, now=NOW)
    book = store.get_item(first["item_id"])
    assert inspect_sketch(book, 1, 1)["drawing"] == original
    assert inspect_sketch(book)["drawing"]["title"] == changed["title"]
    reopened = WorldStore(path)
    try:
        assert reopened.perform_home_action(first["token"], drawing=DRAWING)["already_applied"]
        assert inspect_sketch(reopened.get_item(book.id), 1, 1)["drawing"] == original
    finally:
        reopened.close()


def test_shared_daily_budget_and_real_colocated_supplies_are_required(home):
    store, _, _ = home
    for index in range(3):
        store.perform_home_action(
            option(store)["token"], drawing={**DRAWING, "title": f"Study {index}"},
        )
    assert not any(row["action"] in {"new_sketch", "revise_sketch"}
                   for row in store.available_home_actions()["actions"])
    tomorrow = NOW + timedelta(days=1)
    assert option(store, now=tomorrow)
    tools = store.find_item("pencil and eraser set")
    store.update_item(tools.id, location_id=store.get_location("bed").id)
    assert not any(row["action"] in {"new_sketch", "revise_sketch"}
                   for row in store.available_home_actions(now=tomorrow)["actions"])


def test_inspection_returns_one_actual_page_and_renderer_never_executes_markup(home):
    store, _, _ = home
    drawing = {**DRAWING, "title": "<script>alert(1)</script>"}
    receipt = store.perform_home_action(option(store)["token"], drawing=drawing)
    item = store.get_item(receipt["item_id"])
    session = SimpleNamespace(_world_store=store)
    result = json.loads(InspectItemTool(session).run({"item": "sketchbook"}))
    assert "pages" not in result["state"]["home_sketchbook"]
    assert result["sketch"]["drawing"]["strokes"] == validate_drawing(drawing)["strokes"]
    svg = render_sketch_svg(result["sketch"]["drawing"])
    assert "<script>" not in svg and "&lt;script&gt;" in svg
    assert len(ET.fromstring(svg).findall("{http://www.w3.org/2000/svg}polyline")) == 2
    app = FastAPI()
    proxy = MagicMock()
    proxy.world_store = store
    register(app, proxy, MagicMock(), MagicMock(), None)
    with TestClient(app) as client:
        response = client.get(result["sketch"]["render_url"])
        assert response.status_code == 200
        assert "image/svg+xml" in response.headers["content-type"]
        assert client.get(f"/api/world/items/{item.id}/sketch.svg?page=99").status_code == 400


def test_idle_model_saves_drawing_data_and_fallback_never_invents_it(home):
    from app.core.world.idle_life_store import IdleLifeStore
    from app.core.world.idle_life_trace import trace_run
    from scripts.review_self_choices import EpisodeReview, compare_choices

    store, db, _ = home
    draft = option(store)

    class Model:
        def chat_json(self, messages, **kwargs):
            if kwargs.get("surface") == "away_activity_plan":
                assert "Coordinates stay within x=0-512 and y=0-320" in messages[-1]["content"]
                assert "points=[[x,y],...]" in messages[-1]["content"]
                return json.dumps({"home_action_token": draft["token"], "drawing": DRAWING,
                                   "creation": None, "choice_reason": "own_interest"}), {}
            return "{}", {}

    worker = IdleAwayActivityWorker(
        world_store=store, kv_get=db.kv_get, kv_set=db.kv_set,
        user_display_name_provider=lambda: "Test user", ollama=Model(), model="test",
        llm_activity_ratio=1, cooldown_seconds=0,
    )
    snapshot = worker._build_candidates("Test user", NOW)
    assert "home_new_sketch" not in snapshot.candidates
    life = IdleLifeStore(db)
    with trace_run(life, "idle_activity", world=store):
        result = worker.run()
    assert result["fired"] == 1 and result["key"] == "home_new_sketch"
    assert inspect_sketch(store.get_item(draft["item_id"]))["drawing"]["title"] == DRAWING["title"]
    assert load_journal(db.kv_get)[-1]["key"] == "home_new_sketch"
    run_id = life.history(kind="choice")["events"][0]["run_id"]
    review = EpisodeReview(run_id, run_id)
    choices = compare_choices(life, "home_new_sketch", earlier=(), later=[review])
    assert choices["later"]["returns"] == [run_id]
