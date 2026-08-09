#!/usr/bin/env python3
"""Read-only lead/follow diagnostic for the K85-K90 family.

Answers one question: *is Aiko leading the conversation or following it?*
K52 through K56 shipped five interacting mechanisms on judgement, and the
result was five features that all "work" while the behaviour they exist
to fix persisted -- discoverable only by hand-reading transcripts. This
turns that reading into numbers.

Two halves, from two sources:

**Text metrics**, computed over the whole ``messages`` log, so they are
retroactive: run this on an untouched database and you get a real
baseline going back to the first turn. Question-ending rate and reply
length are the interviewing/sprawl signals; opener echo and the
anaphoric-opener rate are the parroting signals; own-material share is
the one positive signal.

**Block firing rates**, from ``turn_prompt_blocks`` (schema v35). These
are NOT retroactive: the per-block character table lives only in the
turn's telemetry object, so nothing before the upgrade can be
reconstructed and the section stays empty until the new build has run
for a while. That is stated in the output rather than rendered as a wall
of zeroes, because a block that has never been *recorded* looks exactly
like a block that never *fires*.

Run it before and after a change and diff the two:

    python scripts/lead_follow_report.py --json > /tmp/before.json
    # ... ship something, use it for a week ...
    python scripts/lead_follow_report.py --json > /tmp/after.json

Nothing here writes: the database is opened read-only via a URI, so it
is safe to run against a live instance while Aiko is up.

``--json`` emits the same figures as one JSON object. ``--db`` points at
an alternate database. ``--windows`` overrides the cohort days.
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

# Corpus assembly lives in ``app`` rather than here so the diagnostics
# endpoint computes the same numbers from the same code -- a second
# implementation behind the panel would eventually disagree with the one
# the baseline was diffed against.
from app.core.persona.lead_follow_corpus import WINDOWS, collect  # noqa: E402

DEFAULT_DB = REPO_ROOT / "data" / "chat_sessions.db"

# Blocks whose firing rate is worth calling out by name -- the ones this
# family is supposed to move. Everything else is still in ``--json``.
_LEAD_BLOCKS = (
    "initiative_block",
    "wants_block",
    "thread_ownership_block",
    "taste_lean_block",
    "pursuit_lean_block",
    "topic_appetite_block",
    "style_pattern_block",
    "question_balance_block",
    "curiosity_seeds_block",
    "idle_seeds_block",
    "away_activities_block",
    "turning_over_block",
    "hobby_block",
)

# How many of the most common openers to show.
_TOP_OPENERS = 8


def _connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        sys.exit(f"no database at {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _window_label(days: float | None) -> str:
    return "all time" if days is None else f"last {days:g}d"


def _render(data: dict[str, Any]) -> str:
    out: list[str] = []
    out.append(f"Lead/follow report  {data['generated_at']}")
    out.append("")
    out.append(
        f"{data['total_assistant_turns']} measurable assistant turns "
        f"(own-material history window: {data['history_messages']} messages)"
    )

    out.append("")
    out.append(
        "                    turns  ends-Q  words(med)  anaph  echo   own"
    )
    for cohort in data["cohorts"]:
        out.append(
            f"  {_window_label(cohort['window_days']):<16}"
            f"{cohort['turns']:>6}  "
            f"{cohort['question_end_rate'] * 100:>5.1f}%  "
            f"{cohort['mean_words']:>5.0f}"
            f"({cohort['median_words']:>3.0f})  "
            f"{cohort['anaphoric_opener_rate'] * 100:>4.0f}%  "
            f"{cohort['mean_opener_echo'] * 100:>3.0f}%  "
            f"{cohort['mean_own_material'] * 100:>4.0f}%"
        )
    out.append("")
    out.append(
        "  ends-Q  she closed on a question -- high means interviewing"
    )
    out.append(
        "  anaph   her first sentence needed his to stand up -- the "
        "following tell"
    )
    out.append(
        "  echo    share of her opening content words taken from his"
    )
    out.append(
        "  own     share of her content words that were hers rather than "
        "recycled from"
    )
    out.append(
        "          his turn or the recent history -- the one number here "
        "you want UP."
    )
    out.append(
        "          Lexical only: elaborating on his subject scores here "
        "too, so read"
    )
    out.append(
        "          it as 'did she bring anything', not 'did she change "
        "the subject'."
    )

    newest = data["cohorts"][0] if data["cohorts"] else None
    if newest and newest["top_openers"]:
        out.append("")
        out.append(
            f"Most common openers ({_window_label(newest['window_days'])})"
        )
        for row in newest["top_openers"][:_TOP_OPENERS]:
            out.append(f"  {row['opener']:<16} x{row['count']}")

    if newest:
        blocks = newest["blocks"]
        out.append("")
        if not blocks["available"]:
            out.append(f"Lead-cue firing: unavailable -- {blocks['reason']}.")
        else:
            out.append(
                f"Lead-cue firing per hundred turns "
                f"({_window_label(newest['window_days'])}, "
                f"{blocks['turns']} recorded)"
            )
            by_name = {b["block"]: b for b in blocks["blocks"]}
            for name in _LEAD_BLOCKS:
                row = by_name.get(name)
                if row is None:
                    out.append(f"  {name:<26}      never")
                else:
                    out.append(
                        f"  {name:<26} {row['per_hundred_turns']:>9.1f}  "
                        f"({row['avg_chars']:.0f} chars)"
                    )

    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--json", action="store_true", help="emit raw JSON instead of a table"
    )
    parser.add_argument(
        "--windows",
        type=str,
        default="",
        help=(
            "comma-separated cohort windows in days; 'all' for the whole "
            "log (default: 7,30,all)"
        ),
    )
    args = parser.parse_args()

    windows: tuple[float | None, ...] = WINDOWS
    if args.windows.strip():
        parsed: list[float | None] = []
        for chunk in args.windows.split(","):
            token = chunk.strip().lower()
            if not token:
                continue
            parsed.append(None if token in {"all", "none"} else float(token))
        if parsed:
            windows = tuple(parsed)

    conn = _connect(args.db)
    try:
        data = collect(
            conn, now=datetime.now(timezone.utc), windows=windows,
        )
    finally:
        conn.close()

    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(_render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
