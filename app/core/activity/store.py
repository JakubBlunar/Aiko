"""SQLite event store + focus-flicker sessionizer.

Retention (H33 shape 14) lives on :class:`ActivityStore.prune` and is
driven by :class:`app.core.activity.prune_worker.ActivityPruneWorker`.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from app.core.activity.envelope import ActivityEnvelope
from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.activity.store")

# Consecutive events of the same identity within this gap collapse into
# one session so a 1 s collector tick does not mint a row per sample.
SESSION_GAP_SECONDS = 8.0


class ActivityStore:
    """Writes ``activity_events`` / ``activity_sessions``; never raises."""

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    def add_event(self, envelope: ActivityEnvelope) -> int | None:
        payload = envelope.to_payload_json()
        at = _parse_at(envelope.at) or timephrase.utcnow()
        at_iso = at.isoformat()
        try:
            event_id = self._db.execute_commit(
                "INSERT INTO activity_events "
                "(at, source, tier, signal_kind, app, title, surface_id, "
                "payload_json, envelope_v) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    at_iso,
                    envelope.source,
                    envelope.tier,
                    envelope.signal_kind,
                    envelope.subject.app,
                    envelope.subject.title,
                    envelope.subject.surface_id,
                    json.dumps(payload, ensure_ascii=False),
                    int(envelope.v),
                ),
            )
        except Exception:
            log.debug("activity event insert failed", exc_info=True)
            return None
        envelope.at = at_iso
        try:
            self._sessionize(envelope)
        except Exception:
            log.debug("activity sessionize failed", exc_info=True)
        return int(event_id) if event_id else None

    def recent_sessions(self, *, limit: int = 20) -> list[dict[str, Any]]:
        try:
            rows = self._db.execute_fetchall(
                "SELECT id, source, app, title, surface_id, started_at, "
                "ended_at, duration_seconds, event_count "
                "FROM activity_sessions ORDER BY ended_at DESC LIMIT ?",
                (max(1, int(limit)),),
            )
        except Exception:
            log.debug("activity session list failed", exc_info=True)
            return []
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append({
                "id": row[0],
                "source": row[1],
                "app": row[2],
                "title": row[3],
                "surface_id": row[4],
                "started_at": row[5],
                "ended_at": row[6],
                "duration_seconds": row[7],
                "event_count": row[8],
            })
        return out

    def last_event(self) -> dict[str, Any] | None:
        try:
            row = self._db.execute_fetchone(
                "SELECT at, source, tier, signal_kind, app, title, "
                "surface_id, payload_json, envelope_v "
                "FROM activity_events ORDER BY id DESC LIMIT 1",
            )
        except Exception:
            log.debug("activity last event failed", exc_info=True)
            return None
        if row is None:
            return None
        payload: Any = {}
        try:
            payload = json.loads(row[7] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        return {
            "at": row[0],
            "source": row[1],
            "tier": row[2],
            "signal_kind": row[3],
            "app": row[4],
            "title": row[5],
            "surface_id": row[6],
            "payload": payload if isinstance(payload, dict) else {},
            "envelope_v": row[8],
        }

    def counts(self) -> dict[str, Any]:
        events = 0
        sessions = 0
        oldest = None
        try:
            row = self._db.execute_fetchone(
                "SELECT COUNT(*), MIN(at) FROM activity_events",
            )
            if row is not None:
                events = int(row[0] or 0)
                oldest = row[1]
            row = self._db.execute_fetchone(
                "SELECT COUNT(*) FROM activity_sessions",
            )
            if row is not None:
                sessions = int(row[0] or 0)
        except Exception:
            log.debug("activity counts failed", exc_info=True)
        return {
            "events": events,
            "sessions": sessions,
            "oldest_event_at": oldest,
        }

    def stale_event_count(self, keep_days: int) -> int:
        if int(keep_days) <= 0:
            return 0
        cutoff = timephrase.utcnow() - timedelta(days=int(keep_days))
        try:
            row = self._db.execute_fetchone(
                "SELECT COUNT(*) FROM activity_events WHERE at < ?",
                (cutoff.isoformat(),),
            )
        except Exception:
            log.debug("activity stale count failed", exc_info=True)
            return 0
        return int(row[0] or 0) if row else 0

    def prune(self, keep_days: int) -> dict[str, int]:
        """Drop events and sessions older than ``keep_days``.

        ``keep_days <= 0`` means keep forever (the worker no-ops).
        """
        if int(keep_days) <= 0:
            return {"events": 0, "sessions": 0}
        cutoff = timephrase.utcnow() - timedelta(days=int(keep_days))
        iso = cutoff.isoformat()
        events = 0
        sessions = 0
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            cursor = conn.execute(
                "DELETE FROM activity_events WHERE at < ?", (iso,),
            )
            events = int(cursor.rowcount or 0)
            cursor = conn.execute(
                "DELETE FROM activity_sessions WHERE ended_at < ?", (iso,),
            )
            sessions = int(cursor.rowcount or 0)
            conn.commit()
        except Exception:
            log.warning("activity prune failed", exc_info=True)
            return {"events": 0, "sessions": 0}
        return {"events": events, "sessions": sessions}

    def _sessionize(self, envelope: ActivityEnvelope) -> None:
        identity = _identity(envelope)
        at = _parse_at(envelope.at) or timephrase.utcnow()
        at_iso = at.isoformat()
        row = self._db.execute_fetchone(
            "SELECT id, source, app, surface_id, signal_kind, ended_at, "
            "started_at, event_count "
            "FROM activity_sessions ORDER BY id DESC LIMIT 1",
        )
        if row is not None:
            last_identity = (
                str(row[1] or ""),
                str(row[4] or ""),
                str(row[2] or ""),
                str(row[3] or ""),
            )
            last_end = _parse_at(str(row[5] or ""))
            gap_ok = False
            if last_end is not None:
                gap_ok = (at - last_end) <= timedelta(seconds=SESSION_GAP_SECONDS)
            if last_identity == identity and gap_ok:
                started = _parse_at(str(row[6] or "")) or at
                duration = max(0.0, (at - started).total_seconds())
                self._db.execute_commit(
                    "UPDATE activity_sessions SET ended_at = ?, "
                    "duration_seconds = ?, event_count = ?, title = ? "
                    "WHERE id = ?",
                    (
                        at_iso,
                        duration,
                        int(row[7] or 0) + 1,
                        envelope.subject.title,
                        int(row[0]),
                    ),
                )
                return
        self._db.execute_commit(
            "INSERT INTO activity_sessions "
            "(source, app, title, surface_id, signal_kind, started_at, "
            "ended_at, duration_seconds, event_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                envelope.source,
                envelope.subject.app,
                envelope.subject.title,
                envelope.subject.surface_id,
                envelope.signal_kind,
                at_iso,
                at_iso,
                0.0,
                1,
            ),
        )


def _identity(envelope: ActivityEnvelope) -> tuple[str, str, str, str]:
    if envelope.source == "foreground":
        return (
            "foreground",
            envelope.signal_kind,
            envelope.subject.app or "",
            envelope.subject.surface_id or "",
        )
    return (
        envelope.source,
        envelope.signal_kind,
        envelope.subject.app or "",
        envelope.subject.surface_id or "",
    )


def _parse_at(raw: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
