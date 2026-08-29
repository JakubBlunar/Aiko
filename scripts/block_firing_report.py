#!/usr/bin/env python3
"""Read-only per-block firing report, with a reason for every zero (H53).

``turn_prompt_blocks`` can tell you a block rendered on 0 of 1,062 turns.
It cannot tell you whether that is a bug, and H53 is the entry that spent
a day finding out it usually is not: of the thirty-one blocks that had
never rendered, ten were K16's grounding line replacing them by design and
two were beats on a 30-day and a 100-day clock measured over an 18-day
table. This turns that day into a command.

Every registered block gets one of five verdicts:

``fires``         rendered at least once; the rate is quoted.
``suppressed``    ``grounding_line_mode`` replaces it. Working as designed.
``disabled``      its master switch is off.
``unobservable``  its gate is a cooldown **longer than this table is
                  old**, so no rate is quoted at all -- see below.
``silent``        never rendered, and the window was long enough to mean
                  something. These are the findings.

**Why a verdict refuses to print a number.** A monthly beat measured over
eighteen days reports ``0 / 1,062``, and density reads as authority: that
looks like a more thorough refutation than ``0 / 20`` while being exactly
as uninformative. So a block whose cadence exceeds the window comes back
with no rate rather than a zero, and says how long the window must get.
The cadences live in :data:`app.core.session.block_firing_audit.CADENCES`.

Nothing here writes: the database is opened read-only through a URI, so
it is safe against a live instance while Aiko is up.

    python scripts/block_firing_report.py
    python scripts/block_firing_report.py --findings-only
    python scripts/block_firing_report.py --json > /tmp/blocks.json
    python scripts/block_firing_report.py --window-days 30
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The classification lives in ``app`` rather than here for the reason
# ``lead_follow_report.py`` states: a second implementation behind a
# panel eventually disagrees with the one the baseline was diffed against.
from app.core.session.block_firing_audit import (  # noqa: E402
    BENIGN,
    classify_all,
    summarise,
)
from app.core.session.prompt_assembler import (  # noqa: E402
    _BLOCK_TIER_OF,
    grounding_suppressed,
)

DEFAULT_DB = REPO_ROOT / "data" / "chat_sessions.db"


def _open(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise SystemExit(f"no database at {db_path}")
    return sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)


def _window(
    con: sqlite3.Connection, window_days: int | None,
) -> tuple[str, list[Any], float]:
    """``(sql clause, params, window length in days)``.

    The window length is measured from the *rows*, not from the flag: a
    ``--window-days 90`` against a table that is eighteen days old is an
    eighteen-day window, and reporting it as ninety would defeat the
    cadence refusal this whole script exists for.
    """
    clause, params = "", []
    if window_days is not None:
        cutoff = (
            datetime.now(timezone.utc).timestamp() - window_days * 86400.0
        )
        stamp = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        clause, params = " WHERE created_at >= ?", [stamp]
    row = con.execute(
        "SELECT MIN(created_at), MAX(created_at) "
        f"FROM turn_prompt_blocks{clause}",
        tuple(params),
    ).fetchone()
    if not row or not row[0]:
        return clause, params, 0.0
    lo = datetime.fromisoformat(str(row[0]))
    hi = datetime.fromisoformat(str(row[1]))
    return clause, params, max(0.0, (hi - lo).total_seconds() / 86400.0)


def _disabled_blocks() -> set[str]:
    """Blocks whose master switch is off in the live config.

    Best-effort and deliberately narrow: only the handful of names where
    the switch is unambiguous. A wrong entry here would mark a real
    defect ``disabled`` and hide it, so an unknown block is left to be
    judged on its firing record instead.
    """
    try:
        from app.core.infra.settings import load_settings

        agent = load_settings().agent
    except Exception:
        return set()
    pairs = {
        "activity_block": "activity_awareness_enabled",
        "opinion_injection_block": "opinion_injection_enabled",
        "boundary_clash_block": "boundary_clash_enabled",
        "stance_persistence_block": "stance_persistence_enabled",
        "inside_joke_block": "inside_joke_birth_enabled",
        "vulnerability_budget_block": "vulnerability_budget_enabled",
        "milestone_block": "milestone_celebration_enabled",
        "clarification_block": "clarification_repair_enabled",
        "calibration_block": "calibration_detection_enabled",
        "self_correction_block": "self_correction_enabled",
        "dropped_topic_block": "dropped_topic_enabled",
        "user_correction_block": "user_correction_enabled",
        "fact_reversal_block": "fact_reversal_enabled",
        "second_thought_block": "second_thought_enabled",
        "user_expertise_block": "user_expertise_enabled",
        "concept_learning_block": "concept_learning_reflection_enabled",
        "conduct_notice_block": "surfacing_conduct_notice_enabled",
        "running_tasks_block": "tasks_running_block_enabled",
    }
    return {
        block
        for block, flag in pairs.items()
        if not bool(getattr(agent, flag, True))
    }


def collect(db_path: Path, window_days: int | None) -> dict[str, Any]:
    con = _open(db_path)
    try:
        clause, params, span = _window(con, window_days)
        turns = int(
            con.execute(
                "SELECT COUNT(DISTINCT assistant_message_id) "
                f"FROM turn_prompt_blocks{clause}",
                tuple(params),
            ).fetchone()[0]
            or 0
        )
        fired: dict[str, int] = {}
        sizes: dict[str, float] = {}
        for block, n, avg in con.execute(
            "SELECT block, COUNT(DISTINCT assistant_message_id), AVG(chars) "
            f"FROM turn_prompt_blocks{clause} GROUP BY block",
            tuple(params),
        ):
            fired[str(block)] = int(n or 0)
            sizes[str(block)] = float(avg or 0.0)
    finally:
        con.close()

    try:
        from app.core.infra.settings import load_settings

        mode = str(
            getattr(load_settings().agent, "grounding_line_mode", "off")
        )
    except Exception:
        mode = "off"

    verdicts = classify_all(
        tier_of=_BLOCK_TIER_OF,
        fired=fired,
        turns=turns,
        window_days=span,
        suppressed=grounding_suppressed(mode),
        disabled=_disabled_blocks(),
        avg_chars=sizes,
    )
    # A recorded name the ladder does not know is the one mismatch that
    # would make every number here suspect, so it is reported, not dropped.
    return {
        "turns": turns,
        "window_days": round(span, 1),
        "grounding_line_mode": mode,
        "registered": len(_BLOCK_TIER_OF),
        "unregistered_recorded": sorted(set(fired) - set(_BLOCK_TIER_OF)),
        "summary": summarise(verdicts),
        "blocks": [v.as_dict() for v in verdicts],
    }


def render(data: dict[str, Any], *, findings_only: bool) -> str:
    out: list[str] = []
    turns = data["turns"]
    out.append(
        f"{turns} turns over {data['window_days']} days, "
        f"{data['registered']} registered blocks, "
        f"grounding_line_mode={data['grounding_line_mode']}"
    )
    summary = data["summary"]
    out.append(
        "  " + "  ".join(f"{k}={v}" for k, v in sorted(summary.items()))
    )
    if data["unregistered_recorded"]:
        out.append(
            "  WARNING recorded but not in the ladder: "
            + ", ".join(data["unregistered_recorded"])
        )
    out.append("")

    rows = data["blocks"]
    if findings_only:
        rows = [r for r in rows if r["verdict"] not in BENIGN]
        if not rows:
            out.append("No findings: every zero has an explanation.")
            return "\n".join(out)

    by_tier: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_tier.setdefault(str(row["tier"]), []).append(row)

    for tier, group in by_tier.items():
        out.append(f"-- {tier} " + "-" * max(0, 58 - len(tier)))
        for row in group:
            verdict = str(row["verdict"])
            rate = row["rate"]
            shown = (
                f"{100.0 * float(rate):5.1f}%" if rate is not None else "    -"
            )
            line = f"  {shown}  {verdict:<13} {row['block']}"
            if row["reason"]:
                line += f"\n{' ' * 24}{row['reason']}"
            out.append(line)
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--window-days",
        type=int,
        default=None,
        help="restrict to the last N days (default: the whole table)",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--findings-only",
        action="store_true",
        help="only blocks whose zero has no explanation",
    )
    args = parser.parse_args(argv)

    data = collect(args.db, args.window_days)
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(render(data, findings_only=args.findings_only))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
