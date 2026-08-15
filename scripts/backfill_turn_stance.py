#!/usr/bin/env python3
"""Replay the K92 stance arbiter over recorded turns.

The arbiter is a pure function over data that is already durable --
``turn_prompt_blocks`` for what the providers offered, ``messages`` for
what the user's turn was doing -- so unlike K90 it does not have to
accumulate forward before it can be read. This script recomputes it for
every turn with recorded blocks and writes the result to ``turn_stance``.

That matters more than saving a wait. Phase 1 exists to answer *is the
eight-stance set the right set*, and a set you can only evaluate two
weeks after each edit is a set nobody edits. Rows are keyed one per turn
and written with ``INSERT OR REPLACE``, so the intended workflow is:
change a rule in ``app/core/conversation/stance.py``, re-run this, read
the distribution again.

Each row is stamped with the timestamp of the turn it describes rather
than the moment of the backfill, so windowed reads keep working.

    python scripts/backfill_turn_stance.py            # write, then report
    python scripts/backfill_turn_stance.py --dry-run  # report only

``--db`` points at an alternate database.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.conversation.stance import (  # noqa: E402
    STANCE_LADDER,
    StanceInputs,
    decide,
)

DEFAULT_DB = REPO_ROOT / "data" / "chat_sessions.db"


def _load_turns(conn: sqlite3.Connection) -> list[dict]:
    """Every turn with recorded blocks, paired with the user turn before it.

    The pairing is by message id within a session rather than by
    timestamp: ids are monotonic per insert and timestamps are not
    guaranteed distinct, and attributing an assembly to the wrong user
    turn would corrupt exactly the ceiling this phase is measuring.
    """
    blocks_by_turn: dict[int, set[str]] = {}
    for mid, block in conn.execute(
        "SELECT assistant_message_id, block FROM turn_prompt_blocks "
        "WHERE chars > 0"
    ):
        blocks_by_turn.setdefault(int(mid), set()).add(str(block))
    if not blocks_by_turn:
        return []

    turns: list[dict] = []
    pending: dict[str, dict] = {}
    for mid, session, role, content, act, arc, created in conn.execute(
        "SELECT id, session_id, role, content, dialogue_act, arc, created_at "
        "FROM messages ORDER BY id"
    ):
        if role == "user":
            pending[str(session)] = {
                "text": content or "",
                "act": act,
                "arc": arc,
            }
            continue
        if role != "assistant" or int(mid) not in blocks_by_turn:
            continue
        prior = pending.get(str(session)) or {}
        turns.append({
            "id": int(mid),
            "created_at": str(created),
            "blocks": frozenset(blocks_by_turn[int(mid)]),
            "text": str(prior.get("text") or ""),
            "act": prior.get("act"),
            "arc": prior.get("arc"),
        })
    return turns


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument(
        "--dry-run", action="store_true",
        help="compute and report without writing any rows",
    )
    args = ap.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"no database at {path}")
        return 1

    # Always read-only: writes go through ChatDatabase below, which
    # owns the schema and the migration this table arrived in.
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        turns = _load_turns(conn)
    finally:
        conn.close()
    if not turns:
        print("no turns with recorded prompt blocks -- nothing to replay.")
        print("turn_prompt_blocks fills from K90; give the build a few days.")
        return 0

    chosen: Counter[str] = Counter()
    wanted: Counter[str] = Counter()
    clamps: Counter[tuple[str, str]] = Counter()
    written = 0

    store = None
    if not args.dry_run:
        from app.core.infra.chat_database import ChatDatabase
        from app.core.memory.turn_stance_store import TurnStanceStore

        store = TurnStanceStore(ChatDatabase(path))

    for turn in turns:
        decision = decide(StanceInputs(
            blocks=turn["blocks"],
            user_text=turn["text"],
            dialogue_act=turn["act"],
            arc=turn["arc"],
        ))
        chosen[decision.stance] += 1
        wanted[decision.desire] += 1
        if decision.clamped:
            clamps[(decision.reason, decision.desire)] += 1
        if store is not None and store.add_turn(
            turn["id"], decision, created_at=turn["created_at"],
        ):
            written += 1

    total = len(turns)
    print(f"K92 stance replay over {total} recorded turns")
    if args.dry_run:
        print("(dry run -- nothing written)")
    else:
        print(f"wrote {written} rows to turn_stance")
    print()
    print(f"  {'stance':17} {'chosen':>14}  {'wanted':>14}")
    for stance in STANCE_LADDER:
        c, w = chosen.get(stance, 0), wanted.get(stance, 0)
        print(
            f"  {stance:17} {c:6} ({100.0*c/total:4.1f}%) "
            f" {w:6} ({100.0*w/total:4.1f}%)"
        )

    held = sum(clamps.values())
    print()
    print(
        f"  held back by the user's turn: {held} "
        f"({100.0*held/total:.1f}% of turns)"
    )
    for (reason, desire), n in clamps.most_common():
        print(f"     {reason:20} blocked {desire:16} x{n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
