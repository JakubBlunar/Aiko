"""Bounded execution metadata and task-local correlation for autonomous life."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from functools import wraps
from typing import Any
from uuid import uuid4
import logging
import time

from app.core.infra import timephrase

log = logging.getLogger("app.idle_life")
LIFE_WORKERS = frozenset({
    "away_activity", "hobby", "garden_visit", "plant_growth", "room_evolution",
    "home_maintenance", "sleep_lifecycle", "idle_curiosity", "idle_knowledge",
})


@dataclass(frozen=True)
class TraceRun:
    store: Any
    run_id: str
    worker: str
    actor: str


_current: ContextVar[TraceRun | None] = ContextVar("idle_life_run", default=None)


def current_run() -> TraceRun | None:
    return _current.get()


def emit(kind: str, payload: dict[str, Any], *, event_key: str | None = None) -> None:
    run = current_run()
    if run is not None:
        try:
            run.store.record(kind, run.worker, payload, run_id=run.run_id,
                             actor=run.actor, event_key=event_key)
        except Exception:
            log.warning("idle life diagnostic capture failed", exc_info=True)


def bounded(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return {"omitted": "depth_limit"}
    if isinstance(value, dict):
        rows = list(value.items())
        result = {str(k)[:100]: bounded(v, depth + 1) for k, v in rows[:80]}
        if len(rows) > 80:
            result["omitted_keys"] = len(rows) - 80
        return result
    if isinstance(value, (list, tuple)):
        result = [bounded(v, depth + 1) for v in value[:80]]
        if len(value) > 80:
            result.append({"omitted_entries": len(value) - 80})
        return result
    if isinstance(value, str):
        return value[:500] + (" [truncated]" if len(value) > 500 else "")
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)[:100]


def item_facts(item: Any) -> dict[str, Any]:
    """Numeric resource/state facts, never full writings, drawings or private labels."""
    row = item if isinstance(item, dict) else {
        key: getattr(item, key, None) for key in ("quantity", "location_id", "state")
    }
    facts = {"quantity": row.get("quantity"), "location_id": row.get("location_id")}

    def visit(value, prefix="state", depth=0):
        if depth > 5 or not isinstance(value, dict):
            return
        for key, child in value.items():
            path = prefix + "." + str(key)
            if isinstance(child, (int, float, bool)) or key in {
                "stage", "phase", "recipe", "prepared_at", "status", "sha256",
            }:
                facts[path] = child
            elif isinstance(child, dict):
                visit(child, path, depth + 1)
            elif key in {"revisions", "pages"} and isinstance(child, list):
                facts[path + ".count"] = len(child)
                if child and isinstance(child[-1], dict):
                    visit(child[-1], path + ".latest", depth + 1)
    visit(row.get("state") or {})
    return facts


def item_changes(before: list[Any], after: list[Any]) -> list[dict[str, Any]]:
    def index(rows):
        return {str(row.get("id") if isinstance(row, dict) else row.id): item_facts(row)
                for row in rows}
    old, new = index(before), index(after)
    changes = []
    for iid in sorted(old.keys() | new.keys()):
        left, right = old.get(iid, {}), new.get(iid, {})
        fields = {key: {"before": left.get(key), "after": right.get(key)}
                  for key in left.keys() | right.keys() if left.get(key) != right.get(key)}
        if fields or iid not in old or iid not in new:
            changes.append({"item_id": iid, "change": "created" if iid not in old else
                            "deleted" if iid not in new else "updated", "fields": fields})
    return changes


def world_snapshot(world: Any) -> dict[str, Any]:
    if world is None:
        return {}
    try:
        state = world.get_state()
        return {"items": deepcopy(world.list_items()),
                "state": {key: getattr(state, key, None) for key in
                          ("scene_id", "location_id", "posture", "activity")}}
    except Exception:
        return {"snapshot_unavailable": True}


@contextmanager
def trace_run(store, worker: str, *, actor="autonomous", world=None):
    """Direct debug runs and scheduled runs share one correlation scope."""
    previous = current_run()
    if store is None or previous is not None:
        yield
        return
    run = TraceRun(store, uuid4().hex, worker, actor)
    token = _current.set(run)
    started = time.monotonic()
    before = world_snapshot(world)
    emit("run_started", {"narrative_at": timephrase.utcnow().isoformat()})
    try:
        yield
    except BaseException as exc:
        emit("run_failed", {"error_type": type(exc).__name__})
        raise
    finally:
        try:
            after = world_snapshot(world)
            try:
                changes = item_changes(before.get("items", []), after.get("items", []))
            except Exception as exc:
                changes = []
                after["snapshot_unavailable"] = True
                emit("capture_failed", {
                    "stage": "resource_delta", "error_type": type(exc).__name__,
                })
            emit("run_finished", {
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
                "resource_changes": changes,
                "state_before": before.get("state"), "state_after": after.get("state"),
                "snapshot_unavailable": bool(before.get("snapshot_unavailable") or
                                             after.get("snapshot_unavailable")),
                "effect_evidence": "observed_before_after; concurrent world edits may contribute",
            })
        finally:
            _current.reset(token)


def execute_traced(store, worker, call, *, actor="autonomous", world=None):
    if store is None or current_run() is not None:
        return call()
    with trace_run(store, worker, actor=actor, world=world):
        result = call()
        if isinstance(result, dict):
            # No excerpts, generated text bodies or unbounded tool output.
            keys = {"fired", "key", "activity", "summary", "reason", "errored", "disabled",
                    "in_progress", "expected_end_at", "episode", "closed_intention",
                    "pursuit_note_id", "item_effect", "move", "progress", "advanced",
                    "started", "rotated", "research_memory_id", "research_result_ids",
                    "completed_in_progress", "resumed", "interrupted", "minutes_left",
                    "status", "episode_id", "phase", "returned_to_slug", "watered", "harvested",
                    "promoted", "scanned", "discarded", "waiting", "from", "to",
                    "research_question_id", "research_result_id"}
            emit("run_result", {key: value for key, value in result.items()
                                if key in keys or key.startswith("skipped_")})
        return result


def traced_worker(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        world = getattr(self, "_world_store", None) or getattr(self, "_world", None)
        store = getattr(self, "_trace_store", None) or getattr(world, "idle_life_store", None)
        return execute_traced(store, self.name, lambda: method(self, *args, **kwargs),
                              actor="direct_worker", world=world)
    return wrapped
