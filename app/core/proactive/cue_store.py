"""The cue pool: things Aiko has not said yet, and whether she said them.

A cue is not a memory. It is a conversational move she is holding --
a topic gone quiet she might reopen, a gap she noticed in what she knows,
an association she wants to follow. Until now each of the seven workers
that produce these kept its own bookkeeping, in three mutually
incompatible shapes: a ``kv_meta`` JSON ring plus a ``surfaced_keys`` set,
a ring plus a watermark plus an in-memory slot, or rows in ``memories``.

None of those could answer the two questions that matter.

**How much stock do I have?** A ring says what is newest, not what is
unspent. So a worker could not stay dormant while cues were already
waiting, and every one of them leaned on a hand-picked daily cap to stop
itself instead -- two a day here, three there, none of them with any
evidence behind them. With the pool a worker counts its pending rows and
reports pressure from the *deficit*, so a full shelf means it is simply
not admitted.

**Was it used?** The watermark advanced the moment a provider rendered
the block. A cue Aiko ignored was retired exactly like one she acted on,
which made the entire ring a write-only log of good intentions. Here a
cue moves ``pending -> surfaced`` when it reaches the prompt and only
reaches ``used`` when post-turn matching finds its subject in what was
actually said -- otherwise it comes back to ``pending`` for another try,
bounded by the two counters described below.

States
------
``pending`` -> ``surfaced`` -> ``used`` | ``awaiting`` -> ``used``, with
``expired`` and ``superseded`` as the two ways out that were nobody's
choice. ``awaiting`` exists because for some cue types Aiko saying the
thing is not the end of it: if she asks about X and the answer never
comes, the curiosity is not satisfied and the cue must survive. See
``CuePolicy.fulfilment`` in :mod:`app.core.proactive.cue_accounting`.

Two counters, not one
---------------------
``surfaced_count`` counts turns where the cue sat in the prompt and Aiko
did not raise it. ``ask_count`` counts times she raised it and got no
answer. They bound different failure modes -- the model ignoring the cue
versus the user not biting -- and collapsing them into one counter would
make both invisible. Either one exhausting sends the cue to ``expired``,
which is what makes the retry loop safe even when the matcher is wrong
every single time.

Never raises on a read. A cue worker running on the idle scheduler and a
provider rendering mid-turn both call into here, and neither has anything
useful to do with a database exception.
"""
from __future__ import annotations

import json
import logging
import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.core.infra import timephrase

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.cue_store")


# ── states ────────────────────────────────────────────────────────────

STATE_PENDING = "pending"
STATE_SURFACED = "surfaced"
STATE_AWAITING = "awaiting"
STATE_USED = "used"
STATE_EXPIRED = "expired"
STATE_SUPERSEDED = "superseded"

# The states a cue can still come back from. Everything else is terminal,
# and terminal rows are kept rather than deleted: "she was offered this
# four times and never took it" is the satisfaction evidence P45 needs,
# and it only exists if the losers stay on the table.
LIVE_STATES: frozenset[str] = frozenset({
    STATE_PENDING, STATE_SURFACED, STATE_AWAITING,
})
TERMINAL_STATES: frozenset[str] = frozenset({
    STATE_USED, STATE_EXPIRED, STATE_SUPERSEDED,
})
VALID_STATES: frozenset[str] = LIVE_STATES | TERMINAL_STATES

# Terminal is not one thing, and a consumer that holds a *derived* copy
# of a cue has to know which kind of terminal it is looking at. Two of
# the three mean the subject is settled -- ``used`` because it came up,
# ``superseded`` because the row was merged into another -- so anything
# built on that cue should retire with it. ``expired`` means the
# opposite: she was offered it, the offer ran out, and she never bit.
#
# H29 is what happens when the difference is ignored. All 110 expired
# ``curiosity_seed`` rows on this install died at ``max_surfacings``, at
# exactly two showings, a median 2.9 hours after birth -- so a wants
# ledger that retired a want whenever its seed left ``LIVE_STATES``
# retired every want in an afternoon, against a pressure mechanic that
# needs 19 hours to reach the first bar and 53 to reach the second.
RESOLVED_STATES: frozenset[str] = frozenset({
    STATE_USED, STATE_SUPERSEDED,
})


