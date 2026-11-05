"""Incremental edit handling without retaining stale topic vectors."""
import logging

from app.core.conversation.topic_memory_policy import topic_member_eligible


class TopicGraphUpdatesMixin:
    def on_memory_updated(self, memory):
        if not self.persistent:
            self.invalidate()
            return
        with self._lock:
            warm = self._warm
            self._coact_cache = {}
        if not warm:
            return  # Warm-start reads the current memory mirror.
        self.on_memory_deleted(int(memory.id))
        self.on_memory_added(memory)

    def _refresh_centroid(self, cluster_id):
        with self._lock:
            cluster = self._live.get(cluster_id)
            member_ids = set(cluster.member_ids) if cluster else set()
        members = [self._memory_store.get(mid) for mid in member_ids]
        members = [m for m in members if topic_member_eligible(m)
                   and getattr(m, "embedding", None) is not None]
        if not members:
            return
        centroid = self._compute_centroid(members)
        from app.core.conversation.topic_graph import _LiveClusterToRow

        with self._lock:
            cluster = self._live.get(cluster_id)
            if cluster is None or set(cluster.member_ids) != member_ids:
                return
            cluster.centroid = centroid
            row = _LiveClusterToRow(cluster)
        try:
            self._cluster_store.upsert_cluster(row)
        except Exception:
            logging.getLogger("app.topic_graph").debug("centroid persist failed", exc_info=True)
