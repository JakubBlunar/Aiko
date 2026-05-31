"""Belief store for the K2 theory-of-mind layer.

The ``beliefs`` table (schema v12) records Aiko's *model* of what the
user believes or feels, kept separate from the facts she knows. Two
shapes ride in one store, distinguished by the ``kind`` column:

* ``mood``    -- "Jacob is excited about the tokyo trip". Carries
                 numeric ``valence`` / ``arousal`` so
                 :class:`BeliefGapDetector` can compare directly against
                 the live :class:`app.core.affect.affect_state.AffectState`.
* ``opinion`` -- "Jacob thinks Rust is overhyped". No automatic
                 verification surface; status flips via the gap
                 detector's lexical-contradiction pass.

Lifecycle::

    upserted -> status='active'
       |
       +- gap detector confirms (mood matches affect, or opinion
       |  re-affirmed)            -> status='confirmed'
       |
       +- gap detector contradicted (lexical negation/antonym vs.
       |  user message, or mood label flips to opposing band)
       |                          -> status='contradicted'
       |
       +- untouched for ``belief_stale_after_days``
                                  -> status='stale'

This module mirrors :class:`app.core.memory.memory_conflict_store.MemoryConflictStore`
in shape and threading model: it talks to SQLite directly (no
MemoryStore mirror needed -- beliefs are not memories, they're a
separate audit surface).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.belief_store")


# Public belief kinds. Worker/REST/UI all key on these strings.
KIND_MOOD = "mood"
KIND_OPINION = "opinion"
VALID_KINDS: frozenset[str] = frozenset((KIND_MOOD, KIND_OPINION))

# Public status values.
STATUS_ACTIVE = "active"
STATUS_CONFIRMED = "confirmed"
STATUS_CONTRADICTED = "contradicted"
STATUS_STALE = "stale"
VALID_STATUSES: frozenset[str] = frozenset(
    (STATUS_ACTIVE, STATUS_CONFIRMED, STATUS_CONTRADICTED, STATUS_STALE)
)

# Origin of the belief write.
SOURCE_SELF_TAG = "self_tag"
SOURCE_WORKER = "worker"
SOURCE_MANUAL = "manual"
VALID_SOURCES: frozenset[str] = frozenset(
    (SOURCE_SELF_TAG, SOURCE_WORKER, SOURCE_MANUAL)
)

# Confidence per-source defaults when a tag/worker omits the field.
# Self-tags are Aiko's deliberate guess; worker is the heuristic
# extractor; manual is the user explicitly typing the belief.
_SOURCE_DEFAULT_CONFIDENCE = {
    SOURCE_SELF_TAG: 0.7,
    SOURCE_WORKER: 0.6,
    SOURCE_MANUAL: 0.85,
}

# Topic-embedding cosine threshold for collapsing near-duplicates on
# upsert. Tuned to match "tokyo trip" / "trip to japan" while keeping
# distinct topics ("rust language" vs "rust framework") apart.
_TOPIC_DEDUPE_COSINE = 0.88


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode_metadata(metadata: dict[str, Any] | None) -> str | None:
    if not metadata:
        return None
    try:
        return json.dumps(dict(metadata), ensure_ascii=False)
    except Exception:
        return None


def _decode_metadata(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _encode_embedding(vec: np.ndarray | None) -> bytes | None:
    if vec is None:
        return None
    arr = np.asarray(vec, dtype=np.float32)
    return arr.tobytes()


def _decode_embedding(raw: bytes | None) -> np.ndarray | None:
    if raw is None:
        return None
    try:
        return np.frombuffer(raw, dtype=np.float32)
    except Exception:
        return None


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Plain cosine; both vectors assumed unit-normalised on insert.

    Guards against zero vectors so a stray empty embedding can't
    crash the dedupe pass.
    """
    if a.size == 0 or b.size == 0 or a.size != b.size:
        return 0.0
    denom = float(np.linalg.norm(a)) * float(np.linalg.norm(b))
    if denom <= 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


def _normalise_topic(topic: str) -> str:
    """Trim + lowercase + collapse whitespace so trivial casing /
    spacing differences hit the UNIQUE constraint."""
    return " ".join((topic or "").strip().lower().split())


