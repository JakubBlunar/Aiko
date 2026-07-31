#!/usr/bin/env python3
"""Read-only intake diagnostic for the L22 concept quality work.

Answers one question: *is the promotion gate letting through concepts that
never earn their place?* The L22 scoreboard reports the standing **stock**
of never-reinforced actives, which is slow by construction -- on the current
decay settings a stalled concept needs tens of hours of conversation to
reach the dormant floor, so the stock barely responds to a threshold change
inside a week. This script reports the **flow** instead: how fast concepts
are being promoted, how many of each recent cohort has already gone quiet,
and (for the kind that dominates the graph) what evidence they promoted on.

Run it before and after a gate change and diff the two:

    python scripts/concept_intake_report.py > /tmp/before.txt
    # ... a week of use later ...
    python scripts/concept_intake_report.py > /tmp/after.txt

Nothing here writes: the database is opened read-only via a URI, so it is
safe to run against a live instance while Aiko is up.

``--json`` emits the same figures as a single JSON object, for diffing with
``jq`` or feeding a notebook. ``--db`` points at an alternate database.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "data" / "chat_sessions.db"

# Cohort windows, in calendar days back from now. The narrow ones show the
# effect of a recent change; the wide ones give the pre-change baseline in
# the same run, so a single invocation is already a before/after.
_COHORTS = (3.0, 7.0, 14.0, 30.0)

# Sample size for the inspectable list of recent never-reinforced actives.
_SAMPLE = 15


def _connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        sys.exit(f"no database at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _stalled(row: sqlite3.Row) -> bool:
    """Never reinforced since promotion -- the same rule as L22 signal C
    (``concept_quality.unreinforced_since_promotion``), restated over raw
    rows so this script needs no app imports."""
    if row["status"] != "active":
        return False
    promoted = _parse(row["promoted_at"])
    reinforced = _parse(row["last_reinforced_at"])
    if promoted is None:
        return reinforced is None
    if reinforced is None:
        return True
    return reinforced <= promoted


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def collect(conn: sqlite3.Connection, *, now: datetime) -> dict[str, Any]:
    rows = list(conn.execute("SELECT * FROM concepts"))
    actives = [r for r in rows if r["status"] == "active"]
    stalled = [r for r in actives if _stalled(r)]

    by_kind: Counter[str] = Counter(r["kind"] for r in actives)
    stalled_by_kind: Counter[str] = Counter(r["kind"] for r in stalled)

    # Per-kind intake quality. Sorted by active count so the kind doing the
    # most damage is first, which is how the identity problem surfaced.
    kinds = []
    for kind, active_n in by_kind.most_common():
        stalled_n = stalled_by_kind.get(kind, 0)
        members = [r for r in actives if r["kind"] == kind]
        sources = [int(r["distinct_source_count"] or 0) for r in members]
        kinds.append({
            "kind": kind,
            "active": active_n,
            "stalled": stalled_n,
            "stalled_pct": _pct(stalled_n, active_n),
            "mean_sources": (
                round(sum(sources) / len(sources), 2) if sources else 0.0
            ),
        })

    # Promotion cohorts: of what was promoted inside each window, how much
    # has already gone quiet. Reads high for the narrow windows by
    # construction (a concept promoted yesterday has barely had a chance to
    # be reinforced), so compare a window against *itself* over time.
    cohorts = []
    for days in _COHORTS:
        cutoff = now.timestamp() - days * 86400.0
        promoted = [
            r
            for r in rows
            if (d := _parse(r["promoted_at"])) is not None
            and d.timestamp() >= cutoff
        ]
        quiet = [r for r in promoted if _stalled(r)]
        cohorts.append({
            "window_days": days,
            "promoted": len(promoted),
            "per_day": round(len(promoted) / days, 2),
            "stalled": len(quiet),
            "stalled_pct": _pct(len(quiet), len(promoted)),
        })

    # Source histogram + promotion latency for the never-reinforced rows of
    # the largest kind. The histogram is what justified a source floor: it
    # says exactly how many rows a given bar would have refused.
    top_kind = by_kind.most_common(1)[0][0] if by_kind else None
    histogram: dict[str, int] = {}
    latency: dict[str, Any] = {}
    if top_kind is not None:
        cohort = [r for r in stalled if r["kind"] == top_kind]
        counts = Counter(int(r["distinct_source_count"] or 0) for r in cohort)
        histogram = {str(k): counts[k] for k in sorted(counts)}
        gaps = []
        for r in cohort:
            first, promoted = _parse(r["first_evidence_at"]), _parse(
                r["promoted_at"]
            )
            if first and promoted and promoted >= first:
                gaps.append((promoted - first).total_seconds() / 3600.0)
        if gaps:
            gaps.sort()
            latency = {
                "n": len(gaps),
                "min_hours": round(gaps[0], 2),
                "median_hours": round(gaps[len(gaps) // 2], 2),
                "mean_hours": round(sum(gaps) / len(gaps), 2),
                "max_hours": round(gaps[-1], 2),
                # The headline: promotion within an hour of first evidence
                # means no stability delay was applied at all.
                "within_1h": sum(1 for g in gaps if g <= 1.0),
                "within_1h_pct": _pct(
                    sum(1 for g in gaps if g <= 1.0), len(gaps)
                ),
            }

    newest = sorted(
        stalled, key=lambda r: str(r["promoted_at"] or ""), reverse=True
    )[:_SAMPLE]

    return {
        "generated_at": now.isoformat(),
        "totals": {
            "concepts": len(rows),
            "active": len(actives),
            "candidates": sum(1 for r in rows if r["status"] == "candidate"),
            "stalled": len(stalled),
            "stalled_pct": _pct(len(stalled), len(actives)),
        },
        "by_kind": kinds,
        "cohorts": cohorts,
        "largest_kind": top_kind,
        "largest_kind_stalled_source_histogram": histogram,
        "largest_kind_stalled_promotion_latency": latency,
        "newest_stalled_sample": [
            {
                "id": int(r["id"]),
                "kind": r["kind"],
                "sources": int(r["distinct_source_count"] or 0),
                "confidence": round(float(r["confidence"] or 0.0), 3),
                "promoted_at": r["promoted_at"] or "",
                "label": " ".join(str(r["label"] or "").split())[:90],
            }
            for r in newest
        ],
    }


def _render(data: dict[str, Any]) -> str:
    out: list[str] = []
    t = data["totals"]
    out.append(f"Concept intake report  {data['generated_at']}")
    out.append("")
    out.append(
        f"{t['concepts']} concepts: {t['active']} active, "
        f"{t['candidates']} candidate. "
        f"{t['stalled']} actives ({t['stalled_pct']}%) never reinforced "
        f"since promotion."
    )

    out.append("")
    out.append("Per kind (active / never-reinforced / mean distinct sources)")
    for row in data["by_kind"]:
        out.append(
            f"  {row['kind']:<22} {row['active']:>4} active  "
            f"{row['stalled']:>4} stalled ({row['stalled_pct']:>5}%)  "
            f"sources {row['mean_sources']}"
        )

    out.append("")
    out.append("Promotion cohorts (intake flow -- the figure that moves)")
    for row in data["cohorts"]:
        out.append(
            f"  last {row['window_days']:>5}d  {row['promoted']:>4} promoted "
            f"({row['per_day']}/day)  {row['stalled']:>4} already stalled "
            f"({row['stalled_pct']}%)"
        )

    kind = data["largest_kind"]
    if kind:
        out.append("")
        out.append(f"Never-reinforced '{kind}' rows, by distinct sources")
        hist = data["largest_kind_stalled_source_histogram"]
        total = sum(hist.values())
        cumulative = 0
        for sources, count in hist.items():
            cumulative += count
            out.append(
                f"  {sources} sources  {count:>4}   "
                f"(a bar above this would have refused "
                f"{cumulative}/{total} = {_pct(cumulative, total)}%)"
            )
        latency = data["largest_kind_stalled_promotion_latency"]
        if latency:
            out.append("")
            out.append(
                f"  first evidence -> promotion: median "
                f"{latency['median_hours']}h, mean {latency['mean_hours']}h, "
                f"max {latency['max_hours']}h"
            )
            out.append(
                f"  promoted within 1h: {latency['within_1h']}/"
                f"{latency['n']} ({latency['within_1h_pct']}%) "
                f"-- no stability delay applied"
            )

    sample = data["newest_stalled_sample"]
    if sample:
        out.append("")
        out.append("Most recently promoted, already never-reinforced")
        for row in sample:
            out.append(
                f"  #{row['id']:<5} {row['kind']:<18} "
                f"{row['sources']}src conf {row['confidence']}  "
                f"{row['label']}"
            )

    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--json", action="store_true", help="emit raw JSON instead of a table"
    )
    args = parser.parse_args()

    conn = _connect(args.db)
    try:
        data = collect(conn, now=datetime.now(timezone.utc))
    finally:
        conn.close()

    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(_render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
