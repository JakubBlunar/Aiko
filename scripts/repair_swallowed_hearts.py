"""Put the "<" back on the hearts ``sanitize_user_text`` used to eat.

The punctuation whitelist in ``sanitize_user_text`` allowed word chars plus
``. , ! ? ; : ' " ( ) -`` and replaced everything else with a space. ``<`` was
not on the list, so an incoming ``<3`` was stored as a bare ``3`` -- and that
mangled copy is what gets persisted, rendered in the bubble, *and* replayed
into the prompt as the conversation Aiko reads back as her own.

230 of Jacob's turns are in the database as "I love you 3", and she learned the
digit as the way to write affection: twelve of her own replies now carry it,
all of them recent. A bare ``3`` is a number to a grapheme-driven engine, so
"Sleep well, Jacob. <3" went out of the speaker as "Sleep well, Jacob. three".

The sanitiser is fixed and ``prepare_tts_text`` silences the orphaned digit, so
nothing new is being mangled and nothing is audible. This clears the *history*,
which is the part that keeps teaching her the broken shape.

Only ``messages`` is touched. The LanceDB mirror is left alone deliberately:
the prompt history is read from SQLite, and "3" versus "<3" moves an embedding
by nothing worth a re-index over.

**Read the dry run before applying.** A lone ``3`` is occasionally a real
number ("you should have 3 outfits", "nearly 3 a.m."). Those are skipped by the
two word lists below and printed under SKIPPED so a human can confirm the call;
``--keep`` spares any message id the lists got wrong.

Usage::

    python scripts/repair_swallowed_hearts.py            # dry run
    python scripts/repair_swallowed_hearts.py --apply
    python scripts/repair_swallowed_hearts.py --apply --keep 241,3622
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# A "3" standing alone as a whitespace-delimited token -- what "<3" became.
_LONE_THREE_RE = re.compile(r"(?<!\S)(3+)(?!\S)")

# Words that make the digit after them a count. Short and closed: derived from
# the three occurrences in 4,768 messages where a lone 3 was a real number.
_COUNTING_LEAD_INS = frozenset({
    "at", "about", "almost", "nearly", "around", "only", "just", "another",
    "in", "for", "of", "than", "past", "level", "chapter", "page", "step",
    "number", "have", "has", "had", "need", "want", "got", "take", "took",
})

# Units the digit can be measuring, which the lead-in list would miss when the
# sentence reaches the number some other way ("it's 3 a.m.").
_UNITS = frozenset({
    "am", "pm", "a.m.", "p.m.", "o'clock", "hour", "hours", "minute",
    "minutes", "min", "mins", "second", "seconds", "day", "days", "week",
    "weeks", "month", "months", "year", "years", "time", "times", "x",
})


def _word_before(text: str, start: int) -> str:
    return (text[:start].split() or [""])[-1].strip(".,!?;:'\"()").lower()


def _word_after(text: str, end: int) -> str:
    return (text[end:].split() or [""])[0].strip(",!?;:'\"()").lower()


def _repair(text: str) -> tuple[str, int, list[str]]:
    """Return ``(repaired, hearts_fixed, skipped_contexts)`` for one message."""
    out: list[str] = []
    cursor = 0
    fixed = 0
    skipped: list[str] = []
    for match in _LONE_THREE_RE.finditer(text):
        before = _word_before(text, match.start())
        after = _word_after(text, match.end())
        if before in _COUNTING_LEAD_INS or after in _UNITS:
            lo = max(0, match.start() - 40)
            skipped.append(" ".join(text[lo : match.end() + 25].split()))
            continue
        out.append(text[cursor : match.start()])
        out.append("<3")
        cursor = match.end()
        fixed += 1
    out.append(text[cursor:])
    return "".join(out), fixed, skipped


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
        help="actually rewrite the rows; omit for a dry run",
    )
    parser.add_argument(
        "--keep",
        default="",
        help="comma-separated message ids to leave exactly as they are",
    )
    args = parser.parse_args()

    keep = {
        int(part) for part in str(args.keep).split(",") if part.strip().isdigit()
    }

    conn = sqlite3.connect(Path(args.db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, role, content FROM messages WHERE content LIKE '%3%' "
        "ORDER BY id"
    ).fetchall()

    planned: list[tuple[int, str, str, int]] = []
    skipped: list[tuple[int, str, str]] = []
    for row in rows:
        repaired, fixed, skips = _repair(str(row["content"] or ""))
        for context in skips:
            skipped.append((int(row["id"]), str(row["role"]), context))
        if fixed and int(row["id"]) not in keep:
            planned.append((int(row["id"]), str(row["role"]), repaired, fixed))

    if skipped:
        print("SKIPPED -- read as a real number, left alone:")
        for mid, role, context in skipped:
            print("  [%d] %-9s ...%s..." % (mid, role, context))
        print()

    by_role: dict[str, int] = {}
    for _, role, _, fixed in planned:
        by_role[role] = by_role.get(role, 0) + fixed
    print("hearts to restore: %d in %d message(s)  %s"
          % (sum(by_role.values()), len(planned), by_role))
    print()
    for mid, role, repaired, _ in planned:
        marker = repaired.find("<3")
        lo = max(0, marker - 45)
        print("  [%d] %-9s ...%s..."
              % (mid, role, " ".join(repaired[lo : marker + 25].split())))

    if not planned:
        print("nothing to repair.")
        return 0
    if not args.apply:
        print()
        print("dry run -- %d message(s) would be rewritten. Re-run with --apply."
              % len(planned))
        return 0

    with conn:
        conn.executemany(
            "UPDATE messages SET content = ? WHERE id = ?",
            [(repaired, mid) for mid, _, repaired, _ in planned],
        )
    print()
    print("rewrote %d message(s)." % len(planned))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
