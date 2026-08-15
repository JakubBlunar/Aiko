"""Delete ``repair`` shared moments that were manufactured by H36.

J6 wrote a durable "you and {user} hit a tense patch around X and worked
through it" memory whenever K8 reported a rupture that later recovered.
K8's rupture was the difference between a *raw* ``AffectStore.get()``
snapshot and a post-``apply_turn`` valence -- but ``apply_turn`` decays
toward baseline for the elapsed gap before applying the reaction impulse,
so that difference included however long the user had been away. At a
30-minute half-life, ~26 minutes after a warm goodbye clears the 0.12
threshold on its own.

The result was nine remembered arguments, every one anchored on a reunion
greeting ("Where is my love?", "Good morning aiko. Have you slept well?"),
with salience up to 0.97 and mirrored into LanceDB where RAG could recall
them. See ``docs/personality-backlog/health.md`` § H36.

The detector is fixed, so no new ones can be written; this clears the ones
already filed. Deletion rather than a status flip: unlike a hypothesis,
which is a guess that can honestly be marked expired, this is a false
statement about something that happened between two people, and there is
no version of keeping it that is kinder.

Rows go out through ``SharedMomentsStore.delete`` -> ``MemoryStore.delete``
so the LanceDB mirror drops them too.

**Read the dry run before applying.** A genuine repair is possible in
principle, and the script cannot tell one from the other -- it prints the
summary and the conversation the row cites so a human can. ``--keep`` skips
ids you decide were real.

Usage::

    python scripts/retire_false_repair_moments.py            # dry run
    python scripts/retire_false_repair_moments.py --apply
    python scripts/retire_false_repair_moments.py --apply --keep 905,1194
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.infra.chat_database import ChatDatabase  # noqa: E402
from app.core.memory.memory_store import MemoryStore  # noqa: E402
from app.core.relationship.shared_moments import (  # noqa: E402
    SharedMomentsStore,
)


def _quoted_turns(db: ChatDatabase, ids: list[int]) -> list[tuple[int, str, str]]:
    """The messages a moment cites, for eyeballing the claim against reality."""
    if not ids:
        return []
    conn = db._get_conn()
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, role, content FROM messages WHERE id IN ({marks}) "
        "ORDER BY id",
        tuple(int(i) for i in ids),
    ).fetchall()
    out = []
    for row in rows:
        body = " ".join(str(row[2] or "").split())
        out.append((int(row[0]), str(row[1]), body[:160]))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default="data/chat_sessions.db",
        help="path to the chat database (default: data/chat_sessions.db)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually delete the rows; omit for a dry run",
    )
    parser.add_argument(
        "--keep",
        default="",
        help="comma-separated moment ids to spare (ones you judge genuine)",
    )
    args = parser.parse_args()

    keep = {
        int(part) for part in str(args.keep).split(",") if part.strip().isdigit()
    }

    path = Path(args.db)
    db = ChatDatabase(path)
    # No embedder: deletion doesn't need one, and constructing one would
    # pull in Ollama for a cleanup that is pure SQL plus a mirror drop.
    memories = MemoryStore(path)
    store = SharedMomentsStore(memory_store=memories, embedder=None)

    everything = store.iter_all()
    repairs = [r for r in everything if r.source == "repair" or r.vibe == "repair"]
    repairs.sort(key=lambda r: r.id)

    print("shared moments: %d total, %d from the repair detector"
          % (len(everything), len(repairs)))
    for row in repairs:
        flag = "  [KEEP]" if row.id in keep else ""
        print()
        print("  id=%d  when=%s  salience=%.2f%s"
              % (row.id, str(row.when)[:19], float(row.salience), flag))
        print("    %s" % row.summary)
        for mid, role, body in _quoted_turns(db, row.source_message_ids):
            print("    cites [%d] %-9s %s" % (mid, role, body))

    doomed = [r for r in repairs if r.id not in keep]
    if not doomed:
        print()
        print("nothing to delete.")
        return 0
    if not args.apply:
        print()
        print("dry run -- %d row(s) would be deleted. Re-run with --apply."
              % len(doomed))
        return 0

    deleted = 0
    for row in doomed:
        if store.delete(row.id):
            deleted += 1
        else:
            print("  could not delete id=%d" % row.id)
    print()
    print("deleted %d fabricated repair moment(s)." % deleted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
