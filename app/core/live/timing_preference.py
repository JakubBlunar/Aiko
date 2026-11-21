"""Source-backed, explicitly taught timing for casual conversation in Live."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.core.activity.evidence import CODING_CONFIDENCE_FLOOR
from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase, MessageRow
    from app.core.live.frame import LiveSituationFrame


_DEFER_CASUAL = re.compile(
    r"remember to (?:save|hold) casual topics (?:for breaks|until i take a break)"
    r" while i(?:'m| am) coding[.!]?"
)
_FORGET_CODING = re.compile(r"forget my (?:coding timing|coding-time) preference[.!]?")


def _instruction(text: str) -> str:
    normalized = " ".join(text.lower().replace("\u2019", "'").split())
    if _DEFER_CASUAL.fullmatch(normalized):
        return "defer_casual"
    if _FORGET_CODING.fullmatch(normalized):
        return "forget"
    return ""


def _source_key(user_id: str) -> str:
    return f"live.coding_timing_source:{user_id}"


def observe_timing_instruction(db: ChatDatabase, user_id: str, message: MessageRow) -> None:
    """Index the newest explicit instruction. The original user row owns its meaning."""
    if message.role != "user" or not _instruction(message.content):
        return
    db.execute_commit(
        "INSERT INTO kv_meta (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at "
        "WHERE CAST(excluded.value AS INTEGER) > CAST(kv_meta.value AS INTEGER)",
        (_source_key(user_id), str(message.id), timephrase.utcnow().isoformat()),
    )


def defer_casual_while_coding(db: ChatDatabase, user_id: str, frame: LiveSituationFrame) -> bool:
    """Apply a preference only with a fresh, positive coding observation."""
    if (
        frame.shared.os_idle != "active"
        or "c6" in frame.constraints.stale_sources
        or frame.shared.inferred.label != "user_coding"
        or frame.shared.inferred.confidence < CODING_CONFIDENCE_FLOOR
    ):
        return False
    row = db.execute_fetchone(
        "SELECT messages.content FROM kv_meta "
        "JOIN messages ON messages.id = CAST(kv_meta.value AS INTEGER) "
        "WHERE kv_meta.key = ? AND messages.role = 'user'",
        (_source_key(user_id),),
    )
    return row is not None and _instruction(row[0]) == "defer_casual"
