"""Put the accents back into proper nouns that came from config (H50).

Until H50, ``sanitize_assistant_text`` kept only ``32 <= ord(ch) <= 126``
and deleted everything else, so her stored replies lost every accent and
every symbol without an ASCII spelling. Most of that is gone for good --
nothing can know that "Kamenn" was "Kamenna" without knowing the word.

Proper nouns are the exception, because for those the correct spelling is
still sitting in ``config/user.json``: ``weather.location_name`` reads
"Kamenna Poruba, Zilina Region, Slovakia" with its accents intact, and the
transcript holds the stripped form. That makes the repair a lookup rather
than a guess.

Worth doing rather than shrugging at, because the transcript is not only
output. She reads it back as her own history, and the mangled copy is what
she learns her own spelling from -- the same mechanism that once put 230
turns of "I love you 3" in front of her until she started writing it back.
Right now the place is spelled correctly in her memories and wrongly in
every message she used it in, and both reach her prompt.

**Both stores, not just SQLite.** ``RagStore.search_messages`` returns the
Lance mirror's own ``content`` column, so repairing only the database would
leave the wrong spelling reachable through retrieval. The mirror row is
rewritten with its existing vector rather than re-embedded: one accent does
not move a sentence's meaning, and re-embedding would drag the model into a
five-row text fix.

Dry run by default. Pass ``--apply`` to write.

    python -m scripts.repair_transcript_accents
    python -m scripts.repair_transcript_accents --apply
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB_PATH = REPO / "data" / "chat_sessions.db"
CONFIG_PATH = REPO / "config" / "user.json"
LANCE_PATH = REPO / "data" / "lancedb"

#: Config keys whose values are proper nouns that reach her prose. Only
#: the location so far; anything added here needs the same property, which
#: is that the stripped form is unambiguous as a whole word.
NOUN_KEYS = (("weather", "location_name"),)


def _strip_ascii(text: str) -> str:
    """What the pre-H50 filter would have stored for this text."""
    return "".join(ch for ch in text if 32 <= ord(ch) <= 126)


def repairs_from_config(config: dict) -> list[tuple[str, str]]:
    """``(damaged, correct)`` pairs, one per accented word, longest first.

    Per *word* rather than per whole value: she writes "Kamenna Poruba",
    never the full "…, Zilina Region, Slovakia" the config holds. Longest
    first so a multi-word noun cannot be half-repaired by a shorter rule
    matching inside it.
    """
    pairs: dict[str, str] = {}
    for section, key in NOUN_KEYS:
        value = str((config.get(section) or {}).get(key) or "")
        for word in re.split(r"[\s,]+", value):
            if not word or all(ord(ch) < 128 for ch in word):
                continue
            damaged = _strip_ascii(word)
            # A word that lost *every* character cannot be matched back,
            # and one that lost nothing needs no rule.
            if not damaged or damaged == word:
                continue
            pairs[damaged] = word
    return sorted(pairs.items(), key=lambda kv: len(kv[0]), reverse=True)


def apply_repairs(text: str, pairs: list[tuple[str, str]]) -> str:
    """Rewrite whole-word occurrences of each damaged spelling.

    Word-bounded on purpose. "ilina" as a bare substring would match
    inside unrelated words; as a whole token it can only be the thing that
    lost its leading Z-caron.
    """
    out = text
    for damaged, correct in pairs:
        out = re.sub(rf"\b{re.escape(damaged)}\b", correct, out)
    return out


def repair_sqlite(pairs, *, apply: bool) -> list[int]:
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    touched: list[int] = []
    try:
        rows = con.execute("SELECT id, content FROM messages").fetchall()
        for row in rows:
            before = row["content"] or ""
            after = apply_repairs(before, pairs)
            if after == before:
                continue
            touched.append(int(row["id"]))
            if apply:
                con.execute(
                    "UPDATE messages SET content = ? WHERE id = ?",
                    (after, row["id"]),
                )
        if apply:
            con.commit()
    finally:
        con.close()
    return touched


def _table_names(db) -> set[str]:
    """Table names regardless of lancedb version.

    Newer ``list_tables()`` returns a response object with a ``.tables``
    attribute; older versions return a plain list. Same fallback as
    ``RagStore._existing_table_names``, for the same reason.
    """
    try:
        response = db.list_tables()
        tables = getattr(response, "tables", response)
        return {str(t) for t in tables}
    except (AttributeError, TypeError):
        return set(db.table_names())


def repair_lance(pairs, *, apply: bool) -> int:
    """Rewrite the mirror's ``content``, keeping each row's vector.

    Absent or unreadable is not a failure: the mirror is rebuildable, and
    a five-row text fix is not worth aborting because a vector store is
    mid-migration.
    """
    if not LANCE_PATH.exists():
        print("  lancedb: absent, nothing to mirror")
        return 0
    try:
        import lancedb
    except Exception as exc:
        print(f"  lancedb: not importable ({exc}); skipping")
        return 0
    try:
        db = lancedb.connect(str(LANCE_PATH))
        if "messages" not in _table_names(db):
            print("  lancedb: no messages table")
            return 0
        table = db.open_table("messages")
        # Arrow rather than pandas: pandas is not a dependency here, and a
        # text fix should not add one.
        rows = table.to_arrow().to_pylist()
    except Exception as exc:
        print(f"  lancedb: could not read ({exc}); skipping")
        return 0

    changed = 0
    for record in rows:
        before = str(record.get("content") or "")
        after = apply_repairs(before, pairs)
        if after == before:
            continue
        changed += 1
        if not apply:
            continue
        rid = str(record["id"]).replace("'", "''")
        record["content"] = after
        try:
            table.delete(f"id = '{rid}'")
            table.add([record])
        except Exception as exc:
            print(f"  lancedb: row {rid} failed ({exc})")
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="write the changes"
    )
    args = parser.parse_args(argv)

    if not DB_PATH.is_file():
        print(f"no database at {DB_PATH}")
        return 1
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    pairs = repairs_from_config(config)
    if not pairs:
        print("no accented proper nouns in config; nothing to repair")
        return 0

    print("repair rules, from config:")
    for damaged, correct in pairs:
        print(f"  {damaged!r} -> {correct!r}")

    mode = "applying" if args.apply else "dry run"
    print(f"\nsqlite ({mode}):")
    touched = repair_sqlite(pairs, apply=args.apply)
    print(f"  {len(touched)} message(s) affected: {touched}")

    print(f"\nlancedb mirror ({mode}):")
    mirrored = repair_lance(pairs, apply=args.apply)
    print(f"  {mirrored} mirrored row(s) affected")

    if not args.apply and (touched or mirrored):
        print("\nnothing written. re-run with --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
