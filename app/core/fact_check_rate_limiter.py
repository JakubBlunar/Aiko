"""Hourly + daily rate limiter for the F1 background fact-checker.

Implemented as a simple counter persisted in ``kv_meta`` (one entry for
the hour bucket, one for the day bucket). Each ``allow(now)`` call:

  1. Rolls over the buckets when the wall-clock crosses an hour / day
     boundary, dropping the previous counter to zero.
  2. Compares the live counter against the configured cap.
  3. Increments + persists when allowed.

A persisted limiter survives restarts so a daily cap isn't gamed by
killing the app at minute 59. The hour bucket uses
``YYYY-MM-DDTHH``-style keys; the day bucket uses ``YYYY-MM-DD``.

Concurrency: one process owns the DB by design, but the limiter still
takes an internal lock so e.g. the REST status endpoint can read counts
without racing the worker tick.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.chat_database import ChatDatabase


log = logging.getLogger("app.fact_check_rate_limiter")


_KV_KEY = "fact_checker.rate_state"


class FactCheckRateLimiter:
    """Token-bucket style rate limiter with hour + day caps."""

    def __init__(
        self,
        chat_db: "ChatDatabase",
        *,
        per_hour_cap: int = 10,
        per_day_cap: int = 50,
    ) -> None:
        self._chat_db = chat_db
        self._per_hour_cap = max(0, int(per_hour_cap))
        self._per_day_cap = max(0, int(per_day_cap))
        self._lock = threading.Lock()

    @property
    def per_hour_cap(self) -> int:
        return self._per_hour_cap

    @property
    def per_day_cap(self) -> int:
        return self._per_day_cap

    def update_caps(self, *, per_hour: int | None = None, per_day: int | None = None) -> None:
        if per_hour is not None:
            self._per_hour_cap = max(0, int(per_hour))
        if per_day is not None:
            self._per_day_cap = max(0, int(per_day))

    # ── state plumbing ───────────────────────────────────────────────

    def _now_bucket_keys(self, now: datetime) -> tuple[str, str]:
        utc = now.astimezone(timezone.utc)
        return (
            utc.strftime("%Y-%m-%dT%H"),
            utc.strftime("%Y-%m-%d"),
        )

    def _load(self) -> dict[str, object]:
        raw = self._chat_db.kv_get(_KV_KEY)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, state: dict[str, object]) -> None:
        try:
            self._chat_db.kv_set(_KV_KEY, json.dumps(state))
        except Exception:
            log.debug("rate limiter save failed", exc_info=True)

    # ── public API ───────────────────────────────────────────────────

    def snapshot(self, now: datetime | None = None) -> dict[str, int]:
        """Return current ``{hour_used, hour_cap, day_used, day_cap}``."""
        cur = now or datetime.now(timezone.utc)
        hour_key, day_key = self._now_bucket_keys(cur)
        with self._lock:
            state = self._load()
            hour_used = (
                int(state.get("hour_used", 0))
                if state.get("hour_bucket") == hour_key
                else 0
            )
            day_used = (
                int(state.get("day_used", 0))
                if state.get("day_bucket") == day_key
                else 0
            )
        return {
            "hour_used": hour_used,
            "hour_cap": self._per_hour_cap,
            "day_used": day_used,
            "day_cap": self._per_day_cap,
        }

    def allow(self, now: datetime | None = None) -> bool:
        """Atomically check + consume one token. Returns True if allowed."""
        cur = now or datetime.now(timezone.utc)
        hour_key, day_key = self._now_bucket_keys(cur)
        with self._lock:
            state = self._load()
            hour_used = (
                int(state.get("hour_used", 0))
                if state.get("hour_bucket") == hour_key
                else 0
            )
            day_used = (
                int(state.get("day_used", 0))
                if state.get("day_bucket") == day_key
                else 0
            )
            if self._per_hour_cap == 0 or self._per_day_cap == 0:
                return False
            if hour_used >= self._per_hour_cap:
                return False
            if day_used >= self._per_day_cap:
                return False
            new_state = {
                "hour_bucket": hour_key,
                "day_bucket": day_key,
                "hour_used": hour_used + 1,
                "day_used": day_used + 1,
                "last_allow_at": cur.astimezone(timezone.utc).isoformat(),
            }
            self._save(new_state)
            return True
