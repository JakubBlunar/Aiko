"""L45 gate tuning report -- what each concept threshold would become, and why.

Three modes, all read-only unless you ask otherwise:

    python scripts/concept_gate_report.py              # dry run: solve every gate now
    python scripts/concept_gate_report.py --trend       # read the snapshot history
    python scripts/concept_gate_report.py --adopt NAME  # hand a hand-set value over

The dry run solves the live registry against the live database without the app
running and without writing anything, so a spec change can be checked against
real data before it ships. It is the same code path the worker uses -- the
specs, the populations and the solver are all imported, never restated -- so
what it prints is what the worker would do.

``--trend`` is the report to read before promoting an observe-mode gate to
``apply``: it walks ``data/tuning/concept_population.jsonl`` and shows how the
graph and each gate's proposal have moved, which is the question a single
snapshot cannot answer.

``--adopt`` is the one writing path, and it is deliberately manual. See
:func:`app.core.infra.gate_tuning_store.adopt_gate`.
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.concepts.concept_store import Concept  # noqa: E402
from app.core.concepts.gate_measure import populations  # noqa: E402
from app.core.concepts.gate_tuning import (  # noqa: E402
    GATE_SPECS,
    MODE_APPLY,
    kind_floor_defaults,
    solve_all,
)
from app.core.infra.gate_tuning_store import (  # noqa: E402
    adopt_gate,
    build_document,
    load_gates,
    load_population,
    user_memory_overrides,
)
from app.core.infra.settings import load_settings  # noqa: E402
from app.core.memory.surfacing_outcome_store import ENGAGED_LABEL  # noqa: E402

DEFAULT_DB = Path("data/chat_sessions.db")

_CAP_SETTINGS = (
    "context_budget_core_cap",
    "concept_core_openness_slots",
    "profile_concept_max_lines",
)


def _connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        sys.exit(f"no database at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _decode(blob: bytes | None, dim: int) -> np.ndarray:
    """Label embedding from its stored blob, empty on any mismatch."""
    if not blob or dim <= 0:
        return np.zeros(0, dtype=np.float32)
    arr = np.frombuffer(blob, dtype=np.float32)
    if arr.size != dim:
        return np.zeros(0, dtype=np.float32)
    return np.array(arr, dtype=np.float32)


def _load_concepts(conn: sqlite3.Connection) -> list[Concept]:
    """Live rows including embeddings -- the cosine gates need the vectors."""
    out: list[Concept] = []
    for r in conn.execute(
        "SELECT id, label, kind, subject, status, confidence, plasticity, "
        "       evidence_count, distinct_source_count, created_at, "
        "       promoted_at, last_reinforced_at, embedding, dim "
        "FROM concepts"
    ):
        out.append(
            Concept(
                label=str(r["label"] or ""),
                kind=str(r["kind"] or ""),
                subject=str(r["subject"] or "user"),
                status=str(r["status"] or ""),
                confidence=float(r["confidence"] or 0.0),
                plasticity=float(r["plasticity"] or 0.5),
                evidence_count=int(r["evidence_count"] or 0),
                distinct_source_count=int(r["distinct_source_count"] or 0),
                created_at=str(r["created_at"] or ""),
                promoted_at=r["promoted_at"],
                last_reinforced_at=r["last_reinforced_at"],
                embedding=_decode(r["embedding"], int(r["dim"] or 0)),
                concept_id=int(r["id"]),
            )
        )
    return out


def _cluster_rates(conn: sqlite3.Connection, *, min_settled: int) -> list[float]:
    """Per-cluster engaged rates from the L37 ledger.

    The worker asks ``SurfacingOutcomeStore.engaged_rate_by_cluster`` for this,
    but that store needs a *writable* ``ChatDatabase`` (it migrates on
    construction), so offline the aggregate is recomputed. Same two arms as the
    real query: ``cluster`` rows whose ``item_id`` is the cluster, plus
    ``memory`` rows joined through ``memory_topic_assignments``. Concept and
    cue rows carry no cluster and are excluded either way.
    """
    try:
        rows = conn.execute(
            "SELECT cluster_id, SUM(settled_flag) AS settled, "
            "       SUM(engaged_flag) AS engaged FROM ("
            "  SELECT item_id AS cluster_id, "
            "         CASE WHEN settled_at IS NOT NULL THEN 1 ELSE 0 END "
            "           AS settled_flag, "
            "         CASE WHEN engagement_label = ? THEN 1 ELSE 0 END "
            "           AS engaged_flag "
            "  FROM surfacing_outcomes "
            "  WHERE item_kind = 'cluster' AND item_id > 0 "
            "  UNION ALL "
            "  SELECT a.cluster_id AS cluster_id, "
            "         CASE WHEN so.settled_at IS NOT NULL THEN 1 ELSE 0 END, "
            "         CASE WHEN so.engagement_label = ? THEN 1 ELSE 0 END "
            "  FROM surfacing_outcomes so "
            "  JOIN memory_topic_assignments a ON a.memory_id = so.item_id "
            "  WHERE so.item_kind = 'memory' AND so.item_id > 0"
            ") GROUP BY cluster_id HAVING SUM(settled_flag) >= ?",
            (ENGAGED_LABEL, ENGAGED_LABEL, max(1, int(min_settled))),
        ).fetchall()
    except sqlite3.Error:
        return []
    out: list[float] = []
    for row in rows:
        settled = int(row["settled"] or 0)
        if settled <= 0:
            continue
        out.append(float(int(row["engaged"] or 0)) / float(settled))
    return out


def collect(
    conn: sqlite3.Connection, *, settings: Any, pairs: int, seed: int | None,
) -> dict[str, Any]:
    ms = settings.memory
    rows = _load_concepts(conn)
    pops = populations(
        rows,
        cluster_engaged_rates=_cluster_rates(
            conn, min_settled=int(getattr(ms, "taste_min_settled", 4)),
        ),
        cosine_pairs=pairs,
        rng=random.Random(seed) if seed is not None else random.Random(),
    )

    current = dict(kind_floor_defaults())
    for spec in GATE_SPECS:
        if spec.is_setting_field:
            current[spec.setting] = float(getattr(ms, spec.setting, 0.0))
    caps = {name: int(getattr(ms, name, 0)) for name in _CAP_SETTINGS}

    solutions = solve_all(GATE_SPECS, pops, current=current, caps=caps)
    document = build_document(
        solutions,
        now=datetime.now(timezone.utc),
        previous=load_gates(),
        user_overrides=user_memory_overrides(),
    )
    return {
        "concepts": len(rows),
        "populations": {name: len(vals) for name, vals in sorted(pops.items())},
        "gates": document["gates"],
        "missing_populations": sorted(
            spec.population
            for spec in GATE_SPECS
            if spec.population not in pops
        ),
    }


def render(data: dict[str, Any]) -> str:
    out: list[str] = []
    out.append(
        f"Gate tuning dry run  {datetime.now(timezone.utc).isoformat()}"
    )
    out.append(f"{data['concepts']} concepts loaded")
    missing = sorted(set(data["missing_populations"]))
    if missing:
        out.append(f"  unmeasurable populations: {missing}")
    out.append("")

    gates = data["gates"]
    applied = [n for n, e in gates.items() if e.get("mode") == MODE_APPLY]
    observed = [n for n, e in gates.items() if e.get("mode") != MODE_APPLY]

    for title, names in (
        ("Applied gates (these move settings)", sorted(applied)),
        ("Observed gates (recorded only)", sorted(observed)),
    ):
        out.append(title)
        if not names:
            out.append("  (none)")
        for name in names:
            entry = gates[name]
            stats = entry.get("stats") or {}
            mark = "->" if entry.get("applied") else "  "
            raw = entry.get("raw")
            out.append(
                f"  {mark} {name:<44} {entry['value']:<7} "
                f"(raw {raw if raw is not None else '-'}, "
                f"n={stats.get('n', 0)}, "
                f"median={stats.get('median', '-')}, "
                f"max={stats.get('max', '-')})"
            )
            # Both, when they differ: "observe mode" explains why nothing was
            # written, "warmup" explains why there is nothing to write yet,
            # and conflating them hides a gate that has no data at all.
            notes = [
                note
                for note in (
                    entry.get("unapplied_because"), entry.get("clamped_by"),
                )
                if note
            ]
            if notes:
                out.append(f"       {' / '.join(dict.fromkeys(notes))}")
            if "drift_from_user" in entry:
                out.append(
                    f"       you set {entry['user_value']}; "
                    f"data says {entry['value']} "
                    f"(drift {entry['drift_from_user']:+})"
                )
        out.append("")
    return "\n".join(out).rstrip()


def render_trend(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return (
            "No population history yet. The tuner writes a line per run; "
            "force one with the get/force_gate_tuning MCP tools."
        )
    out: list[str] = [f"{len(rows)} snapshot line(s)", ""]
    out.append(
        f"{'at':<20} {'total':>6} {'active':>7} {'ratio':>6} "
        f"{'gap_h':>6}  events"
    )
    for row in rows:
        at = str(row.get("at", ""))[:19]
        events = row.get("events_since_previous") or {}
        top = ", ".join(
            f"{k}={v}" for k, v in sorted(
                events.items(), key=lambda kv: -int(kv[1])
            )[:4]
        )
        out.append(
            f"{at:<20} {row.get('total', 0):>6} {row.get('active', 0):>7} "
            f"{str(row.get('constraint_ratio')):>6} "
            f"{str(row.get('hours_since_previous')):>6}  {top}"
        )

    document = load_gates()
    gates = document.get("gates") or {}
    if gates:
        out.append("")
        out.append("Gate history (oldest -> newest)")
        for name in sorted(gates):
            history = gates[name].get("history") or []
            walk = " -> ".join(str(h.get("value")) for h in history)
            out.append(f"  {name:<44} {walk}")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--trend",
        action="store_true",
        help="read the snapshot history instead of solving",
    )
    parser.add_argument(
        "--adopt",
        metavar="SETTING",
        help=(
            "hand a value set in config/user.json over to the tuner, seeded "
            "from its current value (the only mode that writes)"
        ),
    )
    parser.add_argument(
        "--pairs",
        type=int,
        default=None,
        help="cosine pairs to sample (default: the configured value)",
    )
    parser.add_argument(
        "--seed", type=int, default=None, help="fix the sampling seed",
    )
    args = parser.parse_args()

    settings = load_settings()

    if args.trend:
        print(render_trend(load_population()))
        return 0

    if args.adopt:
        setting = str(args.adopt)
        current = getattr(settings.memory, setting, None)
        if current is None:
            print(f"{setting} is not a memory setting")
            return 2
        result = adopt_gate(setting, current_value=float(current))
        print(json.dumps(result, indent=2))
        if not result.get("ok"):
            return 2
        print(
            f"\nSeeded {setting} at {current} and removed it from "
            f"config/user.json. The tuner walks it from there; nothing "
            f"changes until its next run."
        )
        return 0

    pairs = (
        int(args.pairs)
        if args.pairs is not None
        else int(
            getattr(settings.memory, "concept_gate_tuning_cosine_pairs", 4000)
        )
    )
    conn = _connect(args.db)
    try:
        data = collect(conn, settings=settings, pairs=pairs, seed=args.seed)
    finally:
        conn.close()

    print(json.dumps(data, indent=2, default=str) if args.json else render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
