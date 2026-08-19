#!/usr/bin/env python3
"""Lift promise deadlines out of prose and into ``metadata.promise_deadline``.

H41 gave promises a machine-readable deadline, but only going forward.
The rows already in the store state theirs inside the content sentence --
``"<actor> promised: <action> (by 2026-08-19)"`` -- where the lifecycle
cannot see it, so every decision about them still falls back to creation
age.

This re-reads that suffix with the same parser the extractor now uses and
stamps the result on the row. It is a re-read rather than an inference:
the date is one the model already committed to, and each row is anchored
to its own ``created_at`` so a relative word resolves against the day it
was written rather than the day of the backfill.

Rows without a stated deadline are left alone -- most promises name no
time, and ``None`` there means "unknown", never "not yet due".

    python scripts/backfill_promise_deadlines.py --dry-run  # report only
    python scripts/backfill_promise_deadlines.py            # write

``--db`` points at an alternate database.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.infra import timephrase  # noqa: E402
from app.core.memory.promise_worker import _read_deadline  # noqa: E402

DEFAULT_DB = REPO_ROOT / "data" / "chat_sessions.db"

#: The suffix ``Promise.to_memory_content`` writes. Anchored to the end so
#: a bracketed aside mid-sentence is not mistaken for a deadline.
_SUFFIX_RE = re.compile(r"\(by ([^)]{0,60})\)\s*$", re.IGNORECASE)


def _rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT id, content, created_at, metadata FROM memories "
        "WHERE kind = 'promise' ORDER BY id"
    ).fetchall()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument(
        "--dry-run", action="store_true",
        help="report what would change without writing",
    )
    args = ap.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"no database at {path}")
        return 1

    conn = sqlite3.connect(str(path))
    rows = _rows(conn)
    tally: Counter[str] = Counter()
    writes: list[tuple[str, int]] = []
    samples: list[str] = []

    for row in rows:
        metadata = {}
        try:
            metadata = json.loads(row["metadata"] or "{}") or {}
        except ValueError:
            tally["unreadable metadata"] += 1
            continue
        if metadata.get("promise_deadline"):
            tally["already stamped"] += 1
            continue
        match = _SUFFIX_RE.search(str(row["content"] or ""))
        if match is None:
            tally["no stated deadline"] += 1
            continue
        anchor = timephrase.parse_iso(row["created_at"])
        when, _ = _read_deadline(match.group(1), anchor=anchor)
        if when is None:
            tally["deadline names no moment"] += 1
            continue
        metadata["promise_deadline"] = when.isoformat()
        writes.append((json.dumps(metadata), int(row["id"])))
        tally["stamped"] += 1
        if len(samples) < 10:
            local = when.astimezone()
            samples.append(
                f"  [{row['id']}] {match.group(1)!r} -> "
                f"{local.strftime('%a %Y-%m-%d %H:%M')}"
            )

    print(f"{len(rows)} promise rows in {path}")
    for label, count in tally.most_common():
        print(f"  {label:28} {count:4}")
    if samples:
        print("\nsample of what gets stamped:")
        print("\n".join(samples))

    if args.dry_run:
        print(f"\ndry run: {len(writes)} rows would be updated")
        return 0

    if writes:
        conn.executemany(
            "UPDATE memories SET metadata = ? WHERE id = ?", writes,
        )
        conn.commit()
    print(f"\nupdated {len(writes)} rows")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
