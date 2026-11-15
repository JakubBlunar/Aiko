"""Real child-process isolation, deterministic state capture and compatible executor replay."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.world.idle_activity_worker import IdleAwayActivityWorker
from app.core.world.idle_life_replay import (
    pack_rng,
    replay_manifest,
    restore_rng,
    world_fingerprint,
)
from app.core.world.idle_life_store import IdleLifeStore
from app.core.world.world_store import WorldStore

NOW = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def backup(db, path):
    with closing(sqlite3.connect(path)) as target:
        db._get_conn().backup(target)


def run_child(path, *args):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.core.world.idle_life_simulation",
            "--snapshot",
            str(path),
            "--start",
            NOW.isoformat(),
            *args,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=50,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(timephrase, "utcnow", lambda: NOW)
    db = ChatDatabase(tmp_path / "original.db")
    store = WorldStore(db._db_path)
    store.seed_default()
    store.ensure_home_drinks()
    trace = IdleLifeStore(db)
    store.idle_life_store = trace
    yield store, db, trace
    store.close()


def test_rng_portable_without_pickle_and_invalid_state_rejected():
    original, restored = random.Random(19), random.Random(0)
    packed = pack_rng(original)
    restore_rng(restored, packed)
    assert [original.random() for _ in range(10)] == [restored.random() for _ in range(10)]
    with pytest.raises(ValueError):
        restore_rng(restored, ["A" * 401])


def test_fingerprint_ignores_time_derived_drink_views(world, monkeypatch):
    store, _, _ = world
    action = next(
        row
        for row in store.available_home_actions()["actions"]
        if row["action"] == "prepare_drink" and row["recipe"] == "tea"
    )
    store.perform_home_action(action["token"], relocate=True)
    first = world_fingerprint(store)
    monkeypatch.setattr(timephrase, "utcnow", lambda: NOW + timedelta(hours=4))
    assert world_fingerprint(store) == first


def test_simulation_never_changes_original_or_parent_clock_and_cleans_scratch(world):
    store, db, _ = world
    initial = world_fingerprint(store)
    clock = timephrase._now_provider
    before = set((ROOT / ".tmp-idle-life").glob("run-*"))
    report = run_child(db._db_path, "--days", ".02", "--max-ticks", "40", "--no-sleep")
    assert report["status"] == "complete" and report["model_calls"] == 0
    assert "away_activity" in report["workers"]
    assert timephrase._now_provider is clock
    store._reload_mirror()
    assert world_fingerprint(store) == initial
    assert db.kv_get("away_activity.last_fired_at") is None
    assert set((ROOT / ".tmp-idle-life").glob("run-*")) == before


def test_child_flag_cannot_point_at_original_state(world):
    store, db, _ = world
    fingerprint = world_fingerprint(store)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.core.world.idle_life_simulation",
            "--child",
            "--snapshot",
            str(db._db_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 1
    assert "owned disposable snapshot" in result.stderr
    assert world_fingerprint(store) == fingerprint


def test_recorded_home_executor_replays_only_compatible_snapshot(world, tmp_path):
    store, db, trace = world
    before = tmp_path / "before.db"
    backup(db, before)
    worker = IdleAwayActivityWorker(
        world_store=store,
        kv_get=db.kv_get,
        kv_set=db.kv_set,
        user_display_name_provider=lambda: "the user",
        rng=random.Random(4),
    )
    worker.force_activity("home_prepare_drink")
    assert worker.run()["fired"] == 1
    run_id = trace.history(kind="plan_ready")["events"][0]["run_id"]
    manifest = replay_manifest(trace.decision(run_id)["events"], store)
    assert "text" not in json.dumps(manifest)
    path = tmp_path / "replay.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = run_child(before, "--replay", str(path))
    assert result["status"] == "matched" and result["model_calls"] == 0
    # The after-state is incompatible and must be rejected before executing anything.
    incompatible = tmp_path / "after.db"
    backup(db, incompatible)
    result = run_child(incompatible, "--replay", str(path))
    assert result["status"] == "diverged"
    assert result["reason"] == "snapshot_fingerprint_mismatch"


def test_saved_writing_replay_checks_exact_revision_and_missing_work(world, tmp_path):
    store, db, trace = world
    before = tmp_path / "before-writing.db"
    backup(db, before)

    def chat(*args, **kwargs):
        return json.dumps(
            {
                "activity": "writing",
                "posture": "sitting",
                "location_slug": "desk",
                "creation": {
                    "title": "A small lamp",
                    "form": "poem",
                    "item_id": None,
                    "base_revision": 0,
                    "text": "The light went softly down the stairs. " * 3,
                },
            }
        ), None

    worker = IdleAwayActivityWorker(
        world_store=store,
        kv_get=db.kv_get,
        kv_set=db.kv_set,
        user_display_name_provider=lambda: "the user",
        rng=random.Random(4),
        llm_activity_ratio=1,
        ollama=SimpleNamespace(chat_json=chat),
        model="fixture",
    )
    result = worker.run()
    assert result["fired"] == 1
    run_id = trace.history(kind="plan_ready")["events"][0]["run_id"]
    manifest = replay_manifest(trace.decision(run_id)["events"], store)
    assert manifest["plans"][0]["creation"]["text"].startswith("The light")
    path = tmp_path / "writing.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert run_child(before, "--replay", str(path))["status"] == "matched"
    item = store.get_item(result["item_effect"]["item_id"])
    item.state["revisions"][0]["text"] = "tampered"
    with pytest.raises(ValueError, match="unavailable or changed"):
        replay_manifest(trace.decision(run_id)["events"], store)


def test_shared_hold_scenario_uses_virtual_time_and_retains_evidence(world):
    _, db, _ = world
    report = run_child(
        db._db_path, "--days", ".3", "--max-ticks", "500", "--shared-hold", "--no-sleep"
    )
    assert report["status"] == "complete"
    assert any(row["kind"] == "long_shared_hold" for row in report["review"]["flags"])
    assert report["review"]["autonomous_steps"] == 0


def test_partial_sequence_exports_only_completed_effects(world, tmp_path, monkeypatch):
    from app.core.world.idle_activity_plan import ActivityPlan

    store, db, trace = world
    before = tmp_path / "before-partial.db"
    backup(db, before)
    worker = IdleAwayActivityWorker(
        world_store=store,
        kv_get=db.kv_get,
        kv_set=db.kv_set,
        user_display_name_provider=lambda: "the user",
        rng=random.Random(4),
    )
    worker.force_activity("home_prepare_drink")
    monkeypatch.setattr(
        worker,
        "_plan_episode",
        lambda first, snapshot, now: [
            first,
            ActivityPlan(key="wander", posture="sitting", activity="thinking", summary="rested"),
        ],
    )
    apply = worker._apply_world_mutation

    def fail_second(plan):
        if plan.key == "wander":
            raise RuntimeError("second step failed")
        return apply(plan)

    monkeypatch.setattr(worker, "_apply_world_mutation", fail_second)
    assert worker.run()["reason"] == "world_action_failed"
    run_id = trace.history(kind="plan_ready")["events"][0]["run_id"]
    manifest = replay_manifest(trace.decision(run_id)["events"], store)
    assert manifest["partial_sequence"] and len(manifest["plans"]) == 1
    path = tmp_path / "partial.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = run_child(before, "--replay", str(path))
    assert result["status"] == "matched" and result["partial_sequence"]


def test_drawing_export_and_replay_use_the_exact_saved_strokes(world, tmp_path):
    store, db, trace = world
    store.ensure_home_sketchbook()
    offered = next(
        row for row in store.available_home_actions()["actions"] if row["action"] == "new_sketch"
    )
    drawing = {
        "title": "Roof shadows",
        "strokes": [{"points": [[10, 100], [60, 40], [120, 100]], "width": 2, "shade": 1}],
        "note": "A fictional skyline study.",
    }
    before = tmp_path / "before-sketch.db"
    backup(db, before)

    def chat(*args, **kwargs):
        return json.dumps(
            {
                "home_action_token": offered["token"],
                "creation": None,
                "drawing": drawing,
                "changed_item": "",
            }
        ), None

    worker = IdleAwayActivityWorker(
        world_store=store,
        kv_get=db.kv_get,
        kv_set=db.kv_set,
        user_display_name_provider=lambda: "the user",
        rng=random.Random(4),
        llm_activity_ratio=1,
        ollama=SimpleNamespace(chat_json=chat),
        model="fixture",
    )
    assert worker.run()["fired"] == 1
    run_id = trace.history(kind="plan_ready")["events"][0]["run_id"]
    manifest = replay_manifest(trace.decision(run_id)["events"], store)
    assert manifest["plans"][0]["home_action_drawing"]["title"] == "Roof shadows"
    path = tmp_path / "drawing.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    assert run_child(before, "--replay", str(path))["status"] == "matched"


def test_explicit_model_mode_uses_copied_context_and_bounded_local_transport(world):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    store, db, _ = world
    db._get_conn().execute(
        "INSERT INTO messages(session_id,role,content,token_count,created_at) VALUES(?,?,?,?,?)",
        ("test", "user", "A ceramic mug handle design.", 8, NOW.isoformat()),
    )
    db._get_conn().commit()
    initial = world_fingerprint(store)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            content = json.dumps(
                {
                    "activity": "writing",
                    "posture": "sitting",
                    "location_slug": "desk",
                    "creation": {
                        "title": "Handle study",
                        "form": "poem",
                        "item_id": None,
                        "base_revision": 0,
                        "text": "A warm curve fits the hand at rest. " * 3,
                    },
                }
            )
            response = json.dumps(
                {
                    "message": {"role": "assistant", "content": content},
                    "prompt_eval_count": 100,
                    "eval_count": 70,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        report = run_child(
            db._db_path,
            "--days",
            ".03",
            "--max-ticks",
            "50",
            "--no-sleep",
            "--model",
            "fixture",
            "--ollama-url",
            f"http://127.0.0.1:{server.server_port}",
            "--max-model-calls",
            "1",
            "--max-model-tokens",
            "10000",
            "--context-ratio",
            "1",
            "--llm-ratio",
            "1",
            "--session-id",
            "test",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    assert report["mode"] == "model_world_simulation"
    assert report["status"] == "model_budget" and report["model_calls"] == 1
    assert len(requests) == 1
    assert "ceramic mug handle" in json.dumps(requests[0])
    assert report["review"]["activity_mix"]["writing"] == 1
    store._reload_mirror()
    assert world_fingerprint(store) == initial
    assert not any(item.state.get("creation_schema") for item in store.list_items())
