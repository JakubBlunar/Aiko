"""Persistence for the K9 topic graph (schema v20).

The topic graph used to be a fully in-memory clustering recomputed on
every read. This store persists it so (a) a restart doesn't pay a cold
O(n^2) rebuild and (b) a new memory can be assigned to the nearest
existing cluster incrementally instead of re-clustering the whole
corpus. Two tables back it:

- ``topic_clusters`` -- one row per cluster: a raw float32 ``centroid``
  blob (so nearest-cluster assignment is a tiny in-memory matmul over
  the handful of centroids), an optional ``label``, and a running
  ``size``.
- ``memory_topic_assignments`` -- one row per *clustered* memory mapping
  it to its ``cluster_id``. A memory with no row is "unclustered /
  pending" (it arrived but didn't fit any existing cluster; the next
  batch refit will place it).

Like :class:`app.core.memory.memory_conflict_store.MemoryConflictStore`
this talks to SQLite directly and does cascade cleanup in Python
(``delete_for_memory``) rather than via SQL foreign keys, because
``memories`` rows are owned by ``MemoryStore``'s in-memory mirror.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.topic_cluster_store")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode_centroid(vec: "np.ndarray | None") -> bytes:
    if vec is None:
        return b""
    arr = np.asarray(vec, dtype=np.float32).ravel()
    return arr.tobytes()


def _decode_centroid(blob: "bytes | None", dim: int) -> np.ndarray:
    if not blob or dim <= 0:
        return np.zeros(0, dtype=np.float32)
    arr = np.frombuffer(blob, dtype=np.float32)
    if arr.size != dim:
        # Dimension drift (embedding-model swap). Treat as empty so the
        # caller re-derives the centroid on the next batch refit.
        return np.zeros(0, dtype=np.float32)
    return np.array(arr, dtype=np.float32)  # copy: frombuffer is read-only


@dataclass(slots=True)
class ClusterRow:
    """One persisted cluster. ``centroid`` is a unit-norm float32 vector
    (empty array when unknown)."""

    cluster_id: int
    label: str = ""
    centroid: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))
    size: int = 0
    created_at: str = ""
    updated_at: str = ""


class TopicClusterStore:
    """CRUD for ``topic_clusters`` + ``memory_topic_assignments``."""

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    # ── reads ─────────────────────────────────────────────────────────

    def load_all(self) -> tuple[list[ClusterRow], dict[int, int]]:
        """Return ``(clusters, assignments)`` for boot-time warm start.

        ``assignments`` maps ``memory_id -> cluster_id``. Both come from
        SQLite in one pass so the in-process graph can rebuild its state
        without touching embeddings.
        """
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        clusters: list[ClusterRow] = []
        try:
            rows = conn.execute(
                "SELECT cluster_id, label, centroid, dim, size, "
                "created_at, updated_at FROM topic_clusters"
            ).fetchall()
        except Exception:
            log.warning("topic_clusters load failed", exc_info=True)
            rows = []
        for r in rows:
            clusters.append(
                ClusterRow(
                    cluster_id=int(r[0]),
                    label=str(r[1] or ""),
                    centroid=_decode_centroid(r[2], int(r[3] or 0)),
                    size=int(r[4] or 0),
                    created_at=str(r[5] or ""),
                    updated_at=str(r[6] or ""),
                )
            )
        assignments: dict[int, int] = {}
        try:
            arows = conn.execute(
                "SELECT memory_id, cluster_id FROM memory_topic_assignments"
            ).fetchall()
        except Exception:
            log.warning("topic assignments load failed", exc_info=True)
            arows = []
        for r in arows:
            assignments[int(r[0])] = int(r[1])
        return clusters, assignments

    def next_cluster_id(self) -> int:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        row = conn.execute("SELECT MAX(cluster_id) FROM topic_clusters").fetchone()
        return int(row[0]) + 1 if row and row[0] is not None else 1

    # ── incremental writes ────────────────────────────────────────────

    def upsert_cluster(self, row: ClusterRow) -> None:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        centroid = np.asarray(row.centroid, dtype=np.float32).ravel()
        now = _now_iso()
        try:
            conn.execute(
                "INSERT INTO topic_clusters "
                "(cluster_id, label, centroid, dim, size, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(cluster_id) DO UPDATE SET "
                "  label = excluded.label, centroid = excluded.centroid, "
                "  dim = excluded.dim, size = excluded.size, "
                "  updated_at = excluded.updated_at",
                (
                    int(row.cluster_id),
                    str(row.label or ""),
                    _encode_centroid(centroid),
                    int(centroid.size),
                    int(row.size),
                    row.created_at or now,
                    now,
                ),
            )
            conn.commit()
        except Exception:
            log.warning("upsert_cluster failed (id=%s)", row.cluster_id, exc_info=True)

    def set_assignment(
        self, memory_id: int, cluster_id: int, *, assigned_at: str | None = None
    ) -> None:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            conn.execute(
                "INSERT INTO memory_topic_assignments "
                "(memory_id, cluster_id, assigned_at) VALUES (?, ?, ?) "
                "ON CONFLICT(memory_id) DO UPDATE SET "
                "  cluster_id = excluded.cluster_id, "
                "  assigned_at = excluded.assigned_at",
                (int(memory_id), int(cluster_id), assigned_at or _now_iso()),
            )
            conn.commit()
        except Exception:
            log.warning("set_assignment failed (mem=%s)", memory_id, exc_info=True)

    def delete_assignment(self, memory_id: int) -> None:
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            conn.execute(
                "DELETE FROM memory_topic_assignments WHERE memory_id = ?",
                (int(memory_id),),
            )
            conn.commit()
        except Exception:
            log.warning("delete_assignment failed (mem=%s)", memory_id, exc_info=True)

    def delete_cluster(self, cluster_id: int) -> None:
        """Drop a cluster and any assignments still pointing at it."""
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        try:
            conn.execute(
                "DELETE FROM memory_topic_assignments WHERE cluster_id = ?",
                (int(cluster_id),),
            )
            conn.execute(
                "DELETE FROM topic_clusters WHERE cluster_id = ?",
                (int(cluster_id),),
            )
            conn.commit()
        except Exception:
            log.warning("delete_cluster failed (id=%s)", cluster_id, exc_info=True)

    def delete_for_memory(self, memory_id: int) -> None:
        """Cascade hook for ``MemoryStore.delete`` -- drop the row's
        assignment. The cluster's running ``size`` is reconciled by the
        in-process graph (or the next batch refit), not here."""
        self.delete_assignment(memory_id)

    # ── batch refit ───────────────────────────────────────────────────

    def replace_all(
        self, clusters: list[ClusterRow], assignments: dict[int, int]
    ) -> None:
        """Atomically swap the entire persisted graph (batch refit).

        Wipes both tables and re-inserts in a single transaction so a
        crash mid-write never leaves a half-clustered state.
        """
        conn = self._db._get_conn()  # type: ignore[attr-defined]
        now = _now_iso()
        try:
            conn.execute("BEGIN")
            conn.execute("DELETE FROM memory_topic_assignments")
            conn.execute("DELETE FROM topic_clusters")
            for row in clusters:
                centroid = np.asarray(row.centroid, dtype=np.float32).ravel()
                conn.execute(
                    "INSERT INTO topic_clusters "
                    "(cluster_id, label, centroid, dim, size, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        int(row.cluster_id),
                        str(row.label or ""),
                        _encode_centroid(centroid),
                        int(centroid.size),
                        int(row.size),
                        row.created_at or now,
                        now,
                    ),
                )
            if assignments:
                conn.executemany(
                    "INSERT INTO memory_topic_assignments "
                    "(memory_id, cluster_id, assigned_at) VALUES (?, ?, ?)",
                    [
                        (int(mid), int(cid), now)
                        for mid, cid in assignments.items()
                    ],
                )
            conn.commit()
        except Exception:
            log.warning("replace_all failed; rolling back", exc_info=True)
            try:
                conn.rollback()
            except Exception:
                pass


__all__ = ["ClusterRow", "TopicClusterStore"]
