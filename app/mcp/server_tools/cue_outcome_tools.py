"""G4 -- did the cue this worker produced ever reach Aiko?

There are 50-odd workers on the idle scheduler, many making LLM calls, and
``get_idle_workers_status`` could only ever say that one **ran**: overdue
seconds, a duration EMA, error counts. Whether the cue it produced actually
got in front of Aiko was unanswerable, because every way of losing one is
silent -- a topic gate returning ``""``, the gap-cue priority mutex, the
K47 question-balance veto. A worker whose gate never matches looked exactly
like a worker quietly doing its job.

This is the window onto that. Read ``armed`` as the denominator: it counts
turns where the cue *had material waiting*, not turns where a worker ran,
so a worker that writes ten findings before one gets through is not ten
failures -- it is one delivery and nine supersessions.
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
    cue: str | None = None,
) -> dict[str, Any]:
    store = getattr(session, "_cue_decision_store", None)
    if store is None:
        return {
            "enabled": False,
            "hint": (
                "Cue accounting is off (agent.cue_accounting_enabled) or the "
                "chat database is unavailable, so nothing is being recorded."
            ),
        }

    from app.core.proactive.cue_accounting import COARSE_ARMING, CUE_SPECS

    total = store.count()
    reach = store.reach(window_days=window_days)
    declines = store.decline_reasons(window_days=window_days, cue=cue)

    # Registered cues with no rows at all. Distinct from a low reach rate
    # and a more common finding: a cue whose journal is never written, or
    # whose arming signal this module reads wrongly, produces silence here
    # rather than a bad number -- and silence is easy to miss.
    seen = {row["cue"] for row in reach}
    never_armed = sorted(set(CUE_SPECS) - seen)

    report: dict[str, Any] = {
        "enabled": True,
        "window_days": window_days,
        "rows_total": total,
        "registered_cues": len(CUE_SPECS),
        "reach": reach,
        "decline_reasons": declines,
        "never_armed": never_armed,
        "coarse_arming": sorted(COARSE_ARMING),
    }
    if total == 0:
        report["hint"] = (
            "No rows yet. A row is written only on turns where a cue had "
            "material waiting, which is rare for most cues -- let the idle "
            "workers run, or force one (force_turning_over, "
            "force_follow_up, ...) and take a turn."
        )
    report["reading_guide"] = (
        "reach_rate = surfaced / armed, where armed counts turns the cue "
        "had something to say. A low rate is not automatically a bug: a "
        "topic-gated cue that stays quiet while the conversation is "
        "elsewhere is working correctly. What to act on is a rate at or "
        "near zero over a long window, which means the gate never matches "
        "at all and every run of that worker is wasted -- for an "
        "LLM-calling worker, wasted tokens. "
        "decline_reasons says which mechanism refused it. "
        "'lost_priority:<cue>' names the winner of the gap-cue mutex, "
        "which is a deterministic priority order, NOT a tie-break -- so the "
        "same cue loses every time both are armed, and a cue that only "
        "ever loses to one specific rival is structurally unreachable "
        "rather than unlucky. 'question_balance' is K47's share-first "
        "countdown vetoing question-shaped cues wholesale. 'provider' is "
        "the catch-all for a cue's own internal gates (cooldown, topic "
        "gate, no candidate cleared the picker) -- those are not yet "
        "individually attributed. "
        "coarse_arming lists cues whose journals dedupe by a per-topic key "
        "set rather than a single watermark: arming for those degrades to "
        "'the ring is non-empty', which OVER-counts, so their reach_rate "
        "is a floor rather than an estimate. "
        "never_armed is the loudest signal here -- a registered cue with "
        "no rows at all either never gets written by its worker or is "
        "being read wrongly by the arming model, and neither shows up as a "
        "bad rate."
    )
    return report


def register(mcp, session: "SessionController") -> None:
    @mcp.tool()
    def get_cue_outcomes(
        window_days: int = 30,
        cue: str = "",
    ) -> str:
        """G4 -- of the times a worker cue was ready, how often did it land.

        Reports the armed-to-surfaced ratio per cue, the reasons armed cues
        were declined, and which registered cues have never been armed at
        all. This is the first view that can distinguish a worker whose
        output never reaches the prompt from one quietly succeeding.

        ``cue`` narrows ``decline_reasons`` to one cue name (the reach
        table always covers all of them, since the comparison between cues
        is most of the signal). Defaults to the last 30 days for the same
        reason the surfacing ledger does -- a windowed aggregate stays
        cheap and answers the more useful question. Pass ``window_days=0``
        for lifetime.
        """
        try:
            return json.dumps(
                build_report(
                    session,
                    window_days=(None if int(window_days) <= 0
                                 else int(window_days)),
                    cue=(cue.strip() or None),
                ),
                indent=2,
                default=str,
            )
        except Exception as exc:
            return f"get_cue_outcomes failed: {exc}"
