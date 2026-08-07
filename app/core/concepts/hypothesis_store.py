"""Persistence for invented hypotheses (schema v33, L30 Phase B).

A **hypothesis** is something Aiko has *guessed*. That is not a weaker
concept, it is a different kind of object, and the separate table is the
reason the rest of the system can stay simple.

Why not a ``concepts`` row with a new status
--------------------------------------------
Because every guarantee the concept layer makes would have to be
re-checked. A ``concepts`` row is *inferred from evidence that already
exists* — L2 reads clusters and memories and proposes an abstraction over
them. A hypothesis may rest on nothing at all: the ``free`` origin is
deliberate speculation, and it is the point of the layer rather than a
degenerate case. Letting one of those sit in ``concepts`` would mean
every reader that trusts the graph — the T0 profile block,
``ConceptView.core``, ``_stable_rank``, the L3 lifecycle engine — needs a
new exclusion, and one missed check puts an invention into the prompt as
something Aiko believes about the user. A separate table makes that
mistake impossible to make by omission: an invention reaches the concept
graph only by being *written* there, at graduation, on purpose.

Credence is not confidence
--------------------------
They answer different questions and could not share a column even if the
tables were merged. ``concepts.confidence`` is "how well evidenced is
this", it is derived from the evidence the graph can see, and L3 is its
single writer. ``credence`` here is "how likely do I think this is" — it
begins as the proposer's own subjective bet on something with no evidence
whatsoever, and it moves only when an answer comes back.

Which is also why this store has no lifecycle worker on the scale of L3.
There is no decay: an untested guess has not become less plausible with
time, it has just gone stale, so hygiene is a TTL that expires the row
rather than a curve that erodes a number.

The link field, and the race it settles
---------------------------------------
``linked_concept_id`` looks like a nicety and is not. A confirmed
hypothesis stores its answer as an ordinary memory; that memory gets
clustered, and L2 proposes a concept from it knowing nothing about the
hypothesis. L2 usually wins the race, because it needs *one* confirmation
where graduation needs two — so "this turned out to be something I
already believe" is the normal ending, not the exceptional one. Stamping
the link at the first confirmation is what lets the lane stop surfacing
the guess and the concept as two separate open questions.

Statuses
--------
``open`` → ``supported`` / ``refuted``, then one of three exits:
``graduated`` (minted a new candidate concept), ``merged`` (folded into a
concept that already existed), or ``expired`` (TTL). ``merged`` is
deliberately distinct from ``graduated`` because "my guess was already
true" and "my guess became a new belief" are different stories, and the
L17f diary and L19 autobiography should be able to narrate them
differently.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Sequence

import numpy as np

from app.core.infra import timephrase

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.hypothesis_store")


#: Still being wondered about.
STATUS_OPEN = "open"
#: At least one confirmation, not yet enough to graduate.
STATUS_SUPPORTED = "supported"
#: The user said no. Kept rather than deleted so the proposer's novelty
#: check can see it and not re-invent the same wrong guess next week.
STATUS_REFUTED = "refuted"
#: Proved itself and became a new candidate concept.
STATUS_GRADUATED = "graduated"
#: Proved itself and turned out to be a concept that already existed.
STATUS_MERGED = "merged"
#: Never tested, and now stale.
STATUS_EXPIRED = "expired"

#: Statuses that still occupy a slot in the open-count cap.
LIVE_STATUSES: frozenset[str] = frozenset({STATUS_OPEN, STATUS_SUPPORTED})
#: Statuses past the point of change.
CLOSED_STATUSES: frozenset[str] = frozenset(
    {STATUS_REFUTED, STATUS_GRADUATED, STATUS_MERGED, STATUS_EXPIRED}
)

#: Where the guess came from. ``free`` is unreferenced speculation, and
#: it is the origin the layer exists to allow.
ORIGIN_EXTRAPOLATION = "extrapolation"
ORIGIN_MEMORY = "memory"
ORIGIN_CONCEPT = "concept"
ORIGIN_FREE = "free"
ORIGINS: frozenset[str] = frozenset(
    {ORIGIN_EXTRAPOLATION, ORIGIN_MEMORY, ORIGIN_CONCEPT, ORIGIN_FREE}
)

#: The concept subjects, plus one of our own. A guess about how something
#: *works* is a legitimate thing to wonder and has no concept kind to
#: graduate into, so it exits as a durable memory instead.
SUBJECT_WORLD = "world"

_COLS = (
    "id, statement, kind, subject, user_id, rationale, origin, "
    "origin_refs, credence, support_count, refute_count, status, "
    "embedding, dim, asked_count, origin_session, created_at, "
    "updated_at, last_tested_at, closed_at, linked_concept_id, "
    "graduated_concept_id, graduated_memory_id, answer_memory_ids"
)


def _int_list(raw: Any) -> list[int]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    out: list[int] = []
    for item in parsed:
        try:
            out.append(int(item))
        except (ValueError, TypeError):
            continue
    return out


def _now_iso() -> str:
    return timephrase.utcnow().isoformat()


def _encode_embedding(vec: "np.ndarray | None") -> tuple[bytes, int]:
    if vec is None:
        return b"", 0
    arr = np.asarray(vec, dtype=np.float32).ravel()
    return arr.tobytes(), int(arr.size)


def _decode_embedding(blob: "bytes | None", dim: int) -> np.ndarray:
    if not blob or dim <= 0:
        return np.zeros(0, dtype=np.float32)
    arr = np.frombuffer(blob, dtype=np.float32)
    if arr.size != dim:
        # Dimension drift (embedding-model swap): treat as absent so the
        # row is re-embedded rather than crashing a cosine matmul.
        return np.zeros(0, dtype=np.float32)
    return np.array(arr, dtype=np.float32)


def _unit(vec: "np.ndarray | None") -> np.ndarray:
    if vec is None:
        return np.zeros(0, dtype=np.float32)
    arr = np.asarray(vec, dtype=np.float32).ravel()
    if arr.size == 0:
        return arr
    norm = float(np.linalg.norm(arr))
    if norm <= 0.0:
        return np.zeros(0, dtype=np.float32)
    return arr / norm


@dataclass(slots=True)
class Hypothesis:
    """One thing Aiko has guessed but not established."""

    statement: str
    kind: str = "identity"
    subject: str = "user"
    user_id: str | None = None
    rationale: str = ""
    origin: str = ORIGIN_FREE
    #: Ids the guess leaned on, shaped by ``origin``. Empty for ``free``.
    origin_refs: list[int] = field(default_factory=list)
    #: "How likely I think this is" — see the module docstring on why this
    #: is not ``concepts.confidence``.
    credence: float = 0.5
    support_count: int = 0
    refute_count: int = 0
    status: str = STATUS_OPEN
    embedding: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.float32)
    )
    asked_count: int = 0
    #: Memories the user's own answers were stored as. The only evidence
    #: an invented belief ever gathers, and what a graduated concept is
    #: built on — which is why they are remembered rather than found
    #: again later by similarity.
    answer_memory_ids: list[int] = field(default_factory=list)
    origin_session: str | None = None
    created_at: str = ""
    updated_at: str = ""
    last_tested_at: str | None = None
    closed_at: str | None = None
    #: Set as soon as a confirmation reveals the belief already exists as
    #: a concept. Non-null means the concept speaks for this belief now.
    linked_concept_id: int | None = None
    graduated_concept_id: int | None = None
    graduated_memory_id: int | None = None
    hypothesis_id: int = 0

    @property
    def is_live(self) -> bool:
        return self.status in LIVE_STATUSES

    @property
    def is_world(self) -> bool:
        return self.subject == SUBJECT_WORLD


class HypothesisStore:
    """CRUD plus an in-process cosine mirror for ``hypotheses``.

    Same shape as :class:`~app.core.concepts.concept_store.ConceptStore`
    and for the same reason: cardinality stays small (capped by
    ``hypothesis_max_open``), so nearest-neighbour is a tiny numpy matmul
    rather than an ANN index. The mirror is the source for the proposer's
    novelty check, which runs on every proposal and must not pay a query.
    """

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db
        self._rows: dict[int, Hypothesis] = {}
        self._vectors: dict[int, np.ndarray] = {}
        self._dirty = True
        self._live_mat: tuple[list[int], np.ndarray, int] | None = None

    # ── mirror ────────────────────────────────────────────────────────

    def _put_mirror(self, row: Hypothesis) -> None:
        self._rows[row.hypothesis_id] = row
        unit = _unit(row.embedding)
        if unit.size:
            self._vectors[row.hypothesis_id] = unit
        else:
            self._vectors.pop(row.hypothesis_id, None)
        self._dirty = True

    def _drop_mirror(self, hypothesis_id: int) -> None:
        self._rows.pop(int(hypothesis_id), None)
        self._vectors.pop(int(hypothesis_id), None)
        self._dirty = True

    def load_all(self) -> list[Hypothesis]:
        """Warm the mirror from SQLite. Called once at boot."""
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        self._rows.clear()
        self._vectors.clear()
        self._dirty = True
        try:
            rows = conn.execute(f"SELECT {_COLS} FROM hypotheses").fetchall()
        except Exception:
            log.warning("hypotheses load failed", exc_info=True)
            rows = []
        for r in rows:
            self._put_mirror(self._row_to_hypothesis(r))
        return list(self._rows.values())

    @staticmethod
    def _row_to_hypothesis(r: tuple) -> Hypothesis:
        return Hypothesis(
            hypothesis_id=int(r[0]),
            statement=str(r[1] or ""),
            kind=str(r[2] or "identity"),
            subject=str(r[3] or "user"),
            user_id=(str(r[4]) if r[4] is not None else None),
            rationale=str(r[5] or ""),
            origin=str(r[6] or ORIGIN_FREE),
            origin_refs=_int_list(r[7]),
            credence=float(r[8] or 0.0),
            support_count=int(r[9] or 0),
            refute_count=int(r[10] or 0),
            status=str(r[11] or STATUS_OPEN),
            embedding=_decode_embedding(r[12], int(r[13] or 0)),
            asked_count=int(r[14] or 0),
            origin_session=(str(r[15]) if r[15] is not None else None),
            created_at=str(r[16] or ""),
            updated_at=str(r[17] or ""),
            last_tested_at=(str(r[18]) if r[18] is not None else None),
            closed_at=(str(r[19]) if r[19] is not None else None),
            linked_concept_id=(int(r[20]) if r[20] is not None else None),
            graduated_concept_id=(int(r[21]) if r[21] is not None else None),
            graduated_memory_id=(int(r[22]) if r[22] is not None else None),
            answer_memory_ids=_int_list(r[23] if len(r) > 23 else None),
        )

    # ── reads ─────────────────────────────────────────────────────────

    def get(self, hypothesis_id: int) -> Hypothesis | None:
        return self._rows.get(int(hypothesis_id))

    def all(self) -> list[Hypothesis]:
        return list(self._rows.values())

    def list_by(
        self,
        *,
        status: str | None = None,
        subject: str | None = None,
        kind: str | None = None,
        origin: str | None = None,
        live: bool = False,
        linked: bool | None = None,
    ) -> list[Hypothesis]:
        """Filter the mirror. ``live=True`` means open or supported."""
        out = [
            h
            for h in self._rows.values()
            if (status is None or h.status == status)
            and (subject is None or h.subject == subject)
            and (kind is None or h.kind == kind)
            and (origin is None or h.origin == origin)
            and (not live or h.is_live)
            and (
                linked is None
                or (h.linked_concept_id is not None) is bool(linked)
            )
        ]
        out.sort(key=lambda h: -int(h.hypothesis_id))
        return out

    def count_live(self) -> int:
        """How many hypotheses are still in play — the open-count cap."""
        return sum(1 for h in self._rows.values() if h.is_live)

    def nearest(
        self,
        query_vec: "np.ndarray",
        *,
        k: int = 5,
        live_only: bool = False,
    ) -> list[tuple[Hypothesis, float]]:
        """Up to ``k`` ``(hypothesis, cosine)`` pairs, nearest first.

        ``live_only=False`` by default because the novelty check wants to
        see refuted rows too: re-inventing a guess the user already
        rejected is exactly the repetition worth catching.
        """
        q = _unit(query_vec)
        if q.size == 0 or k <= 0 or not self._vectors:
            return []
        pairs: list[tuple[Hypothesis, float]] = []
        for hid, vec in self._vectors.items():
            row = self._rows.get(hid)
            if row is None or vec.size != q.size:
                continue
            if live_only and not row.is_live:
                continue
            pairs.append((row, float(np.dot(q, vec))))
        pairs.sort(key=lambda p: (-p[1], int(p[0].hypothesis_id)))
        return pairs[: int(k)]

    # ── writes ────────────────────────────────────────────────────────

    def add(self, row: Hypothesis) -> int:
        """Insert, populate ``hypothesis_id``, mirror, return the id."""
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        now = _now_iso()
        row.created_at = row.created_at or now
        row.updated_at = now
        blob, dim = _encode_embedding(row.embedding)
        cursor = conn.execute(
            "INSERT INTO hypotheses "
            "(statement, kind, subject, user_id, rationale, origin, "
            " origin_refs, credence, support_count, refute_count, status, "
            " embedding, dim, asked_count, origin_session, created_at, "
            " updated_at, last_tested_at, closed_at, linked_concept_id, "
            " graduated_concept_id, graduated_memory_id, "
            " answer_memory_ids) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?)",
            (
                str(row.statement),
                str(row.kind),
                str(row.subject),
                row.user_id,
                str(row.rationale or ""),
                str(row.origin),
                json.dumps([int(x) for x in (row.origin_refs or [])]),
                float(row.credence),
                int(row.support_count),
                int(row.refute_count),
                str(row.status),
                blob,
                int(dim),
                int(row.asked_count),
                row.origin_session,
                row.created_at,
                row.updated_at,
                row.last_tested_at,
                row.closed_at,
                row.linked_concept_id,
                row.graduated_concept_id,
                row.graduated_memory_id,
                json.dumps([int(x) for x in (row.answer_memory_ids or [])]),
            ),
        )
        conn.commit()
        row.hypothesis_id = int(cursor.lastrowid or 0)
        self._put_mirror(row)
        return row.hypothesis_id

    def update(self, row: Hypothesis) -> None:
        """Persist every mutable field and refresh the mirror."""
        if not row.hypothesis_id:
            raise ValueError("update requires a persisted hypothesis_id")
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        row.updated_at = _now_iso()
        blob, dim = _encode_embedding(row.embedding)
        try:
            conn.execute(
                "UPDATE hypotheses SET "
                "  statement = ?, kind = ?, subject = ?, user_id = ?, "
                "  rationale = ?, origin = ?, origin_refs = ?, "
                "  credence = ?, support_count = ?, refute_count = ?, "
                "  status = ?, embedding = ?, dim = ?, asked_count = ?, "
                "  origin_session = ?, updated_at = ?, last_tested_at = ?, "
                "  closed_at = ?, linked_concept_id = ?, "
                "  graduated_concept_id = ?, graduated_memory_id = ?, "
                "  answer_memory_ids = ? "
                "WHERE id = ?",
                (
                    str(row.statement),
                    str(row.kind),
                    str(row.subject),
                    row.user_id,
                    str(row.rationale or ""),
                    str(row.origin),
                    json.dumps([int(x) for x in (row.origin_refs or [])]),
                    float(row.credence),
                    int(row.support_count),
                    int(row.refute_count),
                    str(row.status),
                    blob,
                    int(dim),
                    int(row.asked_count),
                    row.origin_session,
                    row.updated_at,
                    row.last_tested_at,
                    row.closed_at,
                    row.linked_concept_id,
                    row.graduated_concept_id,
                    row.graduated_memory_id,
                    json.dumps(
                        [int(x) for x in (row.answer_memory_ids or [])]
                    ),
                    int(row.hypothesis_id),
                ),
            )
            conn.commit()
        except Exception:
            log.warning(
                "hypothesis update failed (id=%s)",
                row.hypothesis_id,
                exc_info=True,
            )
            return
        self._put_mirror(row)

    def close(
        self,
        row: Hypothesis,
        *,
        status: str,
        concept_id: int | None = None,
        memory_id: int | None = None,
    ) -> None:
        """Move a row to a terminal status and stamp its exit."""
        row.status = str(status)
        row.closed_at = _now_iso()
        if concept_id is not None:
            row.graduated_concept_id = int(concept_id)
        if memory_id is not None:
            row.graduated_memory_id = int(memory_id)
        self.update(row)

    def link(self, row: Hypothesis, concept_id: int) -> None:
        """Point a row at the concept that already carries its belief."""
        row.linked_concept_id = int(concept_id)
        self.update(row)

    def delete(self, hypothesis_id: int) -> None:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            conn.execute(
                "DELETE FROM hypotheses WHERE id = ?", (int(hypothesis_id),)
            )
            conn.commit()
        except Exception:
            log.warning(
                "hypothesis delete failed (id=%s)", hypothesis_id, exc_info=True
            )
            return
        self._drop_mirror(hypothesis_id)

    # ── hygiene ───────────────────────────────────────────────────────

    def expire_stale(self, *, ttl_hours: float, now: Any = None) -> int:
        """Close untested rows past their TTL. Returns how many.

        Only ``open`` rows with no asks age out. A row that has been put
        to the user is either settled or has a real answer pending, and a
        clock should not decide either way.
        """
        if ttl_hours <= 0:
            return 0
        moment = now or timephrase.utcnow()
        closed = 0
        for row in list(self._rows.values()):
            if row.status != STATUS_OPEN or row.asked_count > 0:
                continue
            age_h = _age_hours(row.created_at, moment)
            if age_h is None or age_h < float(ttl_hours):
                continue
            self.close(row, status=STATUS_EXPIRED)
            closed += 1
        if closed:
            log.info("hypotheses expired on TTL: n=%d", closed)
        return closed

    def counts_by_status(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self._rows.values():
            out[row.status] = out.get(row.status, 0) + 1
        return out


def _age_hours(created_at: str, now: Any) -> float | None:
    if not created_at:
        return None
    try:
        stamp = timephrase.parse_iso(created_at)
    except Exception:
        return None
    if stamp is None:
        return None
    try:
        return max(0.0, (now - stamp).total_seconds() / 3600.0)
    except Exception:
        return None


def statement_texts(rows: Sequence[Hypothesis]) -> list[str]:
    """The statements, for prompting a novelty check or a debug surface."""
    return [str(r.statement) for r in rows]


__all__ = [
    "CLOSED_STATUSES",
    "LIVE_STATUSES",
    "ORIGINS",
    "ORIGIN_CONCEPT",
    "ORIGIN_EXTRAPOLATION",
    "ORIGIN_FREE",
    "ORIGIN_MEMORY",
    "STATUS_EXPIRED",
    "STATUS_GRADUATED",
    "STATUS_MERGED",
    "STATUS_OPEN",
    "STATUS_REFUTED",
    "STATUS_SUPPORTED",
    "SUBJECT_WORLD",
    "Hypothesis",
    "HypothesisStore",
    "statement_texts",
]
