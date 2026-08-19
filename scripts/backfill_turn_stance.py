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
    BREVITY_RUN,
    BREVITY_WORD_FLOOR,
    PROTECTED_ARC_FRESH_TURNS,
    STANCE_LADDER,
    StanceInputs,
    decide,
)

DEFAULT_DB = REPO_ROOT / "data" / "chat_sessions.db"


def _load_turns(conn: sqlite3.Connection, *, brevity_run: int) -> list[dict]:
    """Every turn with recorded blocks, paired with the user turn before it.

    The pairing is by message id within a session rather than by
    timestamp: ids are monotonic per insert and timestamps are not
    guaranteed distinct, and attributing an assembly to the wrong user
    turn would corrupt exactly the ceiling this phase is measuring.

    Two of the arbiter's inputs are *sequences* rather than properties of
    the turn, and both are reconstructed here in the same order the live
    session maintains them, per session:

    ``arc_age_turns``
        How many consecutive prior turns carried the same arc. Read
        before the turn and advanced after it, matching the live
        counter's post-turn update -- off by one in either direction
        would move the whole freshness window.

    ``recent_reply_words``
        Her replies before this one, most recent first. Deliberately
        *excludes* the turn being replayed: at assembly the reply does
        not exist yet, and including it would let a long answer suppress
        its own length.
    """
    blocks_by_turn: dict[int, set[str]] = {}
    for mid, block in conn.execute(
        "SELECT assistant_message_id, block FROM turn_prompt_blocks "
        "WHERE chars > 0"
    ):
        blocks_by_turn.setdefault(int(mid), set()).add(str(block))
    if not blocks_by_turn:
        return []

    window = max(2, int(brevity_run))
    turns: list[dict] = []
    pending: dict[str, dict] = {}
    last_arc: dict[str, str | None] = {}
    arc_age: dict[str, int] = {}
    replies: dict[str, list[int]] = {}
    for mid, session, role, content, act, arc, created in conn.execute(
        "SELECT id, session_id, role, content, dialogue_act, arc, created_at "
        "FROM messages ORDER BY id"
    ):
        key = str(session)
        if role == "user":
            pending[key] = {
                "text": content or "",
                "act": act,
                "arc": arc,
            }
            continue
        if role != "assistant":
            continue
        prior = pending.get(key) or {}
        turn_arc = prior.get("arc")
        if int(mid) in blocks_by_turn:
            turns.append({
                "id": int(mid),
                "created_at": str(created),
                "blocks": frozenset(blocks_by_turn[int(mid)]),
                "text": str(prior.get("text") or ""),
                "act": prior.get("act"),
                "arc": turn_arc,
                "arc_age_turns": int(arc_age.get(key, 0)),
                "recent_reply_words": tuple(replies.get(key, ())[:window]),
            })
        # Advance the per-session sequences whether or not this turn had
        # recorded blocks. A turn with no assembly still happened, and
        # skipping it would make the arc look younger and the reply run
        # shorter than Aiko actually experienced.
        if turn_arc == last_arc.get(key):
            arc_age[key] = int(arc_age.get(key, 0)) + 1
        else:
            arc_age[key] = 0
        last_arc[key] = turn_arc
        replies[key] = [
            len(str(content or "").split()),
            *replies.get(key, []),
        ][:window]
    return turns


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument(
        "--dry-run", action="store_true",
        help="compute and report without writing any rows",
    )
    # Exposed because the intended loop is "edit a rule, re-run, re-read",
    # and these three are the rules most worth sweeping. They default to
    # the module constants, so a plain run replays what the live session
    # is doing.
    ap.add_argument(
        "--protected-arc-turns", type=int, default=PROTECTED_ARC_FRESH_TURNS,
        help="turns a support/reflection arc keeps its veto (0 disables)",
    )
    ap.add_argument(
        "--brevity-word-floor", type=int, default=BREVITY_WORD_FLOOR,
        help="words at or above which one of her replies counts as long",
    )
    ap.add_argument(
        "--brevity-run", type=int, default=BREVITY_RUN,
        help="how many long replies in a row engage the brevity brake",
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
        turns = _load_turns(conn, brevity_run=args.brevity_run)
    finally:
        conn.close()
    if not turns:
        print("no turns with recorded prompt blocks -- nothing to replay.")
        print("turn_prompt_blocks fills from K90; give the build a few days.")
        return 0

    chosen: Counter[str] = Counter()
    wanted: Counter[str] = Counter()
    clamps: Counter[tuple[str, str]] = Counter()
    brevity_hits = 0
    written = 0

    store = None
    if not args.dry_run:
        from app.core.infra.chat_database import ChatDatabase
        from app.core.memory.turn_stance_store import TurnStanceStore

        store = TurnStanceStore(ChatDatabase(path))

    for turn in turns:
        decision = decide(
            StanceInputs(
                blocks=turn["blocks"],
                user_text=turn["text"],
                dialogue_act=turn["act"],
                arc=turn["arc"],
                arc_age_turns=turn["arc_age_turns"],
                recent_reply_words=turn["recent_reply_words"],
            ),
            protected_arc_turns=args.protected_arc_turns,
            brevity_word_floor=args.brevity_word_floor,
            brevity_run=args.brevity_run,
        )
        chosen[decision.stance] += 1
        wanted[decision.desire] += 1
        if decision.brevity:
            brevity_hits += 1
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
    print()
    print(
        f"  brevity asked for: {brevity_hits} "
        f"({100.0*brevity_hits/total:.1f}% of turns)"
    )
    print(
        f"     rules: arc veto {args.protected_arc_turns} turns, "
        f"brevity {args.brevity_run} replies at {args.brevity_word_floor}+ "
        f"words"
    )
    print(
        "     note: the brevity share is retrospective. Live it should sit "
        "lower,\n"
        "     since a short reply ends the run that armed the brake."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
