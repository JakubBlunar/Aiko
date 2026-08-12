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

Since the cue pool landed there is a second, stricter measure alongside
it. Reach stops at "the block rendered"; the pool's ``used`` only counts
cues whose subject actually turned up in what was said. The gap between
the two is the interesting part -- a cue with high reach and no uses is
one Aiko is being handed and quietly dropping, which the reach number on
its own reads as a success.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController


log = logging.getLogger("app.mcp.server")


def _pool_section(session: "SessionController") -> dict[str, Any]:
    """Depth and outcomes per cue type, straight off ``cue_pool``.

    Independent of the decision store: the pool is the workers' own
    bookkeeping, so this stays readable even with cue accounting off.
    """
    from app.core.proactive.cue_accounting import CUE_POLICIES

    try:
        stats = session.cue_pool_stats()
    except Exception:
        log.debug("cue pool stats failed", exc_info=True)
        stats = None
    if stats is None:
        return {"enabled": False}

    by_type = {str(entry.get("cue_type") or ""): entry for entry in stats}
    rows: list[dict[str, Any]] = []
    for name, policy in sorted(CUE_POLICIES.items()):
        entry = by_type.get(name, {})
        pending = int(entry.get("pending", 0) or 0)
        rows.append({
            "cue": name,
            # Depth against the shelf the worker is trying to fill --
            # a deficit of zero is why a migrated worker stays dormant.
            "pending": pending,
            "target": policy.inventory_target,
            "deficit": max(0, policy.inventory_target - pending),
            "surfaced": int(entry.get("surfaced", 0) or 0),
            "awaiting": int(entry.get("awaiting", 0) or 0),
            "used": int(entry.get("used", 0) or 0),
            "expired": int(entry.get("expired", 0) or 0),
            "superseded": int(entry.get("superseded", 0) or 0),
            "asks": int(entry.get("asks", 0) or 0),
            "mean_surfacings_before_use": entry.get(
                "mean_surfacings_before_use",
            ),
            "fulfilment": policy.fulfilment,
        })
    return {"enabled": True, "by_type": rows}


def build_report(
    session: "SessionController",
    *,
    window_days: int | None,
    cue: str | None = None,
) -> dict[str, Any]:
    pool = _pool_section(session)
    store = getattr(session, "_cue_decision_store", None)
    if store is None:
        return {
            "enabled": False,
            "pool": pool,
            "hint": (
                "Cue accounting is off (agent.cue_accounting_enabled) or the "
                "chat database is unavailable, so no reach rows are being "
                "recorded. The pool section above is unaffected -- it is the "
                "workers' own inventory, not a decision log."
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
        "pool": pool,
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
        "countdown vetoing question-shaped cues wholesale. The provider's "
        "own reasons are 'topic_miss' (stock existed, none of it about "
        "what he said), 'importance_floor' (topical enough, too light for "
        "the slot), 'cadence_block' (a cooldown or minimum gap said not "
        "yet), 'no_stock' (the shelf was empty at the moment of asking, "
        "which is a supply-timing finding rather than a gate) and "
        "'cross_lane' (another lane had already claimed the material). "
        "'provider' is the remaining catch-all and means the provider "
        "declined WITHOUT saying why -- a cue still dominated by it has an "
        "uninstrumented bail point, not a diagnosed cause. "
        "coarse_arming lists cues whose journals dedupe by a per-topic key "
        "set rather than a single watermark: arming for those degrades to "
        "'the ring is non-empty', which OVER-counts, so their reach_rate "
        "is a floor rather than an estimate. "
        "never_armed is the loudest signal here -- a registered cue with "
        "no rows at all either never gets written by its worker or is "
        "being read wrongly by the arming model, and neither shows up as a "
        "bad rate. "
        "pool.by_type is the stricter half and covers only the migrated "
        "types. 'used' means post-turn matching found the cue's subject in "
        "what was actually said, so used vs. expired is the real verdict "
        "where reach_rate only says the block rendered -- a type with reach "
        "and no uses is one Aiko is handed and drops. "
        "deficit is why a migrated worker is dormant: at zero its shelf is "
        "full and it reports no pressure, which is correct rather than "
        "broken. A target of 0 means no worker stocks that type at all -- "
        "it is event-armed, and its rows exist only as retries of "
        "surfacings Aiko did not use, so an empty shelf there is the "
        "resting state and not a gap. "
        "mean_surfacings_before_use is the framing check -- 1.0 means she "
        "takes a cue the first time she sees it, and a number near the "
        "type's max_surfacings means the cue line is not reading as "
        "something to act on."
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

        The ``pool`` section adds the stricter measure for the cue types
        that have moved onto ``cue_pool``: shelf depth against each type's
        inventory target, used vs. expired, and how many surfacings a cue
        of that type needs on average before Aiko actually spends it.

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
