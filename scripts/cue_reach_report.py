#!/usr/bin/env python3
"""Read-only cue reach diagnostic -- the offline twin of ``get_cue_outcomes``.

Answers one question: *which inner-life cues are actually starving?*

The trap this exists to close
-----------------------------
The obvious query is wrong, and it is wrong in the direction that invents
work. Group ``cue_decisions`` by ``reason`` over a week and four cues look
dead::

    self_callback         452 declined,  2 surfaced   (cadence_block 426)
    shared_ritual         450 declined,  4 surfaced   (cadence_block 426)
    dormant_interest      336 declined,  4 surfaced   (no_opening 225)
    caught_mid_activity   265 declined,  2 surfaced   (no_stock 246)

``cadence_block``, ``no_opening``, ``no_stock`` and ``question_balance``
are in :data:`~app.core.proactive.cue_accounting.INELIGIBLE_REASONS`:
they mean the cue never had a chance that turn, so they are not chances
it passed up. Drop them from the denominator and three of those four
turn out to take **every** live chance they get -- 4 of 4, 3 of 3 -- and
the fourth takes 2 of 12. They are scarce, not broken.

What the raw table hides is the actual finding: with every cue on the
right denominator, **one gate, ``topic_miss``, accounted for 96.8% of
every eligible decline** across five topic-gated cues that each looked
like a private problem. Hence the by-gate aggregate at the bottom of the
report.

It also hides the one cue that *is* worth attention, by burying it among
the false positives: ``self_callback`` has an **empty** denominator -- 0
surfaced and 0 eligible declines against 401 structural ones. Undefined
reach is not low reach, which is why it gets its own section rather than
a 0.0% row it would be wrong to rank.

One window caveat, since it changes the headline. ``provider`` is the
pre-instrumentation catch-all and it stops at zero on 13 Aug 2026; a
window reaching back past that date fills with rows whose reason was
never recorded (58% of eligible declines at 30 days, against 0% at 6).
If ``provider`` dominates the by-gate table, shorten the window before
concluding anything from it.

H30 established this and health.md warns about it in prose, and it was
still got wrong afterwards, because the correct denominator lived only in
code and in an MCP tool that needs a running app. Offline forensics --
which is exactly when you ask this question -- had no route but a
hand-written query. Hence a script, importing the production predicate so
it cannot drift from what the app counts.

Reading the output
------------------
* **reach** = ``surfaced / (surfaced + eligible declines)``. This is the
  number. A low reach means the cue had live chances and did not take
  them.
* **inelig** is not a failure count. A large number here with a high
  reach is a deliberately scarce cue working correctly, and the two
  columns should be read together or not at all.
* **never eligible** (an empty denominator) is the one genuinely
  ambiguous row: it means every single decline was structural, so the cue
  has no measured reach at all. Look at its supply, not its gates.
* The **dominant eligible reason** is where the work is. ``topic_miss``
  concentrated across several cues is one gate, not several bugs.

Nothing here writes: the database is opened read-only via a URI, so it is
safe to run against a live instance while Aiko is up.

    python scripts/cue_reach_report.py
    python scripts/cue_reach_report.py --days 30 --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Imported rather than restated. A local copy of the reason set is how the
# script and the app would come to disagree about which declines counted,
# and the disagreement would be invisible -- both numbers look plausible.
from app.core.proactive.cue_accounting import (  # noqa: E402
    INELIGIBLE_REASONS,
    is_eligible_decline,
)

DEFAULT_DB = REPO_ROOT / "data" / "chat_sessions.db"
DEFAULT_DAYS = 7


def _connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        sys.exit(f"no database at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def collect(conn: sqlite3.Connection, *, days: float | None) -> dict[str, Any]:
    """Per-cue reach over the window, plus the reason breakdown."""
    sql = "SELECT cue, outcome, reason FROM cue_decisions"
    params: tuple[Any, ...] = ()
    if days is not None:
        # UTC, not local. ``created_at`` is stored as an ISO string ending
        # ``+00:00`` and SQLite compares it as text, so a bound carrying a
        # different offset is off by that offset no matter that it names
        # the same instant -- a local ``+02:00`` bound silently moves the
        # window two hours and changes the counts.
        since = datetime.now(timezone.utc) - timedelta(days=float(days))
        sql += " WHERE created_at >= ?"
        params = (since.isoformat(),)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        return {"error": str(exc), "cues": []}

    surfaced: Counter[str] = Counter()
    eligible: Counter[str] = Counter()
    ineligible: Counter[str] = Counter()
    # Reason tallies are kept split by eligibility, because the same table
    # mixing both is the thing that misleads.
    elig_reasons: dict[str, Counter[str]] = defaultdict(Counter)
    inelig_reasons: dict[str, Counter[str]] = defaultdict(Counter)

    for row in rows:
        cue = str(row["cue"] or "?")
        if str(row["outcome"]) == "surfaced":
            surfaced[cue] += 1
            continue
        reason = str(row["reason"] or "")
        # ``lost_priority:<winner>`` is collapsed to its head so the mutex
        # reads as one cause rather than one row per rival.
        head = reason.split(":", 1)[0] or "(blank)"
        if is_eligible_decline(reason):
            eligible[cue] += 1
            elig_reasons[cue][head] += 1
        else:
            ineligible[cue] += 1
            inelig_reasons[cue][head] += 1

    cues: list[dict[str, Any]] = []
    for cue in sorted(set(surfaced) | set(eligible) | set(ineligible)):
        got = surfaced[cue]
        missed = eligible[cue]
        denom = got + missed
        top = sorted(elig_reasons[cue].items(), key=lambda kv: -kv[1])
        cues.append({
            "cue": cue,
            "surfaced": got,
            "eligible_declines": missed,
            "eligible": denom,
            "ineligible_declines": ineligible[cue],
            "reach_pct": (100.0 * got / denom) if denom else None,
            "dominant_eligible_reason": top[0][0] if top else None,
            "dominant_eligible_count": top[0][1] if top else 0,
            "eligible_reasons": dict(top),
            "ineligible_reasons": dict(
                sorted(inelig_reasons[cue].items(), key=lambda kv: -kv[1])
            ),
        })

    # One gate showing up as the top eligible reason for several cues is a
    # different (and cheaper) finding than several starving cues, so it is
    # aggregated rather than left to be spotted by eye.
    by_gate: Counter[str] = Counter()
    for entry in cues:
        for reason, n in entry["eligible_reasons"].items():
            by_gate[reason] += n

    return {
        "window_days": days,
        "decisions": len(rows),
        "ineligible_reasons_by_design": sorted(INELIGIBLE_REASONS),
        "cues": cues,
        "eligible_declines_by_gate": [
            {"reason": r, "declines": n}
            for r, n in sorted(by_gate.items(), key=lambda kv: -kv[1])
        ],
    }


def _render(data: dict[str, Any]) -> str:
    if data.get("error"):
        return f"cue_decisions unreadable: {data['error']}"
    out: list[str] = []
    window = data["window_days"]
    out.append(
        f"cue reach over {'all time' if window is None else f'{window:g} days'}"
        f"  ({data['decisions']} decisions)"
    )
    out.append(
        "excluded from the denominator by design: "
        + ", ".join(data["ineligible_reasons_by_design"])
    )
    out.append("")
    out.append(
        f"{'cue':24}{'surf':>6}{'missed':>7}{'reach':>8}{'inelig':>8}"
        "  dominant eligible reason"
    )
    out.append("-" * 88)
    # Worst reach first: that ordering is the point of the report.
    ranked = sorted(
        data["cues"],
        key=lambda e: (
            e["reach_pct"] if e["reach_pct"] is not None else 1e9,
            -e["eligible"],
        ),
    )
    for e in ranked:
        reach = (
            "  n/a  " if e["reach_pct"] is None else f"{e['reach_pct']:6.1f}%"
        )
        gate = (
            f"{e['dominant_eligible_reason']}={e['dominant_eligible_count']}"
            if e["dominant_eligible_reason"] else "-"
        )
        out.append(
            f"{e['cue']:24}{e['surfaced']:6}{e['eligible_declines']:7}"
            f"{reach:>8}{e['ineligible_declines']:8}  {gate}"
        )

    blind = [e for e in ranked if e["reach_pct"] is None]
    if blind:
        out.append("")
        out.append(
            "never eligible -- no measured reach at all. Every decline was "
            "structural, so look at supply rather than at gates:"
        )
        for e in blind:
            top = sorted(
                e["ineligible_reasons"].items(), key=lambda kv: -kv[1]
            )
            why = f"{top[0][0]}={top[0][1]}" if top else "-"
            out.append(
                f"  {e['cue']:24} {e['surfaced']} surfaced, "
                f"{e['ineligible_declines']} structural declines ({why})"
            )

    starving = [
        e for e in ranked
        if e["reach_pct"] is not None and e["eligible"] >= 20
        and e["reach_pct"] < 25.0
    ]
    if starving:
        out.append("")
        out.append(
            "genuinely starving -- live chances not taken (>=20 eligible, "
            "reach under 25%):"
        )
        for e in starving:
            out.append(
                f"  {e['cue']:24} {e['surfaced']} of {e['eligible']} "
                f"({e['reach_pct']:.1f}%)  "
                f"{e['dominant_eligible_reason']}="
                f"{e['dominant_eligible_count']}"
            )

    gates = data["eligible_declines_by_gate"]
    if gates:
        out.append("")
        out.append(
            "eligible declines by gate, across every cue. A single gate "
            "dominating here is one problem, not many:"
        )
        total = sum(g["declines"] for g in gates) or 1
        for g in gates:
            out.append(
                f"  {g['reason']:20}{g['declines']:7}"
                f"  {100.0 * g['declines'] / total:5.1f}% of all "
                "eligible declines"
            )
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--days",
        type=float,
        default=float(DEFAULT_DAYS),
        help=(
            f"window in days (default {DEFAULT_DAYS}); 0 for the whole "
            "ledger"
        ),
    )
    parser.add_argument(
        "--json", action="store_true", help="emit raw JSON instead of a table"
    )
    args = parser.parse_args()

    conn = _connect(args.db)
    try:
        data = collect(conn, days=(None if args.days <= 0 else args.days))
    finally:
        conn.close()

    print(json.dumps(data, indent=2) if args.json else _render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
