"""Batch refit worker for the persisted K9 topic graph (schema v20).

The topic graph maintains itself incrementally on every memory add /
delete (nearest-centroid assignment), which is cheap but drifts: new
memories that didn't fit any cluster pile up "unclustered", centroids
wander as members are deleted without re-derivation, and genuinely new
topic families never form a cluster on their own. This :class:`IdleWorker`
runs the *full* mutual-k-NN rebuild during quiet windows to correct all
of that, persisting the result so the next boot warm-starts instantly.

It fires on two triggers, whichever comes first:

  - the periodic interval (``topic_graph_rebuild_interval_seconds``,
    default daily), and
  - pressure: once ``pending_unclustered`` crosses
    ``topic_graph_refit_pending_threshold``, so a burst of new topics
    (e.g. a web-knowledge enrichment run) gets folded in promptly rather
    than waiting a whole day.

The rebuild itself routes through LanceDB ANN above the corpus-size
threshold, so it stays affordable even on a large / uncapped memory
store.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.conversation.topic_graph import TopicGraph


log = logging.getLogger("app.topic_graph_rebuild_worker")

_DEFAULT_INTERVAL_SECONDS = 86_400.0  # daily
_DEFAULT_PENDING_THRESHOLD = 25


class TopicGraphRebuildWorker:
    """IdleWorker wrapping :meth:`TopicGraph.rebuild`."""

    name = "topic_graph_rebuild"

    def __init__(
        self,
        topic_graph: "TopicGraph",
        *,
        interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
        pending_threshold: int = _DEFAULT_PENDING_THRESHOLD,
    ) -> None:
        self._graph = topic_graph
        self._interval = max(60.0, float(interval_seconds))
        self._pending_threshold = max(1, int(pending_threshold))

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        if not getattr(self._graph, "persistent", False):
            return False
        # Pressure trigger: enough memories have arrived that didn't fit
        # an existing cluster -> refit early, ignoring the interval.
        try:
            if self._graph.pending_count() >= self._pending_threshold:
                return True
        except Exception:
            pass
        return default_is_ready(
            self._interval, now=now, last_run_at=last_run_at,
        )

    def run(self) -> dict[str, Any]:
        if not getattr(self._graph, "persistent", False):
            return {"skipped": True, "reason": "not_persistent"}
        pending_before = 0
        try:
            pending_before = self._graph.pending_count()
        except Exception:
            pending_before = 0
        try:
            clusters = self._graph.rebuild()
        except Exception:
            log.warning("topic graph rebuild failed", exc_info=True)
            raise
        log.info(
            "topic_graph_rebuild: clusters=%d pending_before=%d",
            clusters,
            pending_before,
        )
        return {"clusters": clusters, "pending_before": pending_before}


__all__ = ["TopicGraphRebuildWorker"]
