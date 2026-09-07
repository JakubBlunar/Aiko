"""Structured, per-session read of what the conversation is doing now.

The worker-facing extraction is deliberately open-ended: shared activities and
places are prose, not a catalogue of scenarios.  Deterministic code owns the
lifecycle, evidence validation, staleness, and whether an inferred situation is
allowed to constrain the world.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable

from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase


ACTIVE = "active"
UNCERTAIN = "uncertain"
ENDED = "ended"
VALID_STATUSES = frozenset({ACTIVE, UNCERTAIN, ENDED})

KEEP = "keep"
REPLACE = "replace"
CLEAR = "clear"
VALID_OPERATIONS = frozenset({KEEP, REPLACE, CLEAR})

MAX_SUMMARY_CHARS = 360
MAX_FIELD_CHARS = 120
MAX_EVIDENCE_IDS = 8
IMPLICIT_CLEAR_MISSES = 2

_JSON_BLOCK_RE = re.compile(r"\{.*\}", flags=re.DOTALL)


def _clean_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text[:limit]


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        match = _JSON_BLOCK_RE.search(text)
        candidate = match.group(0) if match else None
    if not candidate:
        return None
    try:
        payload = json.loads(candidate)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


@dataclass(frozen=True, slots=True)
class SituationExtraction:
    """One untrusted worker proposal after structural validation."""

    operation: str
    summary: str = ""
    shared: bool = False
    shared_activity: str = ""
    place_ref: str = ""
    aiko_activity: str = ""
    evidence_message_ids: tuple[int, ...] = ()
    explicit_end: bool = False


@dataclass(frozen=True, slots=True)
class ConversationSituationState:
    """Durable semantic state inferred from a bounded transcript."""

    session_id: str
    generation: int
    status: str
    summary: str
    shared: bool
    shared_activity: str
    place_ref: str
    aiko_activity: str
    evidence_message_ids: tuple[int, ...]
    source_message_id: int
    miss_count: int
    updated_at: str

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class WorldSituation:
    """Authoritative world observation used to validate inferred context."""

    scene_id: int | None = None
    scene_name: str = ""
    location_id: int | None = None
    location_slug: str = ""
    location_name: str = ""
    posture: str = ""
    activity: str = ""
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class ConversationSituationSnapshot:
    """Immutable turn-facing projection shared by typed and voice paths."""

    session_id: str
    generation: int
    observed_at: str
    input_mode: str
    floor_transition: str
    dialogue_act: str
    arc: str
    arc_confidence: float
    mood_label: str
    vitality_band: str
    user_present: bool
    user_active_app: str
    world: WorldSituation
    inferred: ConversationSituationState | None
    inferred_stale: bool
    world_compatible: bool
    conflict_reason: str
    shared_commitment_active: bool
    since_user_activity_ms: int
    sleep: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


def parse_extraction(
    raw: str,
    *,
    valid_message_ids: Iterable[int],
) -> SituationExtraction | None:
    """Parse worker JSON without trusting model confidence or evidence ids."""
    payload = _extract_json_object(raw)
    if payload is None or "confidence" in payload:
        return None
    operation = str(payload.get("operation") or "").strip().lower()
    if operation not in VALID_OPERATIONS:
        return None

    valid_ids = {int(value) for value in valid_message_ids}
    evidence: list[int] = []
    raw_evidence = payload.get("evidence_message_ids")
    if isinstance(raw_evidence, list):
        for value in raw_evidence:
            try:
                message_id = int(value)
            except (TypeError, ValueError):
                return None
            if message_id not in valid_ids:
                return None
            if message_id not in evidence:
                evidence.append(message_id)
            if len(evidence) >= MAX_EVIDENCE_IDS:
                break

    summary = _clean_text(payload.get("summary"), limit=MAX_SUMMARY_CHARS)
    shared_activity = _clean_text(
        payload.get("shared_activity"), limit=MAX_FIELD_CHARS
    )
    place_ref = _clean_text(payload.get("place_ref"), limit=MAX_FIELD_CHARS)
    aiko_activity = _clean_text(
        payload.get("aiko_activity"), limit=MAX_FIELD_CHARS
    )
    if operation == REPLACE and (not summary or not evidence):
        return None
    stored_text = " ".join((summary, shared_activity, place_ref, aiko_activity))
    if operation == REPLACE and timephrase.has_relative_deictic(stored_text):
        return None

    return SituationExtraction(
        operation=operation,
        summary=summary,
        shared=bool(payload.get("shared", False)),
        shared_activity=shared_activity,
        place_ref=place_ref,
        aiko_activity=aiko_activity,
        evidence_message_ids=tuple(evidence),
        explicit_end=bool(payload.get("explicit_end", False)),
    )


def reduce_situation(
    previous: ConversationSituationState | None,
    extraction: SituationExtraction,
    *,
    session_id: str,
    source_message_id: int,
    now: datetime | None = None,
) -> ConversationSituationState | None:
    """Apply lifecycle hysteresis to one validated extraction."""
    timestamp = (now or timephrase.utcnow()).isoformat(timespec="seconds")
    generation = int(previous.generation if previous is not None else 0)

    if extraction.operation == KEEP:
        if previous is None:
            return None
        return replace(
            previous,
            status=ACTIVE,
            source_message_id=max(previous.source_message_id, int(source_message_id)),
            miss_count=0,
            updated_at=timestamp,
        )

    if extraction.operation == REPLACE:
        return ConversationSituationState(
            session_id=str(session_id),
            generation=generation + 1,
            status=ACTIVE,
            summary=extraction.summary,
            shared=bool(extraction.shared),
            shared_activity=extraction.shared_activity,
            place_ref=extraction.place_ref,
            aiko_activity=extraction.aiko_activity,
            evidence_message_ids=extraction.evidence_message_ids,
            source_message_id=int(source_message_id),
            miss_count=0,
            updated_at=timestamp,
        )

    if previous is None:
        return None
    if extraction.explicit_end:
        return replace(
            previous,
            generation=generation + 1,
            status=ENDED,
            source_message_id=max(previous.source_message_id, int(source_message_id)),
            miss_count=IMPLICIT_CLEAR_MISSES,
            updated_at=timestamp,
        )
    misses = int(previous.miss_count) + 1
    return replace(
        previous,
        generation=generation + (1 if misses >= IMPLICIT_CLEAR_MISSES else 0),
        status=ENDED if misses >= IMPLICIT_CLEAR_MISSES else UNCERTAIN,
        source_message_id=max(previous.source_message_id, int(source_message_id)),
        miss_count=misses,
        updated_at=timestamp,
    )


class ConversationSituationStore:
    """SQLite adapter for the single bounded situation row per session."""

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    def get(self, session_id: str) -> ConversationSituationState | None:
        row = self._db.execute_fetchone(
            "SELECT generation, status, state_json, evidence_message_ids, "
            "source_message_id, miss_count, updated_at "
            "FROM conversation_situation WHERE session_id = ?",
            (str(session_id),),
        )
        if row is None:
            return None
        try:
            state = json.loads(str(row[2] or "{}"))
            evidence = json.loads(str(row[3] or "[]"))
            if not isinstance(state, dict) or not isinstance(evidence, list):
                return None
            status = str(row[1] or ENDED)
            if status not in VALID_STATUSES:
                status = ENDED
            return ConversationSituationState(
                session_id=str(session_id),
                generation=max(0, int(row[0] or 0)),
                status=status,
                summary=_clean_text(state.get("summary"), limit=MAX_SUMMARY_CHARS),
                shared=bool(state.get("shared", False)),
                shared_activity=_clean_text(
                    state.get("shared_activity"), limit=MAX_FIELD_CHARS
                ),
                place_ref=_clean_text(state.get("place_ref"), limit=MAX_FIELD_CHARS),
                aiko_activity=_clean_text(
                    state.get("aiko_activity"), limit=MAX_FIELD_CHARS
                ),
                evidence_message_ids=tuple(
                    int(value) for value in evidence[:MAX_EVIDENCE_IDS]
                ),
                source_message_id=max(0, int(row[4] or 0)),
                miss_count=max(0, int(row[5] or 0)),
                updated_at=str(row[6] or ""),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def upsert(self, state: ConversationSituationState) -> None:
        state_json = json.dumps(
            {
                "summary": state.summary,
                "shared": state.shared,
                "shared_activity": state.shared_activity,
                "place_ref": state.place_ref,
                "aiko_activity": state.aiko_activity,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        evidence_json = json.dumps(list(state.evidence_message_ids), separators=(",", ":"))
        self._db.execute_commit(
            "INSERT INTO conversation_situation "
            "(session_id, generation, status, state_json, evidence_message_ids, "
            "source_message_id, miss_count, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "generation=excluded.generation, status=excluded.status, "
            "state_json=excluded.state_json, "
            "evidence_message_ids=excluded.evidence_message_ids, "
            "source_message_id=excluded.source_message_id, "
            "miss_count=excluded.miss_count, updated_at=excluded.updated_at",
            (
                state.session_id,
                int(state.generation),
                state.status,
                state_json,
                evidence_json,
                int(state.source_message_id),
                int(state.miss_count),
                state.updated_at,
            ),
        )

    def clear(self, session_id: str) -> None:
        self._db.execute_commit(
            "DELETE FROM conversation_situation WHERE session_id = ?",
            (str(session_id),),
        )


def state_age_seconds(
    state: ConversationSituationState | None,
    *,
    now: datetime | None = None,
) -> float | None:
    """Return wall-clock age for persistence staleness checks."""
    if state is None:
        return None
    updated = _parse_iso(state.updated_at)
    if updated is None:
        return None
    current = now or timephrase.utcnow()
    return max(0.0, (current - updated).total_seconds())


__all__ = [
    "ACTIVE",
    "CLEAR",
    "ConversationSituationSnapshot",
    "ConversationSituationState",
    "ConversationSituationStore",
    "ENDED",
    "KEEP",
    "REPLACE",
    "SituationExtraction",
    "UNCERTAIN",
    "WorldSituation",
    "parse_extraction",
    "reduce_situation",
    "state_age_seconds",
]
