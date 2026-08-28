#!/usr/bin/env python3
"""Read-only reliability report for every learned rate (H18).

H18 found L38's earned standing ranking 466 concepts off a number that
carried no information, and left behind a rule: before a signal is
allowed to rank anything, measure its split-half reliability against the
null of shuffling it. A rule written as a sentence does not get re-run,
and the two months the cluster engaged rate spent as "not yet a finding"
is what that costs. This is the sentence as a command.

Each signal gets one of four verdicts:

``signal``        reliable enough to rank on without further argument.
``weak``          carries information, but a much blunter instrument
                  than its readers probably assume.
``noise``         a constant with extra steps. Whatever reads this is
                  ordering items at random.
``underpowered``  too thin to answer. **Not** evidence of absence, which
                  is the distinction that kept the cluster case open.

Three checks, because one correlation is not enough
--------------------------------------------------
A re-run of H18's own test promotes the cluster engaged rate to a usable
signal (0.233 at an evidence floor of 8, over its own 0.2 line). It is
not one, and what shows that is not a bigger correlation: it is a
shape-matched null (the same split over a structureless series with
identical row counts) and an excess-spread ratio (observed between-item
spread over the spread one shared rate would produce). Both must pass.
The reasoning is in :mod:`app.core.memory.signal_reliability`.

Nothing here writes: the database is opened read-only through a URI, so
it is safe against a live instance while Aiko is up.

    python scripts/signal_reliability_report.py
    python scripts/signal_reliability_report.py --findings-only
    python scripts/signal_reliability_report.py --buckets
    python scripts/signal_reliability_report.py --json > /tmp/rates.json
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The statistics live in ``app`` rather than here for the reason
# ``block_firing_report.py`` states: a second implementation behind a
# panel eventually disagrees with the one the decision was made against.
from app.core.memory.signal_reliability import (  # noqa: E402
    VERDICT_NOISE,
    VERDICT_UNDERPOWERED,
    SignalVerdict,
    classify,
)

DEFAULT_DB = REPO_ROOT / "data" / "chat_sessions.db"

# ``(item_kind, measure)`` pairs and who reads each one. The consumer
# column is the point: a noise verdict is only interesting because
# something is ranking on it.
SIGNALS: tuple[tuple[str, str, str], ...] = (
    ("concept", "engaged", "L38 earned standing (until H18)"),
    ("concept", "echoed", "L38 earned standing (since H18)"),
    ("memory", "engaged", "L38 earned standing (until H18)"),
    ("memory", "echoed", "L38 earned standing (since H18)"),
    ("cluster", "engaged", "K81 taste affinity, L42 neglect"),
    ("cluster", "echoed", "nothing - clusters get no echo test"),
    ("cue", "engaged", "nothing - stats_for skips item_id = 0"),
)


def _open(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SystemExit(f"no database at {db_path}")
    return sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)


def _series(
    conn: sqlite3.Connection, item_kind: str, measure: str
) -> tuple[dict[str, list[int]], dict[int, list[str]], dict[int, bool]]:
    """Per-item observations, plus the per-turn view the null needs.

    Cues are keyed on ``item_key`` rather than ``item_id`` because they
    carry ``item_id = 0`` by design (G4: there is no integer cue
    registry), so keying them the usual way collapses every cue in the
    system into a single row.
    """
    def engaged_hit(value: Any) -> bool:
        return str(value) == "engaged"

    def echo_hit(value: Any) -> bool:
        return bool(int(value or 0))

    key = "COALESCE(item_key, '')" if item_kind == "cue" else "item_id"
    if measure == "engaged":
        rows = conn.execute(
            f"SELECT {key}, assistant_message_id, engagement_label "
            "FROM surfacing_outcomes WHERE item_kind = ? "
            "AND engagement_label IS NOT NULL AND engagement_label != ''",
            (item_kind,),
        ).fetchall()
        hit_of = engaged_hit
    else:
        # Restricted to rows an echo test actually ran on: a row with no
        # verdict is no evidence, not evidence of nothing. H18 fixed the
        # same denominator bug in ``echo_rate``.
        rows = conn.execute(
            f"SELECT {key}, assistant_message_id, echoed "
            "FROM surfacing_outcomes WHERE item_kind = ? "
            "AND echo_kind IS NOT NULL AND echo_kind != ''",
            (item_kind,),
        ).fetchall()
        hit_of = echo_hit

    series: dict[str, list[int]] = {}
    per_turn: dict[int, list[str]] = {}
    labels: dict[int, bool] = {}
    for item, turn, value in rows:
        name = str(item)
        if not name:
            continue
        hit = 1 if hit_of(value) else 0
        series.setdefault(name, []).append(hit)
        per_turn.setdefault(int(turn), []).append(name)
        labels[int(turn)] = bool(hit) or labels.get(int(turn), False)
    return series, per_turn, labels


def _row(verdict: SignalVerdict, consumer: str) -> dict[str, Any]:
    peak = verdict.peak
    return {
        "signal": verdict.name,
        "consumer": consumer,
        "verdict": verdict.verdict,
        "items": peak.items if peak else 0,
        "r": (peak.r if peak else None),
        "null_r": verdict.null_r,
        "excess": verdict.excess,
        "p_value": verdict.p_value,
        "detail": verdict.detail,
        "sweep": [
            {"floor": s.floor, "items": s.items, "r": s.r, "sd": s.sd}
            for s in verdict.sweep
        ],
        "buckets": [
            {
                "label": b.label,
                "items": b.items,
                "mean_rows": round(b.mean_rows, 1),
                "mean_rate": round(b.mean_rate, 3),
                "excess": (round(b.excess, 2) if b.excess else None),
            }
            for b in verdict.buckets
        ],
    }


def _fmt(value: float | None, places: int = 3) -> str:
    return "-" if value is None else f"{value:.{places}f}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--findings-only",
        action="store_true",
        help="only signals a reader should stop trusting",
    )
    parser.add_argument(
        "--buckets",
        action="store_true",
        help="print the row-count buckets behind each verdict",
    )
    parser.add_argument("--floor", type=int, default=8)
    parser.add_argument("--trials", type=int, default=40)
    parser.add_argument("--seed", type=int, default=20260828)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    conn = _open(args.db)
    rows: list[dict[str, Any]] = []
    verdicts: list[tuple[SignalVerdict, str]] = []
    for item_kind, measure, consumer in SIGNALS:
        series, per_turn, labels = _series(conn, item_kind, measure)
        verdict = classify(
            f"{item_kind}/{measure}",
            series,
            per_turn_items=(per_turn if measure == "engaged" else None),
            turn_labels=(labels if measure == "engaged" else None),
            floor=args.floor,
            trials=args.trials,
            rng=random.Random(args.seed),
        )
        verdicts.append((verdict, consumer))
        rows.append(_row(verdict, consumer))

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    shown = [
        (v, c) for v, c in verdicts
        if not args.findings_only
        or v.verdict in (VERDICT_NOISE, VERDICT_UNDERPOWERED)
    ]
    print(f"reliability of learned rates  (db: {args.db})")
    print()
    print(
        f"{'signal':<18} {'verdict':<14} {'items':>6} {'r':>7} "
        f"{'null':>7} {'excess':>7} {'p':>7}  consumer"
    )
    print("-" * 108)
    for verdict, consumer in shown:
        peak = verdict.peak
        print(
            f"{verdict.name:<18} {verdict.verdict:<14} "
            f"{(peak.items if peak else 0):>6} "
            f"{_fmt(peak.r if peak else None):>7} "
            f"{_fmt(verdict.null_r):>7} "
            f"{_fmt(verdict.excess, 2):>7} "
            f"{_fmt(verdict.p_value, 3):>7}  {consumer}"
        )

    print()
    print("why each verdict landed")
    for verdict, _consumer in shown:
        print(f"  {verdict.name}: {verdict.detail}")

    if args.buckets:
        for verdict, _consumer in shown:
            if not verdict.buckets:
                continue
            print()
            print(f"{verdict.name} by row count")
            print(
                f"  {'rows':<10} {'items':>6} {'mean rows':>10} "
                f"{'mean rate':>10} {'excess':>7}"
            )
            for bucket in verdict.buckets:
                print(
                    f"  {bucket.label:<10} {bucket.items:>6} "
                    f"{bucket.mean_rows:>10.1f} {bucket.mean_rate:>10.3f} "
                    f"{_fmt(bucket.excess, 2):>7}"
                )

    print()
    print(
        "'excess' is between-item spread over the spread one shared rate "
        "would produce.\nAt 1.0 the items are indistinguishable and "
        "nothing can be ranked on them."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
