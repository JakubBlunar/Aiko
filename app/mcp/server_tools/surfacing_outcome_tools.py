"""L37 -- did the things Aiko surfaced into the prompt actually land?

Surfacing has always been write-only: a concept that reached the prompt
two hundred times to no visible effect looked exactly like one that opened
up a good conversation every time, because the only trace either left was
a habituation timestamp. The ``surfacing_outcomes`` ledger joins what was
surfaced against what happened next, and this tool is the only window onto
it -- without a view, the measurement would be as invisible as the thing
it measures.

Read the ``denominators``, not the rates. Every rate here is over settled
rows only, and a 1-for-1 item shows the same 100% as a 40-of-50 one. That
is why ``settled`` is reported beside every rate and why the leaderboard
takes a ``min_settled`` floor.

``unsettled`` rows are expected, not broken: the engagement label comes
from the user's *next* message, so the last turn of every session never
settles. A number that climbs with total rows means the settle path has
stopped running.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController


log = logging.getLogger("app.mcp.server")


def build_report(
    session: "SessionController",
    *,
    window_days: int | None,
    min_settled: int,
    top: int,
) -> dict[str, Any]:
    store = getattr(session, "_surfacing_outcome_store", None)
    if store is None:
        return {
            "enabled": False,
            "hint": (
                "The ledger is off (agent.surfacing_ledger_enabled) or the "
                "chat database is unavailable, so nothing is being recorded."
            ),
        }

    total = store.count()
    unsettled = store.unsettled_count()
    report: dict[str, Any] = {
        "enabled": True,
        "window_days": window_days,
        "rows_total": total,
        "rows_unsettled": unsettled,
        "pending_message_id": int(
            getattr(session, "_prev_surfacing_message_id", 0) or 0
        ),
        "by_lane": store.lane_breakdown(window_days=window_days),
        "by_echo_kind": store.echo_breakdown(window_days=window_days),
        "semantic_floor_replay": store.semantic_floor_candidates(
            window_days=window_days,
        ),
        "leaderboard": store.leaderboard(
            window_days=window_days, min_settled=min_settled, limit=top,
        ),
    }
    if total == 0:
        report["hint"] = (
            "No rows yet. The ledger only records turns where something was "
            "surfaced into relevant_context; run a few turns that touch "
            "remembered material first."
        )
    elif not report["leaderboard"]:
        report["hint"] = (
            f"{total} row(s) recorded but none meet min_settled="
            f"{min_settled}. Outcomes settle one turn late, so a fresh "
            "ledger is mostly unsettled by design."
        )
    report["reading_guide"] = (
        f"window_days={window_days} bounds every aggregate here "
        f"({'lifetime' if window_days is None else 'recent flow'}); a "
        "lifetime figure is only meaningful against the same window "
        "measured at another time. "
        "engaged_rate is over settled rows only -- always read it next to "
        "settled, since a 1-for-1 item scores the same as 40-of-50. It is "
        "a property of the TURN, not of the item: the median turn surfaces "
        "67 items and they all share its one label, so per-item engaged "
        "rates are noise (measured split-half reliability 0.05) and must "
        "not be used to rank items. "
        "echo_rate is over judged rows -- the ones an echo test actually "
        "ran on, which is fewer than surfaced and zero for clusters -- and "
        "asks a different question: did Aiko use the item at all, "
        "regardless of how the user reacted. That one IS attributable to "
        "the item (reliability 0.60), which is why L38 standing reads it. "
        "High echo with low engaged means she takes the "
        "bait and the user doesn't. rows_unsettled is expected to hold "
        "roughly one turn's worth per session, since the label comes from "
        "the following message; a number growing in step with rows_total "
        "means settling has stopped. "
        "by_echo_kind and semantic_floor_replay answer F12's deferred "
        "question: a semantic echo currently earns less memory-retention "
        "credit than a quoted one, on the theory that surfaced items were "
        "already picked for topical similarity so cosine here partly "
        "measures 'was on topic' rather than 'she used it'. If semantic "
        "rows engage about as often as lexical ones, that discount is "
        "unjustified; if they engage no better than rows with no echo, it "
        "was right. semantic_floor_replay re-runs each candidate floor "
        "over the recorded cosines (misses included) -- a floor whose "
        "engaged_rate is flat all the way up is measuring topic, not use."
    )
    return report


def register(mcp, session: "SessionController") -> None:
    @mcp.tool()
    def get_surfacing_outcomes(
        window_days: int = 30,
        min_settled: int = 1,
        top: int = 20,
    ) -> str:
        """L37 -- which surfaced memories and concepts actually landed.

        Reports a per-item leaderboard from the surfacing outcome ledger
        with engaged counts *and* denominators, a per-lane rollup (does
        the whole activation lane earn its tokens?), and the unsettled-row
        count that doubles as the ledger's health metric.

        Defaults to the last 30 days, which is both the more useful
        question and vastly the cheaper one: the aggregate is bounded by
        rows *in the window*, so a windowed board stays near a
        millisecond while the lifetime one grows linearly with history
        (measured at 200k rows: 23 ms windowed against 578 ms lifetime).
        Pass ``window_days=0`` for lifetime when you specifically want the
        all-time stock and can afford it.

        ``min_settled`` keeps single-observation noise off the top of the
        board -- raise it once there is enough history. Nothing consumes
        these numbers yet (that is L38); this is the view for deciding
        whether the signal is worth acting on.
        """
        try:
            return json.dumps(
                build_report(
                    session,
                    window_days=(None if int(window_days) <= 0
                                 else int(window_days)),
                    min_settled=int(min_settled),
                    top=int(top),
                ),
                indent=2,
                default=str,
            )
        except Exception as exc:
            return f"get_surfacing_outcomes failed: {exc}"
