"""L25 concept edge integrity sweep -- the idle defence-in-depth worker.

A tiny :class:`~app.core.proactive.idle_worker.IdleWorker` that periodically
asks the :class:`~app.core.concepts.concept_edge_reconciler.ConceptEdgeReconciler`
to garbage-collect concept edges whose memory endpoint no longer exists.

Most memory deletes are reconciled *synchronously* by the reconciler's
delete-listener hook, but ``MemoryStore.prune`` batch-deletes rows without
firing delete listeners, so orphaned edges can still accumulate. This
worker is the safety net that reconciles them. It runs infrequently (an
hour by default) over a bounded batch, does no LLM work, and mirrors the
L3 ``ConceptLifecycleWorker`` shape.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.core.proactive.idle_worker import WorkSignal, pressure_from_count

if TYPE_CHECKING:
    from app.core.concepts.concept_edge_reconciler import ConceptEdgeReconciler


class ConceptEdgeIntegrityWorker:
    """IdleWorker: garbage-collect orphaned concept<->memory edges and
    reconcile the affected concepts' evidence counts (L25)."""

    name = "concept_edge_integrity"

    def __init__(
        self,
        *,
        reconciler: "ConceptEdgeReconciler",
        memory_settings: Any,
        agent_settings: Any,
    ) -> None:
        self._reconciler = reconciler
        self._memory_settings = memory_settings
        self._agent_settings = agent_settings

    # ── idle worker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(
                self._memory_settings,
                "concept_edge_integrity_interval_seconds",
                3600.0,
            )
        )

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None
    ) -> bool:
        return self._enabled()

    def demand(
        self, *, now: datetime, last_run_at: datetime | None
    ) -> "WorkSignal | None":
        """Count orphaned edges without dropping them.

        This is a safety net for the one path that can create orphans
        (``MemoryStore.prune`` batch-deletes without firing delete
        listeners), so on an install where nothing has been pruned the
        count is zero and the worker never takes a slot — which is most
        of the time.
        """
        if not self._enabled():
            return WorkSignal(pressure=0.0, reason="disabled")
        batch = self._batch_size()
        try:
            count = self._reconciler.orphan_backlog(batch)
        except Exception:
            return None
        return WorkSignal(
            pressure=pressure_from_count(count, saturation=batch),
            reason=f"{count} orphan edges",
        )

    def _batch_size(self) -> int:
        return max(
            1,
            int(
                getattr(
                    self._memory_settings,
                    "concept_edge_integrity_batch_size",
                    200,
                )
            ),
        )

    def _enabled(self) -> bool:
        if not bool(getattr(self._agent_settings, "concepts_enabled", False)):
            return False
        return bool(
            getattr(
                self._memory_settings, "concept_edge_integrity_enabled", True
            )
        )

    # ── run ────────────────────────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        if not self._enabled():
            return {"skipped": True, "reason": "disabled"}
        return self._reconciler.sweep(self._batch_size())


__all__ = ["ConceptEdgeIntegrityWorker"]