_COLS = (
    "id, user_id, cue_type, subject, text, payload, state, "
    "surfaced_count, ask_count, last_surfaced_at, last_asked_at, "
    "not_before, created_at, expires_at, used_at, used_evidence"
)
_COLS_EMB = _COLS + ", embedding"


def _now_iso() -> str:
    return timephrase.utcnow().isoformat()


def _stamp(when: datetime | None) -> str:
    return (when or timephrase.utcnow()).isoformat()


def normalise_subject(subject: str) -> str:
    """Canonical form of a cue's subject.

    The subject is a key before it is a display string -- it decides
    supersession and it is what consumption matches against -- so it is
    folded to lowercase with runs of whitespace collapsed. Cue subjects
    are already topic slugs and cluster labels in practice, so this is
    almost always a no-op; it exists so that ``Film Photography`` arriving
    from one worker and ``film photography`` from another are one subject
    rather than two competing cues.
    """
    return " ".join(str(subject or "").split()).lower()


def _encode_vec(vec: Sequence[float] | None) -> bytes | None:
    """Pack a vector the way ``memories.embedding`` is packed."""
    if vec is None:
        return None
    try:
        values = [float(x) for x in vec]
    except (TypeError, ValueError):
        return None
    if not values:
        return None
    return struct.pack(f"{len(values)}f", *values)


def _decode_vec(blob: Any) -> list[float] | None:
    if not blob:
        return None
    try:
        count = len(blob) // 4
        return list(struct.unpack(f"{count}f", blob))
    except Exception:
        return None


def _encode_payload(payload: Any) -> str | None:
    if not payload:
        return None
    try:
        return json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return None


