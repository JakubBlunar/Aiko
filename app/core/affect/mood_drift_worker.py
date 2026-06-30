"""MoodDriftSampleWorker — daily sampler for H3.

Thin :class:`IdleWorker` that appends **one sample per local day** of
Aiko's read of the user's mood (``valence``) plus the four relationship
axes into the ``aiko.mood_drift_samples`` kv ring. Matches the
[`DayColorWorker`](day_color_worker.py) shape exactly so it slots into
the :class:`IdleWorkerScheduler` with no special handling: class-level
``name``, ``interval_seconds`` property, ``is_ready``, ``run() -> dict``.

The worker only *samples* — detection + surfacing live in the
``_render_mood_drift_block`` provider. Like K27, the provider also has a
cheap lazy-sample fallback (via :func:`record_daily_sample`) so the ring
keeps growing even when the idle scheduler is starved by a long live
session.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.core.affect import mood_drift
from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.affect.affect_state import AffectStore
    from app.core.infra.chat_database import ChatDatabase
    from app.core.infra.settings import AgentSettings
    from app.core.relationship.relationship_axes import RelationshipAxesStore


log = logging.getLogger("app.mood_drift_worker")


def record_daily_sample(
    *,
    chat_db: "ChatDatabase",
    affect_store: "AffectStore",
    axes_store: "RelationshipAxesStore | None",
    user_id: str,
    now: datetime,
) -> tuple[list[mood_drift.DriftSample], bool]:
    """Append today's sample if one isn't recorded yet for the local date.

    Returns ``(samples, wrote)`` — the full (possibly updated) ring and a
    flag for whether a write happened. Best-effort: on any read failure
    the existing ring is returned with ``wrote=False``. Shared by the
    worker (regular cadence) and the provider (lazy fallback) so both
    sample identically and dedupe by date.
    """
    try:
        samples = mood_drift.deserialize_samples(chat_db.kv_get(mood_drift.KV_SAMPLES))
    except Exception:
        log.debug("mood_drift kv_get(samples) failed", exc_info=True)
        samples = []

    today = mood_drift.today_str(now)
    if samples and samples[-1].date == today:
        return samples, False

    try:
        affect = affect_store.get(user_id)
        valence = float(affect.valence)
    except Exception:
        log.debug("mood_drift affect read failed", exc_info=True)
        return samples, False

    closeness = humor = trust = comfort = 0.0
    if axes_store is not None:
        try:
            axes = axes_store.get(user_id)
            closeness = float(axes.closeness)
            humor = float(axes.humor)
            trust = float(axes.trust)
            comfort = float(axes.comfort)
        except Exception:
            log.debug("mood_drift axes read failed", exc_info=True)

    sample = mood_drift.DriftSample(
        date=today,
        valence=valence,
        closeness=closeness,
        humor=humor,
        trust=trust,
        comfort=comfort,
    )
    updated = mood_drift.append_sample(samples, sample)
    try:
        chat_db.kv_set(mood_drift.KV_SAMPLES, mood_drift.serialize_samples(updated))
    except Exception:
        log.debug("mood_drift kv_set(samples) failed", exc_info=True)
        return samples, False
    return updated, True


class MoodDriftSampleWorker:
    """IdleWorker that records one mood/axes sample per local day."""

    name = "mood_drift"

    def __init__(
        self,
        *,
        chat_db: "ChatDatabase",
        settings: "AgentSettings",
        affect_store: "AffectStore",
        axes_store: "RelationshipAxesStore | None",
        user_id: str,
    ) -> None:
        self._chat_db = chat_db
        self._settings = settings
        self._affect_store = affect_store
        self._axes_store = axes_store
        self._user_id = user_id

    @property
    def interval_seconds(self) -> float:
        # Hourly check; the actual sample only lands once per local day.
        # Floored at 60s in the AgentSettings parser.
        return float(
            getattr(self._settings, "mood_drift_check_interval_seconds", 3600)
        )

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        if not bool(getattr(self._settings, "mood_drift_enabled", True)):
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        )

    def run(self) -> dict[str, Any]:
        if not bool(getattr(self._settings, "mood_drift_enabled", True)):
            return {"skipped": True, "reason": "disabled"}
        now = datetime.now().astimezone()
        try:
            samples, wrote = record_daily_sample(
                chat_db=self._chat_db,
                affect_store=self._affect_store,
                axes_store=self._axes_store,
                user_id=self._user_id,
                now=now,
            )
        except Exception:
            log.warning("mood_drift worker: sample failed", exc_info=True)
            return {"skipped": True, "reason": "sample_failed"}
        if not wrote:
            return {"sampled": False, "reason": "fresh", "count": len(samples)}
        log.info(
            "mood_drift sampled: date=%s count=%d valence=%.3f",
            samples[-1].date, len(samples), samples[-1].valence,
        )
        return {"sampled": True, "count": len(samples)}


__all__ = ["MoodDriftSampleWorker", "record_daily_sample"]
