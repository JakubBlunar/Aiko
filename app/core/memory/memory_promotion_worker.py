"""Memory tier shuffler (schema v8 / E1).

Periodically promotes scratchpad rows that proved themselves into
long_term, demotes long-untouched long_term rows into archive, deletes
stale scratchpad rows that were never used, and re-coerces any
mis-tiered pinned rows back to long_term. Runs through the
:class:`IdleWorkerScheduler` so it only fires during quiet windows.

Gates (all configurable via :class:`MemorySettings`):

  * **Promote scratchpad -> long_term** when
    ``(age_days >= scratchpad_promote_min_age_days
        AND use_count >= scratchpad_promote_min_use_count)``
    OR ``revival_score >= scratchpad_promote_min_revival``.
  * **Delete scratchpad** when
    ``age_days >= scratchpad_ttl_days AND use_count == 0
        AND revival_score == 0``.
  * **Demote long_term -> archive** when
    ``idle_days >= archive_demote_idle_days AND revival_score < 0.05
        AND NOT pinned``.
  * **Coerce pinned -> long_term** unconditionally (defense in depth).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.memory.memory_store import Memory, MemoryStore
    from app.core.infra.settings import MemorySettings


log = logging.getLogger("app.memory_promotion_worker")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


@dataclass(slots=True)
class _Gates:
    """Resolved knobs for one sweep. Cached per ``run()`` so settings
    can change between runs without mid-sweep mutation."""

    interval_seconds: float
    promote_min_age_days: int
    promote_min_use_count: int
    promote_min_revival: float
    ttl_days: int
    demote_idle_days: int


class MemoryPromotionWorker:
    """IdleWorker that shuffles memories between tiers each pass."""

    name = "memory_promotion"

    def __init__(
        self,
        store: "MemoryStore",
        settings: "MemorySettings",
    ) -> None:
        self._store = store
        self._settings = settings

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(self._settings.promotion_worker_interval_seconds)

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        if not self._settings.tiers_enabled:
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        )

    def run(self) -> dict[str, Any]:
        if not self._settings.tiers_enabled:
            return {
                "skipped": True,
                "reason": "tiers_disabled",
            }
        gates = _Gates(
            interval_seconds=self.interval_seconds,
            promote_min_age_days=int(self._settings.scratchpad_promote_min_age_days),
            promote_min_use_count=int(
                self._settings.scratchpad_promote_min_use_count
            ),
            promote_min_revival=float(
                self._settings.scratchpad_promote_min_revival
            ),
            ttl_days=int(self._settings.scratchpad_ttl_days),
            demote_idle_days=int(self._settings.archive_demote_idle_days),
        )
        now = _utcnow()
        promoted = self._promote_scratchpad(gates, now)
        deleted = self._delete_dead_scratchpad(gates, now)
        demoted = self._demote_idle_long_term(gates, now)
        coerced = self._coerce_pinned()
        # After tier shuffling, re-run prune() so any tier that grew
        # past its cap (rare but possible after promote) gets trimmed.
        try:
            pruned = self._store.prune()
        except Exception:
            log.debug("prune after promotion failed", exc_info=True)
            pruned = 0
        result = {
            "promoted": promoted,
            "deleted_scratchpad": deleted,
            "demoted_archive": demoted,
            "coerced_pinned": coerced,
            "pruned": pruned,
        }
        log.info("memory_promotion sweep: %s", result)
        return result

    # ── sweep stages ─────────────────────────────────────────────────

    def _promote_scratchpad(self, gates: _Gates, now: datetime) -> int:
        rows = self._store.iter_by_tier("scratchpad")
        promoted = 0
        for mem in rows:
            if mem.pinned:
                # Pinned rows shouldn't sit in scratchpad anyway; the
                # coerce step picks them up too, but doing it here
                # avoids double work.
                self._update_tier(mem, "long_term")
                promoted += 1
                continue
            age_days = self._age_days(mem, now)
            qualifies_age_use = (
                age_days >= gates.promote_min_age_days
                and mem.use_count >= gates.promote_min_use_count
            )
            qualifies_revival = mem.revival_score >= gates.promote_min_revival
            if qualifies_age_use or qualifies_revival:
                self._update_tier(mem, "long_term")
                promoted += 1
        return promoted

    def _delete_dead_scratchpad(self, gates: _Gates, now: datetime) -> int:
        rows = self._store.iter_by_tier("scratchpad")
        deleted = 0
        for mem in rows:
            if mem.pinned:
                continue
            age_days = self._age_days(mem, now)
            if (
                age_days >= gates.ttl_days
                and mem.use_count == 0
                and mem.revival_score == 0.0
            ):
                try:
                    if self._store.delete(mem.id):
                        deleted += 1
                except Exception:
                    log.debug(
                        "scratchpad delete failed id=%s", mem.id, exc_info=True,
                    )
        return deleted

    def _demote_idle_long_term(self, gates: _Gates, now: datetime) -> int:
        rows = self._store.iter_by_tier("long_term")
        demoted = 0
        for mem in rows:
            if mem.pinned:
                continue
            if mem.revival_score >= 0.05:
                continue
            anchor = _parse_iso(mem.last_used_at) or _parse_iso(mem.created_at)
            if anchor is None:
                continue
            idle_days = (now - anchor).total_seconds() / 86400.0
            if idle_days >= gates.demote_idle_days:
                self._update_tier(mem, "archive")
                demoted += 1
        return demoted

    def _coerce_pinned(self) -> int:
        # Walk scratchpad + archive only; long_term is the target.
        coerced = 0
        for tier in ("scratchpad", "archive"):
            for mem in self._store.iter_by_tier(tier):
                if mem.pinned and mem.tier != "long_term":
                    self._update_tier(mem, "long_term")
                    coerced += 1
        return coerced

    # ── helpers ──────────────────────────────────────────────────────

    def _age_days(self, mem: "Memory", now: datetime) -> float:
        created = _parse_iso(mem.created_at)
        if created is None:
            return 0.0
        return max(0.0, (now - created).total_seconds() / 86400.0)

    def _update_tier(self, mem: "Memory", tier: str) -> None:
        try:
            self._store.update(mem.id, tier=tier)
        except Exception:
            log.debug(
                "tier update failed id=%s tier=%s", mem.id, tier, exc_info=True,
            )


__all__ = ["MemoryPromotionWorker"]
