"""User-requested, one-shot reminders; independent of calendar appointments."""
from __future__ import annotations

from app.core.goals.calendar import absolute_time
from app.core.infra import timephrase


_FIELDS = (
    "id", "user_id", "text", "due_at", "fired_at", "cancelled_at", "source_session", "created_at",
)


class ReminderStore:
    def __init__(self, db, user_id: str) -> None:
        if not user_id:
            raise ValueError("A reminder owner is required.")
        self.db = db
        self.user_id = user_id

    def get(self, reminder_id: int) -> dict | None:
        row = self.db.execute_fetchone(
            f"SELECT {', '.join(_FIELDS)} FROM reminders WHERE user_id=? AND id=?",
            (self.user_id, reminder_id),
        )
        return dict(zip(_FIELDS, row, strict=True)) if row else None

    def create(self, text: str, due_at: str, *, source_session: str = "") -> dict:
        if not isinstance(text, str) or not (clean := " ".join(text.split())) or len(clean) > 500:
            raise ValueError("Reminder text must contain 1-500 characters.")
        due = absolute_time(due_at)
        if due <= timephrase.utcnow().isoformat(timespec="seconds"):
            raise ValueError("Reminder must be due in the future.")
        self.db.execute_commit(
            "INSERT OR IGNORE INTO reminders (user_id, text, due_at, source_session, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.user_id, clean, due, source_session,
             timephrase.utcnow().isoformat(timespec="seconds")),
        )
        row = self.db.execute_fetchone(
            "SELECT id FROM reminders WHERE user_id=? AND text=? AND due_at=? "
            "AND fired_at IS NULL AND cancelled_at IS NULL ORDER BY id DESC LIMIT 1",
            (self.user_id, clean, due),
        )
        return self.get(row[0])

    def list_pending(self, *, limit: int = 100) -> list[dict]:
        rows = self.db.execute_fetchall(
            f"SELECT {', '.join(_FIELDS)} FROM reminders WHERE user_id=? "
            "AND fired_at IS NULL AND cancelled_at IS NULL ORDER BY due_at, id LIMIT ?",
            (self.user_id, max(1, min(int(limit), 100))),
        )
        return [dict(zip(_FIELDS, row, strict=True)) for row in rows]

    def deliver_next(self, session_id: str):
        if not session_id:
            raise ValueError("An active session is required to deliver a reminder.")
        return self.db.deliver_due_reminder(
            self.user_id, session_id, timephrase.utcnow().isoformat(timespec="seconds"),
        )

    def cancel(self, reminder_id: int) -> dict:
        if self.get(reminder_id) is None:
            raise ValueError("Reminder not found for this user.")
        self.db.execute_commit(
            "UPDATE reminders SET cancelled_at=? WHERE id=? AND user_id=? "
            "AND cancelled_at IS NULL AND fired_at IS NULL",
            (timephrase.utcnow().isoformat(timespec="seconds"), reminder_id, self.user_id),
        )
        return self.get(reminder_id)
