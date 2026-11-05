"""Fail closed on sources that cannot establish the proposed personal claim."""
from app.core.memory.memory_origin import claim_source_refusal
from app.core.memory.observation_receipts import longitudinal_sources_current


class ConceptSourcePolicyMixin:
    def _cluster_source_refusal(self, cluster, *, subject, kind):
        rows = self._memory_store.get_many(cluster.member_ids)
        if not rows or set(rows) != {int(mid) for mid in cluster.member_ids}:
            return "missing_cluster_sources"
        if any(not longitudinal_sources_current(memory, self._memory_store)
               for memory in rows.values()):
            return "changed_observation_sources"
        return next((reason for memory in rows.values()
                     if (reason := claim_source_refusal(memory, subject=subject, kind=kind))), None)

    def _claim_source_refusal(self, proposal):
        for node_type, raw_id in proposal.evidence:
            if node_type in {"memory", "moment"}:
                memory = self._memory_store.get(int(raw_id))
                if memory is not None and not longitudinal_sources_current(
                    memory, self._memory_store,
                ):
                    return "changed_observation_sources"
                reason = (claim_source_refusal(memory, subject=proposal.subject, kind=proposal.kind)
                          if memory is not None else "missing_memory")
                if reason:
                    return reason
            elif node_type == "cluster":
                cluster = next((c for c in self._topic_graph.topic_clusters()
                                if int(c.representative_id) == int(raw_id)), None)
                if cluster is None:
                    return "missing_cluster"
                reason = self._cluster_source_refusal(cluster, subject=proposal.subject,
                                                      kind=proposal.kind)
                if reason:
                    return reason
        return None
