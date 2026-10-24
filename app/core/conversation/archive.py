"""Bounded, user-scoped transcript access and extractive daily archive index.

No embeddings, inference, memory writes or prompt blocks. Reads run in a SQLite
snapshot; signed cursors bind filters, owner, source revision and insertion
watermark. Edits/deletions invalidate cursors, while new messages do not move
an existing page boundary. Exact long messages can be opened in character pages.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.core.infra import timephrase
from app.core.infra.time_expr import parse_time_window


SCAN_LIMIT = 5000
PAGE_ROWS = 20
TEXT_CHARS = 12000
MESSAGE_CHARS = 1600
GENERATOR_VERSION = "extractive-v1"
OWNER_SQL = "substr(session_id, 1, instr(session_id, ':') - 1) = ?"


def resolve_range(
    *,
    when: str = "",
    start: str = "",
    end: str = "",
    tz_name: str = "system",
) -> dict[str, str]:
    """Local calendar bounds, stored as a half-open UTC interval.

    System-zone bounds are converted independently, so OS DST rules are used
    for each date rather than reusing today's fixed UTC offset.
    """
    zone = None if tz_name == "system" else ZoneInfo(tz_name)

    def localize(value: datetime) -> datetime:
        wall = value.replace(tzinfo=None)
        return wall.replace(tzinfo=zone) if zone is not None else wall.astimezone()

    if start or end:
        if when or not start or not end:
            raise ValueError("Use either when, or both start and end local ISO dates.")
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        left = datetime.combine(first, time.min)
        right = datetime.combine(last + timedelta(days=1), time.min)
    else:
        anchor = timephrase.now()
        if zone is not None:
            anchor = anchor.astimezone(zone)
        window = parse_time_window(when or "today", now=anchor)
        if window is None:
            raise ValueError("Unrecognized time expression; supply start and end ISO dates.")
        left = window.start
        right = window.end + timedelta(microseconds=1)
    left, right = localize(left), localize(right)
    wall_duration = right.replace(tzinfo=None) - left.replace(tzinfo=None)
    if right <= left or wall_duration > timedelta(days=366):
        raise ValueError("Choose an ordered range of at most 366 local days.")
    return {
        "start": left.astimezone(timezone.utc).isoformat(),
        "end_exclusive": right.astimezone(timezone.utc).isoformat(),
        "local_start": left.isoformat(),
        "local_end_exclusive": right.isoformat(),
        "timezone": tz_name,
    }


class ConversationArchive:
    def __init__(self, db: Any, user_id: str) -> None:
        if not user_id or ":" in user_id:
            raise ValueError("Archive requires a non-empty, unambiguous user namespace.")
        self.db = db
        self.user_id = user_id
        # Per database instance, not per per-turn tool instance. No persistent
        # secret or write is needed for read-only browsing; restart expires cursors.
        if not hasattr(db, "_archive_cursor_key"):
            db._archive_cursor_key = secrets.token_bytes(32)
        self._key = db._archive_cursor_key

    @contextmanager
    def _snapshot(self):
        conn = self.db._get_conn()
        if conn.in_transaction:
            raise RuntimeError("Archive cannot read during an unfinished write transaction.")
        conn.execute("BEGIN")
        try:
            yield conn
        finally:
            conn.rollback()

    def _revision(self, conn) -> int:
        row = conn.execute(
            "SELECT revision FROM archive_revisions WHERE user_id=?",
            (self.user_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    def _encode(self, data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
        sig = hmac.digest(self._key, raw, "sha256")
        return base64.urlsafe_b64encode(sig + raw).decode()

    def _decode(self, cursor: str) -> dict:
        try:
            if len(cursor) > 5000:
                raise ValueError("oversized")
            raw = base64.b64decode(cursor, altchars=b"-_", validate=True)
            if not hmac.compare_digest(raw[:32], hmac.digest(self._key, raw[32:], "sha256")):
                raise ValueError("signature")
            data = json.loads(raw[32:])
            if data["user"] != self.user_id or data["version"] != 1:
                raise ValueError("scope")
            return data
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("Invalid or expired archive cursor; restart the lookup.") from exc

    def _scan(self, conn, span: dict, watermark: int, session: str, topic: str) -> list:
        params = [self.user_id, span["start"], span["end_exclusive"], watermark]
        match_sql = "1"
        if topic:
            match_sql = "instr(lower(content), lower(?)) > 0"
            params.insert(0, topic)
        sql = (
            f"SELECT id, session_id, role, created_at, {match_sql} AS matched "
            f"FROM messages WHERE {OWNER_SQL} AND instr(session_id, ':')>0 "
            "AND created_at>=? AND created_at<? AND id<=? "
            "AND role IN ('user', 'assistant')"
        )
        if session:
            sql += " AND session_id=?"
            params.append(session)
        sql += " ORDER BY created_at, id LIMIT ?"
        params.append(SCAN_LIMIT + 1)
        return conn.execute(sql, params).fetchall()

    def _message(self, conn, message_id: int, *, offset: int = 0, chars: int = MESSAGE_CHARS):
        row = conn.execute(
            "SELECT id, session_id, role, created_at, substr(content, ?, ?), length(content) "
            f"FROM messages WHERE id=? AND {OWNER_SQL} AND instr(session_id, ':')>0 "
            "AND role IN ('user', 'assistant')",
            (offset + 1, chars, message_id, self.user_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "message_id": row[0],
            "session_id": row[1],
            "role": row[2],
            "created_at": row[3],
            "text": row[4],
            "content_offset": offset,
            "content_length": row[5],
            "text_complete": offset == 0 and len(row[4]) == row[5],
            "next_offset": offset + len(row[4]) if offset + len(row[4]) < row[5] else None,
        }

    def message(self, message_id: int = 0, offset: int = 0, *, cursor: str = "") -> dict:
        if offset < 0:
            raise ValueError("content_offset must be non-negative.")
        with self._snapshot() as conn:
            revision = self._revision(conn)
            if cursor:
                state = self._decode(cursor)
                if state.get("mode") != "message":
                    raise ValueError("Use the cursor's original mode.")
                if state["revision"] != revision:
                    return {"status": "stale_cursor", "action": "restart_lookup"}
                message_id, offset = state["message_id"], state["offset"]
            message = self._message(conn, message_id, offset=offset, chars=TEXT_CHARS)
            if message is None:
                return {"status": "not_found", "message": None}
            if offset > message["content_length"]:
                raise ValueError("content_offset exceeds the retained message length.")
            next_cursor = (
                self._encode(
                    {
                        "version": 1,
                        "mode": "message",
                        "user": self.user_id,
                        "revision": revision,
                        "message_id": message_id,
                        "offset": message["next_offset"],
                    }
                )
                if message["next_offset"] is not None
                else None
            )
            return {
                "status": "ok",
                "message": message,
                "next_cursor": next_cursor,
                "source_revision": revision,
                "authority": "source_message",
            }

    def messages(
        self,
        span: dict | None = None,
        *,
        session: str = "",
        topic: str = "",
        cursor: str = "",
        limit: int = PAGE_ROWS,
    ) -> dict:
        if not 1 <= limit <= PAGE_ROWS:
            raise ValueError("limit must be 1-20.")
        with self._snapshot() as conn:
            revision = self._revision(conn)
            if cursor:
                state = self._decode(cursor)
                if state.get("mode") != "messages":
                    raise ValueError("Use the cursor's original mode.")
                if state["revision"] != revision:
                    return {"status": "stale_cursor", "action": "restart_lookup"}
                span, session, topic = state["range"], state["session"], state["topic"]
            else:
                watermark = (
                    conn.execute(
                        f"SELECT MAX(id) FROM messages WHERE {OWNER_SQL}",
                        (self.user_id,),
                    ).fetchone()[0]
                    or 0
                )
                state = {
                    "version": 1,
                    "mode": "messages",
                    "user": self.user_id,
                    "revision": revision,
                    "watermark": watermark,
                    "range": span,
                    "session": session,
                    "topic": topic,
                    "offset": 0,
                    "snapshot_at": timephrase.utcnow().isoformat(),
                }
            scanned = self._scan(conn, span, state["watermark"], session, topic)
            scan_complete = len(scanned) <= SCAN_LIMIT
            inspected = scanned[:SCAN_LIMIT]
            matches = [row for row in inspected if not topic or row[4]]
            selected, used = [], 0
            for row in matches[state["offset"] : state["offset"] + limit]:
                message = self._message(conn, row[0])
                if used + len(message["text"]) > TEXT_CHARS:
                    break
                selected.append(message)
                used += len(message["text"])
            next_offset = state["offset"] + len(selected)
            next_cursor = None
            if next_offset < len(matches):
                next_cursor = self._encode({**state, "offset": next_offset})
            # One previous and next turn from each selected session. These
            # overlap deliberately and are labelled, never counted as matches.
            context = []
            selected_ids = {m["message_id"] for m in selected}
            by_session: dict[str, list] = {}
            for row in inspected:
                by_session.setdefault(row[1], []).append(row)
            context_ids = set()
            for row in selected:
                turns = by_session[row["session_id"]]
                index = next(i for i, turn in enumerate(turns) if turn[0] == row["message_id"])
                for neighbor in turns[max(0, index - 1) : index + 2]:
                    if neighbor[0] in selected_ids or neighbor[0] in context_ids:
                        continue
                    message = self._message(conn, neighbor[0], chars=400)
                    if used + len(message["text"]) <= TEXT_CHARS:
                        context.append(message)
                        context_ids.add(neighbor[0])
                        used += len(message["text"])
            status = "ok" if selected else ("no_topic_match" if inspected else "empty_range")
            if not scan_complete and not selected:
                status = "partial_scan"
            context.sort(key=lambda item: (item["created_at"], item["message_id"]))
            return {
                "status": status,
                "range": span,
                "messages": selected,
                "context": context,
                "selection": "literal_topic_matches" if topic else "chronological_transcript",
                "matched_count": len(matches) if scan_complete else None,
                "matched_count_lower_bound": len(matches),
                "retained_messages": len(inspected) if scan_complete else None,
                "coverage": {
                    "scan_complete": scan_complete,
                    "inspected_messages": len(inspected),
                    "page_complete": next_cursor is None,
                    "period_complete": scan_complete
                    and not topic
                    and not cursor
                    and next_cursor is None
                    and all(m["text_complete"] for m in selected),
                    "snapshot_max_message_id": state["watermark"],
                    "snapshot_at": state["snapshot_at"],
                    "current_period_open": span["end_exclusive"] > timephrase.utcnow().isoformat(),
                    "next_action": "next_page"
                    if next_cursor
                    else ("narrow_date_range" if not scan_complete else None),
                },
                "next_cursor": next_cursor,
                "authority": "source_messages; context may overlap adjacent pages",
            }

    def build_day(self, day: str, tz_name: str = "system") -> dict:
        """Persist one extractive outline. Never invent events or user facts.

        Exact, timestamped excerpts may contain the source's relative wording;
        they are explicitly quotes, not generated stored narrative.
        """
        span = resolve_range(start=day, end=day, tz_name=tz_name)
        with self._snapshot() as conn:
            watermark = (
                conn.execute(
                    f"SELECT MAX(id) FROM messages WHERE {OWNER_SQL}",
                    (self.user_id,),
                ).fetchone()[0]
                or 0
            )
            revision = self._revision(conn)
            scanned = self._scan(conn, span, watermark, "", "")
            rows = scanned[:SCAN_LIMIT]
            if not rows:
                return {"status": "empty_range", "local_day": day}
            # Evenly-spaced chronological anchors avoid presenting only the
            # beginning/end of a long day. Attribution always comes from rows.
            anchors = sorted({round(i * (len(rows) - 1) / 5) for i in range(6)})
            excerpt_ids = set()
            for index in anchors:
                excerpt_ids.add(rows[index][0])
                # Include a same-session neighboring reply for pronouns/context.
                for neighbor in rows[index + 1 :]:
                    if neighbor[1] == rows[index][1]:
                        excerpt_ids.add(neighbor[0])
                        break
            excerpts = [
                self._message(conn, row[0], chars=400) for row in rows if row[0] in excerpt_ids
            ]
            # Fingerprint every inspected source ID/stamp plus the transactional
            # edit revision, including changes to undisplayed source text.
            digest = hashlib.sha256()
            for row in rows:
                digest.update(json.dumps(row, ensure_ascii=False).encode())
            # The mutation revision catches text changes, including unsampled rows.
            digest.update(str(revision).encode())
            complete = len(scanned) <= SCAN_LIMIT
            overview = {
                "local_day": day,
                "range": span,
                "kind": "extractive_outline",
                "generator_version": GENERATOR_VERSION,
                "message_count": len(rows) if complete else None,
                "message_count_lower_bound": len(rows),
                "user_messages": sum(row[2] == "user" for row in rows),
                "assistant_messages": sum(row[2] == "assistant" for row in rows),
                "session_count": len({row[1] for row in rows}),
                "first_message_id": rows[0][0],
                "last_message_id": rows[-1][0],
                "source_watermark": watermark,
                "source_revision": revision,
                "source_hash": digest.hexdigest(),
                "coverage": {
                    "scan_complete": complete,
                    "highlights_selective": True,
                    "current_day_open": span["end_exclusive"] > timephrase.utcnow().isoformat(),
                },
                "excerpts": excerpts,
                "note": "Selected source quotes, not a generated topic summary or activity log.",
            }
        # Compare-and-save under a write lock so an in-flight edit/deletion can
        # never reintroduce a private excerpt after trigger invalidation.
        conn = self.db._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            current_max = (
                conn.execute(
                    f"SELECT MAX(id) FROM messages WHERE {OWNER_SQL}",
                    (self.user_id,),
                ).fetchone()[0]
                or 0
            )
            if self._revision(conn) != revision or current_max != watermark:
                conn.rollback()
                return {"status": "source_changed", "local_day": day}
            conn.execute(
                "INSERT INTO conversation_daily_overviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, timezone, local_day) DO UPDATE SET "
                "start_at=excluded.start_at, end_at=excluded.end_at, "
                "source_hash=excluded.source_hash, generator_version=excluded.generator_version, "
                "overview_json=excluded.overview_json, updated_at=excluded.updated_at",
                (
                    self.user_id,
                    tz_name,
                    day,
                    span["start"],
                    span["end_exclusive"],
                    overview["source_hash"],
                    GENERATOR_VERSION,
                    json.dumps(overview, ensure_ascii=False),
                    timephrase.utcnow().isoformat(),
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {"status": "built", "overview": overview}

    def overviews(self, span: dict | None = None, *, cursor: str = "") -> dict:
        """Read existing day records and explicitly report missing source days."""
        with self._snapshot() as conn:
            revision = self._revision(conn)
            state = self._decode(cursor) if cursor else None
            if state:
                if state.get("mode") != "overviews":
                    raise ValueError("Use the cursor's original mode.")
                if state["revision"] != revision:
                    return {"status": "stale_cursor", "action": "restart_lookup"}
                span = state["range"]
            watermark = (
                conn.execute(
                    f"SELECT MAX(id) FROM messages WHERE {OWNER_SQL}",
                    (self.user_id,),
                ).fetchone()[0]
                or 0
            )
            scanned = self._scan(conn, span, watermark, "", "")
            zone = None if span["timezone"] == "system" else ZoneInfo(span["timezone"])
            days = sorted(
                {
                    datetime.fromisoformat(row[3]).astimezone(zone).date().isoformat()
                    for row in scanned[:SCAN_LIMIT]
                }
            )
            metadata = conn.execute(
                "SELECT local_day, source_hash, updated_at, start_at, end_at "
                "FROM conversation_daily_overviews "
                "WHERE user_id=? AND timezone=? AND start_at>=? AND end_at<=? "
                "AND generator_version=? ORDER BY local_day",
                (
                    self.user_id,
                    span["timezone"],
                    span["start"],
                    span["end_exclusive"],
                    GENERATOR_VERSION,
                ),
            ).fetchall()
            # 'system' can change zones between runs. Old local-day boundaries
            # must not silently become outlines for the new timezone.
            metadata = [
                row
                for row in metadata
                if (
                    (bounds := resolve_range(start=row[0], end=row[0], tz_name=span["timezone"]))
                    and (row[3], row[4]) == (bounds["start"], bounds["end_exclusive"])
                )
            ]
            fingerprint = hashlib.sha256(json.dumps(metadata).encode()).hexdigest()
            if state and state["fingerprint"] != fingerprint:
                return {"status": "stale_cursor", "action": "restart_lookup"}
            offset = state["offset"] if state else 0
            selected_days = metadata[offset : offset + 14]
            # Multi-day lookups are a small day directory. Open one day for
            # excerpts; original messages remain separately available for quotes.
            entries = []
            for day, _, updated_at, _, _ in selected_days:
                raw = conn.execute(
                    "SELECT overview_json FROM conversation_daily_overviews "
                    "WHERE user_id=? AND timezone=? AND local_day=?",
                    (self.user_id, span["timezone"], day),
                ).fetchone()[0]
                entry = json.loads(raw)
                entry["updated_at"] = updated_at
                entry["coverage"]["current_day_open"] = (
                    entry["range"]["end_exclusive"] > timephrase.utcnow().isoformat()
                )
                if (
                    span["local_start"][:10]
                    != (datetime.fromisoformat(span["local_end_exclusive"]) - timedelta(days=1))
                    .date()
                    .isoformat()
                ):
                    entry.pop("excerpts", None)
                    entry["excerpts_omitted"] = True
                entries.append(entry)
            missing = sorted(set(days) - {row[0] for row in metadata})
            next_offset = offset + len(selected_days)
            next_cursor = (
                self._encode(
                    {
                        "version": 1,
                        "mode": "overviews",
                        "user": self.user_id,
                        "revision": revision,
                        "range": span,
                        "fingerprint": fingerprint,
                        "offset": next_offset,
                    }
                )
                if next_offset < len(metadata)
                else None
            )
            return {
                "status": "ok" if metadata else ("missing_overviews" if days else "empty_range"),
                "range": span,
                "overviews": entries,
                "missing_days": missing,
                "next_cursor": next_cursor,
                "total_overview_days": len(metadata),
                "coverage": {
                    "source_scan_complete": len(scanned) <= SCAN_LIMIT,
                    "all_source_days_indexed": not missing and len(scanned) <= SCAN_LIMIT,
                    "page_complete": next_cursor is None,
                },
                "next_action": "open a local day, or use messages for original dialogue",
                "authority": "derived navigation only; exact quotes come from source messages",
            }
