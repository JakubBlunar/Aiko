"""SQLite interpretations and their outcome receipts.

Repeated extraction is not verification. Structured worker readings stay
explicitly tentative; manual confirmation records the user's judgement, and
applicability still expires. Legacy rows remain available for review.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
from app.core.infra import timephrase
from app.core.relationship.belief_interpretation import (
    is_interpretation, is_current, supporting_ids,
)

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

# Statuses that still represent something Aiko believes. Both are
# eligible for context filtering; this is not a statement of certainty.
BELIEVED_STATUSES: frozenset[str] = frozenset(
    (STATUS_ACTIVE, STATUS_CONFIRMED)
)
_BELIEVED_PLACEHOLDERS = ", ".join("?" * len(BELIEVED_STATUSES))

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
    return timephrase.utcnow().isoformat()


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
        auto_confirm_after: int | None = None,
    ) -> Belief | None:
        """Insert or revise one current reading per canonical subject.

        Structured interpretations match exact canonical subjects only; the
        worker adjudicates equivalent full readings using offered existing ids.
        Legacy callers retain their same-kind topic match for compatibility.
        Revisions retain three prior scoped receipts in metadata. This does not
        certify an inference, and automatic confirmation is disabled.

        Returns the resulting row, or None for invalid input or a protected
        user correction. No schema migration or historical certification.
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
        # A fresh row is one observation. Seeding the counter here (rather
        # than treating "absent" as zero) keeps the arithmetic in
        # ``_resolve_reobservation`` honest for rows written before the
        # counter existed, which read as one observation rather than as
        # brand new.
        first_metadata = dict(metadata or {})
        first_metadata.setdefault("observations", 1)
        first_metadata.setdefault("first_observed_at", when)
        first_metadata["support_message_ids"] = sorted(
            supporting_ids(first_metadata, source_message_id)
        )
        metadata_text = _encode_metadata(first_metadata)
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

        structured = is_interpretation(first_metadata)
        if existing is None and embedding_blob is not None and not structured:
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
                if is_interpretation(_decode_metadata(row[14])):
                    continue
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
            prior_meta = _decode_metadata(existing[14])
            # Self-tags and manual legacy edits cannot silently replace a richer
            # scoped interpretation, nor can the worker overwrite a user edit.
            if ((is_interpretation(prior_meta) and not structured)
                    or (structured and prior_meta.get("manually_edited")
                        and is_current(_row_to_belief(existing)))):
                return None
            row_id = int(existing[0])
            next_status, merged_meta = self._resolve_reobservation(
                existing,
                new_state=state_norm,
                new_metadata=first_metadata,
                observed_at=when,
                auto_confirm_after=auto_confirm_after,
            )
            if (not (supporting_ids(first_metadata, source_message_id)
                             - set(prior_meta.get("support_message_ids", []))
                             - supporting_ids(prior_meta, existing[9]))
                    and _normalise_topic(state_norm) == _normalise_topic(str(existing[4]))):
                when = str(existing[10])
            conn.execute(
                "UPDATE beliefs SET "
                "  topic = ?, topic_embedding = COALESCE(?, topic_embedding), "
                "  predicted_state = ?, confidence = ?, "
                "  valence = COALESCE(?, valence), arousal = COALESCE(?, arousal), "
                "  source = ?, source_message_id = COALESCE(?, source_message_id), "
                "  observed_at = ?, status = ?, metadata = ? "
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
                    next_status,
                    _encode_metadata(merged_meta),
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

    def _resolve_reobservation(
        self,
        existing: tuple[Any, ...],
        *,
        new_state: str,
        new_metadata: dict[str, Any] | None,
        observed_at: str,
        auto_confirm_after: int | None,
    ) -> tuple[str, dict[str, Any]]:
        """Keep independent support receipts without turning inference into truth.

        ``auto_confirm_after`` remains an accepted compatibility argument; no
        number of extractions verifies a user's internal state.
        """
        prior_state = str(existing[4] or "")
        prior_status = str(existing[12] or STATUS_ACTIVE)
        prior_meta = _decode_metadata(existing[14])
        incoming = dict(new_metadata or {})
        old_ids = set(prior_meta.get("support_message_ids", []))
        old_ids |= supporting_ids(prior_meta, existing[9])
        new_ids = set(incoming.get("support_message_ids", []))
        state_changed = _normalise_topic(new_state) != _normalise_topic(prior_state)
        scope_changed = any(incoming.get(k) != prior_meta.get(k)
                            for k in ("scope", "session_key", "interpretation_version",
                                      "situation", "uncertainty", "revise_when"))
        expired = not is_current(_row_to_belief(existing))
        fresh = bool(new_ids - old_ids)
        merged = {**prior_meta, **incoming}
        try:
            count = max(1, int(prior_meta.get("observations", 1)))
        except (ValueError, TypeError):
            count = 1
        if state_changed or scope_changed or expired:
            history = list(prior_meta.get("interpretation_history", []))[-3:]
            history.append({"interpretation": prior_state, "status": prior_status,
                            "observed_at": existing[10], "metadata": {
                                k: v for k, v in prior_meta.items()
                                if k != "interpretation_history"
                            }})
            merged = {**incoming, "interpretation_history": history[-3:],
                      "observations": 1, "first_observed_at": observed_at,
                      "support_message_ids": sorted(new_ids)}
            return STATUS_ACTIVE, merged
        merged["observations"] = count + int(fresh)
        merged["first_observed_at"] = prior_meta.get("first_observed_at", existing[10])
        merged["support_message_ids"] = sorted(old_ids | new_ids)[-64:]
        # An unsupported re-run must not refresh the applicability clock.
        if not fresh:
            for k in ("last_supported_at", "expires_at", "evidence"):
                if k in prior_meta:
                    merged[k] = prior_meta[k]
        if prior_status in (STATUS_CONTRADICTED, STATUS_STALE):
            return (STATUS_ACTIVE if fresh else prior_status), merged
        return prior_status, merged

    def update(
        self,
        belief_id: int,
        *,
        predicted_state: str | None = None,
        confidence: float | None = None,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
        resolution_method: str = "manual",
        situation: str | None = None,
        uncertainty: str | None = None,
    ) -> Belief | None:
        """Apply a partial REST/UI edit to a row."""
        prior = self.get(belief_id)
        if predicted_state is not None and (
            not predicted_state.strip() or len(predicted_state) > 600
        ):
            raise ValueError("interpretation must be 1-600 characters")
        for key, text in (("situation", situation), ("uncertainty", uncertainty)):
            if text is not None:
                if not text.strip() or len(text) > 300 or timephrase.has_relative_deictic(text):
                    raise ValueError(f"{key} must be 1-300 characters without relative dates")
                metadata = {**(metadata or {}), key: text.strip()}
        if predicted_state is not None and timephrase.has_relative_deictic(predicted_state):
            raise ValueError("interpretation must use explicit dates")
        sets: list[str] = []
        args: list[Any] = []
        status_norm = None
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
        if prior is not None and resolution_method == "manual":
            metadata = {**prior.metadata, **(metadata or {})}
            if ((predicted_state is not None and predicted_state != prior.predicted_state)
                    or situation is not None or uncertainty is not None):
                history = list(prior.metadata.get("interpretation_history", []))[-2:]
                history.append({"interpretation": prior.predicted_state,
                                "observed_at": prior.observed_at, "status": prior.status,
                                "metadata": {k: v for k, v in prior.metadata.items()
                                             if k != "interpretation_history"}})
                metadata["interpretation_history"] = history
                metadata["manually_edited"] = True
                metadata["user_corrected_at"] = _now_iso()
                if status_norm != STATUS_CONFIRMED:
                    metadata.pop("confirmation_basis", None)
                metadata.pop("response_hint", None)
                metadata["reason"] = "Wording corrected by the user."
                if status is None:
                    sets.append("status = ?")
                    args.append(STATUS_ACTIVE)
            if status_norm == STATUS_CONFIRMED:
                metadata["confirmation_basis"] = "user"
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
        if prior is not None and status_norm in (STATUS_CONFIRMED, STATUS_CONTRADICTED):
            changed = (predicted_state is not None and prior.predicted_state != cleaned
                       or situation is not None or uncertainty is not None)
            if metadata is not None:
                prior.metadata = metadata
            if predicted_state is not None:
                prior.predicted_state = cleaned
            self._append_outcome(
                prior, outcome=status_norm, method=resolution_method, resolved_at=_now_iso(),
                evidence={"edited_claim": predicted_state is not None},
                force=changed,
            )
        conn.commit()
        if not cursor.rowcount:
            return None
        return self.get(int(belief_id))

    def mark_confirmed(
        self, belief_id: int, *, method: str = "unspecified",
        evidence_message_id: int | None = None, evidence: dict[str, Any] | None = None,
    ) -> bool:
        return self._set_status(
            belief_id, STATUS_CONFIRMED, stamp_gap=False, method=method,
            evidence_message_id=evidence_message_id, evidence=evidence,
        )

    def mark_contradicted(
        self, belief_id: int, *, stamp_gap: bool = True, method: str = "unspecified",
        evidence_message_id: int | None = None, evidence: dict[str, Any] | None = None,
    ) -> bool:
        return self._set_status(
            belief_id, STATUS_CONTRADICTED, stamp_gap=stamp_gap, method=method,
            evidence_message_id=evidence_message_id, evidence=evidence,
        )

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
        method: str = "unspecified",
        evidence_message_id: int | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        when = _now_iso()
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        prior = self.get(belief_id)
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
        if prior is not None and status in (STATUS_CONFIRMED, STATUS_CONTRADICTED):
            self._append_outcome(
                prior, outcome=status, method=method, resolved_at=when,
                evidence_message_id=evidence_message_id, evidence=evidence,
            )
        conn.commit()
        return bool(cursor.rowcount)

    def _append_outcome(
        self, claim: Belief, *, outcome: str, method: str, resolved_at: str,
        evidence_message_id: int | None = None, evidence: dict[str, Any] | None = None,
        force: bool = False,
    ) -> None:
        """Append within the caller's status transaction; never infer historic outcomes."""
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        if claim.status == outcome and evidence_message_id is None and not force:
            latest = conn.execute(
                "SELECT method, outcome, claim_json FROM belief_outcomes "
                "WHERE belief_id = ? ORDER BY id DESC LIMIT 1", (claim.id,),
            ).fetchone()
            if latest is not None and latest[0] == method and latest[1] == outcome:
                previous = json.loads(latest[2])
                if previous["topic"] == claim.topic and previous["predicted_state"] == (
                    claim.predicted_state
                ):
                    return
        dedupe_key = None
        if evidence_message_id is not None:
            identity = [
                claim.id, claim.topic, claim.predicted_state,
                claim.metadata.get("first_observed_at", claim.observed_at),
                outcome, method, evidence_message_id,
            ]
            dedupe_key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        try:
            conn.execute(
                "INSERT INTO belief_outcomes "
                "(belief_id, user_id, outcome, method, resolved_at, prior_status, "
                "claim_json, evidence_message_id, evidence_json, dedupe_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(dedupe_key) DO NOTHING",
                (
                    claim.id, claim.user_id, outcome, method, resolved_at, claim.status,
                    json.dumps(claim.to_payload()), evidence_message_id,
                    json.dumps(evidence or {}), dedupe_key,
                ),
            )
        except Exception:
            conn.rollback()
            raise

    def list_outcomes(
        self, *, user_id: str, after_id: int = 0, limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read immutable, forward-only evidence in cursor order, scoped to one user."""
        rows = self._db.execute_fetchall(
            "SELECT id, belief_id, outcome, method, resolved_at, prior_status, "
            "claim_json, evidence_message_id, evidence_json FROM belief_outcomes "
            "WHERE user_id = ? AND id > ? ORDER BY id LIMIT ?",
            (user_id, int(after_id), max(1, min(int(limit), 1000))),
        )
        return [
            {
                "id": row[0], "belief_id": row[1], "outcome": row[2],
                "method": row[3], "resolved_at": row[4], "prior_status": row[5],
                "claim": json.loads(row[6]), "evidence_message_id": row[7],
                "evidence": json.loads(row[8]),
            }
            for row in rows
        ]

    def outcome_counts(self, *, user_id: str) -> dict[str, Any]:
        """Method counts are coverage diagnostics, deliberately not accuracy rates."""
        methods = self._db.execute_fetchall(
            "SELECT method, outcome, COUNT(*) FROM belief_outcomes "
            "WHERE user_id = ? GROUP BY method, outcome", (user_id,),
        )
        sources = self._db.execute_fetchall(
            "SELECT source, COUNT(*) FROM beliefs WHERE user_id = ? GROUP BY source", (user_id,),
        )
        return {
            "total": sum(row[2] for row in methods),
            "by_method": [
                {"method": row[0], "outcome": row[1], "count": row[2]} for row in methods
            ],
            "belief_sources": dict(sources),
        }

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
        interpretations_only: bool = False,
    ) -> list[Belief]:
        return self._list(
            user_id=user_id,
            kind=kind,
            status=status,
            limit=limit,
            offset=offset, interpretations_only=interpretations_only,
        )

    def _list(
        self,
        *,
        user_id: str | None,
        kind: str | None,
        status: str | None,
        limit: int,
        offset: int,
        interpretations_only: bool = False,
    ) -> list[Belief]:
        clauses: list[str] = []
        args: list[Any] = []
        if interpretations_only:
            clauses.append("CASE WHEN json_valid(metadata) THEN "
                           "json_extract(metadata, '$.interpretation_version') END = 2")
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

    def list_context(self, *, user_id: str, session_key: str, user_text: str,
                     limit: int = 4) -> list[Belief]:
        """Fresh, relevant tentative readings plus explicitly user-confirmed legacy rows.

        A historic auto-confirmed worker fragment is not certified by migration.
        Filtering precedes the output cap so irrelevant recent rows cannot starve
        a still-applicable reading. The scan is bounded by the existing active cap.
        """
        from app.core.relationship.belief_interpretation import applicable

        rows = self.list_believed(user_id=user_id, limit=200)
        return [b for b in rows if (
            is_interpretation(b.metadata) or b.metadata.get("confirmation_basis") == "user"
        ) and applicable(b, session_key=session_key, user_text=user_text)][:max(0, limit)]

    def list_trusted(
        self,
        *,
        user_id: str,
        kind: str | None = None,
        limit: int = 20,
    ) -> list[Belief]:
        """Compatibility status read; prompt consumers must use list_context.

        Historical confirmation alone is not evidence certification. Expired
        rows are omitted even if maintenance has not run yet.
        """
        clauses = ["user_id = ?", "status = ?"]
        args: list[Any] = [str(user_id), STATUS_CONFIRMED]
        if kind is not None:
            kind_norm = str(kind).strip().lower()
            if kind_norm not in VALID_KINDS:
                raise ValueError(
                    f"invalid kind filter {kind!r} (valid: {sorted(VALID_KINDS)})"
                )
            clauses.append("kind = ?")
            args.append(kind_norm)
        args.append(int(limit))
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM beliefs "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY confidence DESC, observed_at DESC LIMIT ?",
            tuple(args),
        ).fetchall()
        return [b for b in map(_row_to_belief, rows) if is_current(b)]

    def list_believed(
        self,
        *,
        user_id: str,
        kind: str | None = None,
        limit: int = 200,
    ) -> list[Belief]:
        """Everything Aiko currently believes — ``active`` **and**
        ``confirmed``.

        The gap detector reads this rather than ``list_active`` so that
        corroborating a belief does not exempt it from ever being
        checked again. Under the old split, marking a belief correct
        moved it out of the only pool anything read, so a confirmed
        belief could not be contradicted no matter what the user later
        said.
        """
        clauses = ["user_id = ?", f"status IN ({_BELIEVED_PLACEHOLDERS})"]
        args: list[Any] = [str(user_id), *sorted(BELIEVED_STATUSES)]
        if kind is not None:
            kind_norm = str(kind).strip().lower()
            if kind_norm not in VALID_KINDS:
                raise ValueError(
                    f"invalid kind filter {kind!r} (valid: {sorted(VALID_KINDS)})"
                )
            clauses.append("kind = ?")
            args.append(kind_norm)
        args.append(int(limit))
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM beliefs "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY observed_at DESC LIMIT ?",
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
        """Believed rows newer than ``since_iso`` (for gap detection).

        Returns mood beliefs by default; the detector only cares about
        rows whose valence is set so we filter that here. Covers both
        ``active`` and ``confirmed`` — see :meth:`list_believed`.
        """
        kind_norm = str(kind).strip().lower()
        if kind_norm not in VALID_KINDS:
            raise ValueError(
                f"invalid kind {kind!r} (valid: {sorted(VALID_KINDS)})"
            )
        clauses = ["user_id = ?", "kind = ?", f"status IN ({_BELIEVED_PLACEHOLDERS})"]
        args: list[Any] = [str(user_id), kind_norm, *sorted(BELIEVED_STATUSES)]
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

    def count_by_status(self, *, user_id: str | None = None,
                        interpretations_only: bool = False) -> dict[str, int]:
        out = {s: 0 for s in VALID_STATUSES}
        clauses = []
        args = []
        if user_id is not None:
            clauses.append("user_id = ?")
            args.append(str(user_id))
        if interpretations_only:
            clauses.append("CASE WHEN json_valid(metadata) THEN "
                           "json_extract(metadata, '$.interpretation_version') END = 2")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self._db._get_conn().execute(
            "SELECT status, COUNT(*) FROM beliefs" + where + " GROUP BY status", args,
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
        """Expire active AND confirmed readings by support, never last inspection."""
        conn = self._db._get_conn()
        user_clause = " AND user_id = ?" if user_id is not None else ""
        args = (user_id,) if user_id is not None else ()
        rows = conn.execute(
            f"SELECT {_SELECT_COLS} FROM beliefs WHERE status IN ('active', 'confirmed')"
            + user_clause, args,
        ).fetchall()
        now = timephrase.utcnow()
        stale_ids = [b.id for b in map(_row_to_belief, rows)
                     if not is_current(b, now) or b.observed_at < cutoff_iso]
        if not stale_ids:
            return 0
        conn.executemany("UPDATE beliefs SET status = ? WHERE id = ?",
                         [(STATUS_STALE, bid) for bid in stale_ids])
        conn.commit()
        return len(stale_ids)

    def prune_to_cap(self, *, user_id: str, cap: int) -> int:
        """Retire excess active readings without deleting their receipts.

        Legacy fragments yield before structured interpretations, then the
        oldest support yields first. Model confidence cannot crowd a fresh
        tentative interpretation out of a backlog of old confident fragments.
        Confirmed/contradicted/stale history is outside this active-pool cap.
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
            "UPDATE beliefs SET status = ? WHERE id IN ("
            "  SELECT id FROM beliefs "
            "  WHERE user_id = ? AND status = ? "
            "  ORDER BY CASE WHEN json_valid(metadata) THEN "
            "    COALESCE(json_extract(metadata, '$.interpretation_version') = 2, 0) "
            "    ELSE 0 END ASC, observed_at ASC, id ASC LIMIT ?"
            ")",
            (STATUS_STALE, str(user_id), STATUS_ACTIVE, excess),
        )
        conn.commit()
        return int(cursor.rowcount or 0)
