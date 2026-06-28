"""H16 — circadian "where you find her" default location.

Even with H13 moving Aiko around during away-beats, the *seed* default —
where she is when nothing in particular has happened for a long stretch —
was a single fixed spot (whatever the last turn / garden worker left).
This very-low-frequency :class:`IdleWorker` gives that idle baseline a
believable time-of-day shape: late at night you find her curled up in
bed, mid-morning at the desk, late afternoon reading in the beanbag.

It is deliberately the *gentlest* mover in the room and defers to
everything else:

* **Intentional-placement hold** — if the brain / user deliberately put
  her somewhere recently, it never overrides that (the core constraint:
  if she decided to stay in the garden, no worker drags her off).
* **Garden visit outstanding** — never fights the garden worker.
* **Staleness gate** — only settles when her room state hasn't changed
  for ``settle_after_seconds`` (default 2h). While the away-activity
  worker is actively giving her beats, ``updated_at`` stays fresh and this
  worker stays quiet; it's the backstop that takes over once the lively
  beats taper off (e.g. the daily away-cap is spent, or overnight).
* **Already-there** — if she's already at the period-appropriate spot it
  does nothing.

Pure target table (:func:`settle_target`) is I/O-free and unit-tested.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.world.world_store import WorldStore


log = logging.getLogger("app.circadian_settle")

# Must match ``app.core.session.world_mixin.WORLD_INTENTIONAL_STATE_KEY``.
_INTENTIONAL_STATE_KEY = "world.intentional_state_at"
# Must match ``garden_visit_worker.GardenVisitWorker._RETURN_KEY``.
_GARDEN_RETURN_KEY = "garden_visit.return_at"


# period -> (location slug, posture, activity). The resting baseline she
# drifts to when nothing else is going on.
_SETTLE_TARGET: dict[str, tuple[str, str, str]] = {
    "late_night": ("bed", "lying", "napping"),
    "night": ("bed", "lying", "napping"),
    "early_morning": ("bed", "lying", "napping"),
    "morning": ("desk", "sitting", "idle"),
    "midday": ("desk", "sitting", "idle"),
    "afternoon": ("beanbag", "curled_up", "idle"),
    "evening": ("beanbag", "curled_up", "idle"),
}


def settle_target(period: str) -> tuple[str, str, str] | None:
    """Resting (slug, posture, activity) for a circadian period, or None."""
    return _SETTLE_TARGET.get((period or "").strip())


def _parse_iso(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class CircadianSettleWorker:
    """IdleWorker that nudges Aiko to her time-of-day resting default."""

    name = "circadian_settle"

    def __init__(
        self,
        store: "WorldStore",
        *,
        notify: Callable[[dict[str, Any]], None] | None = None,
        kv_get: Callable[[str], str | None] | None = None,
        enabled_provider: Callable[[], bool] | None = None,
        circadian_period_provider: Callable[[], str] | None = None,
        interval_seconds: float = 3600.0,
        settle_after_seconds: float = 7200.0,
        intentional_hold_seconds: float = 0.0,
        rng: random.Random | None = None,
    ) -> None:
        self._store = store
        self._notify = notify
        self._kv_get = kv_get
        self._enabled_provider = enabled_provider
        self._circadian_period_provider = circadian_period_provider
        self._interval_seconds = max(60.0, float(interval_seconds))
        self._settle_after_seconds = max(0.0, float(settle_after_seconds))
        self._intentional_hold_seconds = max(0.0, float(intentional_hold_seconds))
        self._rng = rng or random.Random()

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> bool:
        if self._enabled_provider is not None:
            try:
                if not bool(self._enabled_provider()):
                    return False
            except Exception:
                pass
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        )

    def run(self) -> dict[str, Any]:
        if self._enabled_provider is not None:
            try:
                if not bool(self._enabled_provider()):
                    return {"fired": 0, "disabled": True}
            except Exception:
                pass
        now = datetime.now(timezone.utc)
        if self._intentional_hold_active(now):
            return {"fired": 0, "skipped_intentional_hold": True}
        if self._garden_visit_outstanding(now):
            return {"fired": 0, "skipped_garden_visit": True}

        try:
            state = self._store.get_state()
        except Exception:
            log.debug("circadian_settle get_state failed", exc_info=True)
            return {"fired": 0, "no_state": True}

        # Staleness gate — only settle when she's been static a while.
        updated = _parse_iso(getattr(state, "updated_at", None))
        if (
            self._settle_after_seconds > 0
            and updated is not None
            and (now - updated).total_seconds() < self._settle_after_seconds
        ):
            return {"fired": 0, "skipped_recent_activity": True}

        period = self._read_period()
        target = settle_target(period)
        if target is None:
            return {"fired": 0, "no_target": True}
        slug, posture, activity = target

        loc = None
        try:
            loc = self._store.get_location(slug)
        except Exception:
            loc = None
        if loc is None:
            return {"fired": 0, "no_location": True}

        # Already at the period-appropriate spot — nothing to do.
        if getattr(state, "location_id", None) == loc.id:
            return {"fired": 0, "already_there": True}

        try:
            new_state = self._store.set_state(
                location_id=loc.id,
                posture=posture,
                activity=activity,
            )
            self._broadcast({"state": new_state.to_dict()})
        except Exception:
            log.debug("circadian_settle set_state failed", exc_info=True)
            return {"fired": 0, "set_state_failed": True}

        log.info(
            "circadian_settle: period=%s -> %s (%s/%s)",
            period, slug, posture, activity,
        )
        return {
            "fired": 1,
            "period": period,
            "slug": slug,
            "posture": posture,
            "activity": activity,
        }

    # ── gates ────────────────────────────────────────────────────────

    def _read_period(self) -> str:
        if self._circadian_period_provider is not None:
            try:
                return str(self._circadian_period_provider() or "")
            except Exception:
                pass
        try:
            from app.core.affect.circadian import compute

            now = datetime.now()
            return str(compute(now).period)
        except Exception:
            return ""

    def _kv_read(self, key: str) -> str | None:
        if self._kv_get is None:
            return None
        try:
            return self._kv_get(key)
        except Exception:
            return None

    def _intentional_hold_active(self, now: datetime) -> bool:
        if self._intentional_hold_seconds <= 0:
            return False
        stamped = _parse_iso(self._kv_read(_INTENTIONAL_STATE_KEY))
        if stamped is None:
            return False
        return (now - stamped).total_seconds() < self._intentional_hold_seconds

    def _garden_visit_outstanding(self, now: datetime) -> bool:
        return_at = _parse_iso(self._kv_read(_GARDEN_RETURN_KEY))
        return return_at is not None and now < return_at

    def _broadcast(self, patch: dict[str, Any]) -> None:
        if self._notify is None:
            return
        try:
            self._notify(patch)
        except Exception:
            log.debug("circadian_settle notify raised", exc_info=True)


__all__ = ["CircadianSettleWorker", "settle_target"]
