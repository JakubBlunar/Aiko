"""Portable, validated executor replay without private prompts or pickle/eval."""

from __future__ import annotations

import base64
from dataclasses import asdict, fields
import hashlib
import json
import math
import zlib

from app.core.world.home_sketches import drawing_hash, validate_drawing
from app.core.world.idle_activity_plan import ActivityPlan, ItemEffect
from app.core.world.idle_creations import parse_draft, revisions


def world_fingerprint(world):
    state = world.get_state()
    # Raw persisted fields exclude time-derived temperature/view projections.
    body = {
        "state": asdict(state),
        "items": [asdict(item) for item in world.list_items()],
        "locations": [asdict(loc) for loc in world.list_locations()],
    }
    raw = json.dumps(body, sort_keys=True, default=str, ensure_ascii=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def pack_rng(rng):
    raw = json.dumps(rng.getstate()).encode()
    text = base64.b64encode(zlib.compress(raw)).decode("ascii")
    return [text[index : index + 400] for index in range(0, len(text), 400)]


def restore_rng(rng, chunks):
    if (
        not isinstance(chunks, list)
        or len(chunks) > 40
        or any(not isinstance(chunk, str) or len(chunk) > 400 for chunk in chunks)
    ):
        raise ValueError("Invalid replay RNG state")
    compressed = base64.b64decode("".join(chunks), validate=True)
    decoder = zlib.decompressobj()
    raw = decoder.decompress(compressed, 16000)
    if decoder.unconsumed_tail or not decoder.eof:
        raise ValueError("Replay RNG state exceeds its bound")
    version, state, gaussian = json.loads(raw)
    if (
        version != 3
        or len(state) != 625
        or any(type(n) is not int or not 0 <= n <= 2**32 - 1 for n in state[:-1])
        or type(state[-1]) is not int
        or not 0 <= state[-1] <= 624
        or gaussian is not None
        and (type(gaussian) not in (int, float) or not math.isfinite(gaussian))
    ):
        raise ValueError("Invalid replay RNG state")
    rng.setstate((version, tuple(state), gaussian))


def plan_metadata(plan):
    result = {
        field.name: getattr(plan, field.name)
        for field in fields(plan)
        if field.name not in {"creation", "home_action_drawing", "item_effect"}
    }
    result["item_effect"] = asdict(plan.item_effect) if plan.item_effect else None
    result["creation"] = (
        {key: value for key, value in asdict(plan.creation).items() if key != "text"}
        if plan.creation
        else None
    )
    result["drawing_required"] = plan.home_action_drawing is not None
    return result


def replay_manifest(events, world):
    ready = next((row for row in events if row["kind"] == "plan_ready"), None)
    if ready is None:
        raise ValueError("This decision has no recorded executable plan")
    completed = [row for row in events if row["kind"] == "step_completed"]
    plans = ready["payload"]["plans"]
    if not completed:
        raise ValueError("No completed steps are available for replay")
    result = {
        "version": 1,
        "scope": "executor replay; not fresh model or scheduler behavior",
        "run_id": ready["run_id"],
        "occurred_at": ready["occurred_at"],
        "world_fingerprint": ready["payload"]["world_fingerprint"],
        "runtime_fingerprint": ready["payload"].get("runtime_fingerprint"),
        "rng_state": ready["payload"]["rng_state"],
        "execution_policy": ready["payload"].get("execution_policy", {}),
        "plans": [],
    }
    for plan, event in zip(plans, completed, strict=False):
        receipt = event["payload"].get("receipt") or {}
        row = {**plan, "expected_receipt": receipt}
        if plan.get("creation"):
            item = world.get_item(receipt.get("item_id"))
            saved = next(
                (rev for rev in revisions(item) if rev.get("revision") == receipt.get("revision")),
                None,
            )
            if saved is None or hashlib.sha256(saved["text"].encode()).hexdigest() != receipt.get(
                "text_sha256"
            ):
                raise ValueError("The exact saved writing is unavailable or changed")
            row["creation"] = {**plan["creation"], "text": saved["text"]}
        if plan.get("drawing_required"):
            item = world.get_item(receipt.get("item_id"))
            pages = (item.state.get("home_sketchbook") or {}).get("pages", []) if item else []
            matching = [
                rev
                for page in pages
                for rev in page.get("revisions", [])
                if rev.get("sha256") == receipt.get("drawing_sha256")
            ]
            drawing = (
                validate_drawing(
                    {
                        key: matching[0][key]
                        for key in ("title", "strokes", "note")
                        if key in matching[0]
                    }
                )
                if matching
                else None
            )
            if drawing is None or drawing_hash(drawing) != receipt.get("drawing_sha256"):
                raise ValueError("The exact saved drawing is unavailable or changed")
            row["home_action_drawing"] = drawing
        result["plans"].append(row)
    result["partial_sequence"] = len(completed) != len(plans)
    return result


def load_plan(raw, world):
    if not isinstance(raw, dict):
        raise ValueError("Invalid replay plan")
    allowed = {field.name for field in fields(ActivityPlan)}
    values = {key: value for key, value in raw.items() if key in allowed}
    if values.get("item_effect") is not None:
        values["item_effect"] = ItemEffect(**values["item_effect"])
    if values.get("creation") is not None:
        values["creation"] = parse_draft(values["creation"], world.list_items())
        if values["creation"] is None:
            raise ValueError("The creation no longer matches this snapshot")
    if values.get("home_action_drawing") is not None:
        values["home_action_drawing"] = validate_drawing(values["home_action_drawing"])
    return ActivityPlan(**values)


def trace_plan(world, chain, rng, hold_seconds):
    from app.core.world.idle_life_trace import current_run, emit

    run = current_run()
    if run is None:
        return
    try:
        emit(
            "plan_ready",
            {
                "runtime_fingerprint": run.store.fingerprint,
                "world_fingerprint": world_fingerprint(world),
                "rng_state": pack_rng(rng),
                "execution_policy": {"intentional_hold_seconds": hold_seconds},
                "plans": [plan_metadata(beat) for beat in chain],
            },
        )
    except Exception as exc:
        emit("capture_failed", {"stage": "replay_plan", "error_type": type(exc).__name__})