def _decode_payload(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        out = json.loads(raw)
    except Exception:
        return {}
    return out if isinstance(out, dict) else {}


@dataclass(slots=True)
class CueRow:
    """One row of ``cue_pool``."""

    id: int
    user_id: str
    cue_type: str
    subject: str
    text: str
    payload: dict[str, Any] = field(default_factory=dict)
    state: str = STATE_PENDING
    surfaced_count: int = 0
    ask_count: int = 0
    last_surfaced_at: str | None = None
    last_asked_at: str | None = None
    not_before: str | None = None
    created_at: str = ""
    expires_at: str | None = None
    used_at: str | None = None
    used_evidence: str | None = None
    # Only populated by reads that asked for it -- see ``with_embedding``.
    embedding: list[float] | None = None

    def as_dict(self) -> dict[str, Any]:
        """Shape returned by the REST route and the MCP tools.

        The embedding is deliberately omitted: it is 768 floats of no use
        to any reader of this dict, and including it would make the cue
        list response an order of magnitude larger than its content.
        """
        return {
            "id": self.id,
            "cue_type": self.cue_type,
            "subject": self.subject,
            "text": self.text,
            "payload": dict(self.payload),
            "state": self.state,
            "surfaced_count": self.surfaced_count,
            "ask_count": self.ask_count,
            "last_surfaced_at": self.last_surfaced_at,
            "last_asked_at": self.last_asked_at,
            "not_before": self.not_before,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "used_at": self.used_at,
            "used_evidence": self.used_evidence,
        }


def _to_row(raw: Sequence[Any], *, with_embedding: bool) -> CueRow:
    row = CueRow(
        id=int(raw[0] or 0),
        user_id=str(raw[1] or ""),
        cue_type=str(raw[2] or ""),
        subject=str(raw[3] or ""),
        text=str(raw[4] or ""),
        payload=_decode_payload(raw[5]),
        state=str(raw[6] or STATE_PENDING),
        surfaced_count=int(raw[7] or 0),
        ask_count=int(raw[8] or 0),
        last_surfaced_at=raw[9],
        last_asked_at=raw[10],
        not_before=raw[11],
        created_at=str(raw[12] or ""),
        expires_at=raw[13],
        used_at=raw[14],
        used_evidence=raw[15],
    )
    if with_embedding and len(raw) > 16:
        row.embedding = _decode_vec(raw[16])
    return row


class CueStore:
    """Reads and writes the ``cue_pool`` table for one user."""

    def __init__(
        self,
        db: "ChatDatabase",
        *,
        user_id: str = "default",
        embedder: Any = None,
    ) -> None:
        self._db = db
        self._user_id = str(user_id or "default")
        # Optional, and wired here rather than into each of the six
        # producing workers: every one of them would need the same
        # plumbing to reach the same embedder, and the subject is the only
        # thing being embedded. Production happens while the user is idle,
        # so the round-trip is free in the sense that matters.
        self._embedder = embedder

    @property
    def user_id(self) -> str:
        return self._user_id

    def _conn(self):
        return self._db._get_conn()  # type: ignore[attr-defined]

    # ── writes ────────────────────────────────────────────────────────

    def add(
        self,
        cue_type: str,
        subject: str,
        text: str,
        *,
        payload: dict[str, Any] | None = None,
        ttl_hours: float | None = None,
        embedding: Sequence[float] | None = None,
        now: datetime | None = None,
        hold_hours: float = 0.0,
    ) -> int:
        """Queue a cue, retiring any live cue about the same subject.

        Supersession is across cue *types*, not within one. Two cues about
        film photography are the same conversational move whichever worker
        noticed it, and letting both queue would have Aiko raise the
        subject twice from two angles -- which reads as a loop rather than
        as depth. The newer cue wins because its framing is built from
        fresher context.

        ``hold_hours`` seals the cue for a while by setting ``not_before``
        at insert. The column already gates every read, and ``release()``
        already writes it after an unanswered ask -- this closes the
        write-side gap for a cue that has to *ripen* rather than cool
        down. A tease banked and collected in the same sitting is a
        comeback, not a callback.

        Returns the new row id, or 0 if the write failed or the input was
        unusable.
        """
        cue_type = str(cue_type or "").strip()
        subject_key = normalise_subject(subject)
        text = str(text or "").strip()
        if not cue_type or not subject_key or not text:
            return 0
        if embedding is None:
            embedding = self._embed_subject(subject_key)
        when = now or timephrase.utcnow()
        created = when.isoformat()
        expires = (
            (when + timedelta(hours=float(ttl_hours))).isoformat()
            if ttl_hours and float(ttl_hours) > 0
            else None
        )
        hold = (
            (when + timedelta(hours=float(hold_hours))).isoformat()
            if hold_hours and float(hold_hours) > 0
            else None
        )
        try:
            conn = self._conn()
            conn.execute(
                "UPDATE cue_pool SET state = ? "
                "WHERE user_id = ? AND subject = ? AND state IN "
                f"({','.join('?' * len(LIVE_STATES))})",
                (
                    STATE_SUPERSEDED,
                    self._user_id,
                    subject_key,
                    *sorted(LIVE_STATES),
                ),
            )
            cursor = conn.execute(
                "INSERT INTO cue_pool "
                "(user_id, cue_type, subject, text, payload, state, "
                " created_at, expires_at, not_before, embedding) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._user_id,
                    cue_type,
                    subject_key,
                    text,
                    _encode_payload(payload),
                    STATE_PENDING,
                    created,
                    expires,
                    hold,
                    _encode_vec(embedding),
                ),
            )
            conn.commit()
        except Exception:
            log.warning("cue_pool insert failed: type=%s", cue_type, exc_info=True)
            return 0
        return int(cursor.lastrowid or 0)

    def _embed_subject(self, subject: str) -> Sequence[float] | None:
        """Vector for the subject, or ``None`` if we cannot get one.

        Only the types whose ``match_mode`` allows cosine will ever read
        it, but it is cheaper to store one for every cue than to teach the
        store which policies exist -- and a policy promoted to
        ``lexical_or_cosine`` later then works on the existing pool
        instead of only on cues written after the change.
        """
        if self._embedder is None:
            return None
        try:
            return self._embedder.embed(subject)
        except Exception:
            log.debug("cue subject embed failed", exc_info=True)
            return None

    def _update(self, cue_id: int, sets: str, params: Sequence[Any]) -> bool:
        if int(cue_id) <= 0:
            return False
        try:
            conn = self._conn()
            cursor = conn.execute(
                f"UPDATE cue_pool SET {sets} WHERE id = ? AND user_id = ?",
                (*params, int(cue_id), self._user_id),
            )
            conn.commit()
        except Exception:
            log.warning("cue_pool update failed: id=%s", cue_id, exc_info=True)
            return False
        return bool(cursor.rowcount)

    def mark_surfaced(self, cue_id: int, *, now: datetime | None = None) -> bool:
        """The cue reached the prompt. Not the same as it being used."""
        return self._update(
            cue_id,
            "state = ?, surfaced_count = surfaced_count + 1, "
            "last_surfaced_at = ?",
            (STATE_SURFACED, _stamp(now)),
        )

    def mark_asked(self, cue_id: int, *, now: datetime | None = None) -> bool:
        """Aiko raised the subject; the cue now waits on an answer."""
        return self._update(
            cue_id,
            "state = ?, ask_count = ask_count + 1, last_asked_at = ?",
            (STATE_AWAITING, _stamp(now)),
        )

    def mark_used(
        self,
        cue_id: int,
        *,
        evidence: str = "",
        now: datetime | None = None,
    ) -> bool:
        """Terminal success. ``evidence`` records how it was decided."""
        return self._update(
            cue_id,
            "state = ?, used_at = ?, used_evidence = ?",
            (STATE_USED, _stamp(now), str(evidence or "")),
        )

    def release(
        self,
        cue_id: int,
        *,
        not_before: datetime | None = None,
        evidence: str = "",
    ) -> bool:
        """Return an unconsumed cue to the pool, optionally behind a gate.

        ``not_before`` is the re-ask cooldown: a question that went by
        unanswered should not be asked again on the next breath.
        """
        return self._update(
            cue_id,
            "state = ?, not_before = ?, used_evidence = ?",
            (
                STATE_PENDING,
                not_before.isoformat() if not_before else None,
                str(evidence or ""),
            ),
        )

    def expire(self, cue_id: int, *, evidence: str = "") -> bool:
        """Out of retries, or past its TTL."""
        return self._update(
            cue_id,
            "state = ?, used_evidence = ?",
            (STATE_EXPIRED, str(evidence or "")),
        )

    def supersede(self, cue_id: int, *, evidence: str = "") -> bool:
        """Another cue says this one's thing, so this one stops asking.

        Distinct from :meth:`expire`, which is a cue that ran out of time
        or retries. :meth:`add` performs this transition inline for the
        subject it is about to claim; this is the same move addressed to
        one row, for callers that decide the duplicate on something other
        than an equal subject string.
        """
        return self._update(
            cue_id,
            "state = ?, used_evidence = ?",
            (STATE_SUPERSEDED, str(evidence or "")),
        )

    def retire_for_sources(
        self, source_ids: Iterable[Any], *, evidence: str = "source_merged",
    ) -> int:
        """Retire live cues drafted from memory rows that no longer stand.

        A cue outlives the row it was drafted from. When K35 folds a
        duplicate memory into its primary the cue built from the loser
        stays pending, still asking about a fact whose row has been
        archived -- and since the primary produces a cue of its own, the
        question gets asked twice from two rows saying the same thing.
        Matched on ``payload.source_id`` in Python rather than in SQL
        because the payload is opaque JSON to the table.
        """
        wanted = {str(s) for s in source_ids if s is not None and str(s).strip()}
        if not wanted:
            return 0
        try:
            conn = self._conn()
            rows = conn.execute(
                "SELECT id, payload FROM cue_pool "
                "WHERE user_id = ? AND payload IS NOT NULL AND state IN "
                f"({','.join('?' * len(LIVE_STATES))})",
                (self._user_id, *sorted(LIVE_STATES)),
            ).fetchall()
        except Exception:
            log.warning("cue_pool source retire read failed", exc_info=True)
            return 0
        doomed: list[int] = []
        for row in rows:
            try:
                blob = json.loads(row[1] or "{}")
            except Exception:
                continue
            if str((blob or {}).get("source_id") or "").strip() in wanted:
                doomed.append(int(row[0]))
        if not doomed:
            return 0
        try:
            conn.executemany(
                "UPDATE cue_pool SET state = ?, used_evidence = ? WHERE id = ?",
                [(STATE_SUPERSEDED, str(evidence or ""), cid) for cid in doomed],
            )
            conn.commit()
        except Exception:
            log.warning("cue_pool source retire failed", exc_info=True)
            return 0
        log.info("cue_pool retired %d cue(s) for merged sources", len(doomed))
        return len(doomed)

    def sweep_expired(self, *, now: datetime | None = None) -> int:
        """Retire live cues past their TTL. Returns rows changed."""
        stamp = _stamp(now)
        try:
            conn = self._conn()
            cursor = conn.execute(
                "UPDATE cue_pool SET state = ?, used_evidence = 'ttl' "
                "WHERE user_id = ? AND expires_at IS NOT NULL "
                "  AND expires_at <= ? AND state IN "
                f"({','.join('?' * len(LIVE_STATES))})",
                (STATE_EXPIRED, self._user_id, stamp, *sorted(LIVE_STATES)),
            )
            conn.commit()
        except Exception:
            log.warning("cue_pool ttl sweep failed", exc_info=True)
            return 0
        return int(cursor.rowcount or 0)

    # ── reads ─────────────────────────────────────────────────────────

    def _select(
        self,
        where: str,
        params: Sequence[Any],
        *,
        order: str = "",
        limit: int = 0,
        offset: int = 0,
        with_embedding: bool = False,
    ) -> list[CueRow]:
        cols = _COLS_EMB if with_embedding else _COLS
        sql = f"SELECT {cols} FROM cue_pool WHERE {where}"
        if order:
            sql += f" ORDER BY {order}"
        args = list(params)
        if limit and int(limit) > 0:
            sql += " LIMIT ?"
            args.append(int(limit))
            if offset and int(offset) > 0:
                sql += " OFFSET ?"
                args.append(int(offset))
        try:
            rows = self._conn().execute(sql, tuple(args)).fetchall()
        except Exception:
            log.warning("cue_pool read failed", exc_info=True)
            return []
        return [_to_row(r, with_embedding=with_embedding) for r in rows]

    def get(self, cue_id: int, *, with_embedding: bool = False) -> CueRow | None:
        rows = self._select(
            "id = ? AND user_id = ?",
            (int(cue_id), self._user_id),
            limit=1,
            with_embedding=with_embedding,
        )
        return rows[0] if rows else None

    def _available_clause(self, stamp: str) -> tuple[str, list[Any]]:
        """Pending, off cooldown, not past its TTL."""
        return (
            "user_id = ? AND state = ? "
            "AND (not_before IS NULL OR not_before <= ?) "
            "AND (expires_at IS NULL OR expires_at > ?)",
            [self._user_id, STATE_PENDING, stamp, stamp],
        )

    def pending(
        self,
        cue_type: str | None = None,
        *,
        limit: int = 20,
        now: datetime | None = None,
        with_embedding: bool = False,
        oldest_first: bool = False,
    ) -> list[CueRow]:
        """Cues available to surface, best candidate first.

        Ordered by ``surfaced_count`` ascending before recency, so a cue
        Aiko has already ignored once yields to one she has not seen. Both
        still get their turn; this only decides which comes first.

        ``oldest_first`` flips the tie-break among cues with the same
        number of chances. Freshest framing wins by default, because a cue
        is built from the context that produced it; the exception is a cue
        whose *age* is the content, which the caller declares through
        ``CuePolicy.pick_order``.
        """
        where, params = self._available_clause(_stamp(now))
        if cue_type:
            where += " AND cue_type = ?"
            params.append(str(cue_type))
        recency = "ASC" if oldest_first else "DESC"
        return self._select(
            where,
            params,
            order=f"surfaced_count ASC, created_at {recency}",
            limit=limit,
            with_embedding=with_embedding,
        )

    def count_pending(
        self, cue_type: str | None = None, *, now: datetime | None = None,
    ) -> int:
        """How much stock is on the shelf. The ``demand()`` hot path.

        Called by every migrated cue worker's probe on every scheduler
        tick, which is why ``idx_cue_pool_type_state`` covers it.
        """
        where, params = self._available_clause(_stamp(now))
        if cue_type:
            where += " AND cue_type = ?"
            params.append(str(cue_type))
        try:
            row = self._conn().execute(
                f"SELECT COUNT(*) FROM cue_pool WHERE {where}", tuple(params),
            ).fetchone()
        except Exception:
            log.warning("cue_pool count failed", exc_info=True)
            return 0
        return int(row[0]) if row else 0

    def last_surfaced_at(self, cue_type: str) -> str | None:
        """When a cue of this type last reached the prompt, in any state.

        Backs ``CuePolicy.surface_cooldown_hours``. Terminal rows are
        counted deliberately: the question is "how recently did Aiko do
        one of these", and a cue she *used* is the strongest possible yes.
        Excluding it would let a callback that landed be followed by
        another on the very next turn, which is the pattern the cadence
        gate exists to prevent.
        """
        try:
            row = self._conn().execute(
                "SELECT MAX(last_surfaced_at) FROM cue_pool "
                "WHERE user_id = ? AND cue_type = ? "
                "  AND last_surfaced_at IS NOT NULL",
                (self._user_id, str(cue_type)),
            ).fetchone()
        except Exception:
            log.warning("cue_pool cadence read failed", exc_info=True)
            return None
        return str(row[0]) if row and row[0] else None

    def in_state(
        self,
        state: str,
        *,
        cue_type: str | None = None,
        limit: int = 50,
        with_embedding: bool = False,
    ) -> list[CueRow]:
        """Every cue currently in one state, oldest first.

        Post-turn consumption walks ``surfaced`` and ``awaiting`` through
        here; both sets are small by construction (a cue only enters them
        by having rendered into a prompt).
        """
        where = "user_id = ? AND state = ?"
        params: list[Any] = [self._user_id, str(state)]
        if cue_type:
            where += " AND cue_type = ?"
            params.append(str(cue_type))
        return self._select(
            where,
            params,
            order="last_surfaced_at ASC, id ASC",
            limit=limit,
            with_embedding=with_embedding,
        )

    def live(
        self,
        cue_type: str | None = None,
        *,
        limit: int = 50,
        with_embedding: bool = False,
    ) -> list[CueRow]:
        """Every cue that has not reached a terminal state yet.

        The distinction from :meth:`pending` is the one a caller almost
        always means when it asks "is this cue still a thing": pending
        answers "may I surface it *now*", which a cue stops satisfying
        the moment it renders into a prompt. A reader that treats the
        second question as the first retires anything Aiko has been
        shown once -- see the K52 wants ledger, where it drained the
        pressure mechanic for months.
        """
        placeholders = ", ".join("?" for _ in LIVE_STATES)
        where = f"user_id = ? AND state IN ({placeholders})"
        params: list[Any] = [self._user_id, *sorted(LIVE_STATES)]
        if cue_type:
            where += " AND cue_type = ?"
            params.append(str(cue_type))
        return self._select(
            where,
            params,
            order="id ASC",
            limit=limit,
            with_embedding=with_embedding,
        )

    def resolved_ids(self, ids: Iterable[int]) -> set[int]:
        """Which of ``ids`` have been *settled* rather than merely spent.

        The read a consumer holding a derived copy of a cue should use
        when it asks "may I keep this". It differs from :meth:`live` in
        two ways that both matter.

        The first is semantic: ``live`` treats ``expired`` as gone, and
        for a cue that is correct -- it will not surface again. For
        something built *on* that cue it is wrong, because a cue expires
        by being offered and refused, which is the state that most
        deserves to keep wanting. Only ``used`` and ``superseded`` say
        the subject is settled.

        The second is about failure shape, and is the reason this takes
        ids rather than returning a page. ``live`` answers by *absence*,
        so a truncated page silently reads as "these are all gone" and
        the caller has to defend itself with a page-full check. This
        answers by *presence* over a set the caller already holds: an
        empty result retires nothing, a read failure retires nothing,
        and there is no page to overflow. Prune on evidence that the
        subject is done, never on the absence of evidence that it is
        not.
        """
        try:
            wanted = sorted({int(cue_id) for cue_id in ids})
        except (TypeError, ValueError):
            return set()
        if not wanted:
            return set()
        states = sorted(RESOLVED_STATES)
        state_slots = ", ".join("?" for _ in states)
        found: set[int] = set()
        # Chunked so a large caller cannot outrun SQLite's variable
        # limit; in practice the ledger holds single digits.
        for start in range(0, len(wanted), 400):
            chunk = wanted[start:start + 400]
            id_slots = ", ".join("?" for _ in chunk)
            sql = (
                f"SELECT id FROM cue_pool WHERE user_id = ? "
                f"AND state IN ({state_slots}) AND id IN ({id_slots})"
            )
            try:
                rows = self._conn().execute(
                    sql, (self._user_id, *states, *chunk),
                ).fetchall()
            except Exception:
                # Deliberately not re-raised and deliberately not a
                # partial answer: the contract above is that nothing is
                # retired without positive evidence, and an empty result
                # is exactly that.
                log.warning("cue_pool resolved read failed", exc_info=True)
                return set()
            found.update(int(row[0]) for row in rows)
        return found

    def recent_subjects(
        self,
        cue_type: str | None = None,
        *,
        within_hours: float = 168.0,
        states: Iterable[str] | None = None,
    ) -> set[str]:
        """Subjects already spoken for, so a worker does not re-propose them.

        This is what replaces the per-topic ``surfaced_keys`` sets the
        workers kept in ``kv_meta``. Defaults to a week and to every state
        including the terminal ones -- a subject that was used, or that
        expired unwanted, is exactly as poor a candidate for a new cue as
        one still pending.
        """
        cutoff = (
            timephrase.utcnow() - timedelta(hours=max(0.0, float(within_hours)))
        ).isoformat()
        where = "user_id = ? AND created_at >= ?"
        params: list[Any] = [self._user_id, cutoff]
        if cue_type:
            where += " AND cue_type = ?"
            params.append(str(cue_type))
        wanted = [str(s) for s in states] if states is not None else []
        if wanted:
            where += f" AND state IN ({','.join('?' * len(wanted))})"
            params.extend(wanted)
        try:
            rows = self._conn().execute(
                f"SELECT DISTINCT subject FROM cue_pool WHERE {where}",
                tuple(params),
            ).fetchall()
        except Exception:
            log.warning("cue_pool subject read failed", exc_info=True)
            return set()
        return {str(r[0] or "") for r in rows if r and r[0]}

    def claimed_source_ids(
        self,
        cue_type: str | None = None,
        *,
        within_hours: float = 168.0,
    ) -> set[str]:
        """``payload.source_id`` values a cue already exists for.

        The subject-based companion to :meth:`recent_subjects`, and the
        exact one. A worker that drafts from memories keys its dedupe on
        the row it drafted from, and its own journal ring only remembers
        the last handful -- so the same memory came back around as soon
        as it rotated out. The pool has no such horizon.
        """
        cutoff = (
            timephrase.utcnow() - timedelta(hours=max(0.0, float(within_hours)))
        ).isoformat()
        where = "user_id = ? AND created_at >= ? AND payload IS NOT NULL"
        params: list[Any] = [self._user_id, cutoff]
        if cue_type:
            where += " AND cue_type = ?"
            params.append(str(cue_type))
        try:
            rows = self._conn().execute(
                f"SELECT payload FROM cue_pool WHERE {where}", tuple(params),
            ).fetchall()
        except Exception:
            log.warning("cue_pool source read failed", exc_info=True)
            return set()
        out: set[str] = set()
        for row in rows:
            try:
                blob = json.loads(row[0] or "{}")
            except Exception:
                continue
            source_id = str((blob or {}).get("source_id") or "").strip()
            if source_id:
                out.add(source_id)
        return out

    def list_for_user(
        self,
        *,
        user_id: str | None = None,
        cue_type: str | None = None,
        state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CueRow]:
        """Filtered page for the settings panel and the MCP tools."""
        where = "user_id = ?"
        params: list[Any] = [str(user_id or self._user_id)]
        if cue_type:
            where += " AND cue_type = ?"
            params.append(str(cue_type))
        if state:
            where += " AND state = ?"
            params.append(str(state))
        return self._select(
            where,
            params,
            order="created_at DESC, id DESC",
            limit=limit,
            offset=offset,
        )

    def count_for_user(
        self,
        *,
        user_id: str | None = None,
        cue_type: str | None = None,
        state: str | None = None,
    ) -> int:
        where = "user_id = ?"
        params: list[Any] = [str(user_id or self._user_id)]
        if cue_type:
            where += " AND cue_type = ?"
            params.append(str(cue_type))
        if state:
            where += " AND state = ?"
            params.append(str(state))
        try:
            row = self._conn().execute(
                f"SELECT COUNT(*) FROM cue_pool WHERE {where}", tuple(params),
            ).fetchone()
        except Exception:
            return 0
        return int(row[0]) if row else 0

    def stats(self) -> list[dict[str, Any]]:
        """Per-type pool depth, outcomes, and mean surfacings before use.

        That last number is the one that says whether a cue type earns its
        keep: a type Aiko routinely needs shown twice before she picks it
        up is one whose framing is not landing, and a type that mostly
        expires is one nobody wants.
        """
        try:
            rows = self._conn().execute(
                "SELECT cue_type, state, COUNT(*), "
                "       AVG(CASE WHEN state = ? THEN surfaced_count END), "
                "       SUM(ask_count) "
                "FROM cue_pool WHERE user_id = ? "
                "GROUP BY cue_type, state",
                (STATE_USED, self._user_id),
            ).fetchall()
        except Exception:
            log.warning("cue_pool stats failed", exc_info=True)
            return []
        by_type: dict[str, dict[str, Any]] = {}
        for cue_type, state, count, mean_surfacings, asks in rows:
            entry = by_type.setdefault(
                str(cue_type or ""),
                {
                    "cue_type": str(cue_type or ""),
                    "total": 0,
                    "asks": 0,
                    "mean_surfacings_before_use": None,
                },
            )
            entry[str(state or "")] = int(count or 0)
            entry["total"] += int(count or 0)
            entry["asks"] += int(asks or 0)
            if mean_surfacings is not None:
                entry["mean_surfacings_before_use"] = round(
                    float(mean_surfacings), 2,
                )
        for entry in by_type.values():
            for state in sorted(VALID_STATES):
                entry.setdefault(state, 0)
        return sorted(by_type.values(), key=lambda e: str(e["cue_type"]))


__all__ = [
    "LIVE_STATES",
    "RESOLVED_STATES",
    "STATE_AWAITING",
    "STATE_EXPIRED",
    "STATE_PENDING",
    "STATE_SUPERSEDED",
    "STATE_SURFACED",
    "STATE_USED",
    "TERMINAL_STATES",
    "VALID_STATES",
    "CueRow",
    "CueStore",
    "normalise_subject",
]