@dataclass(slots=True)
class Belief:
    """One row of the ``beliefs`` table."""

    id: int
    user_id: str
    kind: str
    topic: str
    predicted_state: str
    confidence: float
    valence: float | None
    arousal: float | None
    source: str
    source_message_id: int | None
    observed_at: str
    last_checked_at: str | None
    status: str
    gap_seen_at: str | None
    metadata: dict[str, Any]
    # Loaded lazily by ``get`` / ``list_*`` only when the caller asks
    # for it via ``include_embedding=True`` -- the bytes blob is
    # otherwise dropped to keep payloads small.
    topic_embedding: np.ndarray | None = None

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe dict for REST / WS broadcasts (no numpy arrays)."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "kind": self.kind,
            "topic": self.topic,
            "predicted_state": self.predicted_state,
            "confidence": self.confidence,
            "valence": self.valence,
            "arousal": self.arousal,
            "source": self.source,
            "source_message_id": self.source_message_id,
            "observed_at": self.observed_at,
            "last_checked_at": self.last_checked_at,
            "status": self.status,
            "gap_seen_at": self.gap_seen_at,
            "metadata": dict(self.metadata),
        }


_SELECT_COLS = (
    "id, user_id, kind, topic, predicted_state, confidence, valence, "
    "arousal, source, source_message_id, observed_at, last_checked_at, "
    "status, gap_seen_at, metadata"
)

_SELECT_COLS_WITH_EMBEDDING = _SELECT_COLS + ", topic_embedding"


def _row_to_belief(row: tuple[Any, ...], *, with_embedding: bool = False) -> Belief:
    return Belief(
        id=int(row[0]),
        user_id=str(row[1]),
        kind=str(row[2]),
        topic=str(row[3]),
        predicted_state=str(row[4]),
        confidence=float(row[5]),
        valence=(float(row[6]) if row[6] is not None else None),
        arousal=(float(row[7]) if row[7] is not None else None),
        source=str(row[8]),
        source_message_id=(int(row[9]) if row[9] is not None else None),
        observed_at=str(row[10]),
        last_checked_at=(str(row[11]) if row[11] is not None else None),
        status=str(row[12]),
        gap_seen_at=(str(row[13]) if row[13] is not None else None),
        metadata=_decode_metadata(row[14] if isinstance(row[14], str) else None),
        topic_embedding=(
            _decode_embedding(row[15]) if with_embedding and len(row) > 15 else None
        ),
    )


