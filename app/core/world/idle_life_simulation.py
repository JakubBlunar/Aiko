"""Isolated world-worker simulation and compatible-snapshot executor replay.

python -m app.core.world.idle_life_simulation --snapshot BEFORE.db --days 3
python -m app.core.world.idle_life_simulation --snapshot BEFORE.db --replay decision.json

The parent only backs up a read-only SQLite source. All clocks and writes belong
to a disposable child process. No memory/concept writers or connectors are built.
Model calls require an explicit --model and a loopback Ollama endpoint; default
runs make no LLM calls. Simulation covers the listed world workers, rather than
all background reasoning or live conversation-situation inference.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import timedelta, timezone
import json
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace

from app.core.infra import timephrase


class VirtualClock:
    def __init__(self, start):
        self.value = start

    def now(self):
        return self.value


def next_instant(now, end, db, world):
    """Minute cadence, shortened for open beats, baths, parcels and return deadlines."""
    from app.core.world.in_progress_beat import load

    points = [min(end, now + timedelta(seconds=60))]
    beat = load(db.kv_get)
    if beat is not None:
        point = timephrase.parse_iso(beat.expected_end_at)
        if point and point > now:
            points.append(point)

    def visit(value):
        if not isinstance(value, dict):
            return
        for key, child in value.items():
            if key in {"ready_at", "arrives_at", "return_at", "expires_at"}:
                point = timephrase.parse_iso(child)
                if point and point > now:
                    points.append(point)
            elif isinstance(child, dict):
                visit(child)
            elif isinstance(child, list):
                for row in child[:80]:
                    visit(row)

    for item in world.list_items():
        visit(item.state)
    return min(points)


def _child(args):
    # Refuse an accidental --child call against any original/live database.
    project = Path(__file__).resolve().parents[3]
    path = Path(args.snapshot).resolve(strict=True)
    scratch = project / ".tmp-idle-life"
    if (
        path.name != "snapshot.db"
        or not path.parent.name.startswith("run-")
        or path.parent.parent != scratch.resolve()
        or scratch.resolve().parent != project
    ):
        raise ValueError("Child execution requires an owned disposable snapshot")
    from app.core.infra.agent_settings import AgentSettings
    from app.core.infra.chat_database import ChatDatabase
    from app.core.proactive.idle_worker_scheduler import IdleWorkerScheduler
    from app.core.world.garden_visit_worker import GardenVisitWorker
    from app.core.world.home_maintenance_worker import HomeMaintenanceWorker
    from app.core.world.idle_activity_worker import IdleAwayActivityWorker
    from app.core.world.idle_life_replay import load_plan, restore_rng, world_fingerprint
    from app.core.world.idle_life_review import review_events
    from app.core.world.idle_life_store import IdleLifeStore
    from app.core.world.idle_life_trace import emit, trace_run
    from app.core.world.plant_growth_worker import PlantGrowthWorker
    from app.core.world.sleep_lifecycle_worker import SleepLifecycleWorker
    from app.core.world.sleep_store import SleepStore
    from app.core.world.world_mutation_guard import WorldMutationGuard
    from app.core.world.world_store import WorldStore

    manifest = None
    if args.replay:
        path = Path(args.replay)
        if path.stat().st_size > 1000000:
            raise ValueError("Replay manifest exceeds 1 MB")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest.get("version") != 1 or not 1 <= len(manifest.get("plans", [])) <= 4:
            raise ValueError("Unsupported replay manifest")
    start = timephrase.parse_iso(manifest["occurred_at"] if manifest else args.start)
    if start is None:
        raise ValueError("An explicit ISO narrative start is required")
    clock = VirtualClock(start)
    timephrase._now_provider = clock.now
    db = ChatDatabase(Path(args.snapshot))
    # Inherited production trace is not simulated evidence. This is a disposable copy.
    db._get_conn().execute("DELETE FROM idle_life_events")
    db._get_conn().commit()
    store = IdleLifeStore(db, observation_seconds=lambda: clock.value.timestamp())
    world = WorldStore(Path(args.snapshot))
    world.idle_life_store = store
    sleep = SleepStore(db)
    settings = AgentSettings()
    if args.no_sleep:
        settings.sleep_enabled = False
    snapshot = (
        SimpleNamespace(
            shared_commitment_active=True,
            generation=1,
            inferred=SimpleNamespace(source_message_id=0, updated_at=start.isoformat()),
        )
        if args.shared_hold
        else None
    )
    guard = WorldMutationGuard(
        kv_get=db.kv_get,
        situation_snapshot_provider=lambda: snapshot,
        intentional_hold_seconds=(
            manifest.get("execution_policy", {}).get(
                "intentional_hold_seconds", settings.world_intentional_hold_seconds
            )
            if manifest
            else settings.world_intentional_hold_seconds
        ),
        sleep_state_provider=sleep.get_state,
    )
    from app.core.affect import circadian
    from app.core.proactive.hobby_worker import load_hobby
    from app.core.world.hobby import is_stalled

    def current_hobby():
        state = load_hobby(db.kv_get)
        if state is None or is_stalled(state, now=clock.value):
            return None
        return str(state.get("label") or state.get("artifact") or "")

    def period():
        return circadian.compute(now=timephrase.now()).period

    model = None
    context = None
    if args.model and manifest is None:
        from app.core.world.idle_life_simulation_model import local_model, context_provider

        model = local_model(args)
        context = context_provider(db, args)
    worker = IdleAwayActivityWorker(
        world_store=world,
        kv_get=db.kv_get,
        kv_set=db.kv_set,
        user_display_name_provider=lambda: "the user",
        rng=random.Random(args.seed),
        mutation_guard=guard,
        episode_ratio=0.35,
        in_progress_ratio=0.3,
        day_intention_enabled=True,
        idle_seed_ratio=0,
        ollama=model,
        model=args.model or None,
        llm_activity_ratio=max(0.0, min(1.0, args.llm_ratio)) if model else 0,
        context_provider=context.build if context else None,
        circadian_period_provider=period,
        day_color_provider=lambda: db.kv_get("aiko.day_color"),
        hobby_provider=current_hobby,
    )
    initial = world_fingerprint(world)
    if manifest is not None:
        if store.fingerprint != manifest.get("runtime_fingerprint"):
            return {
                "mode": "executor_replay",
                "status": "diverged",
                "reason": "runtime_fingerprint_mismatch",
                "model_calls": 0,
            }
        if initial != manifest.get("world_fingerprint"):
            return {
                "mode": "executor_replay",
                "status": "diverged",
                "reason": "snapshot_fingerprint_mismatch",
                "model_calls": 0,
            }
        restore_rng(worker._rng, manifest["rng_state"])
        results = []
        with trace_run(store, "away_activity", actor="simulation", world=world):
            for raw in manifest["plans"]:
                plan = load_plan(raw, world)
                if worker._mutation_block_reason(clock.value):
                    return {
                        "mode": "executor_replay",
                        "status": "diverged",
                        "reason": "snapshot_admission_blocked",
                        "model_calls": 0,
                    }
                receipt = worker._apply_world_mutation(plan)
                expected = raw.get("expected_receipt") or {}
                if (plan.item_effect or plan.creation or plan.home_action_token) and not receipt:
                    raise ValueError("A recorded effect did not complete")
                for key in (
                    "effect",
                    "token",
                    "item_id",
                    "revision",
                    "text_sha256",
                    "drawing_sha256",
                    "finished",
                    "progress",
                ):
                    if key in expected and (receipt or {}).get(key) != expected[key]:
                        return {
                            "mode": "executor_replay",
                            "status": "diverged",
                            "reason": "receipt_mismatch",
                            "field": key,
                            "model_calls": 0,
                        }
                emit("step_completed", {"key": plan.key, "receipt": receipt})
                results.append({"key": plan.key, "receipt": receipt})
        return {
            "mode": "executor_replay",
            "status": "matched",
            "steps": results,
            "model_calls": 0,
            "scope": manifest.get("scope"),
            "partial_sequence": manifest.get("partial_sequence", False),
        }

    scheduler = IdleWorkerScheduler(
        is_quiet_callback=lambda: True,
        kv_get=db.kv_get,
        kv_set=db.kv_set,
        trace_store=store,
        trace_world_provider=lambda: world,
        sleep_state_provider=sleep.get_state,
        idle_depth_provider=lambda: (clock.value - start).total_seconds() + 10800,
        contention_provider=lambda: "none",
    )

    def notify(patch):
        return None

    scheduler.register(PlantGrowthWorker(world, notify=notify))
    scheduler.register(HomeMaintenanceWorker(world, notify))
    scheduler.register(
        SleepLifecycleWorker(
            sleep_store=sleep,
            chat_db=db,
            world_store=world,
            world_guard=guard,
            idle_depth_provider=lambda: (clock.value - start).total_seconds() + 10800,
            settings_provider=lambda: settings,
            world_notify=notify,
        )
    )
    scheduler.register(
        GardenVisitWorker(
            world,
            notify=notify,
            kv_get=db.kv_get,
            kv_set=db.kv_set,
            rng=random.Random(args.seed + 1),
            mutation_guard=guard,
            circadian_period_provider=period,
            idle_seconds_provider=lambda: (clock.value - start).total_seconds() + 10800,
        )
    )
    scheduler.register(worker)
    end = start + timedelta(days=max(0.001, min(14.0, args.days)))
    ticks = 0
    while clock.value < end and ticks < min(21000, max(1, args.max_ticks)):
        scheduler._tick()
        clock.value = next_instant(clock.value, end, db, world)
        ticks += 1
        if model is not None and model.exhausted:
            break
    evidence = [
        store._row(row)
        for row in db._get_conn().execute("SELECT * FROM idle_life_events ORDER BY id")
    ]
    report = review_events(evidence, time_basis="narrative", window_end=clock.value)
    return {
        "mode": "model_world_simulation" if model else "world_worker_simulation",
        "model_calls": model.calls if model else 0,
        "model_budget": model.status() if model else None,
        "status": "model_budget"
        if model and model.exhausted
        else "complete"
        if clock.value >= end
        else "tick_limit",
        "narrative_start": start.isoformat(),
        "narrative_end": clock.value.isoformat(),
        "ticks": ticks,
        "seed": args.seed,
        "initial_world_fingerprint": initial,
        "final_world_fingerprint": world_fingerprint(world),
        "workers": [row["name"] for row in scheduler.get_records()],
        "review": report,
        "limitations": "Worker defaults, no hobby/research workers, topic-graph reconstruction "
        "or live conversation-situation inference. Fresh model/context choices "
        "only when --model is explicit; this is not production behavior.",
        "scenario": {
            "shared_hold": args.shared_hold,
            "sleep_enabled": not args.no_sleep,
            "context_ratio": args.context_ratio if model else 0,
            "llm_ratio": args.llm_ratio if model else 0,
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--days", type=float, default=3)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--start")
    parser.add_argument("--max-ticks", type=int, default=21000)
    parser.add_argument("--replay")
    parser.add_argument("--model", default="")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--max-model-calls", type=int, default=12)
    parser.add_argument("--max-model-tokens", type=int, default=24000)
    parser.add_argument("--context-window", type=int, default=8192)
    parser.add_argument("--context-ratio", type=float, default=0.5)
    parser.add_argument("--llm-ratio", type=float, default=0.5)
    parser.add_argument("--session-id", default="")
    parser.add_argument("--shared-hold", action="store_true")
    parser.add_argument("--no-sleep", action="store_true")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.child:
            print(json.dumps(_child(args), ensure_ascii=True))
            return 0
        source = Path(args.snapshot).resolve(strict=True)
        project = Path(__file__).resolve().parents[3]
        scratch = project / ".tmp-idle-life"
        if scratch.resolve().parent != project:
            raise ValueError("Simulation scratch must remain within the project")
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run-", dir=scratch) as work:
            work = Path(work).resolve()
            if not work.is_relative_to(scratch.resolve()):
                raise ValueError("Invalid simulation scratch location")
            print("Disposable simulation state: " + str(work), file=sys.stderr)
            destination = work / "snapshot.db"
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
                with closing(sqlite3.connect(destination)) as copied:
                    original.backup(copied)
            command = [
                sys.executable,
                "-m",
                "app.core.world.idle_life_simulation",
                "--child",
                "--snapshot",
                str(destination),
                "--days",
                str(args.days),
                "--seed",
                str(args.seed),
                "--max-ticks",
                str(args.max_ticks),
                "--start",
                args.start or timephrase.real_now().astimezone(timezone.utc).isoformat(),
            ]
            if args.replay:
                command.extend(["--replay", str(Path(args.replay).resolve(strict=True))])
            if args.model:
                command.extend(
                    [
                        "--model",
                        args.model,
                        "--ollama-url",
                        args.ollama_url,
                        "--max-model-calls",
                        str(args.max_model_calls),
                        "--max-model-tokens",
                        str(args.max_model_tokens),
                        "--context-window",
                        str(max(1024, min(65536, args.context_window))),
                        "--context-ratio",
                        str(max(0.0, min(1.0, args.context_ratio))),
                        "--llm-ratio",
                        str(max(0.0, min(1.0, args.llm_ratio))),
                        "--session-id",
                        args.session_id,
                    ]
                )
            if args.shared_hold:
                command.append("--shared-hold")
            if args.no_sleep:
                command.append("--no-sleep")
            result = subprocess.run(
                command,
                cwd=project,
                capture_output=True,
                text=True,
                timeout=min(600, max(120, min(100, args.max_model_calls) * 30 + 60))
                if args.model
                else 120,
            )
            if result.returncode:
                raise ValueError("Simulation child failed: " + result.stderr[-1000:])
            print(result.stdout.strip())
            return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc), "status": "failed"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
