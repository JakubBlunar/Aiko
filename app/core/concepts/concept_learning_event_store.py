"""Append-only store for L17c learning events -- what changed, and why.

This is the durable spine of Aiko's history of her own understanding, and
the layer L19's autobiography traverses. It is deliberately a different
table from :mod:`app.core.concepts.concept_event_store`:

- ``concept_events`` is a per-concept **lifecycle log**. Every promotion,
  decay sample and merge lands there, written by whichever worker made
  the move. It answers "what happened to this row".
- ``concept_learning_events`` is the much rarer **causal record**,
  written only when the L17b classifier judges a change to be real
  evolution. It answers "what did she come to believe instead, and on the
  strength of what".

Three properties the rest of the system depends on:

- **Append-only, never pruned.** There is no ``update`` and no
  ``delete``. A retired self-concept is part of the story, not garbage.
- **Snapshot-truthful.** Labels and evidence text are captured at
  detection time, so an entry stays readable after the concepts and
  memories behind it are deleted, merged, or pruned.
- **Idempotent.** ``fingerprint`` is UNIQUE and writes use
  ``INSERT OR IGNORE``, so re-running the classifier over history it has
  already seen is absorbed silently rather than duplicating the past.

Never raises: every method logs and degrades to an empty result, because
losing a history write must not be able to break a worker tick.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.concepts.concept_drift import DriftFinding
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.concept_learning_events")

_COLS = (
    "id, fingerprint, shape, concept_id, prior_concept_id, kind, subject, "
    "old_label, new_label, because, resolution, salience, plasticity, "
    "confidence_delta, cosine, decisive_event_id, trigger_event_ids, "
    "evidence_refs, evidence_labels, created_at"
)

# How deep an absorption chain may be followed before we assume a cycle.
# Merges are rare and chains are shallow; this only exists so a corrupt
# row cannot spin a read forever.
_MAX_ALIAS_HOPS = 16


def _now_iso() -> str:
    return timephrase.utcnow().isoformat()


def _dump(value: Any) -> str:
    try:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return ""


def _load(raw: Any, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError):
        return fallback


@dataclass(slots=True)
class LearningEvent:
    """One recorded change in what Aiko understands."""

    shape: str = "emergence"
    concept_id: int | None = None
    prior_concept_id: int | None = None
    kind: str = "identity"
    subject: str = "user"
    old_label: str = ""
    new_label: str = ""
    because: str = ""
    resolution: str = ""
    salience: float = 0.0
    plasticity: float = 0.0
    confidence_delta: float = 0.0
    cosine: float | None = None
    decisive_event_id: int = 0
    trigger_event_ids: tuple[int, ...] = ()
    evidence_refs: tuple[tuple[str, str], ...] = ()
    evidence_labels: tuple[str, ...] = ()
    fingerprint: str = ""
    created_at: str = ""
    event_id: int = 0

    @classmethod
    def from_finding(
        cls,
        finding: "DriftFinding",
        *,
        evidence_labels: Sequence[str] = (),
    ) -> "LearningEvent":
        """Adapt a pure L17b finding, attaching resolved evidence text.

        The labels are resolved by the caller (which owns the stores) and
        frozen here, because the whole point of the snapshot is that it
        survives the disappearance of what it describes.
        """
        return cls(
            shape=finding.shape,
            concept_id=finding.concept_id,
            prior_concept_id=finding.prior_concept_id,
            kind=finding.kind,
            subject=finding.subject,
            old_label=finding.old_label,
            new_label=finding.new_label,
            because=finding.because,
            resolution=finding.resolution,
            salience=float(finding.salience),
            plasticity=float(finding.plasticity),
            confidence_delta=float(finding.confidence_delta),
            cosine=finding.cosine,
            decisive_event_id=int(finding.decisive_event_id),
            trigger_event_ids=tuple(finding.trigger_event_ids),
            evidence_refs=tuple(finding.evidence_refs),
            evidence_labels=tuple(
                str(label).strip() for label in evidence_labels if str(label).strip()
            ),
            fingerprint=finding.fingerprint(),
            created_at=finding.detected_at,
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe view for the REST / MCP debug surfaces."""
        return {
            "id": self.event_id,
            "fingerprint": self.fingerprint,
            "shape": self.shape,
            "concept_id": self.concept_id,
            "prior_concept_id": self.prior_concept_id,
            "kind": self.kind,
            "subject": self.subject,
            "old_label": self.old_label,
            "new_label": self.new_label,
            "because": self.because,
            "resolution": self.resolution,
            "salience": round(float(self.salience), 4),
            "plasticity": round(float(self.plasticity), 4),
            "confidence_delta": round(float(self.confidence_delta), 4),
            "cosine": (
                round(float(self.cosine), 4) if self.cosine is not None else None
            ),
            "decisive_event_id": self.decisive_event_id,
            "trigger_event_ids": list(self.trigger_event_ids),
            "evidence_refs": [[t, i] for t, i in self.evidence_refs],
            "evidence_labels": list(self.evidence_labels),
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class ConceptAlias:
    """One absorption: ``absorbed_id`` was merged into ``canonical_id``."""

    absorbed_id: int
    canonical_id: int
    absorbed_label: str = ""
    kind: str = ""
    subject: str = ""
    merged_at: str = ""


@dataclass(slots=True)
class ProvenanceBundle:
    """Everything known about how one belief got to where it is."""

    concept_id: int
    resolved_id: int
    alias_chain: list[int] = field(default_factory=list)
    learning_events: list[dict[str, Any]] = field(default_factory=list)
    lifecycle: list[dict[str, Any]] = field(default_factory=list)
    prior_labels: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "concept_id": self.concept_id,
            "resolved_id": self.resolved_id,
            "alias_chain": list(self.alias_chain),
            "learning_events": list(self.learning_events),
            "lifecycle": list(self.lifecycle),
            "prior_labels": list(self.prior_labels),
        }


