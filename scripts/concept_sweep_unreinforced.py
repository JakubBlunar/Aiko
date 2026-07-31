#!/usr/bin/env python3
"""One-off sweep: park the bootstrap-era never-reinforced concepts.

The L22 measurements found that most of the never-reinforced `active`
concepts are not an ongoing leak but a **backlog** -- they were minted into
an almost-empty graph before the reinforce path had ever fired once (zero
`reinforced` events between Jul 3 and Jul 11, against 481 concepts created).
They sit at a median confidence around 0.8, competing for surfacing slots
against beliefs that earned their place, and decay cannot clear them: each
needs roughly 86 *engaged* days to reach the dormant floor and the whole
relationship has accumulated about 13.

So they need a push. This script demotes them to `dormant` -- not `retired`
-- so a genuine reinforcement brings any of them back; nothing is deleted
and no confidence is touched. Each demotion appends a `dormant` row to the
concept timeline, so the sweep shows up in the history like any other
lifecycle transition rather than silently rewriting the past.

**Dry-run by default.** It prints exactly what it would change and exits
without opening the database for writing. Pass ``--apply`` to commit.

    python scripts/concept_sweep_unreinforced.py            # look
    python scripts/concept_sweep_unreinforced.py --apply    # do it

Scope is deliberately the *bootstrap cohort*, not "everything that has
never been reinforced": a concept promoted last week may simply not have
been re-observed yet. ``--before`` is the cutoff (default the day
reinforcement started working); widen it only with a reason.

Stop the app before running with ``--apply``. The L3 lifecycle worker is
the single writer of concept status, and this reaches around it -- which is
fine for a one-off run against a quiet database, and a race against a live
one.
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

# The day the reinforce path started firing (44 ``reinforced`` events on
# Jul 12; zero before it). A concept promoted after this had a working
# reinforcement mechanism available to it and its silence means something.
DEFAULT_CUTOFF = "2026-07-13"

_SAMPLE = 20


def _connect(path: Path, *, write: bool) -> sqlite3.Connection:
    if not path.exists():
        sys.exit(f"no database at {path}")
    uri = f"file:{path}" + ("" if write else "?mode=ro")
    conn = sqlite3.connect(uri, uri=True)
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


def _never_reinforced(row: sqlite3.Row) -> bool:
    """L22 signal C, restated over raw rows (same rule as
    ``concept_quality.unreinforced_since_promotion``) so this script needs
    no app imports."""
    promoted = _parse(row["promoted_at"])
    reinforced = _parse(row["last_reinforced_at"])
    if promoted is None:
        return reinforced is None
    if reinforced is None:
        return True
    return reinforced <= promoted


def select(conn: sqlite3.Connection, *, cutoff: datetime) -> list[sqlite3.Row]:
    """Active concepts promoted before ``cutoff`` that no evidence has ever
    landed on since. Ordered oldest-promotion first so a ``--limit`` run
    takes the most stale rows."""
    rows = list(
        conn.execute("SELECT * FROM concepts WHERE status = 'active'")
    )
    out = [
        r
        for r in rows
        if _never_reinforced(r)
        and (p := _parse(r["promoted_at"])) is not None
        and p < cutoff
    ]
    out.sort(key=lambda r: str(r["promoted_at"] or ""))
    return out


def summarise(
    targets: list[sqlite3.Row], *, total_active: int, cutoff: datetime
) -> dict[str, Any]:
    by_kind: Counter[str] = Counter(r["kind"] for r in targets)
    confidences = sorted(float(r["confidence"] or 0.0) for r in targets)
    return {
        "cutoff": cutoff.isoformat(),
        "active_total": total_active,
        "targets": len(targets),
        "targets_pct": (
            round(100.0 * len(targets) / total_active, 1)
            if total_active
            else 0.0
        ),
        "by_kind": [
            {"kind": k, "count": n} for k, n in by_kind.most_common()
        ],
        "median_confidence": (
            round(confidences[len(confidences) // 2], 3)
            if confidences
            else 0.0
        ),
        "sample": [
            {
                "id": int(r["id"]),
                "kind": r["kind"],
                "subject": r["subject"],
                "sources": int(r["distinct_source_count"] or 0),
                "confidence": round(float(r["confidence"] or 0.0), 3),
                "promoted_at": r["promoted_at"] or "",
                "label": " ".join(str(r["label"] or "").split())[:80],
            }
            for r in targets[:_SAMPLE]
        ],
    }


def apply_sweep(
    conn: sqlite3.Connection, targets: list[sqlite3.Row], *, now: datetime
) -> int:
    """Demote each target to ``dormant`` and record it on the timeline.

    One transaction: either the whole cohort moves or none of it does, so a
    failure halfway through can't leave the graph in a state nobody chose.
    """
    stamp = now.isoformat()
    reason = (
        "Swept to dormant: promoted before the reinforce path worked and "
        "never reinforced since. Revives on genuine new evidence."
    )
    with conn:
        for row in targets:
            cid = int(row["id"])
            conn.execute(
                "UPDATE concepts SET status = 'dormant' WHERE id = ?", (cid,)
            )
            conn.execute(
                "INSERT INTO concept_events ("
                "  concept_id, event_type, kind, subject, label, confidence,"
                "  novelty, evidence_count, distinct_source_count,"
                "  source_kinds, reason, created_at"
                ") VALUES (?, 'dormant', ?, ?, ?, ?, 0.0, ?, ?, '', ?, ?)",
                (
                    cid,
                    row["kind"],
                    row["subject"],
                    row["label"],
                    float(row["confidence"] or 0.0),
                    int(row["evidence_count"] or 0),
                    int(row["distinct_source_count"] or 0),
                    reason,
                    stamp,
                ),
            )
    return len(targets)


def _render(data: dict[str, Any], *, applied: bool) -> str:
    out: list[str] = []
    head = "Swept" if applied else "Would sweep"
    out.append(
        f"{head} {data['targets']} of {data['active_total']} active concepts "
        f"({data['targets_pct']}%) to dormant."
    )
    out.append(f"Cohort: promoted before {data['cutoff']}, never reinforced.")
    if not data["targets"]:
        out.append("")
        out.append("Nothing to do.")
        return "\n".join(out)
    out.append(f"Median confidence of the cohort: {data['median_confidence']}")
    out.append("")
    out.append("By kind")
    for row in data["by_kind"]:
        out.append(f"  {row['kind']:<22} {row['count']:>4}")
    out.append("")
    out.append(f"Oldest {len(data['sample'])} of them")
    for row in data["sample"]:
        out.append(
            f"  #{row['id']:<5} {row['kind']:<18} "
            f"{row['sources']}src conf {row['confidence']}  "
            f"{row['promoted_at'][:10]}  {row['label']}"
        )
    if not applied:
        out.append("")
        out.append("Dry run -- nothing was written. Re-run with --apply.")
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--before",
        default=DEFAULT_CUTOFF,
        help=(
            "ISO date; only concepts promoted before this are swept "
            f"(default {DEFAULT_CUTOFF}, the day reinforcement started)"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="sweep at most N (oldest first); 0 means no limit",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write; without it the script only reports",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cutoff = _parse(args.before)
    if cutoff is None:
        sys.exit(f"could not parse --before {args.before!r} as a date")

    conn = _connect(args.db, write=args.apply)
    try:
        total_active = int(
            conn.execute(
                "SELECT COUNT(*) FROM concepts WHERE status = 'active'"
            ).fetchone()[0]
        )
        targets = select(conn, cutoff=cutoff)
        if args.limit > 0:
            targets = targets[: args.limit]
        data = summarise(
            targets, total_active=total_active, cutoff=cutoff
        )
        if args.apply and targets:
            apply_sweep(conn, targets, now=datetime.now(timezone.utc))
    finally:
        conn.close()

    data["applied"] = bool(args.apply)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(_render(data, applied=bool(args.apply)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
