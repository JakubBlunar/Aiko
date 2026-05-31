"""Memory salience decay worker (schema v8 / E1+E2).

Thin :class:`IdleWorker` that calls :meth:`MemoryStore.decay` with the
current :class:`MemorySettings` per-tier rates + revival coefficients.
The actual elapsed-time accounting lives inside ``decay()`` itself,
which reads ``memory.last_decay_run_at`` from the ``kv_meta`` table --
so running every hour applies 1/24 of the daily rate, and coming back
online after 3 days produces 3 days' worth (clamped to
``decay_max_catchup_days``).

Replaces the legacy ``SessionController._memory_decay_loop`` daemon
thread; consolidating into the scheduler means decay shares the same
quiet-window gate as the promotion worker.

Schema v10 — also runs two cheap reclassification passes per tick:

  - ``future_plan`` rows whose ``event_time`` slipped at least an
    hour into the past are flipped to ``past_event`` with a fresh
    ``relevance_until = event_time + 7d``. The 1-hour buffer keeps
    a plan flagged as "future" through the moment it's actually
    happening (no premature flip if Aiko has the chat open while
    the user is at the gym).
  - ``past_event`` rows whose ``relevance_until`` already passed
    are demoted to the ``archive`` tier so they stop crowding RAG
    while staying available for archive / reflection work.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.memory.memory_store import MemoryStore
    from app.core.infra.settings import MemorySettings


log = logging.getLogger("app.memory_decay_worker")


# Buffer between ``event_time`` passing and the future_plan -> past_event
# flip. Keeps the row "live" through the actual moment so retrieval
# bullet annotation still reads as "(planned for tonight 20:00)" while
# the user is actually at the gym.
_FUTURE_TO_PAST_BUFFER = timedelta(hours=1)
# Window past_event memories stay available in normal RAG before the
# decay worker demotes them to ``archive``. Matches the LLM
# extractor's default for past_event so freshly-extracted history
# rolls off after a week.
_PAST_EVENT_RELEVANCE_WINDOW = timedelta(days=7)


class MemoryDecayWorker:
    """IdleWorker wrapping :meth:`MemoryStore.decay`.

    Also piggy-backs the F2 knowledge-gap expiry pass (90-day TTL on
    unresolved unpinned gaps) so we don't need a second worker just to
    sweep stale journal rows.
    """

    name = "memory_decay"

    def __init__(
        self,
        store: "MemoryStore",
        settings: "MemorySettings",
        *,
        knowledge_gap_store: "Any | None" = None,
    ) -> None:
        self._store = store
        self._settings = settings
        # F2: optional handle to the knowledge-gap store. Wired by
        # ``SessionController`` so the decay worker can also run the
        # 90-day expiry pass on the journal. ``None`` keeps tests and
        # lean deployments running without the extra hook.
        self._knowledge_gap_store = knowledge_gap_store

    @property
    def interval_seconds(self) -> float:
        return float(self._settings.decay_worker_interval_seconds)

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
            return {"skipped": True, "reason": "tiers_disabled"}
        rates = {
            "scratchpad": float(self._settings.decay_rate_scratchpad),
            "long_term": float(self._settings.decay_rate_long_term),
            "archive": float(self._settings.decay_rate_archive),
        }
        try:
            stats = self._store.decay(
                decay_rates=rates,
                revival_coefficient=float(self._settings.revival_coefficient),
                revival_decay_per_day=float(self._settings.revival_decay_per_day),
                max_catchup_days=float(self._settings.decay_max_catchup_days),
            )
        except Exception:
            log.warning("memory decay failed", exc_info=True)
            raise
        log.info("memory_decay sweep: %s", stats)
        # F2: piggyback gap expiry. Best-effort — if it fails, the
        # decay sweep result still counts as a successful tick.
        out: dict[str, Any] = dict(stats) if isinstance(stats, dict) else {}
        gap_store = self._knowledge_gap_store
        if gap_store is not None:
            try:
                pruned = gap_store.prune_expired()
                if pruned:
                    out["knowledge_gaps_expired"] = int(pruned)
                    log.info(
                        "memory_decay: expired %d stale knowledge gap(s)",
                        pruned,
                    )
            except Exception:
                log.debug("knowledge gap expiry failed", exc_info=True)
        # Schema v10: temporal reclassification. Best-effort — if it
        # fails the rest of the decay sweep still counts as a tick.
        try:
            stats_temporal = self._reclassify_temporal()
        except Exception:
            log.debug("temporal reclassification failed", exc_info=True)
            stats_temporal = {}
        out.update(stats_temporal)
        return out

    # ── v10 temporal passes ──────────────────────────────────────────

    def _reclassify_temporal(self) -> dict[str, int]:
        """Run the two v10 temporal reclassification passes.

        Returns counters under stable keys so a future telemetry hook
        can graph how often the worker is actually doing useful work.
        """
        now = datetime.now(timezone.utc)
        future_cutoff = (now - _FUTURE_TO_PAST_BUFFER).isoformat()
        relevance_cutoff = now.isoformat()

        out = {
            "future_plans_to_past": 0,
            "past_events_archived": 0,
        }

        # Pass 1: future_plan -> past_event. We only flip rows whose
        # event_time strictly precedes ``now - buffer`` so a plan
        # currently happening keeps its "(planned for tonight 20:00)"
        # framing in retrieval until the moment is over.
        try:
            overdue = self._store.list_by_temporal_type(
                "future_plan",
                event_time_before=future_cutoff,
            )
        except Exception:
            log.debug("list future_plans failed", exc_info=True)
            overdue = []
        for mem in overdue:
            try:
                event_dt = self._parse_iso(mem.event_time)
                # Anchor the new relevance window on event_time so a
                # plan that slipped recognised hours ago still gets
                # the full retrospective window from when it actually
                # happened, not when the worker noticed.
                anchor = event_dt if event_dt is not None else now
                new_relevance = (anchor + _PAST_EVENT_RELEVANCE_WINDOW).isoformat()
                self._store.reclassify(
                    mem.id,
                    temporal_type="past_event",
                    relevance_until=new_relevance,
                )
                out["future_plans_to_past"] += 1
            except Exception:
                log.debug(
                    "reclassify future_plan id=%s failed", mem.id, exc_info=True
                )

        # Pass 2: past_event -> archive (tier demotion). Cheap because
        # the mirror snapshot is already filtered down to the candidates.
        try:
            expired = self._store.list_by_temporal_type(
                "past_event",
                relevance_until_before=relevance_cutoff,
            )
        except Exception:
            log.debug("list expired past_events failed", exc_info=True)
            expired = []
        for mem in expired:
            if mem.tier == "archive":
                # Already demoted on a previous tick.
                continue
            try:
                self._store.update(mem.id, tier="archive")
                out["past_events_archived"] += 1
            except Exception:
                log.debug(
                    "archive past_event id=%s failed", mem.id, exc_info=True
                )

        if out["future_plans_to_past"] or out["past_events_archived"]:
            log.info(
                "memory_decay temporal: %d future->past, %d past->archive",
                out["future_plans_to_past"],
                out["past_events_archived"],
            )
        return out

    @staticmethod
    def _parse_iso(value: str | None) -> datetime | None:
        """Best-effort ISO-8601 -> aware datetime parser.

        Mirrors the parsing done in :mod:`app.core.rag.rag_retriever`; kept
        local so the worker doesn't import the heavyweight retriever
        module just for one helper.
        """
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


__all__ = ["MemoryDecayWorker"]
