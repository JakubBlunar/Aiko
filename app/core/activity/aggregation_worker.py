"""Compute-lane Level-1 activity aggregation (C6 phase 3).

``demand()`` is "has anything meaningful changed" — the same probe
Level-2 interpretation will reuse. No LLM, no titles, no MemoryStore.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from app.core.activity.evidence import compute_activity_rollup
from app.core.infra import timephrase
from app.core.proactive.idle_worker import WorkSignal

if TYPE_CHECKING:
    from app.core.activity.store import ActivityStore

log = logging.getLogger("app.activity.aggregation_worker")

_INTERVAL_SECONDS = 60.0
_SATURATION_SESSIONS = 8
KV_SESSION_ID = "activity.rollup.last_session_id"
KV_ROLLUP = "activity.rollup.json"


class ActivityAggregationWorker:
    """IdleWorker whose ``demand()`` is new activity sessions since last rollup."""

    name = "activity_aggregation"

    def __init__(
        self,
        store: "ActivityStore",
        *,
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        interval_seconds: float = _INTERVAL_SECONDS,
    ) -> None:
        self._store = store
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._interval = max(15.0, float(interval_seconds))

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def is_ready(self, *, now: datetime, last_run_at: datetime | None) -> bool:
        del now, last_run_at
        return self._store is not None

    def demand(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> WorkSignal | None:
        del now, last_run_at
        if self._store is None:
            return WorkSignal(pressure=0.0, reason="no activity store")
        latest = 0
        try:
            latest = int(self._store.latest_session_id() or 0)
        except Exception:
            log.debug("activity rollup demand id failed", exc_info=True)
            return WorkSignal(pressure=0.0, reason="activity store unread")
        watermark = self._watermark()
        if latest <= watermark:
            return WorkSignal(pressure=0.0, reason="no new activity sessions")
        delta = latest - watermark
        return WorkSignal(
            pressure=min(1.0, delta / float(_SATURATION_SESSIONS)),
            reason="%d new activity sessions" % delta,
            needs_llm=False,
        )

    def run(self) -> dict[str, Any] | None:
        if self._store is None:
            return None
        try:
            sessions = self._store.recent_sessions(limit=48)
        except Exception:
            log.debug("activity rollup sessions failed", exc_info=True)
            return None
        rollup = compute_activity_rollup(sessions, now=timephrase.utcnow())
        rollup.pop("title", None)
        payload = json.dumps(rollup, ensure_ascii=False)
        try:
            self._kv_set(KV_ROLLUP, payload)
            self._kv_set(KV_SESSION_ID, str(int(rollup.get("last_session_id") or 0)))
        except Exception:
            log.debug("activity rollup persist failed", exc_info=True)
            return None
        log.info(
            "activity rollup: sessions=%s idle_s=%s lock_s=%s app=%s",
            rollup.get("session_count"),
            rollup.get("idle_span_seconds"),
            rollup.get("lock_span_seconds"),
            str(rollup.get("app") or "-"),
        )
        return rollup

    def _watermark(self) -> int:
        try:
            raw = self._kv_get(KV_SESSION_ID)
        except Exception:
            return 0
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0
