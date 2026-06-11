"""K43 — Promise follow-through worker ("you said you'd look into that").

Aiko makes small assistant-side commitments mid-conversation ("I'll
look into that", "I'll get back to you") which the
:class:`PromiseExtractor` persists as ``kind="promise"`` memories — and
then nothing ever closes the loop. That asymmetry reads as flakiness:
real friends either come back with the thing or own that they haven't
gotten to it yet.

This worker is the silent producer half. During a quiet window it:

  * scans assistant-side promise memories whose lifecycle status
    (``metadata.promise_status``, see
    :mod:`app.core.memory.promise_lifecycle`) is still ``open``,
  * flips rows older than ``drop_after_days`` to ``dropped`` (a 3-week-
    old "I'll check" resurfacing is weirder than letting it go),
  * picks the **oldest** promise past ``min_age_hours`` (longest-owed
    first), stamps it ``surfaced``, and writes a one-shot pending cue
    into kv_meta (``promise_followthrough.pending``).

The consumer is
:meth:`InnerLifeProvidersMixin._render_promise_followthrough_block`,
which folds the cue into the next turn's prompt ("mention what you
found — or own that you haven't yet, casually") and clears the slot.
The worker never speaks and never fires a proactive nudge.

Fulfilment is detected elsewhere: the post-turn hook
(:meth:`PostTurnMixin._maybe_resolve_promises`) lexically matches
Aiko's replies against active promises, and the task-orchestration
mixin auto-fulfils promises whose body matches a just-completed
background task.

Paced by a per-fire wall-clock cooldown (kv watermark). Every failure
path is swallowed and logged at debug — the worst case is a missed
beat, never a corrupt row.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.memory import promise_lifecycle as lifecycle
from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.memory.memory_store import Memory, MemoryStore


log = logging.getLogger("app.promise_followthrough")


# kv_meta keys this worker owns. ``PENDING_KEY`` is shared with the
# surfacing provider (producer writes, consumer clears).
PENDING_KEY = "promise_followthrough.pending"
_KV_LAST_FIRED_AT = "promise_followthrough.last_fired_at"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


# ── pending-slot helpers (shared with the surfacing provider) ────────────


def load_pending(
    kv_get: Callable[[str], str | None],
) -> dict[str, Any] | None:
    """Return the pending follow-through cue, or ``None``."""
    try:
        raw = kv_get(PENDING_KEY)
    except Exception:
        return None
    if not raw:
        return None
    try:
        blob = json.loads(raw)
    except Exception:
        return None
    return blob if isinstance(blob, dict) and blob.get("memory_id") else None


def clear_pending(kv_set: Callable[[str, str], None]) -> None:
    """Consume the pending slot (best-effort)."""
    try:
        kv_set(PENDING_KEY, "")
    except Exception:
        log.debug("promise_followthrough: pending clear failed", exc_info=True)


class PromiseFollowthroughWorker:
    """IdleWorker that arms follow-through cues for open Aiko promises."""

    name = "promise_followthrough"

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        enabled_provider: Callable[[], bool] | None = None,
        interval_seconds: float = 1800.0,
        min_age_hours: float = 4.0,
        cooldown_hours: float = 6.0,
        drop_after_days: float = 14.0,
    ) -> None:
        self._memory_store = memory_store
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._enabled_provider = enabled_provider
        self._interval_seconds = max(30.0, float(interval_seconds))
        self._min_age_hours = max(0.0, float(min_age_hours))
        self._cooldown_hours = max(0.0, float(cooldown_hours))
        self._drop_after_days = max(1.0, float(drop_after_days))

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        )

    def run(self) -> dict[str, Any]:
        if not self._enabled():
            return {"armed": 0, "skipped_disabled": True}
        now = _utcnow()

        # Already a cue waiting? Don't stack — one owed beat at a time.
        if load_pending(self._kv_get) is not None:
            return {"armed": 0, "skipped_pending": True}

        # Per-fire wall-clock cooldown so a backlog of old promises
        # doesn't turn every turn into loop-closing.
        last_fired = _parse_iso(self._kv_safe_get(_KV_LAST_FIRED_AT))
        if (
            last_fired is not None
            and (now - last_fired).total_seconds()
            < self._cooldown_hours * 3600.0
        ):
            return {"armed": 0, "skipped_cooldown": True}

        candidates, dropped = self._scan(now)
        if not candidates:
            return {"armed": 0, "dropped": dropped, "eligible": 0}

        # Longest-owed first.
        candidates.sort(key=lambda pair: pair[1], reverse=True)
        mem, age_hours = candidates[0]
        if not self._arm(mem, age_hours=age_hours, now=now):
            return {"armed": 0, "dropped": dropped, "errored": True}
        return {"armed": 1, "dropped": dropped, "eligible": len(candidates)}

    # ── MCP debug path ───────────────────────────────────────────────

    def force_arm(self) -> dict[str, Any] | None:
        """Bypass age/cooldown gates and arm the oldest active promise.

        Considers ``surfaced`` rows too (a hand-tested promise may have
        been surfaced already). Returns the pending payload or ``None``
        when no assistant promise exists at all.
        """
        now = _utcnow()
        best: "tuple[Memory, float] | None" = None
        for mem in self._iter_promises():
            if lifecycle.promise_status(mem) not in lifecycle.ACTIVE_STATUSES:
                continue
            if not lifecycle.is_assistant_promise(mem):
                continue
            age = lifecycle.promise_age_hours(mem, now=now) or 0.0
            if best is None or age > best[1]:
                best = (mem, age)
        if best is None:
            return None
        if not self._arm(best[0], age_hours=best[1], now=now):
            return None
        return load_pending(self._kv_get)

    # ── internals ────────────────────────────────────────────────────

    def _enabled(self) -> bool:
        if self._enabled_provider is None:
            return True
        try:
            return bool(self._enabled_provider())
        except Exception:
            return True

    def _kv_safe_get(self, key: str) -> str | None:
        try:
            return self._kv_get(key)
        except Exception:
            return None

    def _iter_promises(self) -> "list[Memory]":
        try:
            return list(self._memory_store.iter_by_kind("promise"))
        except Exception:
            log.debug(
                "promise_followthrough: iter_by_kind failed", exc_info=True,
            )
            return []

    def _scan(self, now: datetime) -> "tuple[list[tuple[Memory, float]], int]":
        """Return (eligible open assistant promises with ages, dropped count)."""
        eligible: "list[tuple[Memory, float]]" = []
        dropped = 0
        for mem in self._iter_promises():
            if lifecycle.promise_status(mem) != lifecycle.STATUS_OPEN:
                continue
            if not lifecycle.is_assistant_promise(mem):
                continue
            age_hours = lifecycle.promise_age_hours(mem, now=now)
            if age_hours is None:
                continue
            if age_hours > self._drop_after_days * 24.0:
                if self._mark(mem, status=lifecycle.STATUS_DROPPED, now=now):
                    dropped += 1
                continue
            if age_hours < self._min_age_hours:
                continue
            eligible.append((mem, age_hours))
        return eligible, dropped

    def _arm(self, mem: "Memory", *, age_hours: float, now: datetime) -> bool:
        payload = {
            "memory_id": int(mem.id),
            "what": lifecycle.promise_what(mem)[:200],
            "age_hours": round(float(age_hours), 2),
            "at": now.isoformat(),
        }
        try:
            self._kv_set(PENDING_KEY, json.dumps(payload))
            self._kv_set(_KV_LAST_FIRED_AT, now.isoformat())
        except Exception:
            log.debug(
                "promise_followthrough: pending write failed", exc_info=True,
            )
            return False
        self._mark(mem, status=lifecycle.STATUS_SURFACED, now=now)
        log.info(
            "promise-followthrough armed: memory_id=%s age_h=%.1f what=%r",
            mem.id,
            age_hours,
            payload["what"][:80],
        )
        return True

    def _mark(self, mem: "Memory", *, status: str, now: datetime) -> bool:
        meta: dict[str, Any] = {"promise_status": status}
        if status == lifecycle.STATUS_SURFACED:
            meta["promise_surfaced_at"] = now.isoformat()
        else:
            meta["promise_resolved_at"] = now.isoformat()
        try:
            self._memory_store.update(
                mem.id, metadata=meta, metadata_merge=True,
            )
        except Exception:
            log.debug(
                "promise_followthrough: status update failed for id=%s",
                mem.id,
                exc_info=True,
            )
            return False
        return True


__all__ = [
    "PromiseFollowthroughWorker",
    "PENDING_KEY",
    "load_pending",
    "clear_pending",
]
