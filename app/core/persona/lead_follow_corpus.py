"""K90: assemble the lead/follow corpus from the message log.

The layer between :mod:`app.core.persona.lead_follow_metrics` (pure
string maths) and its two consumers -- ``scripts/lead_follow_report.py``
and the ``/api/lead-follow`` diagnostics endpoint. It takes a
:class:`sqlite3.Connection` rather than a store object precisely so the
script can open the live database read-only and run without booting the
app, while the endpoint hands it the connection it already has.

The only interesting logic here is turn pairing, and it is the part
worth being careful about: score a reply against the wrong user turn and
every number downstream still looks entirely plausible.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.persona.lead_follow_metrics import (
    TurnMetrics,
    as_dict,
    is_measurable,
    measure_turn,
    summarise,
)

# Cohort windows in days back from now. The narrow ones show the effect
# of a recent change; ``None`` is all history, which gives the
# pre-change baseline in the same run so one invocation is already a
# before/after.
WINDOWS: tuple[float | None, ...] = (7.0, 30.0, None)

# How many prior messages count as "the recent history" a word has to be
# absent from before it counts as her own material. Six is roughly three
# exchanges: long enough that continuing the current thread doesn't
# score as leading, short enough that a topic from last week does.
HISTORY_MESSAGES = 6


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,),
    ).fetchone()
    return row is not None


# ── the turn corpus ─────────────────────────────────────────────────


def load_turns(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Pair every assistant reply with the user turn it answered.

    Walks each session in id order and keeps the most recent user
    message as the prompt for the next assistant reply. Two rules that
    are easy to get wrong:

    - A reply with no user turn before it -- a proactive nudge, or the
      first message of a restored session -- is **kept**, with an empty
      prompt. She led that turn by definition, and dropping them would
      bias the corpus against exactly the behaviour being measured.
    - The paired user turn is excluded from that reply's history window.
      Counting it in both places would make every reply look like it was
      echoing the recent context.
    """
    rows = conn.execute(
        "SELECT id, session_id, role, content, created_at "
        "FROM messages WHERE role IN ('user', 'assistant') "
        "ORDER BY session_id ASC, id ASC"
    ).fetchall()

    turns: list[dict[str, Any]] = []
    session = None
    history: list[str] = []
    pending_user = ""
    for row in rows:
        # Positional access throughout this module: the CLI opens its
        # own connection but the endpoint is handed ``ChatDatabase``'s,
        # which has no ``row_factory``, and setting one on a shared
        # connection would change what every other store sees.
        row_id, row_session, role, raw, created_at = row
        if row_session != session:
            session = row_session
            history = []
            pending_user = ""
        content = str(raw or "")
        if role == "user":
            pending_user = content
            history.append(content)
            continue
        if is_measurable(content):
            turns.append({
                "id": int(row_id),
                "session_id": str(session or ""),
                "at": str(created_at or ""),
                "reply": content,
                "user_text": pending_user,
                "history": list(history[:-1][-HISTORY_MESSAGES:]),
            })
        history.append(content)
        pending_user = ""
    return turns


def window_slice(
    turns: list[dict[str, Any]],
    now: datetime,
    days: float | None,
) -> list[dict[str, Any]]:
    if days is None:
        return turns
    cutoff = now - timedelta(days=float(days))
    out = []
    for turn in turns:
        stamp = _parse(turn["at"])
        if stamp is not None and stamp >= cutoff:
            out.append(turn)
    return out


def measure(turns: list[dict[str, Any]]) -> list[TurnMetrics]:
    return [
        measure_turn(t["reply"], t["user_text"], t["history"])
        for t in turns
    ]


# ── block firing ────────────────────────────────────────────────────


def block_firing(
    conn: sqlite3.Connection, now: datetime, days: float | None,
) -> dict[str, Any]:
    """Per-block firing rates over the window, or a reason there are none.

    The "no rows yet" case returns a *reason* rather than an empty list,
    because a block that has never been recorded and a block that never
    fires are the same zero, and a diagnostic that can't tell them apart
    is worse than one that admits it doesn't know.
    """
    if not _has_table(conn, "turn_prompt_blocks"):
        return {
            "available": False,
            "reason": (
                "no turn_prompt_blocks table -- this database predates "
                "schema v35"
            ),
            "turns": 0,
            "blocks": [],
        }
    params: list[Any] = []
    clause = ""
    if days is not None:
        clause = " WHERE created_at >= ?"
        params.append((now - timedelta(days=float(days))).isoformat())

    total = conn.execute(
        "SELECT COUNT(DISTINCT assistant_message_id) "
        f"FROM turn_prompt_blocks{clause}",
        tuple(params),
    ).fetchone()[0]
    total = int(total or 0)
    if total == 0:
        return {
            "available": False,
            "reason": (
                "no turns recorded in this window -- block firing is not "
                "retroactive, so it fills in only as the upgraded build "
                "runs"
            ),
            "turns": 0,
            "blocks": [],
        }

    rows = conn.execute(
        "SELECT block, COUNT(DISTINCT assistant_message_id) AS fired, "
        "       AVG(chars) AS avg_chars "
        f"FROM turn_prompt_blocks{clause} "
        "GROUP BY block ORDER BY fired DESC, block ASC",
        tuple(params),
    ).fetchall()
    return {
        "available": True,
        "reason": "",
        "turns": total,
        "blocks": [
            {
                "block": str(r[0] or ""),
                "fired": int(r[1] or 0),
                "rate": round(int(r[1] or 0) / total, 4),
                "per_hundred_turns": round(100.0 * int(r[1] or 0) / total, 1),
                "avg_chars": round(float(r[2] or 0.0), 1),
            }
            for r in rows
        ],
    }


# ── collect ─────────────────────────────────────────────────────────


def collect(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    windows: tuple[float | None, ...] = WINDOWS,
) -> dict[str, Any]:
    """The whole report as one JSON-ready dict."""
    turns = load_turns(conn)
    cohorts = []
    for days in windows:
        subset = window_slice(turns, now, days)
        summary = as_dict(summarise(measure(subset)))
        summary["window_days"] = days
        summary["blocks"] = block_firing(conn, now, days)
        cohorts.append(summary)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "total_assistant_turns": len(turns),
        "history_messages": HISTORY_MESSAGES,
        "cohorts": cohorts,
    }


__all__ = [
    "HISTORY_MESSAGES",
    "WINDOWS",
    "block_firing",
    "collect",
    "load_turns",
    "measure",
    "window_slice",
]
