"""Atomic message provenance and minimal turn-outcome records in the chat DB."""

from app.core.infra import timephrase
from app.llm.turn_outcome import TurnOutcome


OUTCOME_SCHEMA = """
CREATE TABLE IF NOT EXISTS turn_outcomes (
    attempt_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    assistant_message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    model TEXT NOT NULL,
    route_revision TEXT NOT NULL,
    finish_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turn_outcomes_user ON turn_outcomes(user_message_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_turn_outcomes_assistant
    ON turn_outcomes(assistant_message_id) WHERE assistant_message_id IS NOT NULL;
"""


class MessageOutcomesMixin:
    def _write_turn_outcome(self, conn, session_id: str, outcome: TurnOutcome) -> None:
        if outcome.user_message_id is None and outcome.assistant_message_id is None:
            return
        for message_id, role in (
            (outcome.user_message_id, "user"), (outcome.assistant_message_id, "assistant"),
        ):
            if message_id is not None:
                source = conn.execute(
                    "SELECT session_id, role FROM messages WHERE id=?", (message_id,),
                ).fetchone()
                if source is None or tuple(source) != (session_id, role):
                    raise ValueError("Turn evidence must belong to the same session and role.")
        previous = conn.execute(
            "SELECT session_id, user_message_id, provider_id, model, route_revision "
            "FROM turn_outcomes WHERE attempt_id=?", (outcome.attempt_id,),
        ).fetchone()
        identity = (session_id, outcome.user_message_id, outcome.provider_id,
                    outcome.model, outcome.route_revision)
        if previous is not None and tuple(previous) != identity:
            raise ValueError("A turn attempt cannot change its source identity.")
        conn.execute(
            "INSERT INTO turn_outcomes (attempt_id, session_id, user_message_id, "
            "assistant_message_id, status, provider_id, model, route_revision, "
            "finish_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(attempt_id) DO UPDATE SET "
            "assistant_message_id=excluded.assistant_message_id, "
            "status=excluded.status, finish_reason=excluded.finish_reason",
            (outcome.attempt_id, session_id, outcome.user_message_id,
             outcome.assistant_message_id, outcome.status, outcome.provider_id,
             outcome.model, outcome.route_revision, outcome.finish_reason,
             timephrase.utcnow().isoformat()),
        )

    def record_turn_outcome(self, session_id: str, outcome: TurnOutcome) -> None:
        conn = self._get_conn()
        with conn:
            self._write_turn_outcome(conn, session_id, outcome)

    def get_turn_outcomes(self, session_id: str) -> list[dict]:
        rows = self._get_conn().execute(
            "SELECT attempt_id, user_message_id, assistant_message_id, status, provider_id, "
            "model, route_revision, finish_reason FROM turn_outcomes "
            "WHERE session_id=? ORDER BY created_at, rowid", (session_id,),
        ).fetchall()
        keys = ("attempt_id", "user_message_id", "assistant_message_id", "status", "provider_id",
                "model", "route_revision", "finish_reason")
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def add_message(
        self, session_id, role, content, token_count=0, *, arc=None,
        dialogue_act=None, attachments=None, outcome: TurnOutcome | None = None,
    ) -> int:
        from app.core.infra.chat_database import MessageRow

        conn = self._get_conn()
        created_at = timephrase.utcnow().isoformat()
        with conn:
            cursor = conn.execute(
                "INSERT INTO messages (session_id, role, content, token_count, "
                "created_at, arc, dialogue_act, attachments) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, role, content, token_count, created_at, arc,
                 dialogue_act, attachments),
            )
            msg_id = int(cursor.lastrowid or 0)
            if outcome is not None:
                if role != "assistant" or not outcome.learning_eligible:
                    raise ValueError("Only completed assistant evidence can be committed.")
                previous_id = outcome.assistant_message_id
                outcome.assistant_message_id = msg_id
                try:
                    self._write_turn_outcome(conn, session_id, outcome)
                except Exception:
                    outcome.assistant_message_id = previous_id
                    raise
        self._notify_added_message(MessageRow(
            id=msg_id, session_id=session_id, role=role, content=content,
            token_count=token_count, created_at=created_at, arc=arc,
            dialogue_act=dialogue_act, attachments=attachments,
        ))
        return msg_id
