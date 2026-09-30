"""Local one-off appointments; SQLite is authoritative, no external booking."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from app.core.infra import timephrase


_FIELDS = (
    "id", "user_id", "title", "starts_at", "ends_at", "notes", "status", "revision",
    "source_session", "created_at", "updated_at",
)


def absolute_time(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Use an ISO date/time with an explicit timezone offset.")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("A timezone offset is required; ask rather than guessing.")
    return stamp.astimezone(timezone.utc).isoformat(timespec="seconds")


def _text(value: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("Calendar text must be a string.")
    clean = " ".join(value.split())
    if (required and not clean) or len(clean) > limit:
        minimum = 1 if required else 0
        raise ValueError(f"Calendar text must contain {minimum}-{limit} characters.")
    return clean


class CalendarStore:
    def __init__(self, db, user_id: str) -> None:
        if not user_id:
            raise ValueError("A calendar owner is required.")
        self.db = db
        self.user_id = user_id

    def get(self, event_id: str) -> dict | None:
        row = self.db.execute_fetchone(
            f"SELECT {', '.join(_FIELDS)} FROM calendar_events WHERE user_id=? AND id=?",
            (self.user_id, event_id),
        )
        return dict(zip(_FIELDS, row, strict=True)) if row else None

    def list_events(self, starts_after: str, starts_before: str, *, limit: int = 30) -> list[dict]:
        lower, upper = absolute_time(starts_after), absolute_time(starts_before)
        if upper <= lower:
            raise ValueError("The end of the query must follow its start.")
        rows = self.db.execute_fetchall(
            f"SELECT {', '.join(_FIELDS)} FROM calendar_events "
            "WHERE user_id=? AND status='scheduled' AND starts_at>=? AND starts_at<? "
            "ORDER BY starts_at, id LIMIT ?",
            (self.user_id, lower, upper, max(1, min(int(limit), 100))),
        )
        return [dict(zip(_FIELDS, row, strict=True)) for row in rows]

    def create(
        self, *, title: str, starts_at: str, ends_at: str, notes: str = "",
        source_session: str = "",
    ) -> dict:
        title, notes = _text(title, 160, required=True), _text(notes, 500)
        start, end = absolute_time(starts_at), absolute_time(ends_at)
        if end <= start:
            raise ValueError("An event must end after it starts.")
        identity = json.dumps([self.user_id, title.casefold(), start])
        event_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
        now = timephrase.utcnow().isoformat(timespec="seconds")
        self.db.execute_commit(
            "INSERT OR IGNORE INTO calendar_events "
            "(id, user_id, title, starts_at, ends_at, notes, source_session, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, self.user_id, title, start, end, notes, source_session, now, now),
        )
        matches = self.db.execute_fetchall(
            "SELECT id, title FROM calendar_events "
            "WHERE user_id=? AND starts_at=? AND status='scheduled'",
            (self.user_id, start),
        )
        resolved_id = next(
            (row[0] for row in matches if row[1].casefold() == title.casefold()), event_id,
        )
        event = self.get(resolved_id)
        if event["status"] != "scheduled" or event["ends_at"] != end or event["notes"] != notes:
            raise ValueError("This event already exists; inspect it and use update by ID.")
        return event

    def update(
        self, event_id: str, *, title: str | None = None, starts_at: str | None = None,
        ends_at: str | None = None, notes: str | None = None, cancel: bool = False,
    ) -> dict:
        previous = self.get(event_id)
        if previous is None:
            raise ValueError("Calendar event not found for this user.")
        start = absolute_time(starts_at) if starts_at is not None else previous["starts_at"]
        end = absolute_time(ends_at) if ends_at is not None else previous["ends_at"]
        if end <= start:
            raise ValueError("Rescheduling needs an end time after the new start.")
        updated = dict(previous)
        updated.update(
            title=_text(title, 160, required=True) if title is not None else previous["title"],
            starts_at=start, ends_at=end,
            notes=_text(notes, 500) if notes is not None else previous["notes"],
            status="cancelled" if cancel else previous["status"],
        )
        if updated == previous:
            return previous
        try:
            self.db.execute_commit(
                "UPDATE calendar_events SET title=?, starts_at=?, ends_at=?, notes=?, status=?, "
                "revision=revision+1, updated_at=? WHERE user_id=? AND id=? AND revision=?",
                (updated["title"], start, end, updated["notes"], updated["status"],
                 timephrase.utcnow().isoformat(timespec="seconds"), self.user_id, event_id,
                 previous["revision"]),
            )
        except sqlite3.IntegrityError:
            raise ValueError("That event is already scheduled at the requested time.") from None
        result = self.get(event_id)
        fields = ("title", "starts_at", "ends_at", "notes", "status")
        if any(result[key] != updated[key] for key in fields):
            raise ValueError("Calendar changed concurrently; list it again before retrying.")
        return result

    def upcoming(self, *, hours: int = 48) -> list[dict]:
        now = timephrase.utcnow()
        return self.list_events(now.isoformat(), (now + timedelta(hours=hours)).isoformat())