class ConceptLearningEventStore:
    """Append-only CRUD over ``concept_learning_events`` + the alias map."""

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    # ── writes (append-only) ──────────────────────────────────────────

    def add(self, event: LearningEvent) -> int:
        """Insert one learning event. Returns its id, or ``0`` when the
        fingerprint was already present (the idempotent no-op) or the
        write failed."""
        if not event.fingerprint:
            return 0
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        event.created_at = event.created_at or _now_iso()
        try:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO concept_learning_events "
                "(fingerprint, shape, concept_id, prior_concept_id, kind, "
                " subject, old_label, new_label, because, resolution, "
                " salience, plasticity, confidence_delta, cosine, "
                " decisive_event_id, trigger_event_ids, evidence_refs, "
                " evidence_labels, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(event.fingerprint),
                    str(event.shape),
                    (
                        int(event.concept_id)
                        if event.concept_id is not None else None
                    ),
                    (
                        int(event.prior_concept_id)
                        if event.prior_concept_id is not None else None
                    ),
                    str(event.kind),
                    str(event.subject),
                    str(event.old_label or ""),
                    str(event.new_label or ""),
                    str(event.because or ""),
                    str(event.resolution or ""),
                    float(event.salience),
                    float(event.plasticity),
                    float(event.confidence_delta),
                    (
                        float(event.cosine)
                        if event.cosine is not None else None
                    ),
                    int(event.decisive_event_id),
                    _dump([int(i) for i in event.trigger_event_ids]),
                    _dump([[str(t), str(i)] for t, i in event.evidence_refs]),
                    _dump([str(label) for label in event.evidence_labels]),
                    event.created_at,
                ),
            )
            conn.commit()
        except Exception:
            log.warning("learning event insert failed", exc_info=True)
            return 0
        if not cursor.rowcount:
            return 0
        event.event_id = int(cursor.lastrowid or 0)
        return event.event_id

    def add_many(self, events: Sequence[LearningEvent]) -> int:
        """Insert a batch, returning how many were genuinely new."""
        return sum(1 for event in events if self.add(event) > 0)

    # ── reads ─────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_event(r: tuple) -> LearningEvent:
        refs = _load(r[17], [])
        return LearningEvent(
            event_id=int(r[0]),
            fingerprint=str(r[1] or ""),
            shape=str(r[2] or ""),
            concept_id=(int(r[3]) if r[3] is not None else None),
            prior_concept_id=(int(r[4]) if r[4] is not None else None),
            kind=str(r[5] or "identity"),
            subject=str(r[6] or "user"),
            old_label=str(r[7] or ""),
            new_label=str(r[8] or ""),
            because=str(r[9] or ""),
            resolution=str(r[10] or ""),
            salience=float(r[11] or 0.0),
            plasticity=float(r[12] or 0.0),
            confidence_delta=float(r[13] or 0.0),
            cosine=(float(r[14]) if r[14] is not None else None),
            decisive_event_id=int(r[15] or 0),
            trigger_event_ids=tuple(
                int(i) for i in _load(r[16], []) if isinstance(i, int)
            ),
            evidence_refs=tuple(
                (str(pair[0]), str(pair[1]))
                for pair in (refs if isinstance(refs, list) else [])
                if isinstance(pair, (list, tuple)) and len(pair) == 2
            ),
            evidence_labels=tuple(
                str(label) for label in _load(r[18], [])
            ),
            created_at=str(r[19] or ""),
        )

    def list(
        self,
        *,
        limit: int = 100,
        subject: str | None = None,
        shape: str | None = None,
        concept_id: int | None = None,
        min_salience: float | None = None,
        before_id: int | None = None,
    ) -> list[LearningEvent]:
        """Learning events newest-first.

        ``before_id`` pages backwards (pass the smallest id from the
        previous page). ``concept_id`` matches either endpoint, so asking
        about a belief also returns the change that superseded it.
        """
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        where: list[str] = []
        params: list[object] = []
        if subject:
            where.append("subject = ?")
            params.append(str(subject))
        if shape:
            where.append("shape = ?")
            params.append(str(shape))
        if concept_id is not None:
            where.append("(concept_id = ? OR prior_concept_id = ?)")
            params.extend((int(concept_id), int(concept_id)))
        if min_salience is not None:
            where.append("salience >= ?")
            params.append(float(min_salience))
        if before_id is not None:
            where.append("id < ?")
            params.append(int(before_id))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(max(1, int(limit)))
        try:
            rows = conn.execute(
                f"SELECT {_COLS} FROM concept_learning_events "
                f"{clause} ORDER BY created_at DESC, id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        except Exception:
            log.warning("learning event list failed", exc_info=True)
            return []
        return [self._row_to_event(r) for r in rows]

    def history_for(
        self, concept_id: int, *, limit: int = 100
    ) -> list[LearningEvent]:
        """One belief's learning events **oldest-first** -- read as a story.

        Follows the alias chain first, so asking about a concept that was
        merged away still returns the history that continued under its
        canonical row.
        """
        resolved = self.resolve_alias(concept_id)
        ids = {int(concept_id), int(resolved)}
        placeholders = ",".join("?" * len(ids))
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        params: list[object] = [*ids, *ids, max(1, int(limit))]
        try:
            rows = conn.execute(
                f"SELECT {_COLS} FROM concept_learning_events "
                f"WHERE concept_id IN ({placeholders}) "
                f"   OR prior_concept_id IN ({placeholders}) "
                "ORDER BY created_at ASC, id ASC LIMIT ?",
                tuple(params),
            ).fetchall()
        except Exception:
            log.warning("learning history read failed", exc_info=True)
            return []
        return [self._row_to_event(r) for r in rows]

    def latest_id(self) -> int:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            row = conn.execute(
                "SELECT MAX(id) FROM concept_learning_events"
            ).fetchone()
        except Exception:
            return 0
        return int(row[0] or 0) if row else 0

    def count(self) -> int:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM concept_learning_events"
            ).fetchone()
        except Exception:
            return 0
        return int(row[0]) if row else 0

    def counts_by_shape(self) -> dict[str, int]:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            rows = conn.execute(
                "SELECT shape, COUNT(*) FROM concept_learning_events "
                "GROUP BY shape"
            ).fetchall()
        except Exception:
            return {}
        return {str(r[0] or ""): int(r[1] or 0) for r in rows}

    def has_fingerprint(self, fingerprint: str) -> bool:
        if not fingerprint:
            return False
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            row = conn.execute(
                "SELECT 1 FROM concept_learning_events WHERE fingerprint = ?",
                (str(fingerprint),),
            ).fetchone()
        except Exception:
            return False
        return row is not None

    # ── identity continuity (the alias map) ───────────────────────────

    def record_alias(self, alias: ConceptAlias) -> bool:
        """Record that ``absorbed_id`` was fused into ``canonical_id``.

        Called from inside ``ConceptStore.merge_into`` *before* the
        absorbed row is deleted, which is the only moment its label and
        identity are still knowable.
        """
        if alias.absorbed_id <= 0 or alias.canonical_id <= 0:
            return False
        if alias.absorbed_id == alias.canonical_id:
            return False
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            conn.execute(
                "INSERT OR REPLACE INTO concept_aliases "
                "(absorbed_id, canonical_id, absorbed_label, kind, subject, "
                " merged_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    int(alias.absorbed_id),
                    int(alias.canonical_id),
                    str(alias.absorbed_label or ""),
                    str(alias.kind or ""),
                    str(alias.subject or ""),
                    alias.merged_at or _now_iso(),
                ),
            )
            conn.commit()
        except Exception:
            log.warning("concept alias write failed", exc_info=True)
            return False
        return True

    def resolve_alias(self, concept_id: int) -> int:
        """Follow the absorption chain to the id that is still live.

        Returns ``concept_id`` unchanged when it was never absorbed.
        Chains (A into B, later B into C) are followed transitively and
        capped, so a corrupt self-referential row degrades to a stop
        rather than a hang.
        """
        current = int(concept_id)
        if current <= 0:
            return current
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        seen = {current}
        for _hop in range(_MAX_ALIAS_HOPS):
            try:
                row = conn.execute(
                    "SELECT canonical_id FROM concept_aliases "
                    "WHERE absorbed_id = ?",
                    (current,),
                ).fetchone()
            except Exception:
                log.debug("alias resolve failed", exc_info=True)
                return current
            if row is None:
                return current
            nxt = int(row[0] or 0)
            if nxt <= 0 or nxt in seen:
                return current
            seen.add(nxt)
            current = nxt
        return current

    def alias_chain(self, concept_id: int) -> list[int]:
        """Every id this belief has lived under, oldest first."""
        chain = [int(concept_id)]
        resolved = self.resolve_alias(concept_id)
        if resolved != int(concept_id):
            chain.append(resolved)
        return chain

    def absorbed_into(self, canonical_id: int) -> list[ConceptAlias]:
        """The absorptions this concept is the surviving side of."""
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            rows = conn.execute(
                "SELECT absorbed_id, canonical_id, absorbed_label, kind, "
                "       subject, merged_at FROM concept_aliases "
                "WHERE canonical_id = ? ORDER BY merged_at ASC",
                (int(canonical_id),),
            ).fetchall()
        except Exception:
            log.debug("alias lookup failed", exc_info=True)
            return []
        return [
            ConceptAlias(
                absorbed_id=int(r[0]),
                canonical_id=int(r[1]),
                absorbed_label=str(r[2] or ""),
                kind=str(r[3] or ""),
                subject=str(r[4] or ""),
                merged_at=str(r[5] or ""),
            )
            for r in rows
        ]


__all__ = [
    "ConceptAlias",
    "ConceptLearningEventStore",
    "LearningEvent",
    "ProvenanceBundle",
]
