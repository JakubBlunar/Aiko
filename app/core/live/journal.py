"""Bounded Live experience journal.

Not long-term memory. Policy and the assembler never call MemoryStore.
Entries are privacy-classified, TTL'd, and reject relative deictics.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase


PRIVACY_LOCAL = "local_state"
PRIVACY_TRANSCRIPT = "transcript"
PRIVACY_MEMORY = "memory_eligible"
VALID_PRIVACY = frozenset({PRIVACY_LOCAL, PRIVACY_TRANSCRIPT, PRIVACY_MEMORY})

DEFAULT_TTL_SECONDS = 24 * 3600
DEFAULT_MAX_ROWS = 128
MAX_TEXT_CHARS = 280

JOURNAL_KINDS = frozenset({
    "user.message_sent",
    "user.speech_final",
    "silence.wake",
    "user.session_changed",
    "sleep.status_changed",
    "situation.shared_commitment_changed",
})


def default_privacy(kind: str) -> str:
    if kind in {"user.message_sent", "user.speech_final"}:
        return PRIVACY_TRANSCRIPT
    return PRIVACY_LOCAL


@dataclass(frozen=True, slots=True)
class LiveJournalEntry:
    id: int
    session_id: str
    occurred_at: str
    privacy: str
    kind: str
    text: str
    evidence_ids: tuple[str, ...]
    expires_at: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "occurred_at": self.occurred_at,
            "privacy": self.privacy,
            "kind": self.kind,
            "text": self.text,
            "evidence_ids": list(self.evidence_ids),
            "expires_at": self.expires_at,
        }


def _clean_text(value: str) -> str:
    return " ".join(str(value or "").split()).strip()[:MAX_TEXT_CHARS]


class LiveExperienceJournal:
    """SQLite-backed ring of Live experience rows."""

    def __init__(
        self,
        db: "ChatDatabase",
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> None:
        self._db = db
        self._ttl_seconds = max(60, int(ttl_seconds))
        self._max_rows = max(8, int(max_rows))

    def append(
        self,
        *,
        session_id: str,
        kind: str,
        text: str,
        privacy: str | None = None,
        evidence_ids: tuple[str, ...] = (),
        memory_eligible: bool = False,
    ) -> LiveJournalEntry | None:
        token = str(kind or "").strip()
        if token not in JOURNAL_KINDS:
            return None
        cleaned = _clean_text(text)
        if not cleaned:
            return None
        if timephrase.has_relative_deictic(cleaned):
            return None
        privacy_value = privacy or default_privacy(token)
        if memory_eligible:
            privacy_value = PRIVACY_MEMORY
        if privacy_value not in VALID_PRIVACY:
            privacy_value = PRIVACY_LOCAL
        now = timephrase.utcnow()
        occurred = now.isoformat(timespec="seconds")
        expires = (now + timedelta(seconds=self._ttl_seconds)).isoformat(
            timespec="seconds",
        )
        evidence_json = json.dumps(list(evidence_ids[:8]), separators=(",", ":"))
        row_id = self._db.execute_commit(
            "INSERT INTO live_experience_journal "
            "(session_id, occurred_at, privacy, kind, text, evidence_json, "
            "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(session_id),
                occurred,
                privacy_value,
                token,
                cleaned,
                evidence_json,
                expires,
            ),
        )
        self._prune(str(session_id), now.isoformat(timespec="seconds"))
        return LiveJournalEntry(
            id=int(row_id),
            session_id=str(session_id),
            occurred_at=occurred,
            privacy=privacy_value,
            kind=token,
            text=cleaned,
            evidence_ids=tuple(evidence_ids[:8]),
            expires_at=expires,
        )

    def tail(
        self,
        session_id: str,
        *,
        limit: int = 20,
        include_expired: bool = False,
    ) -> list[LiveJournalEntry]:
        now = timephrase.utcnow().isoformat(timespec="seconds")
        sql = (
            "SELECT id, session_id, occurred_at, privacy, kind, text, "
            "evidence_json, expires_at FROM live_experience_journal "
            "WHERE session_id = ?"
        )
        params: tuple[Any, ...] = (str(session_id),)
        if not include_expired:
            sql += " AND expires_at > ?"
            params = (str(session_id), now)
        sql += " ORDER BY id DESC LIMIT ?"
        params = (*params, max(1, int(limit)))
        rows = self._db.execute_fetchall(sql, params)
        entries: list[LiveJournalEntry] = []
        for row in rows:
            evidence: tuple[str, ...] = ()
            try:
                raw = json.loads(str(row[6] or "[]"))
                if isinstance(raw, list):
                    evidence = tuple(str(item) for item in raw[:8])
            except (TypeError, ValueError, json.JSONDecodeError):
                evidence = ()
            entries.append(
                LiveJournalEntry(
                    id=int(row[0]),
                    session_id=str(row[1]),
                    occurred_at=str(row[2]),
                    privacy=str(row[3]),
                    kind=str(row[4]),
                    text=str(row[5]),
                    evidence_ids=evidence,
                    expires_at=str(row[7]),
                )
            )
        entries.reverse()
        return entries

    def has_unextracted_memory_eligible(self, session_id: str) -> bool:
        now = timephrase.utcnow().isoformat(timespec="seconds")
        row = self._db.execute_fetchone(
            "SELECT COUNT(*) FROM live_experience_journal "
            "WHERE session_id = ? AND privacy = ? AND expires_at > ?",
            (str(session_id), PRIVACY_MEMORY, now),
        )
        return bool(row and int(row[0] or 0) > 0)

    def _prune(self, session_id: str, now_iso: str) -> None:
        self._db.execute_commit(
            "DELETE FROM live_experience_journal "
            "WHERE session_id = ? AND expires_at <= ?",
            (session_id, now_iso),
        )
        count_row = self._db.execute_fetchone(
            "SELECT COUNT(*) FROM live_experience_journal WHERE session_id = ?",
            (session_id,),
        )
        count = int(count_row[0] or 0) if count_row is not None else 0
        extra = count - self._max_rows
        if extra <= 0:
            return
        self._db.execute_commit(
            "DELETE FROM live_experience_journal WHERE id IN ("
            "SELECT id FROM live_experience_journal WHERE session_id = ? "
            "ORDER BY id ASC LIMIT ?)",
            (session_id, extra),
        )
