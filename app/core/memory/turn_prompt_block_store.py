"""Which prompt blocks were in front of Aiko on each turn (K90).

Sibling of :class:`~app.core.memory.cue_decision_store.CueDecisionStore`,
one level wider. That one records the ~15 registered cues and only when
they were *armed*, which answers "did this cue reach the prompt". This
one records every block that rendered, armed or not, cue or not, which
answers the question K90 needs: **how often does each of the ~120 blocks
actually fire, and did that change when we changed something?**

The distinction matters because the two failure modes it separates are
invisible from the transcript alone. A steer that never renders and a
steer that renders and gets ignored produce the same conversation. K52
through K56 shipped five interacting mechanisms on judgement, all of
them "working", while the behaviour they existed to fix persisted --
this table is how the next change avoids that.

**Only blocks that rendered get a row.** The assembler's
``block_chars`` reports every registered block including the empty ones,
and persisting those would write ~120 rows a turn to record nothing. The
denominator for a rate is ``COUNT(DISTINCT assistant_message_id)`` over
the same window, which is exact rather than approximate: every recorded
turn writes at least the persona block, so no turn can be invisible to
the count.

Never raises. A diagnostic write must not be able to break a turn.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import TYPE_CHECKING

from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.turn_prompt_block_store")


class TurnPromptBlockStore:
    """Writes and reads the ``turn_prompt_blocks`` table."""

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    # ── writes ────────────────────────────────────────────────────────

    def add_turn(
        self,
        assistant_message_id: int,
        block_chars: Mapping[str, int],
    ) -> int:
        """Record the non-empty blocks of one assembly. Returns rows written.

        One transaction per turn: a turn's block set is a unit, and a
        half-written turn would understate every rate computed from it
        while still counting in the denominator.
        """
        if not block_chars or int(assistant_message_id) <= 0:
            return 0
        now = timephrase.utcnow().isoformat()
        payload = [
            (int(assistant_message_id), str(name), int(chars), now)
            for name, chars in block_chars.items()
            if name and int(chars or 0) > 0
        ]
        if not payload:
            return 0
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            conn.executemany(
                "INSERT INTO turn_prompt_blocks "
                "(assistant_message_id, block, chars, created_at) "
                "VALUES (?, ?, ?, ?)",
                payload,
            )
            conn.commit()
        except Exception:
            log.warning("prompt block insert failed", exc_info=True)
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

    def turns_recorded(self, *, window_days: int | None = None) -> int:
        """Turns with at least one recorded block -- the rate denominator."""
        clause, params = self._window_clause(window_days)
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            row = conn.execute(
                "SELECT COUNT(DISTINCT assistant_message_id) "
                f"FROM turn_prompt_blocks WHERE 1=1{clause}",
                tuple(params),
            ).fetchone()
        except Exception:
            log.warning("prompt block turn count failed", exc_info=True)
            return 0
        return int(row[0]) if row else 0

    def firing_rates(
        self, *, window_days: int | None = None,
    ) -> list[dict]:
        """Per block: how many turns it fired on, and how big it was.

        ``rate`` is turns-fired over turns-recorded, so 1.0 is an
        always-on block (the persona) and a number near zero is a block
        whose gate almost never matches. Neither is automatically wrong
        -- a topic-gated steer *should* be rare -- but a steer that is
        meant to fire weekly and reads 0.0 over a month is not gated,
        it is broken.
        """
        turns = self.turns_recorded(window_days=window_days)
        clause, params = self._window_clause(window_days)
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            rows = conn.execute(
                "SELECT block, COUNT(DISTINCT assistant_message_id) AS fired, "
                "       AVG(chars) AS avg_chars "
                "FROM turn_prompt_blocks "
                f"WHERE 1=1{clause} "
                "GROUP BY block "
                "ORDER BY fired DESC, block ASC",
                tuple(params),
            ).fetchall()
        except Exception:
            log.warning("prompt block firing read failed", exc_info=True)
            return []
        out = []
        for r in rows:
            fired = int(r[1] or 0)
            out.append({
                "block": str(r[0] or ""),
                "fired": fired,
                "turns": turns,
                "rate": round(fired / turns, 4) if turns else None,
                "avg_chars": round(float(r[2] or 0.0), 1),
            })
        return out

    def count(self) -> int:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM turn_prompt_blocks"
            ).fetchone()
        except Exception:
            return 0
        return int(row[0]) if row else 0

    def prune(self, keep_days: int) -> int:
        """Drop rows older than ``keep_days``. Returns rows removed.

        Same reasoning as the cue ledger: a firing rate averaged over
        years stops responding to how the prompt behaves now. This table
        grows faster than that one -- tens of rows per turn rather than
        a handful -- so it is the more likely of the two to want it.
        """
        if int(keep_days) <= 0:
            return 0
        cutoff = timephrase.utcnow() - timedelta(days=int(keep_days))
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            cursor = conn.execute(
                "DELETE FROM turn_prompt_blocks WHERE created_at < ?",
                (cutoff.isoformat(),),
            )
            conn.commit()
        except Exception:
            log.warning("prompt block prune failed", exc_info=True)
            return 0
        return int(cursor.rowcount or 0)


__all__ = ["TurnPromptBlockStore"]
