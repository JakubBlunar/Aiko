"""Shared engagement clock -- a monotonic "active-conversation time" counter.

Aiko runs intermittently (off overnight, sometimes away for days). Any
decay driven by *wall-clock* time therefore punishes absence: come back
after a week and every belief/memory looks a week staler even though
nothing happened to make it so. This primitive fixes that at the source
by giving the rest of the app a notion of elapsed time that only
advances **while the user is actually engaging** with Aiko.

It maintains one monotonic float in ``kv_meta`` -- accumulated active
seconds -- credited a bounded amount per completed turn:

    credit = clamp(inter_turn_gap, min_turn_seconds, idle_cap_seconds)

so a brief daily hello adds a little, a long deep session adds a lot,
and a week away adds ~one capped turn's worth (not a week). Consumers
(the memory decay worker, the L3 concept lifecycle engine, and future
features) convert accumulated units into their existing per-day rate
domain via ``engaged_days_since``, using a single calibration knob
``engagement_seconds_per_day`` (default "~1 active hour = 1 decay-day").

Deliberately distinct from the K13/K14 ``EngagementTracker``, which
measures *affective* engagement (closeness delta, absence bands); this
is a plain time accumulator fed from the same post-turn hook. It is
persistence-backed (kv_meta) so it survives restarts, and stateless
otherwise, so a single instance can be shared across workers.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from app.core.infra import timephrase

log = logging.getLogger("app.engagement_clock")

# kv_meta keys. ``total_units`` is the monotonic accumulated active
# seconds; ``last_turn_at`` is the wall-clock of the last credited turn
# (used only to size the next credit, never to drive decay).
_KV_TOTAL = "engagement.total_units"
_KV_LAST_TURN = "engagement.last_turn_at"
# DT1 only: the pre-advance total, stashed the first time the debug clock
# credits synthetic engagement so ``debug_restore`` can undo it. Absent
# during normal operation.
_KV_DEBUG_ANCHOR = "engagement.debug_anchor"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


class EngagementClock:
    """Monotonic active-conversation time, persisted in ``kv_meta``.

    Reads its calibration + caps from a settings object (``MemorySettings``
    in practice) via ``getattr`` with defaults, so it stays decoupled
    from any particular settings schema and easy to construct in tests.
    """

    def __init__(
        self,
        *,
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        settings: object,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._settings = settings
        self._clock = clock or timephrase.utcnow

    # ── config knobs ──────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._settings, "engagement_clock_enabled", True))

    @property
    def _idle_cap_seconds(self) -> float:
        return max(
            1.0,
            float(getattr(self._settings, "engagement_idle_cap_seconds", 300.0)),
        )

    @property
    def _min_turn_seconds(self) -> float:
        return max(
            0.0,
            float(getattr(self._settings, "engagement_min_turn_seconds", 15.0)),
        )

    @property
    def _seconds_per_day(self) -> float:
        return max(
            1.0,
            float(getattr(self._settings, "engagement_seconds_per_day", 3600.0)),
        )

    # ── reads ─────────────────────────────────────────────────────────

    def total(self) -> float:
        """Current accumulated active seconds (0.0 if unset / unreadable)."""
        raw = None
        try:
            raw = self._kv_get(_KV_TOTAL)
        except Exception:
            log.debug("engagement clock read failed", exc_info=True)
        if raw is None:
            return 0.0
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 0.0

    # ── writes ────────────────────────────────────────────────────────

    def record_turn(self, *, now: datetime | None = None) -> float:
        """Credit one completed turn and return the new total.

        Credit is the inter-turn gap clamped to ``[min_turn_seconds,
        idle_cap_seconds]`` -- so a long absence adds only one capped
        turn's worth. The first ever turn (no prior anchor) credits the
        floor. A no-op that returns the current total when disabled.
        """
        if not self.enabled:
            return self.total()
        now = now or self._clock()
        last = _parse_iso(self._safe_get(_KV_LAST_TURN))
        if last is None:
            credit = self._min_turn_seconds
        else:
            gap = (now - last).total_seconds()
            credit = min(max(gap, self._min_turn_seconds), self._idle_cap_seconds)
        total = self.total() + credit
        try:
            self._kv_set(_KV_TOTAL, repr(total))
            self._kv_set(_KV_LAST_TURN, now.isoformat())
        except Exception:
            log.debug("engagement clock write failed", exc_info=True)
        return total

    # ── conversion helper ─────────────────────────────────────────────

    def engaged_days_since(
        self,
        anchor_units: float,
        *,
        seconds_per_day: float | None = None,
        clamp_days: float | None = None,
    ) -> float:
        """Engaged time elapsed since ``anchor_units`` (a prior ``total()``),
        expressed in "days" via ``seconds_per_day`` and optionally clamped.

        Negative deltas (anchor ahead of the current total, e.g. a reset)
        read as 0. The clamp bounds any single catch-up so a big jump
        can't over-apply decay in one pass.
        """
        sec_per_day = (
            float(seconds_per_day)
            if seconds_per_day is not None
            else self._seconds_per_day
        )
        sec_per_day = max(1.0, sec_per_day)
        delta = max(0.0, self.total() - float(anchor_units))
        days = delta / sec_per_day
        if clamp_days is not None:
            days = min(days, max(0.0, float(clamp_days)))
        return days

    # ── DT1 debug hooks ───────────────────────────────────────────────
    #
    # Engaged time is the domain concept decay and memory decay actually
    # run on, so the DT1 virtual clock cannot reach them by shifting the
    # wall clock -- it has to credit units here. Unlike the wall-clock
    # offset, which is in-memory and vanishes on restart, this is a real
    # write to persisted state, so it takes an undo anchor with it.

    def debug_advance(self, engaged_days: float) -> dict[str, float]:
        """Credit ``engaged_days`` worth of synthetic engagement (DT1 only).

        Stashes the pre-advance total on first use so
        :meth:`debug_restore` can put it back. Repeated advances keep the
        *original* anchor, so one restore undoes all of them.
        """
        before = self.total()
        # Falsy rather than ``is None``: ``debug_restore`` clears the key
        # to "" (kv has no delete), and a later advance must re-anchor.
        if not self._safe_get(_KV_DEBUG_ANCHOR):
            self._safe_set(_KV_DEBUG_ANCHOR, repr(before))
        after = max(0.0, before + float(engaged_days) * self._seconds_per_day)
        self._safe_set(_KV_TOTAL, repr(after))
        log.warning(
            "DT1 debug clock credited %.2f engaged days (%.0f -> %.0f units)",
            float(engaged_days), before, after,
        )
        return {"before": before, "after": after}

    def debug_restore(self) -> dict[str, float] | None:
        """Undo every :meth:`debug_advance`, returning to the real total.

        ``None`` when no synthetic engagement was ever credited.
        """
        raw = self._safe_get(_KV_DEBUG_ANCHOR)
        if raw is None:
            return None
        try:
            anchor = max(0.0, float(raw))
        except (TypeError, ValueError):
            return None
        before = self.total()
        self._safe_set(_KV_TOTAL, repr(anchor))
        self._safe_set(_KV_DEBUG_ANCHOR, "")
        log.warning(
            "DT1 debug clock restored engagement (%.0f -> %.0f units)",
            before, anchor,
        )
        return {"before": before, "after": anchor}

    def debug_anchor(self) -> float | None:
        """The stashed pre-advance total, or ``None`` if nothing is staged."""
        raw = self._safe_get(_KV_DEBUG_ANCHOR)
        if not raw:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    # ── internals ─────────────────────────────────────────────────────

    def _safe_get(self, key: str) -> str | None:
        try:
            return self._kv_get(key)
        except Exception:
            return None

    def _safe_set(self, key: str, value: str) -> None:
        try:
            self._kv_set(key, value)
        except Exception:
            log.debug("engagement clock write failed", exc_info=True)


__all__ = ["EngagementClock"]
