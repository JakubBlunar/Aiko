"""Bounded, fail-closed re-adjudication for previously disproven concepts."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.core.concepts.concept_support import memory_revision, revision_hash


class RevivalMixin:
    def _review_positive_rows(self, nodes, memory_store):
        rows = []
        graph = self._topic_graph_provider() if self._topic_graph_provider is not None else None
        clusters = (
            {int(cluster.representative_id): cluster for cluster in graph.topic_clusters()}
            if graph is not None
            else {}
        )
        for kind, ident in nodes:
            if kind in {"memory", "cluster", "moment"}:
                ids = [int(ident)]
                if kind == "cluster" and int(ident) in clusters:
                    ids.extend(sorted(set(clusters[int(ident)].member_ids), reverse=True)[:5])
                rows.extend(memory_store.get(member) for member in ids)
            elif kind == "concept":
                base = self._store.get(int(ident))
                rows.append(
                    SimpleNamespace(
                        id=f"concept:{ident}",
                        content=f"{base.label}. {base.rationale}",
                        created_at=base.last_reinforced_at,
                    )
                    if base is not None
                    else None
                )
        if any(row is None for row in rows):
            return []
        unique = {str(row.id): row for row in rows}
        return sorted(
            unique.values(),
            key=lambda row: (
                str(getattr(row, "created_at", "") or ""),
                str(row.id),
            ),
            reverse=True,
        )[:6]

    def _has_revival_support(self, concept) -> bool:
        review = self._store.support.review(concept.concept_id)
        if concept.last_reinforced_at and review.get(
            "blocked_support_at"
        ) == concept.last_reinforced_at:
            return False
        if self._reinforced_since_last(concept, False):
            return True
        return bool(
            concept.last_reinforced_at
            and review.get("support_at") == concept.last_reinforced_at
            and review.get("pending", review.get("outcome") not in {
                "resolved", "assessed_no_support", "invalidated",
            })
        )

    def _review_revival(self, concept, now: datetime, stats) -> bool:
        negatives = [
            edge
            for edge in self._store.edges_from("concept", concept.concept_id)
            if edge.relation == "contradicts"
        ]
        ledger = self._store.support
        if (
            concept.status != "contradicted"
            and not negatives
            and not ledger.had_disproof(concept.concept_id)
        ):
            return True
        if not self._has_revival_support(concept):
            ledger.set_state(concept.concept_id, "not_assessed", "fresh_observation_required")
            return False
        review = ledger.review(concept.concept_id)
        support_at = concept.last_reinforced_at
        review["support_at"] = support_at
        detector = self._contradiction_detector
        reason = "disproof_review_unavailable"
        outcome = "not_assessed"
        if not support_at:
            reason = "fresh_observation_required"
        elif (
            not negatives
            or len(negatives) > 6
            or any(edge.dst_type != "memory" for edge in negatives)
            or set(ledger.disproof_ids(concept.concept_id))
            - {int(edge.dst_id) for edge in negatives if edge.dst_type == "memory"}
        ):
            reason = "disproof_evidence_missing_or_over_budget"
        elif detector is not None and callable(getattr(detector, "reassess", None)):
            memory_store = detector._memory_store
            positive_nodes = ledger.inputs(concept.concept_id)
            if not positive_nodes:
                positive_nodes = [
                    (edge.src_type, edge.src_id)
                    for edge in self._store.evidence_of(concept.concept_id)
                ][:6]
            negative_ids = sorted({int(edge.dst_id) for edge in negatives})
            negative_rows = [memory_store.get(ident) for ident in negative_ids]
            positive_rows = self._review_positive_rows(positive_nodes, memory_store)
            if not positive_rows or any(row is None for row in [*negative_rows, *positive_rows]):
                reason = "review_sources_unavailable"
            else:
                signature = revision_hash(
                    [
                        concept.label,
                        concept.rationale,
                        support_at,
                        review.get("disproof_version", 0),
                        [(row.id, memory_revision(row)) for row in negative_rows],
                        [(row.id, memory_revision(row)) for row in positive_rows],
                    ]
                )
                previous_at = review.get("at")
                retry_due = not previous_at or now >= (
                    datetime.fromisoformat(previous_at) + timedelta(hours=6)
                )
                if review.get("signature") == signature and (
                    review.get("outcome") != "not_assessed" or not retry_due
                ):
                    outcome, reason = review["outcome"], review["reason"]
                elif stats.get("revival_review_checks", 0) < 2 and stats[
                    "contradiction_checks"
                ] < max(0, self._i("concept_contradiction_batch_size", 20)):
                    stats["contradiction_checks"] += 1
                    stats["revival_review_checks"] = stats.get("revival_review_checks", 0) + 1
                    try:
                        verdict = detector.reassess(concept, negative_rows, positive_rows)
                    except Exception:
                        verdict = None
                    outcome, reason = {
                        "RESOLVED": ("resolved", "explicit_disproof_resolution"),
                        "STILL_VALID": ("assessed_no_support", "disproof_still_valid"),
                    }.get(verdict, ("not_assessed", "review_uncertain_or_failed"))
                    review.update(
                        signature=signature, outcome=outcome, reason=reason, at=now.isoformat()
                    )
                else:
                    reason = "review_batch_exhausted"
        review["pending"] = outcome == "not_assessed"
        ledger.record_review(concept.concept_id, review)
        ledger.set_state(
            concept.concept_id, "supported_pending" if outcome == "resolved" else outcome, reason
        )
        stats["revival_review_" + outcome] = stats.get("revival_review_" + outcome, 0) + 1
        return outcome == "resolved"
