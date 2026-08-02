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
        AND revival_score < scratchpad_ttl_min_revival``.
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

from app.core.proactive.idle_worker import WorkSignal, pressure_from_count
from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.memory.memory_store import Memory, MemoryStore
    from app.core.infra.settings import MemorySettings


log = logging.getLogger("app.memory_promotion_worker")


def _utcnow() -> datetime:
    return timephrase.utcnow()


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
    ttl_min_revival: float
    demote_idle_days: int


# Enough pending tier moves to call the sweep urgent. The probe stops
# counting at this point, which keeps its cost from scaling with the
# size of the memory store.
_DEMAND_SATURATION = 25


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
        return bool(self._settings.tiers_enabled)

    def _resolve_gates(self) -> _Gates:
        return _Gates(
            interval_seconds=self.interval_seconds,
            promote_min_age_days=int(self._settings.scratchpad_promote_min_age_days),
            promote_min_use_count=int(
                self._settings.scratchpad_promote_min_use_count
            ),
            promote_min_revival=float(
                self._settings.scratchpad_promote_min_revival
            ),
            ttl_days=int(self._settings.scratchpad_ttl_days),
            # Epsilon floor so a configured 0.0 keeps meaning "delete rows
            # with no revival at all" rather than "< 0.0", which matches
            # nothing and would silently switch TTL cleanup off.
            ttl_min_revival=max(
                1e-9,
                float(getattr(self._settings, "scratchpad_ttl_min_revival", 0.0)),
            ),
            demote_idle_days=int(self._settings.archive_demote_idle_days),
        )

    def demand(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> "WorkSignal | None":
        """Count the tier moves this sweep would make, without making them.

        Runs the same four predicates ``run()`` uses over the store's
        in-memory tier mirrors — no SQL, no writes — and stops at
        :data:`_DEMAND_SATURATION`, so the probe's cost is bounded even
        on a large store.

        The trailing ``prune()`` in ``run()`` is not counted. It only
        does anything when a tier grew past its cap, which in practice
        happens as a *consequence* of the promotions above, and the
        heartbeat run covers the rare case where it wouldn't.
        """
        if not self._settings.tiers_enabled:
            return WorkSignal(pressure=0.0, reason="tiers disabled")
        try:
            pending = self._pending_moves(
                self._resolve_gates(), now, limit=_DEMAND_SATURATION,
            )
        except Exception:
            log.debug("memory_promotion demand probe failed", exc_info=True)
            return None
        return WorkSignal(
            pressure=pressure_from_count(
                pending, saturation=_DEMAND_SATURATION,
            ),
            reason=f"{pending} tier moves",
        )

    def _pending_moves(
        self, gates: _Gates, now: datetime, *, limit: int,
    ) -> int:
        """How many rows the sweep would move, counted up to ``limit``."""
        pending = 0
        for mem in self._store.iter_by_tier("scratchpad"):
            if self._should_promote(mem, gates, now) or self._should_delete(
                mem, gates, now,
            ):
                pending += 1
                if pending >= limit:
                    return pending
        for mem in self._store.iter_by_tier("long_term"):
            if self._should_demote(mem, gates, now):
                pending += 1
                if pending >= limit:
                    return pending
        # Scratchpad's pinned rows are already counted by
        # _should_promote, so only archive is left for the coerce stage.
        for mem in self._store.iter_by_tier("archive"):
            if self._should_coerce(mem):
                pending += 1
                if pending >= limit:
                    return pending
        return pending

    def run(self) -> dict[str, Any]:
        if not self._settings.tiers_enabled:
            return {
                "skipped": True,
                "reason": "tiers_disabled",
            }
        gates = self._resolve_gates()
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

    # ── tier predicates (read-only; shared with demand()) ────────────

    def _should_promote(
        self, mem: "Memory", gates: _Gates, now: datetime,
    ) -> bool:
        """Scratchpad row that has earned long_term."""
        if mem.pinned:
            # Pinned rows shouldn't sit in scratchpad anyway; the coerce
            # step would pick them up too, but handling them here avoids
            # double work.
            return True
        age_days = self._age_days(mem, now)
        qualifies_age_use = (
            age_days >= gates.promote_min_age_days
            and mem.use_count >= gates.promote_min_use_count
        )
        return qualifies_age_use or mem.revival_score >= gates.promote_min_revival

    def _should_delete(
        self, mem: "Memory", gates: _Gates, now: datetime,
    ) -> bool:
        """Scratchpad row nothing ever came back to."""
        if mem.pinned:
            return False
        return (
            self._age_days(mem, now) >= gates.ttl_days
            and mem.use_count == 0
            and mem.revival_score < gates.ttl_min_revival
        )

    def _should_demote(
        self, mem: "Memory", gates: _Gates, now: datetime,
    ) -> bool:
        """long_term row idle long enough to fall back to archive."""
        if mem.pinned or mem.revival_score >= 0.05:
            return False
        anchor = _parse_iso(mem.last_used_at) or _parse_iso(mem.created_at)
        if anchor is None:
            return False
        idle_days = (now - anchor).total_seconds() / 86400.0
        return idle_days >= gates.demote_idle_days

    @staticmethod
    def _should_coerce(mem: "Memory") -> bool:
        """Pinned row that drifted out of long_term."""
        return bool(mem.pinned) and mem.tier != "long_term"

    # ── sweep stages ─────────────────────────────────────────────────

    def _promote_scratchpad(self, gates: _Gates, now: datetime) -> int:
        promoted = 0
        for mem in self._store.iter_by_tier("scratchpad"):
            if self._should_promote(mem, gates, now):
                self._update_tier(mem, "long_term")
                promoted += 1
        return promoted

    def _delete_dead_scratchpad(self, gates: _Gates, now: datetime) -> int:
        """Drop scratchpad rows nothing ever came back to.

        The revival condition is a threshold rather than the exact
        ``revival_score == 0.0`` it used to be. Two reasons, and the
        second is the load-bearing one:

        - Float equality against a value that *decays* toward zero is
          brittle by construction.
        - F12 gave revival a semantic fallback, so a memory now earns a
          small score merely for being close to a reply in embedding
          space. Since surfaced memories were selected for topical
          similarity to the turn in the first place, "any score at all"
          became a bar that almost everything clears -- keeping the
          exact-zero test would have quietly switched scratchpad TTL off
          altogether. The threshold sits above ``semantic_revival_per_hit``
          and at or below ``revival_per_hit``, so a memory Aiko actually
          quoted is still spared exactly as before, while one that was
          merely on topic is not.
        """
        deleted = 0
        for mem in self._store.iter_by_tier("scratchpad"):
            if not self._should_delete(mem, gates, now):
                continue
            try:
                if self._store.delete(mem.id):
                    deleted += 1
            except Exception:
                log.debug(
                    "scratchpad delete failed id=%s", mem.id, exc_info=True,
                )
        return deleted

    def _demote_idle_long_term(self, gates: _Gates, now: datetime) -> int:
        demoted = 0
        for mem in self._store.iter_by_tier("long_term"):
            if self._should_demote(mem, gates, now):
                self._update_tier(mem, "archive")
                demoted += 1
        return demoted

    def _coerce_pinned(self) -> int:
        # Walk scratchpad + archive only; long_term is the target.
        coerced = 0
        for tier in ("scratchpad", "archive"):
            for mem in self._store.iter_by_tier(tier):
                if self._should_coerce(mem):
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
