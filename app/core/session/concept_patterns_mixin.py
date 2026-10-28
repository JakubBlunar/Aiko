"""Public diagnostics and source receipts for concept/pattern learning."""
from __future__ import annotations

from collections import Counter

from app.core.concepts.understanding_arcs import record_learning_sources, understanding_arc
from app.core.concepts.concept_view import concept_view_from
from app.core.memory.shared_shorthand import sources_current
from app.core.memory.pattern_accounting import intake_snapshot


class ConceptPatternsMixin:
    def _record_learning_arc_receipt(self, event):
        record_learning_sources(self._chat_db, event, self._concept_store,
                                getattr(self, "_memory_store", None),
                                getattr(self, "_topic_graph", None))

    def _understanding_arc(self, concept):
        owner = getattr(self, "_user_id", "default")
        if getattr(concept, "user_id", None) not in {None, owner}:
            return {"status": "other_user", "steps": []}
        return understanding_arc(
            concept, store=self._concept_store,
            learning=getattr(self, "_concept_learning_store", None),
            lifecycle=getattr(self, "_concept_event_store", None), db=self._chat_db,
            memories=getattr(self, "_memory_store", None),
            graph=getattr(self, "_topic_graph", None),
        )

    def understanding_arcs(self, *, subject="all", limit=3):
        if subject not in {"all", "user", "aiko", "relationship"}:
            raise ValueError("subject must be all, user, aiko or relationship")
        view = concept_view_from(self)
        if view is None:
            return {"enabled": False, "arcs": []}
        subjects = ("user", "aiko", "relationship") if subject == "all" else (subject,)
        arcs_by_subject, counts = {}, Counter()
        for name in subjects:
            arcs_by_subject[name] = []
            for concept in view.core(subject=name, limit=8):
                packet = self._understanding_arc(concept)
                counts[name + ":" + packet["status"]] += 1
                if packet["status"] == "supported":
                    arcs_by_subject[name].append(packet)
        arcs = [arcs_by_subject[name][index] for index in range(8) for name in subjects
                if index < len(arcs_by_subject[name])]
        return {"enabled": True, "inspected_per_subject_cap": 8,
                "sample_counts": dict(counts), "arcs": arcs[:max(1, min(9, int(limit)))]}

    def pattern_learning_snapshot(self):
        memories = getattr(self, "_memory_store", None)
        db = getattr(self, "_chat_db", None)
        owner = getattr(self, "_user_id", "default")
        result = {"enabled": memories is not None, "sample_limit_per_kind": 256,
                  "by_kind": {}, "reading_guide": (
                      "Admitted, source-valid, included and used are different stages. "
                      "Echo/engagement is not a causal helpfulness judgment. "
                      "Legacy learning events without source receipts remain unverified.")}
        if memories is None or db is None:
            return result
        result["intake"] = intake_snapshot(db, owner)
        for kind, key in (("shared_term", "shared_shorthand"), ("strategy", "learned_strategy")):
            rows = [r for r in memories.list_recent(limit=256, kind=kind)
                    if (r.metadata or {}).get(key, {}).get("owner") == owner]
            counts = Counter({"records": 0, "source_valid": 0, "retired": 0, "failed_outcomes": 0})
            for row in rows:
                value = row.metadata[key]
                counts["records"] += 1
                counts["source_valid"] += int(sources_current(value, db))
                counts["retired"] += int(bool(value.get("retired")))
                counts["failed_outcomes"] += int(value.get("success") is False)
            result["by_kind"][kind] = dict(counts)
        metrics = self.get_last_metrics()
        trace = metrics.get("concepts_surfaced", {})
        result["last_turn_patterns"] = trace.get("patterns", [])
        result["last_turn_arcs"] = trace.get("understanding_arcs", [])
        return result
