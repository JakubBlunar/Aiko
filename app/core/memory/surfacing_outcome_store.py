"""Append-only ledger of what Aiko surfaced and whether it landed (L37).

Everything deciding which memories and concepts reach the system prompt is
a hand-tuned constant -- the per-kind ``surface_weights``, the core-lane
confidence bars, the habituation window -- and none of them move in
response to how the conversation actually went. Surfacing is very nearly
write-only: the only trace a surfaced concept leaves is a habituation
timestamp. So a concept that has been in front of Aiko two hundred times
to no visible effect is indistinguishable from one that opened up a good
conversation every single time. She can grow her knowledge but not her
judgement.

This store is the missing join: *what was surfaced* against *what
happened next*.

Two clocks, which is the whole subtlety
---------------------------------------
``echoed`` is a **same-turn** signal (did Aiko's own reply reference the
item). ``engagement_label`` belongs to the **next** turn: K14 derives
latency from the gap between Aiko's last reply and the current user
message, so the engagement computed at post-turn *N* describes the user's
reaction to reply *N-1*. Rows are therefore keyed by the
``assistant_message_id`` of the reply they helped produce, and settled one
turn later. Getting this backwards would invert the signal.

A row that is never settled is **correct rather than broken** -- silence
after a goodbye is not disengagement -- so :meth:`unsettled_count` is a
health metric, not an error count.

Design, mirroring :class:`~app.core.concepts.concept_event_store.ConceptEventStore`
-----------------------------------------------------------------------------------
- **Append-then-settle.** Rows are inserted once and updated only to
  attach the outcome. No other mutation.
- **Soft references.** ``item_id`` points into ``concepts`` / ``memories``
  / ``topic_clusters`` by ``item_kind`` and is never cascade-deleted, so a
  pruned memory still leaves its surfacing history standing.
- **Never raises.** Every method logs and degrades to an empty result. A
  ledger write must not be able to break a turn.

On the read API
---------------
:meth:`stats_for` returns *counts*, not rates, and takes an explicit
window. Both matter to the consumer (L38's earned standing) and are
awkward to retrofit:

- **Counts** let the caller refuse to act on a thin sample. One item
  settled 1-for-1 must not look like one settled 50-for-50. This mirrors
  :class:`~app.core.affect.engagement_tracker.EngagementTracker`, which
  reports ``warmed=False`` rather than a confident label off two data
  points.
- **A window** keeps the estimate adapting. A lifetime-only rate anchors
  on early data and progressively stops responding, which would invert
  the point of the feature, and it lets the aggregate scan grow without
  bound on a hot path.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING
from app.core.infra import timephrase
from app.core.memory.echo_detector import EchoVerdict

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.surfacing_outcome_store")

# ``item_kind`` values written today. An open enum: a new surfaced source
# is a value, not a migration.
ITEM_KIND_CONCEPT = "concept"
ITEM_KIND_MEMORY = "memory"
ITEM_KIND_CLUSTER = "cluster"
# G4. Identified by NAME rather than row id -- there is no integer cue
# registry anywhere in the codebase. Cue rows carry ``item_id = 0`` and a
# non-NULL ``item_key``; see the schema comment in ``chat_database.py``.
ITEM_KIND_CUE = "cue"

# The engagement label that counts as "this landed". K14's other buckets
# are ``neutral`` / ``disengaged`` / ``abandoned``; only the top one is
# evidence *for* an item, which keeps the rate a measure of the good case
# rather than the absence of the bad one.
ENGAGED_LABEL = "engaged"


def _now_iso() -> str:
    return timephrase.utcnow().isoformat()


@dataclass(slots=True)
class SurfacedItem:
    """One item that reached the prompt on one turn, before its outcome.

    ``lane`` / ``surface_reason`` / ``score`` / ``rank`` snapshot *why* it
    won its slot at surfacing time, which is what makes the ledger
    diagnostic rather than just a counter: "concepts that surface on the
    activation lane never land" is a different and more useful finding
    than "this concept never lands".
    """

    item_kind: str
    item_id: int
    lane: str = ""
    surface_reason: str = ""
    score: float = 0.0
    rank: int = 0
    # G4: set instead of ``item_id`` for name-keyed kinds (cues). The two
    # are mutually exclusive by convention, not by constraint -- a CHECK
    # would have to be added by table rebuild, and the store is the only
    # writer.
    item_key: str = ""


@dataclass(frozen=True, slots=True)
class ItemStats:
    """Outcome counts for one item over some window.

    Deliberately counts rather than rates -- see the module docstring.
    ``surfaced`` includes rows still awaiting their outcome, so
    ``surfaced - settled`` is how much evidence is still in flight.
    """

    surfaced: int = 0
    settled: int = 0
    engaged: int = 0
    echoed: int = 0

    @property
    def engaged_rate(self) -> float | None:
        """Share of *settled* rows that landed, or ``None`` when nothing
        has settled yet. ``None`` rather than ``0.0`` on purpose: "no
        evidence" and "evidence that it never lands" must not collapse
        into the same number."""
        if self.settled <= 0:
            return None
        return self.engaged / self.settled

    @property
    def echo_rate(self) -> float | None:
        """Share of surfaced rows Aiko referenced in her own reply, or
        ``None`` when nothing was surfaced."""
        if self.surfaced <= 0:
            return None
        return self.echoed / self.surfaced


@dataclass(frozen=True, slots=True)
class ClusterTaste:
    """Per-topic-cluster engagement, the raw signal behind K81 taste.

    ``engaged_rate`` is ``engaged / settled`` -- because it is a *rate* it
    is frequency-independent, so a topic raised rarely but that reliably
    lands outscores one raised constantly to no effect. That asymmetry is
    the whole point: taste is not the same as what the user brings up most.
    """

    cluster_id: int
    surfaced: int = 0
    settled: int = 0
    engaged: int = 0

    @property
    def engaged_rate(self) -> float | None:
        """Share of settled rows that landed, or ``None`` below the
        settle floor (the caller gates on ``min_settled`` in SQL, so a
        returned row always has ``settled >= 1``)."""
        if self.settled <= 0:
            return None
        return self.engaged / self.settled


def items_from_selection(
    selection: object,
    *,
    score_components: dict[int, dict] | None = None,
) -> list[SurfacedItem]:
    """Project a :class:`ContextSelection` into ledger rows.

    Pure and duck-typed (no import of the selector), so the mapping can be
    tested without standing up a session. ``relevance`` is taken from the
    chosen candidate rather than from ``score_components`` because it is
    the number that actually competed for the budget and is comparable
    across lanes -- the core lane records raw confidence while the flex
    lane records a blended score, so the components map is the wrong place
    to read a single "score" from.

    Only ``source="memory"`` RAG hits are recorded, matching
    ``RagRetriever.mark_surfaced``: message and document hits have no
    stable reinforceable identity in the ``memories`` namespace, so an id
    from them would collide with a real memory id.
    """
    if selection is None:
        return []
    comps = score_components or {}
    out: list[SurfacedItem] = []

    def _chosen(name: str) -> list:
        try:
            src = selection.source(name)  # type: ignore[attr-defined]
        except Exception:
            return []
        return sorted(
            list(getattr(src, "chosen", []) or []),
            key=lambda c: int(getattr(c, "order", 0)),
        )

    for cand in _chosen("concept"):
        payload = getattr(cand, "payload", None)
        cid = int(getattr(payload, "concept_id", 0) or 0)
        if cid <= 0:
            continue
        comp = comps.get(cid) or {}
        out.append(SurfacedItem(
            item_kind=ITEM_KIND_CONCEPT,
            item_id=cid,
            lane=str(comp.get("lane", "") or ""),
            surface_reason=str(comp.get("reason", "") or ""),
            score=float(getattr(cand, "relevance", 0.0) or 0.0),
            rank=int(getattr(cand, "order", 0) or 0),
        ))

    for cand in _chosen("memory"):
        hit = getattr(cand, "payload", None)
        if getattr(hit, "source", None) != "memory":
            continue
        raw = getattr(getattr(hit, "record", None), "id", None)
        if raw is None:
            continue
        try:
            mid = int(raw)
        except (TypeError, ValueError):
            continue
        if mid <= 0:
            continue
        out.append(SurfacedItem(
            item_kind=ITEM_KIND_MEMORY,
            item_id=mid,
            score=float(getattr(cand, "relevance", 0.0) or 0.0),
            rank=int(getattr(cand, "order", 0) or 0),
        ))

    for cand in _chosen("cluster"):
        payload = getattr(cand, "payload", None)
        try:
            cluster_id = int(payload[0])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if cluster_id <= 0:
            continue
        out.append(SurfacedItem(
            item_kind=ITEM_KIND_CLUSTER,
            item_id=cluster_id,
            score=float(getattr(cand, "relevance", 0.0) or 0.0),
            rank=int(getattr(cand, "order", 0) or 0),
        ))

    return out


class SurfacingOutcomeStore:
    """Append-then-settle access to the ``surfacing_outcomes`` ledger."""

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    # ── writes ────────────────────────────────────────────────────────

    def add_many(
        self,
        assistant_message_id: int,
        items: Sequence[SurfacedItem],
        *,
        echoes: dict[tuple[str, int], "EchoVerdict"] | None = None,
    ) -> int:
        """Record the set surfaced for one reply. Returns rows written.

        ``echoes`` is keyed by ``(item_kind, item_id)`` and may cover only
        some items; anything absent stays NULL on all three echo columns,
        which reads as "not computed" rather than "not echoed" -- a
        distinction that matters, because item kinds differ in whether an
        echo test is even meaningful for them.

        A verdict whose kind is ``none`` still writes ``echoed = 0`` plus
        its ``echo_score``: the sub-floor cosine of a miss is exactly the
        evidence needed to re-derive the floor later, and discarding near
        misses would leave only the successes to calibrate against.

        One transaction for the whole set: the surfaced items of a turn
        are a unit, and a half-written turn would quietly skew every rate
        derived from it.
        """
        if not items or int(assistant_message_id) <= 0:
            return 0
        now = _now_iso()
        marks = echoes or {}
        rows = []
        for it in items:
            kind = str(it.item_kind or "")
            item_id = int(it.item_id or 0)
            item_key = str(getattr(it, "item_key", "") or "")
            # Identified by id OR by name; a row with neither names nothing
            # and would be an untraceable entry in a diagnostic table.
            if not kind or (item_id <= 0 and not item_key):
                continue
            verdict = marks.get((kind, item_id))
            rows.append((
                int(assistant_message_id),
                kind,
                item_id,
                (item_key or None),
                str(it.lane or ""),
                str(it.surface_reason or ""),
                float(it.score or 0.0),
                int(it.rank or 0),
                (None if verdict is None else int(bool(verdict.echoed))),
                (None if verdict is None else str(verdict.kind)),
                (None if verdict is None else float(verdict.score)),
                now,
            ))
        if not rows:
            return 0
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            conn.executemany(
                "INSERT INTO surfacing_outcomes "
                "(assistant_message_id, item_kind, item_id, item_key, lane, "
                " surface_reason, score, rank, echoed, echo_kind, "
                " echo_score, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.commit()
        except Exception:
            log.warning("surfacing ledger insert failed", exc_info=True)
            return 0
        return len(rows)

    def settle(self, assistant_message_id: int, label: str) -> int:
        """Attach the user's reaction to the rows for one reply.

        Idempotent by construction: only unsettled rows are touched, so a
        double-settle (or a retry) cannot overwrite an earlier verdict
        with a later turn's engagement.
        """
        if int(assistant_message_id) <= 0 or not label:
            return 0
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            cursor = conn.execute(
                "UPDATE surfacing_outcomes "
                "SET engagement_label = ?, settled_at = ? "
                "WHERE assistant_message_id = ? AND settled_at IS NULL",
                (str(label), _now_iso(), int(assistant_message_id)),
            )
            conn.commit()
        except Exception:
            log.warning("surfacing ledger settle failed", exc_info=True)
            return 0
        return int(cursor.rowcount or 0)

    # ── reads ─────────────────────────────────────────────────────────

    def stats_for(
        self,
        item_kind: str,
        item_ids: Iterable[int],
        *,
        window_days: int | None,
        lanes: Iterable[str] | None = None,
    ) -> dict[int, ItemStats]:
        """Outcome counts per item, restricted to ``window_days``.

        ``window_days=None`` means lifetime. One grouped query for the
        whole id set rather than a read per item, because the consumer
        scores every candidate on every turn.

        Items with no rows in the window are simply absent from the
        result; callers should treat a missing entry and an all-zero
        :class:`ItemStats` the same way.
        """
        ids = [int(i) for i in item_ids if i is not None and int(i) > 0]
        kind = str(item_kind or "")
        if not ids or not kind:
            return {}
        placeholders = ",".join("?" * len(ids))
        params: list[object] = [kind, *ids]
        lane_values = tuple(
            dict.fromkeys(str(lane or "").strip() for lane in (lanes or ()))
        )
        lane_values = tuple(lane for lane in lane_values if lane)
        lane_clause = ""
        if lane_values:
            lane_placeholders = ",".join("?" * len(lane_values))
            lane_clause = f" AND lane IN ({lane_placeholders})"
            params.extend(lane_values)
        since_clause = ""
        if window_days is not None:
            cutoff = timephrase.utcnow() - timedelta(days=max(0, int(window_days)))
            since_clause = " AND created_at >= ?"
            params.append(cutoff.isoformat())
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            rows = conn.execute(
                "SELECT item_id, COUNT(*), "
                "       SUM(CASE WHEN settled_at IS NOT NULL THEN 1 ELSE 0 END), "
                "       SUM(CASE WHEN engagement_label = ? THEN 1 ELSE 0 END), "
                "       SUM(CASE WHEN echoed = 1 THEN 1 ELSE 0 END) "
                "FROM surfacing_outcomes "
                f"WHERE item_kind = ? AND item_id IN ({placeholders})"
                f"{lane_clause}"
                f"{since_clause} "
                "GROUP BY item_id",
                (ENGAGED_LABEL, *params),
            ).fetchall()
        except Exception:
            log.warning("surfacing ledger stats read failed", exc_info=True)
            return {}
        return {
            int(r[0]): ItemStats(
                surfaced=int(r[1] or 0),
                settled=int(r[2] or 0),
                engaged=int(r[3] or 0),
                echoed=int(r[4] or 0),
            )
            for r in rows
        }

    def engaged_rate_by_cluster(
        self,
        *,
        window_days: int | None,
        min_settled: int = 1,
    ) -> dict[int, ClusterTaste]:
        """Per-topic-cluster engagement over ``window_days`` (K81 taste).

        Two sources map a ledger row to a cluster: a ``cluster`` row whose
        ``item_id`` *is* the ``cluster_id``, and a ``memory`` row joined
        through ``memory_topic_assignments`` to whatever cluster the memory
        currently belongs to. Concept and cue rows carry no cluster and are
        excluded. The two sources are unioned per row, then grouped, so a
        turn that surfaced both a cluster label and a memory from the same
        cluster contributes twice -- which is correct: both touched the
        topic and both inherited the turn's engagement.

        ``min_settled`` is the warmup floor (a ``HAVING`` clause): a cluster
        below it is simply absent, so a cold ledger yields no taste rather
        than confident noise off one observation -- the same posture as
        :class:`ItemStats` and the engagement tracker's ``warmed`` gate.
        ``window_days=None`` means lifetime.
        """
        params: list[object] = [ENGAGED_LABEL]
        since_a = ""
        if window_days is not None:
            cutoff = timephrase.utcnow() - timedelta(days=max(0, int(window_days)))
            since_a = " AND created_at >= ?"
            params.append(cutoff.isoformat())
        # The engaged-label param + optional cutoff repeat for the second
        # (memory-join) arm of the UNION.
        params.append(ENGAGED_LABEL)
        since_b = ""
        if window_days is not None:
            cutoff = timephrase.utcnow() - timedelta(days=max(0, int(window_days)))
            since_b = " AND so.created_at >= ?"
            params.append(cutoff.isoformat())
        params.append(max(0, int(min_settled)))
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            rows = conn.execute(
                "SELECT cluster_id, "
                "       COUNT(*) AS surfaced, "
                "       SUM(settled_flag) AS settled, "
                "       SUM(engaged_flag) AS engaged "
                "FROM ("
                "  SELECT item_id AS cluster_id, "
                "         CASE WHEN settled_at IS NOT NULL THEN 1 ELSE 0 END AS settled_flag, "
                "         CASE WHEN engagement_label = ? THEN 1 ELSE 0 END AS engaged_flag "
                "  FROM surfacing_outcomes "
                f"  WHERE item_kind = 'cluster' AND item_id > 0{since_a} "
                "  UNION ALL "
                "  SELECT a.cluster_id AS cluster_id, "
                "         CASE WHEN so.settled_at IS NOT NULL THEN 1 ELSE 0 END, "
                "         CASE WHEN so.engagement_label = ? THEN 1 ELSE 0 END "
                "  FROM surfacing_outcomes so "
                "  JOIN memory_topic_assignments a ON a.memory_id = so.item_id "
                f"  WHERE so.item_kind = 'memory' AND so.item_id > 0{since_b} "
                ") "
                "GROUP BY cluster_id "
                "HAVING settled >= ? "
                "ORDER BY (CAST(engaged AS REAL) / settled) DESC, settled DESC",
                tuple(params),
            ).fetchall()
        except Exception:
            log.warning("surfacing ledger cluster taste read failed", exc_info=True)
            return {}
        return {
            int(r[0]): ClusterTaste(
                cluster_id=int(r[0]),
                surfaced=int(r[1] or 0),
                settled=int(r[2] or 0),
                engaged=int(r[3] or 0),
            )
            for r in rows
            if int(r[0] or 0) > 0
        }

    def leaderboard(
        self,
        *,
        item_kind: str | None = None,
        window_days: int | None = None,
        min_settled: int = 1,
        limit: int = 20,
    ) -> list[dict]:
        """Per-item counts ordered by engaged rate, for the debug view.

        ``min_settled`` keeps single-observation noise off the top of the
        board -- a 1-for-1 item would otherwise outrank a 40-of-50 one.
        """
        params: list[object] = [ENGAGED_LABEL]
        where = ["1=1"]
        if item_kind:
            where.append("item_kind = ?")
            params.append(str(item_kind))
        if window_days is not None:
            cutoff = timephrase.utcnow() - timedelta(days=max(0, int(window_days)))
            where.append("created_at >= ?")
            params.append(cutoff.isoformat())
        params.append(max(0, int(min_settled)))
        params.append(max(1, int(limit)))
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            rows = conn.execute(
                "SELECT item_kind, item_id, COALESCE(item_key, ''), "
                "       COUNT(*) AS surfaced, "
                "       SUM(CASE WHEN settled_at IS NOT NULL THEN 1 ELSE 0 END) AS settled, "
                "       SUM(CASE WHEN engagement_label = ? THEN 1 ELSE 0 END) AS engaged, "
                "       SUM(CASE WHEN echoed = 1 THEN 1 ELSE 0 END) AS echoed "
                "FROM surfacing_outcomes "
                f"WHERE {' AND '.join(where)} "
                "GROUP BY item_kind, item_id, item_key "
                "HAVING settled >= ? "
                "ORDER BY (CAST(engaged AS REAL) / settled) DESC, settled DESC "
                "LIMIT ?",
                tuple(params),
            ).fetchall()
        except Exception:
            log.warning("surfacing ledger leaderboard failed", exc_info=True)
            return []
        out = []
        for r in rows:
            stats = ItemStats(
                surfaced=int(r[3] or 0), settled=int(r[4] or 0),
                engaged=int(r[5] or 0), echoed=int(r[6] or 0),
            )
            out.append({
                "item_kind": str(r[0] or ""),
                "item_id": int(r[1] or 0),
                # Name-keyed kinds would otherwise appear as ``item_id: 0``
                # repeated once per cue, which is unreadable in the view.
                "item_key": str(r[2] or ""),
                "surfaced": stats.surfaced,
                "settled": stats.settled,
                "engaged": stats.engaged,
                "echoed": stats.echoed,
                "engaged_rate": (
                    None if stats.engaged_rate is None
                    else round(stats.engaged_rate, 4)
                ),
                "echo_rate": (
                    None if stats.echo_rate is None
                    else round(stats.echo_rate, 4)
                ),
            })
        return out

    def lane_breakdown(self, *, window_days: int | None = None) -> list[dict]:
        """Engaged rate grouped by ``(item_kind, lane)``.

        The aggregate that answers a question about the *machinery* rather
        than any one item: whether a whole lane earns its tokens.
        """
        params: list[object] = [ENGAGED_LABEL]
        where = ["settled_at IS NOT NULL"]
        if window_days is not None:
            cutoff = timephrase.utcnow() - timedelta(days=max(0, int(window_days)))
            where.append("created_at >= ?")
            params.append(cutoff.isoformat())
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            rows = conn.execute(
                "SELECT item_kind, lane, COUNT(*) AS settled, "
                "       SUM(CASE WHEN engagement_label = ? THEN 1 ELSE 0 END) AS engaged "
                "FROM surfacing_outcomes "
                f"WHERE {' AND '.join(where)} "
                "GROUP BY item_kind, lane "
                "ORDER BY settled DESC",
                tuple(params),
            ).fetchall()
        except Exception:
            log.warning("surfacing ledger lane breakdown failed", exc_info=True)
            return []
        return [
            {
                "item_kind": str(r[0] or ""),
                "lane": str(r[1] or ""),
                "settled": int(r[2] or 0),
                "engaged": int(r[3] or 0),
                "engaged_rate": (
                    round(int(r[3] or 0) / int(r[2]), 4) if int(r[2] or 0) else None
                ),
            }
            for r in rows
        ]

    def echo_breakdown(self, *, window_days: int | None = None) -> list[dict]:
        """Engagement grouped by *how* the echo was decided (F12).

        This is the query the deferred full-credit decision turns on. A
        semantic echo currently earns less retention credit than a quote,
        on the argument that surfaced items were already selected for
        topical similarity, so cosine against the reply partly measures
        "was on topic" rather than "she used it". That argument is
        testable: if ``semantic`` rows engage the user about as often as
        ``lexical`` ones, the discount is unjustified and semantic hits
        should earn full credit. If they engage no better than rows with
        no echo at all, the signal is topical leakage and the discount was
        right.

        ``score`` is averaged per ``echo_kind`` and not across kinds --
        lexical scores are word counts and semantic ones are cosines.
        Rows from before schema v27 have a NULL ``echo_kind`` and are
        reported under ``"unrecorded"`` rather than silently folded into
        ``none``, since they were judged by the lexical test alone.
        """
        params: list[object] = [ENGAGED_LABEL]
        where = ["settled_at IS NOT NULL"]
        if window_days is not None:
            cutoff = timephrase.utcnow() - timedelta(days=max(0, int(window_days)))
            where.append("created_at >= ?")
            params.append(cutoff.isoformat())
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            rows = conn.execute(
                "SELECT item_kind, COALESCE(echo_kind, 'unrecorded'), "
                "       COUNT(*) AS settled, "
                "       SUM(CASE WHEN engagement_label = ? THEN 1 ELSE 0 END), "
                "       AVG(echo_score), MIN(echo_score), MAX(echo_score) "
                "FROM surfacing_outcomes "
                f"WHERE {' AND '.join(where)} "
                "GROUP BY item_kind, COALESCE(echo_kind, 'unrecorded') "
                "ORDER BY settled DESC",
                tuple(params),
            ).fetchall()
        except Exception:
            log.warning("surfacing ledger echo breakdown failed", exc_info=True)
            return []
        out = []
        for r in rows:
            settled = int(r[2] or 0)
            engaged = int(r[3] or 0)
            out.append({
                "item_kind": str(r[0] or ""),
                "echo_kind": str(r[1] or ""),
                "settled": settled,
                "engaged": engaged,
                "engaged_rate": (
                    round(engaged / settled, 4) if settled else None
                ),
                "avg_score": (None if r[4] is None else round(float(r[4]), 4)),
                "min_score": (None if r[5] is None else round(float(r[5]), 4)),
                "max_score": (None if r[6] is None else round(float(r[6]), 4)),
            })
        return out

    def semantic_floor_candidates(
        self,
        *,
        window_days: int | None = None,
        floors: Sequence[float] = (0.50, 0.55, 0.60, 0.65, 0.70, 0.75),
    ) -> list[dict]:
        """What each candidate cosine floor *would* have selected.

        Answers "where should the floor be?" without having to re-run
        history: every settled row carries the cosine that was measured,
        including the misses, so each floor can be replayed over the same
        data. Only rows that the lexical test did **not** already claim
        are counted, because those are the only ones a floor decides.

        A floor worth adopting shows an engaged rate clearly above the
        all-rows baseline in :meth:`echo_breakdown`; one whose rate is
        flat across every floor is measuring topic, not use.
        """
        base_where = ["settled_at IS NOT NULL", "echo_score IS NOT NULL",
                      "echo_kind != 'lexical'"]
        base_params: list[object] = []
        if window_days is not None:
            cutoff = timephrase.utcnow() - timedelta(days=max(0, int(window_days)))
            base_where.append("created_at >= ?")
            base_params.append(cutoff.isoformat())
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        out = []
        for floor in floors:
            try:
                row = conn.execute(
                    "SELECT COUNT(*), "
                    "       SUM(CASE WHEN engagement_label = ? THEN 1 ELSE 0 END) "
                    "FROM surfacing_outcomes "
                    f"WHERE {' AND '.join(base_where)} AND echo_score >= ?",
                    (ENGAGED_LABEL, *base_params, float(floor)),
                ).fetchone()
            except Exception:
                log.warning(
                    "surfacing ledger floor replay failed", exc_info=True
                )
                return []
            settled = int(row[0] or 0) if row else 0
            engaged = int(row[1] or 0) if row else 0
            out.append({
                "floor": float(floor),
                "would_match": settled,
                "engaged": engaged,
                "engaged_rate": (
                    round(engaged / settled, 4) if settled else None
                ),
            })
        return out

    def count(self) -> int:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM surfacing_outcomes"
            ).fetchone()
        except Exception:
            return 0
        return int(row[0]) if row else 0

    def unsettled_count(self) -> int:
        """Rows still awaiting an outcome.

        A steady trickle is expected -- the last turn of every session
        never settles. A large or growing number means the settle path is
        not running, which is the one failure mode that would silently
        starve the ledger.
        """
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM surfacing_outcomes "
                "WHERE settled_at IS NULL"
            ).fetchone()
        except Exception:
            return 0
        return int(row[0]) if row else 0

    # ── retention ─────────────────────────────────────────────────────

    def prune(self, keep_days: int) -> int:
        """Drop rows older than ``keep_days``. Returns rows removed.

        Not scheduled by anything yet (P34 owns retention policy). Unlike
        most pruning this is about the *signal* as much as the disk: a
        rate computed over years stops responding to how the relationship
        actually works now.
        """
        if int(keep_days) <= 0:
            return 0
        cutoff = timephrase.utcnow() - timedelta(days=int(keep_days))
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            cursor = conn.execute(
                "DELETE FROM surfacing_outcomes WHERE created_at < ?",
                (cutoff.isoformat(),),
            )
            conn.commit()
        except Exception:
            log.warning("surfacing ledger prune failed", exc_info=True)
            return 0
        return int(cursor.rowcount or 0)


__all__ = [
    "ENGAGED_LABEL",
    "ITEM_KIND_CLUSTER",
    "ITEM_KIND_CONCEPT",
    "ITEM_KIND_MEMORY",
    "ClusterTaste",
    "ItemStats",
    "SurfacedItem",
    "SurfacingOutcomeStore",
    "items_from_selection",
]
