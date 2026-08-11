"""DT1 virtual-clock MCP tools -- drive the app's sense of "now".

Most of Aiko's behaviour is time-gated, and before these tools the only
way to exercise decay, promotion age, anniversaries, gap-return or any
inner-life cooldown in a *running* instance was to wait real hours or
days. These turn that into one call.

Two levers, and picking the wrong one is the usual mistake:

- ``advance_clock`` shifts **wall-clock** time. Drives anniversaries,
  cooldowns, candidate TTLs and promotion age.
- ``advance_engagement`` credits **engaged** time -- accumulated active
  conversation, roughly an hour per "day". This is the domain concept
  and memory *decay* run on, and wall-clock advances do not touch it.

Everything here is inert unless the process was started with
``AIKO_DEBUG_CLOCK=1``. See ``app/core/infra/debug_clock.py`` for the
seam itself and for what is deliberately left on real time.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController


log = logging.getLogger("app.mcp.server")

_NO_CLOCK = (
    "debug clock unavailable on this session (it failed to initialise); "
    "see the startup log"
)


def register(mcp, session: "SessionController") -> None:
    def _clock():
        return session.debug_clock

    def _dump(payload: object) -> str:
        return json.dumps(payload, indent=2, default=str)

    @mcp.tool()
    def get_clock_status() -> str:
        """Report whether the app's "now" is currently shifted.

        Shows the gate state, the active offset, real vs virtual now,
        and how much synthetic engagement has been credited. Safe to
        call at any time -- start here if time-gated behaviour is
        surprising you, since a forgotten offset looks like a bug
        everywhere else.
        """
        clock = _clock()
        if clock is None:
            return _NO_CLOCK
        return _dump(clock.status())

    @mcp.tool()
    def advance_clock(days: float = 0.0, hours: float = 0.0) -> str:
        """Shift wall-clock "now" forward by the given amount.

        Accepts negatives to go back. Advances accumulate. This reaches
        anything gated on calendar time -- anniversaries and milestones,
        cue cooldowns, candidate TTLs, promotion age, gap-return -- but
        **not** concept or memory decay, which run on engaged time; use
        ``advance_engagement`` for those.

        Rows written while shifted keep the virtual timestamp after a
        reset, so prefer a copy of the database.
        """
        clock = _clock()
        if clock is None:
            return _NO_CLOCK
        return _dump(clock.advance(days=days, hours=hours))

    @mcp.tool()
    def set_clock(when: str) -> str:
        """Jump to an absolute ISO-8601 instant (e.g. ``2027-01-01T09:00:00Z``).

        Implemented as an offset from real time, so the clock keeps
        ticking from there rather than freezing.
        """
        clock = _clock()
        if clock is None:
            return _NO_CLOCK
        return _dump(clock.set_to(when))

    @mcp.tool()
    def advance_engagement(days: float) -> str:
        """Credit synthetic *engaged* days -- the decay domain.

        An engaged day is roughly an hour of active conversation, not a
        calendar day: the engagement clock exists so that being away for
        a week does not age everything by a week. Concept confidence
        decay (L3) and memory decay both run on this, so this is the
        lever for testing dormancy and pruning thresholds.

        One big advance will not produce a big decay: the L3 sweep
        clamps each pass to ``concept_decay_max_catchup_days`` (3 by
        default), so simulating 60 days means interleaving -- credit 3
        days, ``force_concept_lifecycle``, repeat -- rather than one
        advance and one sweep.

        Unlike the wall-clock offset this **writes persisted state**. It
        stashes an undo anchor on first use; ``reset_clock`` restores it.
        """
        clock = _clock()
        if clock is None:
            return _NO_CLOCK
        return _dump(clock.advance_engaged(days))

    @mcp.tool()
    def reset_clock() -> str:
        """Return to real time and undo any credited engagement.

        Restores the engagement total to its pre-advance anchor. Does
        **not** rewrite rows that were stamped while the clock was
        shifted -- nothing can.
        """
        clock = _clock()
        if clock is None:
            return _NO_CLOCK
        return _dump(clock.reset())
