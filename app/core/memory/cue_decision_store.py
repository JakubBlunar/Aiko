"""Append-only record of armed cues that did or did not get through (G4).

Companion to :class:`~app.core.memory.surfacing_outcome_store.SurfacingOutcomeStore`,
answering the question that one deliberately cannot: **of the times a cue
had something to say, how often did it reach the prompt, and when it did
not, why?**

Why this is a separate table
----------------------------
Every aggregate over ``surfacing_outcomes`` means "of the times this
reached the prompt". Admitting rows for cues that never reached it would
inflate the denominator of the ledger's entire purpose -- ``surfaced``
counts, ``echo_rate``, the leaderboard -- so declines live here and only
the cues that actually rendered are also written there. The two tables
answer different questions on the same events, and merging them would
corrupt the older one.

Rows exist only for **armed** cues. "Not armed" is the overwhelmingly
common case for most of the 15 registered cues on any given turn, carries
no information, and would multiply the table by the cue count per turn to
record nothing.

Never raises: a diagnostic write must not be able to break a turn.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import timedelta
from typing import TYPE_CHECKING

from app.core.infra import timephrase
from app.core.proactive.cue_accounting import OUTCOME_SURFACED

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.cue_decision_store")


class CueDecisionStore:
    """Writes and reads the ``cue_decisions`` table."""

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    # ── writes ────────────────────────────────────────────────────────

    def add_many(
        self,
        assistant_message_id: int,
        rows: Sequence[tuple[str, str, str]],
    ) -> int:
        """Record ``(cue, outcome, reason)`` triples for one turn.

        One transaction for the whole turn: the armed set of a turn is a
        unit, and a half-written turn would skew the ratio it exists to
        measure.
        """
        if not rows or int(assistant_message_id) <= 0:
            return 0
        now = timephrase.utcnow().isoformat()
        payload = [
            (
                int(assistant_message_id),
                str(cue),
                str(outcome),
                str(reason or ""),
                now,
            )
            for cue, outcome, reason in rows
            if cue and outcome
        ]
        if not payload:
            return 0
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            conn.executemany(
                "INSERT INTO cue_decisions "
                "(assistant_message_id, cue, outcome, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                payload,
            )
            conn.commit()
        except Exception:
            log.warning("cue decision insert failed", exc_info=True)
            return 0
        return len(payload)

    # ── reads ─────────────────────────────────────────────────────────

    def _window_clause(
        self, window_days: int | None,
    ) -> tuple[str, list[object]]:
        if window_days is None:
            return "", []
        cutoff = timephrase.utcnow() - timedelta(days=max(0, int(window_days)))
        return " AND created_at >= ?", [cutoff.isoformat()]

    def reach(self, *, window_days: int | None = None) -> list[dict]:
        """Armed-to-surfaced ratio per cue -- the headline diagnostic.

        A cue whose ``reach_rate`` is near zero is producing work that is
        structurally unreachable: the worker runs, the finding lands in a
        journal, and no provider ever renders it. That is the case which
        previously looked identical to a cue quietly doing its job.

        A low rate is not automatically a bug -- a topic-gated cue that
        correctly stays quiet while the conversation is elsewhere *should*
        decline often. The number to act on is a rate near zero over a long
        window, which means the gate never matches at all.

        Which is why there are two rates. ``reach_rate`` divides by every
        armed turn and is the honest measure of throughput. But a cue
        inside its own multi-day ``surface_cooldown_hours`` is armed on
        every turn of that cooldown while being unable to surface on any
        of them, so for the deliberately scarce types the denominator is
        mostly turns that were never in play: ``self_callback`` reads 2%
        and is behaving exactly as designed. ``eligible_rate`` divides by
        the turns the cue could actually have surfaced on -- armed, minus
        the declines that :data:`INELIGIBLE_REASONS` marks as "never had a
        chance" -- so a rate near zero there is a real finding about the
        gate rather than a restatement of the cadence.

        ``eligible=0`` is itself informative and is left as ``None``
        rather than folded to zero: the cue was stocked throughout and
        never once in play, which is either a correctly rare cue or a
        cadence set faster than the shelf refills.
        """
        clause, params = self._window_clause(window_days)
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            rows = conn.execute(
                "SELECT cue, outcome, reason, COUNT(*) AS n "
                "FROM cue_decisions "
                f"WHERE 1=1{clause} "
                "GROUP BY cue, outcome, reason",
                tuple(params),
            ).fetchall()
        except Exception:
            log.warning("cue reach read failed", exc_info=True)
            return []
        # Aggregated in Python rather than SQL because eligibility is a
        # prefix test on the reason (``lost_priority:<winner>``), and a
        # LIKE-per-reason clause here would be a second place the
        # vocabulary is spelled out -- the exact drift the shared
        # predicate exists to prevent.
        from app.core.proactive.cue_accounting import is_eligible_decline

        armed: dict[str, int] = {}
        surfaced: dict[str, int] = {}
        eligible: dict[str, int] = {}
        for r in rows:
            cue = str(r[0] or "")
            count = int(r[3] or 0)
            armed[cue] = armed.get(cue, 0) + count
            eligible.setdefault(cue, 0)
            surfaced.setdefault(cue, 0)
            if str(r[1] or "") == OUTCOME_SURFACED:
                surfaced[cue] += count
                eligible[cue] += count
            elif is_eligible_decline(str(r[2] or "")):
                eligible[cue] += count
        out = []
        for cue in sorted(armed, key=lambda name: (-armed[name], name)):
            total = armed[cue]
            hit = surfaced[cue]
            in_play = eligible[cue]
            out.append({
                "cue": cue,
                "armed": total,
                "surfaced": hit,
                "declined": total - hit,
                "eligible": in_play,
                "reach_rate": round(hit / total, 4) if total else None,
                "eligible_rate": (
                    round(hit / in_play, 4) if in_play else None
                ),
            })
        return out

    def decline_reasons(
        self, *, window_days: int | None = None, cue: str | None = None,
    ) -> list[dict]:
        """Why armed cues did not get through, most common first.

        This is what turns "the cue vanished" into "it lost to
        ``turning_over`` eleven times this week", which is actionable in a
        way the previous debug logs were not.
        """
        clause, params = self._window_clause(window_days)
        where = ["outcome != ?"]
        args: list[object] = [OUTCOME_SURFACED]
        if cue:
            where.append("cue = ?")
            args.append(str(cue))
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            rows = conn.execute(
                "SELECT cue, reason, COUNT(*) AS n "
                "FROM cue_decisions "
                f"WHERE {' AND '.join(where)}{clause} "
                "GROUP BY cue, reason "
                "ORDER BY n DESC",
                (*args, *params),
            ).fetchall()
        except Exception:
            log.warning("cue decline reasons read failed", exc_info=True)
            return []
        return [
            {
                "cue": str(r[0] or ""),
                "reason": str(r[1] or ""),
                "count": int(r[2] or 0),
            }
            for r in rows
        ]

    def count(self) -> int:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            row = conn.execute("SELECT COUNT(*) FROM cue_decisions").fetchone()
        except Exception:
            return 0
        return int(row[0]) if row else 0

    def prune(self, keep_days: int) -> int:
        """Drop rows older than ``keep_days``. Returns rows removed.

        Not scheduled by anything yet (P34 owns retention policy). Same
        reasoning as the surfacing ledger: a ratio computed over years
        stops responding to how the cue machinery behaves now.
        """
        if int(keep_days) <= 0:
            return 0
        cutoff = timephrase.utcnow() - timedelta(days=int(keep_days))
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            cursor = conn.execute(
                "DELETE FROM cue_decisions WHERE created_at < ?",
                (cutoff.isoformat(),),
            )
            conn.commit()
        except Exception:
            log.warning("cue decision prune failed", exc_info=True)
            return 0
        return int(cursor.rowcount or 0)


__all__ = ["CueDecisionStore"]
