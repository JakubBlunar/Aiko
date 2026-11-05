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
        from app.core.memory.memory_relations import review_state

        if any(review_state(memory, self._memory_store)["review_required"]
               for memory in rows.values()):
            return "source_context_review"
        return next((reason for memory in rows.values()
                     if (reason := claim_source_refusal(memory, subject=subject, kind=kind))), None)

    def _claim_source_refusal(self, proposal):
        if proposal.applicability:
            from app.core.concepts.concept_applicability import scope_sources_valid

            if not scope_sources_valid(proposal.applicability, self._memory_store):
                return "changed_applicability_sources"
            citations = [item for field in ("contexts", "exceptions")
                         for item in proposal.applicability.get(field, [])]
            if proposal.applicability.get("validity"):
                citations.append(proposal.applicability["validity"])
            for citation in citations:
                memory = self._memory_store.get(citation["memory_id"])
                reason = claim_source_refusal(memory, subject=proposal.subject,
                                              kind=proposal.kind)
                if reason:
                    return reason
                if not longitudinal_sources_current(memory, self._memory_store):
                    return "changed_applicability_sources"
        for node_type, raw_id in proposal.evidence:
            if node_type in {"memory", "moment"}:
                memory = self._memory_store.get(int(raw_id))
                if memory is not None and not longitudinal_sources_current(
                    memory, self._memory_store,
                ):
                    return "changed_observation_sources"
                if memory is not None:
                    from app.core.memory.memory_relations import review_state

                    if review_state(memory, self._memory_store)["review_required"]:
                        return "source_context_review"
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
