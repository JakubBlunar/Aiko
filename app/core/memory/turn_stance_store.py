"""One row per turn: the stance K92 would have taken (phase 1).

Third of the per-turn accounting tables, and the narrowest.
:class:`~app.core.memory.cue_decision_store.CueDecisionStore` records the
armed cues, :class:`~app.core.memory.turn_prompt_block_store.TurnPromptBlockStore`
records every block that rendered, and this records the single decision
those blocks add up to. The other two answer "what was in front of her";
this answers "what was she being asked to *do*".

Rows are keyed one-per-turn and written with ``INSERT OR REPLACE`` so a
backfill can be re-run without doubling anything. That is the one real
difference from its two siblings, and it exists because this table's
inputs are all durable -- the arbiter is a pure function over
``turn_prompt_blocks`` and ``messages``, so unlike K90 it can be
recomputed for turns that happened before it was written. Re-running
after a rule change is the intended workflow, not an accident to guard
against.

The column worth watching is not ``stance``. It is the disagreement
between ``desire`` and ``ceiling``: how often the providers pushed Aiko
to take the floor at a moment when taking it would have read badly.

Never raises. A diagnostic write must not be able to break a turn.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.conversation.stance import StanceDecision
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.turn_stance_store")


class TurnStanceStore:
    """Writes and reads the ``turn_stance`` table."""

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    # ── writes ────────────────────────────────────────────────────────

    def add_turn(
        self,
        assistant_message_id: int,
        decision: "StanceDecision",
        *,
        created_at: str | None = None,
    ) -> bool:
        """Record one turn's stance. Returns whether a row was written.

        ``created_at`` is injectable so a backfill can stamp each row
        with the timestamp of the turn it describes rather than the
        moment of the backfill -- otherwise every windowed read would
        see the whole history land in one second.
        """
        message_id = int(assistant_message_id or 0)
        if message_id <= 0:
            return False
        stamp = created_at or timephrase.utcnow().isoformat()
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            conn.execute(
                "INSERT OR REPLACE INTO turn_stance "
                "(assistant_message_id, stance, reason, desire, ceiling, "
                " shortlist, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    str(decision.stance),
                    str(decision.reason),
                    str(decision.desire),
                    str(decision.ceiling),
                    decision.shortlist_text(),
                    stamp,
                ),
            )
            conn.commit()
        except Exception:
            log.warning("turn stance insert failed", exc_info=True)
            return False
        return True

    # ── reads ─────────────────────────────────────────────────────────

    def _window_clause(
        self, window_days: int | None,
    ) -> tuple[str, list[object]]:
        if window_days is None:
            return "", []
        cutoff = timephrase.utcnow() - timedelta(days=max(0, int(window_days)))
        return " AND created_at >= ?", [cutoff.isoformat()]

    def distribution(self, *, window_days: int | None = None) -> list[dict]:
        """Per stance: turns chosen, turns wanted, and the shares.

        Both columns matter and they are not the same question.
        ``chosen`` is what the arbiter settled on; ``wanted`` is what the
        providers put on the table before the interruption ceiling
        clamped it. A stance with a high ``wanted`` and a low ``chosen``
        is one Aiko is repeatedly pushed toward at the wrong moments.
        """
        clause, params = self._window_clause(window_days)
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            rows = conn.execute(
                "SELECT stance, COUNT(*) FROM turn_stance "
                f"WHERE 1=1{clause} GROUP BY stance",
                tuple(params),
            ).fetchall()
            wanted = conn.execute(
                "SELECT desire, COUNT(*) FROM turn_stance "
                f"WHERE 1=1{clause} GROUP BY desire",
                tuple(params),
            ).fetchall()
        except Exception:
            log.warning("turn stance distribution failed", exc_info=True)
            return []
        chosen_of = {str(r[0]): int(r[1]) for r in rows}
        wanted_of = {str(r[0]): int(r[1]) for r in wanted}
        total = sum(chosen_of.values())
        from app.core.conversation.stance import STANCE_LADDER

        out = []
        for stance in STANCE_LADDER:
            chosen = chosen_of.get(stance, 0)
            out.append({
                "stance": stance,
                "chosen": chosen,
                "wanted": wanted_of.get(stance, 0),
                "turns": total,
                "share": round(chosen / total, 4) if total else None,
            })
        return out

    def clamps(self, *, window_days: int | None = None) -> list[dict]:
        """Turns where his turn held her back, grouped by the constraint.

        The direct readout for K95: each row is a reason the ceiling
        bound, how many turns it bound on, and which stance was being
        held back. If this table is empty the interruption ceiling is
        costing nothing and can be simplified; if one reason dominates,
        that is the rule to get right first.
        """
        clause, params = self._window_clause(window_days)
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            rows = conn.execute(
                "SELECT reason, desire, stance, COUNT(*) AS n "
                "FROM turn_stance "
                f"WHERE desire <> stance{clause} "
                "GROUP BY reason, desire, stance ORDER BY n DESC",
                tuple(params),
            ).fetchall()
        except Exception:
            log.warning("turn stance clamp read failed", exc_info=True)
            return []
        return [
            {
                "reason": str(r[0] or ""),
                "desire": str(r[1] or ""),
                "stance": str(r[2] or ""),
                "turns": int(r[3] or 0),
            }
            for r in rows
        ]

    def turns_recorded(self, *, window_days: int | None = None) -> int:
        clause, params = self._window_clause(window_days)
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            row = conn.execute(
                f"SELECT COUNT(*) FROM turn_stance WHERE 1=1{clause}",
                tuple(params),
            ).fetchone()
        except Exception:
            log.warning("turn stance count failed", exc_info=True)
            return 0
        return int(row[0]) if row else 0

    def count(self) -> int:
        return self.turns_recorded(window_days=None)

    def prune(self, keep_days: int) -> int:
        """Drop rows older than ``keep_days``. Returns rows removed.

        One row per turn rather than tens, so this grows far slower than
        its two siblings and is the least likely of the three to need
        pruning at all. It exists for symmetry with them.
        """
        if int(keep_days) <= 0:
            return 0
        cutoff = timephrase.utcnow() - timedelta(days=int(keep_days))
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            cursor = conn.execute(
                "DELETE FROM turn_stance WHERE created_at < ?",
                (cutoff.isoformat(),),
            )
            conn.commit()
        except Exception:
            log.warning("turn stance prune failed", exc_info=True)
            return 0
        return int(cursor.rowcount or 0)


__all__ = ["TurnStanceStore"]
