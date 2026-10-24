"""Preview/backfill the derived D8 archive without inference or embeddings.

Read-only by default, including schema access. --write explicitly installs only
the archive schema and populates up to 366 requested local days. No general app
migrations, memories, config files or source messages are changed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.conversation.archive import ConversationArchive, resolve_range  # noqa: E402
from app.core.infra.archive_schema import ARCHIVE_SCHEMA  # noqa: E402


class ArchiveConnection:
    """Minimal connection adapter; never opens the app or runs unrelated migrations."""

    def __init__(self, path: Path, *, write: bool) -> None:
        self._local = threading.local()
        self._local.conn = sqlite3.connect(
            path.resolve().as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True
        )
        self._local.conn.execute("PRAGMA busy_timeout=5000")
        if write:
            self._local.conn.executescript(ARCHIVE_SCHEMA)

    def _get_conn(self):
        return self._local.conn


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--timezone", default="system")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument(
        "--end", help="Last local date, inclusive; defaults to the narrative clock day."
    )
    parser.add_argument(
        "--write", action="store_true", help="Build missing derived daily outlines."
    )
    args = parser.parse_args()
    if not 1 <= args.days <= 366:
        parser.error("--days must be 1-366")
    last = date.fromisoformat(
        args.end
        or resolve_range(
            when="today",
            tz_name=args.timezone,
        )["local_start"][:10]
    )
    adapter = ArchiveConnection(args.db, write=args.write)
    try:
        if args.write:
            archive = ConversationArchive(adapter, args.user)
            results = []
            for age in reversed(range(args.days)):
                result = archive.build_day((last - timedelta(days=age)).isoformat(), args.timezone)
                overview = result.get("overview", {})
                results.append(
                    {
                        "local_day": overview.get("local_day", result.get("local_day")),
                        "status": result["status"],
                        "message_count": overview.get("message_count"),
                    }
                )
            print(json.dumps({"days": results, "generator": "extractive-v1"}, indent=2))
        else:
            conn = adapter._get_conn()
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='conversation_daily_overviews'",
            ).fetchone()
            if not exists:
                print(json.dumps({"status": "archive_not_initialized", "read_only": True}))
                return
            archive = ConversationArchive(adapter, args.user)
            span = resolve_range(
                start=(last - timedelta(days=args.days - 1)).isoformat(),
                end=last.isoformat(),
                tz_name=args.timezone,
            )
            print(json.dumps(archive.overviews(span), ensure_ascii=False, indent=2))
    finally:
        adapter._get_conn().close()


if __name__ == "__main__":
    main()
