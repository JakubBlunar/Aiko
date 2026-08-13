"""Periodic compaction for the LanceDB store -- Lance's ``VACUUM``.

Every write into the vector store is a **single row**: one `add_memory`
when a memory is written, one row per message as the indexer catches up.
Lance records each append as its own fragment with its own manifest
version, and nothing reclaims either on its own, so the store grows a file
per row for as long as the app is used. One live database reached **26,765
files and 1.09 GB** holding 1,796 memories and 4,379 messages -- whose
vectors are about 25 MB of real data -- with one data file per message row
and 18,293 versions against 1,796 rows on `memories`. Compacting it left
10 files and 27 MB, freeing 1.06 GB in 10.7 s.

Nothing in the row counts moves while that happens, which is exactly why
it went unnoticed for months. The cost is not mainly disk: every search
opens and scans every fragment, so this is read latency on the turn path.

Two things make this worker unlike its neighbours:

* **The probe may not measure what it cares about.** Fragment counts come
  from a filesystem walk, and walking 26k files is not a 50 ms probe. The
  dataset *version* is the honest cheap proxy -- it increments once per
  write, which is precisely what accumulates -- and reads in ~0.2 ms.
* **The run takes the store's exclusive lock**, so a turn arriving
  mid-compaction waits for it. That is the reason for a long heartbeat and
  a high saturation bar rather than eager tidying: on a store kept in
  shape a pass is ~2 s, and the scheduler only admits it from the ``away``
  idle tier on.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import WorkSignal

if TYPE_CHECKING:
    from app.core.rag.rag_store import RagStore


log = logging.getLogger("app.rag_maintenance_worker")

# Watermark: the summed dataset version across the three tables as of the
# last successful compaction. Versions only ever increase, so the delta is
# "writes since we last tidied".
KV_KEY = "rag_maintenance.compacted_at_version"

# Writes since the last pass at which the worker reports saturation.
_SATURATION_WRITES = 2000

# Below this the merge would not earn the exclusive lock it needs.
_FLOOR_WRITES = 250

# Long on purpose -- see the module docstring on the write lock.
_INTERVAL_SECONDS = 6.0 * 3600.0


class RagMaintenanceWorker:
    """IdleWorker that compacts the vector store's fragments and versions."""

    name = "rag_maintenance"

    def __init__(
        self,
        store: "RagStore",
        *,
        kv_get: Callable[[str], str | None] | None = None,
        kv_set: Callable[[str, str], None] | None = None,
        saturation_writes: int = _SATURATION_WRITES,
        floor_writes: int = _FLOOR_WRITES,
        interval_seconds: float = _INTERVAL_SECONDS,
    ) -> None:
        self._store = store
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._saturation = max(1, int(saturation_writes))
        self._floor = max(0, int(floor_writes))
        self._interval = max(60.0, float(interval_seconds))

    # ── IdleWorker protocol ─────────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def is_ready(self, *, now: datetime, last_run_at: datetime | None) -> bool:
        return self._store is not None

    def demand(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> "WorkSignal | None":
        """Pressure from writes since the last pass, via dataset version.

        Returns ``None`` -- "no opinion, schedule me the old way" -- only
        when the version cannot be read at all, so a store that has quietly
        stopped answering falls back to the heartbeat instead of being
        silently dropped.
        """
        current = self._version_sum()
        if current is None:
            return None
        since = current - self._watermark()
        if since < self._floor:
            return WorkSignal(
                pressure=0.0,
                reason="only %d writes since last compaction" % max(0, since),
            )
        return WorkSignal(
            pressure=min(1.0, since / float(self._saturation)),
            reason="%d writes since last compaction" % since,
            # Compaction is pure IO and CPU: it must not be charged to the
            # lane that exists to protect a shared GPU.
            needs_llm=False,
        )

    def run(self) -> dict[str, Any] | None:
        before_version = self._version_sum()
        watermark = self._watermark()
        try:
            result = self._store.optimize()
        except Exception:
            log.warning("rag_maintenance: optimize failed", exc_info=True)
            return {"skipped": True, "reason": "optimize_failed"}
        # After, not before: compaction itself commits new versions, and a
        # watermark taken beforehand would count them as fresh writes and
        # re-arm the worker immediately.
        self._store_watermark(self._version_sum())
        before = result.get("before", {})
        after = result.get("after", {})
        summary: dict[str, Any] = {
            "files_before": sum(t.get("files", 0) for t in before.values()),
            "files_after": sum(t.get("files", 0) for t in after.values()),
            "bytes_freed": result.get("bytes_freed", 0),
            "duration_ms": result.get("duration_ms", 0.0),
            "writes_since_last": (
                None if before_version is None else before_version - watermark
            ),
        }
        if "errors" in result:
            summary["errors"] = result["errors"]
        log.info(
            "rag_maintenance done: files %s -> %s, %.1f MB freed in %.1fs",
            summary["files_before"], summary["files_after"],
            float(summary["bytes_freed"]) / 1e6,
            float(summary["duration_ms"]) / 1000.0,
        )
        return summary

    # ── internals ───────────────────────────────────────────────────────

    def _version_sum(self) -> int | None:
        """Summed dataset version across the tables, or ``None`` if unreadable."""
        try:
            return int(
                self._store._memories.version
                + self._store._messages.version
                + self._store._documents.version
            )
        except Exception:
            log.debug("rag_maintenance: version read failed", exc_info=True)
            return None

    def _watermark(self) -> int:
        raw = None
        if self._kv_get is not None:
            try:
                raw = self._kv_get(KV_KEY)
            except Exception:
                log.debug("rag_maintenance: watermark read failed", exc_info=True)
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            # Never compacted: every version to date counts as backlog,
            # which for a fresh store is the 3 the tables were created with.
            return 0

    def _store_watermark(self, version: int | None) -> None:
        if version is None or self._kv_set is None:
            return
        try:
            self._kv_set(KV_KEY, str(int(version)))
        except Exception:
            log.debug("rag_maintenance: watermark write failed", exc_info=True)


__all__ = ["KV_KEY", "RagMaintenanceWorker"]
