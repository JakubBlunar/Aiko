"""L52: bounded AND/OR support receipts over the existing influence graph.

``source --depends_on--> conclusion`` follows ConceptStore's source-to-dependent
direction. Every member of a support set is required; sets are alternatives.
Receipts contain identifiers and semantic fingerprints, never source text.
They are not confidence votes (L49 remains responsible for independence).
"""
from __future__ import annotations

import json
from itertools import combinations

from app.core.concepts.concept_applicability import applicability_state
from app.core.concepts.concept_support import revision_hash
from app.core.infra import timephrase

PREFIX = "concept.dependencies:"
REVIEW_PREFIX = "concept.dependency_review:"
MAX_SETS = 32
MAX_MEMBERS = 24
MAX_NODES = 256
MAX_DEPTH = 8


class DependencyLedger:
    def __init__(self, store):
        self.store = store
        self.db = store._db
        self.reload()

    def reload(self):
        self.records = {
            int(key[len(PREFIX):]): json.loads(value)
            for key, value in self.db._get_conn().execute(
                "SELECT key, value FROM kv_meta WHERE key GLOB ?", (PREFIX + "*",)
            )
        }

    def _memory(self, node):
        return self.db._get_conn().execute(
            "SELECT content, kind, event_time, metadata, confidence, source_message_id "
            "FROM memories WHERE id = ?", (int(node[1]),),
        ).fetchone()

    def fingerprint(self, node):
        kind, ident = node
        if kind == "concept":
            row = self.store.get(int(ident))
            return revision_hash([row.label, row.rationale]) if row else None
        row = self._memory(node)
        return revision_hash(list(row[:3])) if row else None

    def _write(self, cid, sets):
        self.db.kv_set(PREFIX + str(cid), json.dumps(sets))
        self.records[cid] = sets
        self.store.mark_lifecycle_due(cid)

    def add(self, concept_id, support_sets, *, revisions=None):
        """Register complete sufficient sets; reject oversized/cyclic admissions.

        Callers must use this API rather than writing depends_on edges alone.
        No truncation: a partial conjunction would invent sufficient support.
        """
        from app.core.concepts.concept_store import ConceptEdge

        cid = int(concept_id)
        if self.store.get(cid) is None:
            raise ValueError("dependency conclusion must exist")
        proposed = []
        for group in support_sets:
            nodes = sorted({(str(kind), str(int(ident))) for kind, ident in group})
            if not nodes or len(nodes) > MAX_MEMBERS:
                raise ValueError("dependency support set size out of bounds")
            members = []
            for node in nodes:
                if node[0] not in {"concept", "memory", "cluster", "moment"}:
                    raise ValueError("unsupported dependency source")
                fingerprint = self.fingerprint(node)
                if fingerprint is None:
                    raise ValueError("dependency source must exist")
                if revisions is not None and node in revisions and revisions[node] != fingerprint:
                    raise ValueError("dependency source changed during generation")
                members.append([*node, fingerprint])
            proposed.append(members)
        merged = self._minimal([*self.records.get(cid, []), *proposed])
        if len(merged) > MAX_SETS:
            raise ValueError("too many dependency alternatives")
        plan = {**self.records, cid: merged}
        if self._depth(plan, cid) >= MAX_DEPTH:
            raise ValueError("dependency cycle or depth limit")
        # Edges index membership; receipts retain conjunctions even after an
        # endpoint/edge is deleted, so deletion cannot silently shrink a set.
        for group in proposed:
            if group not in merged:
                continue
            for kind, ident, _revision in group:
                if not self.store.add_edge(ConceptEdge(
                    kind, ident, "concept", str(cid), "depends_on",
                )):
                    raise RuntimeError("dependency edge write failed")
        self._write(cid, merged)

    @staticmethod
    def _minimal(groups):
        unique = {json.dumps(group, sort_keys=True): group for group in groups}
        result = []
        for group in sorted(unique.values(), key=len):
            members = {tuple(member) for member in group}
            if not any({tuple(member) for member in old} <= members for old in result):
                result.append(group)
        return result

    def admit(self, concept, nodes, *, revisions=None):
        """Structural policy for existing L2 admissions, without new LLM calls.

        Set evidence supplies alternative observations; sequence/meta proposals
        require their whole admitted set. Historical self-corrections are not
        live premises. Legacy evidence is captured before its first reinforcement.
        """
        if concept.kind == "self_correction":
            return
        nodes = sorted({(str(kind), str(ident)) for kind, ident in nodes})
        if not nodes:
            return
        groups = [[node] for node in nodes] if concept.evidence_model == "set" else [nodes]
        self.add(concept.concept_id, groups, revisions=revisions)

    def capture_legacy(self, concept):
        if concept.concept_id in self.records or concept.kind == "self_correction":
            return
        nodes = [(edge.src_type, edge.src_id) for edge in self.store.evidence_of(
            concept.concept_id
        ) if edge.polarity > 0]
        # L20 historically requires any two active bases, whereas tension
        # requires both. Preserve that policy when a legacy row is reinforced.
        if concept.kind == "generalization" and len(nodes) <= 8:
            self.add(concept.concept_id, list(combinations(nodes, 2)))
        elif nodes:
            self.admit(concept, nodes)

    def _depth(self, records, root):
        remaining = MAX_NODES

        def walk(cid, path):
            nonlocal remaining
            if cid in path or len(path) >= MAX_DEPTH or remaining <= 0:
                return MAX_DEPTH
            remaining -= 1
            children = {int(member[1]) for group in records.get(cid, [])
                        for member in group if member[0] == "concept"}
            return max((1 + walk(child, path | {cid}) for child in children), default=0)

        return walk(root, set())

    def assess(self, concept_id, *, max_nodes=MAX_NODES):
        """Read current support; no cached verdict can outlive a source change."""
        remaining = max(1, min(MAX_NODES, int(max_nodes)))
        visited = 0

        def claim(cid, path):
            nonlocal visited
            if cid in path:
                return {"tracked": True, "viable": False, "reason": "cycle"}
            if len(path) >= MAX_DEPTH or visited >= remaining:
                return {"tracked": True, "viable": False, "reason": "traversal_limit"}
            visited += 1
            if cid not in self.records:
                return {"tracked": False, "viable": True, "reason": "legacy_untracked"}
            valid = []
            invalid = []
            for index, group in enumerate(self.records[cid]):
                failures = []
                bounds = []
                for kind, ident, fingerprint in group:
                    if visited >= remaining:
                        failures.append({"node": [kind, ident], "reason": "traversal_limit"})
                        break
                    visited += 1
                    reason = ""
                    current = self.fingerprint((kind, ident))
                    if current is None:
                        reason = "source_missing"
                    elif current != fingerprint:
                        reason = "source_revision_changed"
                    elif not any(edge.relation == "depends_on" and edge.dst_type == "concept"
                                 and edge.dst_id == str(cid) and edge.polarity > 0
                                 for edge in self.store.edges_from(kind, ident)):
                        reason = "dependency_edge_removed"
                    elif kind == "concept":
                        base = self.store.get(int(ident))
                        review = self.store.support.review(int(ident))
                        if base.status != "active":
                            reason = "premise_" + base.status
                        elif applicability_state(
                            self.store.applicability.get(int(ident)), set(),
                        ) in {
                            "superseded", "outside_validity",
                        }:
                            reason = "premise_outside_validity"
                        elif review.get("outcome") in {"invalidated", "assessed_no_support"}:
                            reason = "premise_disproved"
                        else:
                            nested = claim(int(ident), path | {cid})
                            if not nested["viable"]:
                                reason = nested["reason"]
                            bounds.append(min(base.confidence, nested.get("confidence_bound", 1.0)))
                    else:
                        row = self._memory((kind, ident))
                        metadata = json.loads(row[3] or "{}")
                        if metadata.get("superseded_by") or metadata.get("superseded_reason"):
                            reason = "source_superseded"
                        elif metadata.get("belief_revision") in {"inaccurate", "superseded"}:
                            reason = "source_revised_" + metadata["belief_revision"]
                        elif float(row[4]) < 0.4:
                            reason = "source_low_confidence"
                        else:
                            message_ids = set(metadata.get("source_message_ids") or [])
                            if row[5] is not None:
                                message_ids.add(row[5])
                            if len(message_ids) > MAX_NODES:
                                reason = "source_message_limit"
                            elif message_ids:
                                marks = ",".join("?" for _ in message_ids)
                                found = self.db._get_conn().execute(
                                    f"SELECT COUNT(*) FROM messages WHERE id IN ({marks})",
                                    tuple(message_ids),
                                ).fetchone()[0]
                                if found != len(message_ids):
                                    reason = "source_message_deleted"
                    if reason:
                        failures.append({"node": [kind, ident], "reason": reason})
                        # One failed conjunct is enough. Preserve the remaining
                        # traversal budget for independent alternative sets.
                        break
                if failures:
                    invalid.append({"set": index, "failures": failures})
                else:
                    valid.append({"set": index, "confidence_bound": min(bounds, default=1.0)})
            return {"tracked": True, "viable": bool(valid),
                    "reason": "remaining_support" if valid else "no_valid_support_set",
                    "valid_sets": [item["set"] for item in valid], "invalid_sets": invalid,
                    "valid_source_count": len({(member[0], member[1])
                        for item in valid for member in self.records[cid][item["set"]]}),
                    "confidence_bound": max((item["confidence_bound"] for item in valid),
                                            default=0.0)}

        report = claim(int(concept_id), set())
        report["visited"] = visited
        report["support_sets"] = self.records.get(int(concept_id), [])
        return report

    def reconsider(self, concept_id):
        report = self.assess(concept_id)
        if report["tracked"]:
            key = REVIEW_PREFIX + str(concept_id)
            previous = self.db.kv_get(key)
            signature = revision_hash(report)
            old = json.loads(previous) if previous else {}
            if old.get("signature") != signature:
                self.db.kv_set(key, json.dumps({"signature": signature,
                    "at": timephrase.utcnow().isoformat(), "assessment": report}))
                for child in self.store.dependents_of(concept_id):
                    self.store.mark_lifecycle_due(child)
        return report

    def forget(self, concept_id):
        self.records.pop(int(concept_id), None)
        conn = self.db._get_conn()
        conn.execute("DELETE FROM kv_meta WHERE key IN (?, ?)",
                     (PREFIX + str(concept_id), REVIEW_PREFIX + str(concept_id)))
        conn.commit()

    def defer_untracked(self, concept_id):
        """A refused new admission cannot fall back to legacy assertion policy."""
        if int(concept_id) not in self.records:
            self._write(int(concept_id), [])

    def merge_plan(self, canonical_id, absorbed_id):
        """Reject cycles or increased depth before any merge mutation."""
        review = self.store.support.review(absorbed_id)
        absorbed_viable = self.store.is_assertable(absorbed_id) and review.get("outcome") not in {
            "invalidated", "assessed_no_support",
        }

        def member(cid, kind, ident, fingerprint):
            if kind != "concept" or ident != str(absorbed_id):
                return [kind, ident, fingerprint]
            edge_held = any(
                edge.relation == "depends_on" and edge.dst_type == "concept"
                and edge.dst_id == str(cid) and edge.polarity > 0
                for edge in self.store.edges_from(kind, ident)
            )
            valid = absorbed_viable and edge_held and fingerprint == self.fingerprint((kind, ident))
            return [kind, str(canonical_id), self.fingerprint((kind, str(canonical_id)))
                    if valid else "invalid:" + fingerprint]

        plan = {}
        for cid, groups in self.records.items():
            target = canonical_id if cid == absorbed_id else cid
            remapped = [
                [member(cid, kind, ident, fingerprint) for kind, ident, fingerprint in group]
                for group in groups
            ]
            plan[target] = self._minimal([*plan.get(target, []), *remapped])
        for cid in plan:
            before = max(self._depth(self.records, cid), self._depth(self.records, absorbed_id)
                         if cid == canonical_id else 0)
            after = self._depth(plan, cid)
            if after >= MAX_DEPTH or after > before or len(plan[cid]) > MAX_SETS:
                return None
        return plan

    def apply_merge(self, plan):
        for cid, groups in plan.items():
            if groups != self.records.get(cid):
                self._write(cid, groups)