class BeliefStore:
    """SQLite-backed store for the ``beliefs`` table."""

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    # ── writes ────────────────────────────────────────────────────────

    def upsert(
        self,
        *,
        user_id: str,
        kind: str,
        topic: str,
        predicted_state: str,
        confidence: float | None = None,
        valence: float | None = None,
        arousal: float | None = None,
        source: str = SOURCE_SELF_TAG,
        source_message_id: int | None = None,
        topic_embedding: np.ndarray | None = None,
        metadata: dict[str, Any] | None = None,
        observed_at: str | None = None,
    ) -> Belief | None:
        """Insert or update a belief, deduping near-duplicate topics.

        Resolution order:

        1. If ``(user_id, kind, normalised_topic)`` already exists, update
           the existing row in place (predicted_state / confidence /
           source_message_id / observed_at refresh; status -> 'active'
           again on re-tag).
        2. Otherwise, if any same-kind belief shares a topic embedding
           with cosine >= ``_TOPIC_DEDUPE_COSINE``, update that row
           (keeping its id and history but adopting the new topic
           string + state).
        3. Otherwise INSERT a fresh row.

        Returns the resulting :class:`Belief` or ``None`` on invalid
        input.
        """
        kind_norm = str(kind or "").strip().lower()
        if kind_norm not in VALID_KINDS:
            log.debug("beliefs.upsert reject kind=%r", kind)
            return None
        source_norm = str(source or SOURCE_SELF_TAG).strip().lower()
        if source_norm not in VALID_SOURCES:
            source_norm = SOURCE_SELF_TAG
        topic_norm = _normalise_topic(topic)
        if not topic_norm:
            log.debug("beliefs.upsert reject empty topic")
            return None
        state_norm = str(predicted_state or "").strip()
        if not state_norm:
            log.debug("beliefs.upsert reject empty predicted_state")
            return None
        if confidence is None:
            confidence_f = float(_SOURCE_DEFAULT_CONFIDENCE.get(source_norm, 0.6))
        else:
            confidence_f = float(confidence)
        confidence_f = max(0.0, min(1.0, confidence_f))
        when = observed_at or _now_iso()
        metadata_text = _encode_metadata(metadata)
        embedding_blob = _encode_embedding(topic_embedding)

        # Mood beliefs without numeric valence/arousal are still allowed
        # (the worker may only have a label like "excited"); the gap
        # detector simply skips rows where valence is NULL.
        valence_v = float(valence) if valence is not None else None
        arousal_v = float(arousal) if arousal is not None else None

        conn = self._db._get_conn()  # type: ignore[attr-defined]

        # 1. exact (user_id, kind, topic) match -> update in place.
        existing = conn.execute(
            f"SELECT {_SELECT_COLS_WITH_EMBEDDING} FROM beliefs "
            "WHERE user_id = ? AND kind = ? AND topic = ? LIMIT 1",
            (str(user_id), kind_norm, topic_norm),
        ).fetchone()

        if existing is None and embedding_blob is not None:
            # 2. fuzzy topic match -> pick the highest-cosine same-kind
            # row whose embedding clears the threshold. We cap at the
            # 200 most-recent same-kind beliefs to keep the scan
            # bounded; older beliefs aren't worth fuzzy-merging into.
            candidates = conn.execute(
                f"SELECT {_SELECT_COLS_WITH_EMBEDDING} FROM beliefs "
                "WHERE user_id = ? AND kind = ? AND topic_embedding IS NOT NULL "
                "ORDER BY observed_at DESC LIMIT 200",
                (str(user_id), kind_norm),
            ).fetchall()
            best_row: tuple[Any, ...] | None = None
            best_cos = 0.0
            new_vec = topic_embedding
            for row in candidates:
                vec = _decode_embedding(row[15])
                if vec is None or new_vec is None:
                    continue
                cos = _cosine(new_vec, vec)
                if cos > best_cos:
                    best_cos = cos
                    best_row = row
            if best_row is not None and best_cos >= _TOPIC_DEDUPE_COSINE:
                existing = best_row

        if existing is not None:
            row_id = int(existing[0])
            conn.execute(
                "UPDATE beliefs SET "
                "  topic = ?, topic_embedding = COALESCE(?, topic_embedding), "
                "  predicted_state = ?, confidence = ?, "
                "  valence = COALESCE(?, valence), arousal = COALESCE(?, arousal), "
                "  source = ?, source_message_id = COALESCE(?, source_message_id), "
                "  observed_at = ?, status = ?, metadata = COALESCE(?, metadata) "
                "WHERE id = ?",
                (
                    topic_norm,
                    embedding_blob,
                    state_norm,
                    confidence_f,
                    valence_v,
                    arousal_v,
                    source_norm,
                    int(source_message_id) if source_message_id is not None else None,
                    when,
                    STATUS_ACTIVE,
                    metadata_text,
                    row_id,
                ),
            )
            conn.commit()
            return self.get(row_id)

        # 3. brand new belief.
        cursor = conn.execute(
            "INSERT INTO beliefs ("
            "  user_id, kind, topic, topic_embedding, predicted_state, "
            "  confidence, valence, arousal, source, source_message_id, "
            "  observed_at, last_checked_at, status, gap_seen_at, metadata"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?)",
            (
                str(user_id),
                kind_norm,
                topic_norm,
                embedding_blob,
                state_norm,
                confidence_f,
                valence_v,
                arousal_v,
                source_norm,
                int(source_message_id) if source_message_id is not None else None,
                when,
                STATUS_ACTIVE,
                metadata_text,
            ),
        )
        conn.commit()
        row_id = int(cursor.lastrowid) if cursor.lastrowid else None
        if row_id is None:
            return None
        return self.get(row_id)

    def update(
        self,
        belief_id: int,
        *,
        predicted_state: str | None = None,
        confidence: float | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Belief | None:
        """Apply a partial REST/UI edit to a row."""
        sets: list[str] = []
        args: list[Any] = []
        if predicted_state is not None:
            cleaned = str(predicted_state).strip()
            if not cleaned:
                return None
            sets.append("predicted_state = ?")
            args.append(cleaned)
        if confidence is not None:
            sets.append("confidence = ?")
            args.append(max(0.0, min(1.0, float(confidence))))
        if status is not None:
            status_norm = str(status).strip().lower()
            if status_norm not in VALID_STATUSES:
                raise ValueError(
                    f"invalid status {status!r} (valid: {sorted(VALID_STATUSES)})"
                )
            sets.append("status = ?")
            args.append(status_norm)
        if metadata is not None:
            sets.append("metadata = ?")
            args.append(_encode_metadata(metadata))
        if not sets:
            return self.get(int(belief_id))
        args.append(int(belief_id))
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            f"UPDATE beliefs SET {', '.join(sets)} WHERE id = ?",
            tuple(args),
        )
        conn.commit()
        if not cursor.rowcount:
            return None
        return self.get(int(belief_id))

    def mark_confirmed(self, belief_id: int) -> bool:
        return self._set_status(belief_id, STATUS_CONFIRMED, stamp_gap=False)

    def mark_contradicted(self, belief_id: int, *, stamp_gap: bool = True) -> bool:
        return self._set_status(belief_id, STATUS_CONTRADICTED, stamp_gap=stamp_gap)

    def mark_stale(self, belief_id: int) -> bool:
        return self._set_status(belief_id, STATUS_STALE, stamp_gap=False)

    def stamp_checked(self, belief_id: int, *, gap: bool = False) -> bool:
        """Update ``last_checked_at`` (and optionally ``gap_seen_at``).

        Called by the gap detector even on a no-mismatch pass so we
        know when each row was last evaluated.
        """
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        when = _now_iso()
        if gap:
            cursor = conn.execute(
                "UPDATE beliefs SET last_checked_at = ?, gap_seen_at = ? "
                "WHERE id = ?",
                (when, when, int(belief_id)),
            )
        else:
            cursor = conn.execute(
                "UPDATE beliefs SET last_checked_at = ? WHERE id = ?",
                (when, int(belief_id)),
            )
        conn.commit()
        return bool(cursor.rowcount)

    def delete(self, belief_id: int) -> bool:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        cursor = conn.execute(
            "DELETE FROM beliefs WHERE id = ?",
            (int(belief_id),),
        )
        conn.commit()
        return bool(cursor.rowcount)

    def _set_status(
        self,
        belief_id: int,
        status: str,
        *,
        stamp_gap: bool,
    ) -> bool:
        when = _now_iso()
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        if stamp_gap:
            cursor = conn.execute(
                "UPDATE beliefs SET status = ?, last_checked_at = ?, "
                "gap_seen_at = ? WHERE id = ?",
                (status, when, when, int(belief_id)),
            )
        else:
            cursor = conn.execute(
                "UPDATE beliefs SET status = ?, last_checked_at = ? "
                "WHERE id = ?",
                (status, when, int(belief_id)),
            )
        conn.commit()
        return bool(cursor.rowcount)

    # ── reads ─────────────────────────────────────────────────────────

    def get(self, belief_id: int, *, with_embedding: bool = False) -> Belief | None:
        cols = _SELECT_COLS_WITH_EMBEDDING if with_embedding else _SELECT_COLS
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        row = conn.execute(
            f"SELECT {cols} FROM beliefs WHERE id = ?",
            (int(belief_id),),
        ).fetchone()
        if row is None:
            return None
        return _row_to_belief(row, with_embedding=with_embedding)

    def list_active(
        self,
        *,
        user_id: str | None = None,
        kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Belief]:
        return self._list(
            user_id=user_id,
            kind=kind,
            status=STATUS_ACTIVE,
            limit=limit,
            offset=offset,
        )

    def list_recent(
        self,
        *,
        user_id: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Belief]:
        return self._list(
            user_id=user_id,
            kind=kind,
            status=status,
            limit=limit,
            offset=offset,
        )

    def _list(
        self,
        *,
        user_id: str | None,
        kind: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> list[Belief]:
        clauses: list[str] = []
        args: list[Any] = []
        if user_id is not None:
            clauses.append("user_id = ?")
            args.append(str(user_id))
        if kind is not None:
            kind_norm = str(kind).strip().lower()
            if kind_norm not in VALID_KINDS:
                raise ValueError(
                    f"invalid kind filter {kind!r} (valid: {sorted(VALID_KINDS)})"
                )
            clauses.append("kind = ?")
            args.append(kind_norm)
        if status is not None:
            status_norm = str(status).strip().lower()
            if status_norm not in VALID_STATUSES:
                raise ValueError(
                    f"invalid status filter {status!r} "
                    f"(valid: {sorted(VALID_STATUSES)})"
                )
            clauses.append("status = ?")
            args.append(status_norm)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        args.append(int(limit))
        args.append(int(offset))
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM beliefs {where} "
            "ORDER BY observed_at DESC, id DESC LIMIT ? OFFSET ?",
            tuple(args),
        ).fetchall()
        return [_row_to_belief(r) for r in rows]

    def list_active_for_gap_check(
        self,
        *,
        user_id: str,
        kind: str = KIND_MOOD,
        since_iso: str | None = None,
        limit: int = 50,
    ) -> list[Belief]:
        """Active beliefs newer than ``since_iso`` (for gap detection).

        Returns mood beliefs by default; the detector only cares about
        rows whose valence is set so we filter that here.
        """
        kind_norm = str(kind).strip().lower()
        if kind_norm not in VALID_KINDS:
            raise ValueError(
                f"invalid kind {kind!r} (valid: {sorted(VALID_KINDS)})"
            )
        clauses = ["user_id = ?", "kind = ?", "status = ?"]
        args: list[Any] = [str(user_id), kind_norm, STATUS_ACTIVE]
        if kind_norm == KIND_MOOD:
            clauses.append("valence IS NOT NULL")
        if since_iso is not None:
            clauses.append("observed_at >= ?")
            args.append(str(since_iso))
        args.append(int(limit))
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM beliefs "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY observed_at DESC LIMIT ?",
            tuple(args),
        ).fetchall()
        return [_row_to_belief(r) for r in rows]

    def count_by_status(self, *, user_id: str | None = None) -> dict[str, int]:
        out = {s: 0 for s in VALID_STATUSES}
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        if user_id is None:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM beliefs GROUP BY status"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM beliefs WHERE user_id = ? GROUP BY status",
                (str(user_id),),
            ).fetchall()
        for status, count in rows:
            out[str(status)] = int(count)
        return out

    def count_active(self, *, user_id: str | None = None) -> int:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        if user_id is None:
            row = conn.execute(
                "SELECT COUNT(*) FROM beliefs WHERE status = ?",
                (STATUS_ACTIVE,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM beliefs WHERE user_id = ? AND status = ?",
                (str(user_id), STATUS_ACTIVE),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    # ── maintenance ───────────────────────────────────────────────────

    def mark_stale_older_than(
        self,
        *,
        cutoff_iso: str,
        user_id: str | None = None,
    ) -> int:
        """Bulk-stale active beliefs whose ``last_checked_at`` is older
        than ``cutoff_iso`` (falling back to ``observed_at`` when the
        row was never checked). Returns the number of rows flipped."""
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        if user_id is None:
            cursor = conn.execute(
                "UPDATE beliefs SET status = ?, last_checked_at = ? "
                "WHERE status = ? AND "
                "COALESCE(last_checked_at, observed_at) < ?",
                (STATUS_STALE, _now_iso(), STATUS_ACTIVE, str(cutoff_iso)),
            )
        else:
            cursor = conn.execute(
                "UPDATE beliefs SET status = ?, last_checked_at = ? "
                "WHERE status = ? AND user_id = ? AND "
                "COALESCE(last_checked_at, observed_at) < ?",
                (
                    STATUS_STALE,
                    _now_iso(),
                    STATUS_ACTIVE,
                    str(user_id),
                    str(cutoff_iso),
                ),
            )
        conn.commit()
        return int(cursor.rowcount or 0)

    def prune_to_cap(self, *, user_id: str, cap: int) -> int:
        """Drop the lowest-confidence, oldest beliefs above ``cap``.

        Counts only ``active`` rows; stale/contradicted/confirmed rows
        stay as audit history. Returns the number of rows deleted.
        """
        cap_i = max(0, int(cap))
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        row = conn.execute(
            "SELECT COUNT(*) FROM beliefs WHERE user_id = ? AND status = ?",
            (str(user_id), STATUS_ACTIVE),
        ).fetchone()
        current = int(row[0]) if row is not None else 0
        if current <= cap_i:
            return 0
        excess = current - cap_i
        cursor = conn.execute(
            "DELETE FROM beliefs WHERE id IN ("
            "  SELECT id FROM beliefs "
            "  WHERE user_id = ? AND status = ? "
            "  ORDER BY confidence ASC, observed_at ASC LIMIT ?"
            ")",
            (str(user_id), STATUS_ACTIVE, excess),
        )
        conn.commit()
        return int(cursor.rowcount or 0)
