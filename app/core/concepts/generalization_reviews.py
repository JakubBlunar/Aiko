"""L46 review batches. A receipt belongs to a proposer level, never to lifecycle."""
from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from app.core.concepts.concept_applicability import applicability_state, scope_hint
from app.core.concepts.concept_evidence_lineage import capture_source_manifest
from app.core.concepts.concept_meta_depth import (
    MAX_GENERALIZATION_DEPTH, generalization_target_depth,
)
from app.core.concepts.concept_support import revision_hash
from app.core.concepts.concept_synthesis_routing import SynthesisCallFailed
from app.core.concepts.proposers.base import snippet
from app.core.infra import timephrase

log = logging.getLogger("app.concept_synthesis_worker")
POLICY_VERSION = 1


def review_key(subject: str, depth: int) -> str:
    suffix = "" if depth == 1 else f"_l{depth}"
    return f"concept_synth.generalization{suffix}_sig.{subject}"


def _latest(batches):
    return {cid: batch for batch in batches for cid in batch["premises"]}


def last_premise_reviews(
    kv_get: Callable[[str], str | None], concept_id: int, subject: str | None = None,
) -> list[dict]:
    """Most recent ordinary and focused retry batches at each level, if observed."""
    if subject is None:
        return [review for name in ("user", "aiko")
                for review in last_premise_reviews(kv_get, concept_id, name)]
    result = []
    for depth in range(1, MAX_GENERALIZATION_DEPTH + 1):
        raw = kv_get(review_key(subject, depth))
        try:
            state = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            continue
        if not isinstance(state, dict):
            continue
        for lane in ("reviews", "reconsiderations"):
            for batch in reversed(state.get(lane, [])):
                if str(concept_id) in batch["premises"]:
                    result.append({**batch, "target_depth": depth, "lane": lane})
                    break
    return result


def _record_batch(state, lane, revisions, outcome):
    batch = {"completed_at": timephrase.utcnow().isoformat(), "premises": revisions,
             "policy_version": POLICY_VERSION, "outcome": outcome}
    # Keep complete partner lists, including when a partner was reviewed again
    # elsewhere. Drop a batch only after every member has a newer receipt.
    retained = []
    seen = set(revisions)
    for old in reversed(state.get(lane, [])):
        if seen.issuperset(old["premises"]):
            continue
        retained.append(old)
        seen.update(old["premises"])
    state[lane] = [*reversed(retained), batch]


@dataclass
class _ReviewSnapshot:
    """Shared ancestor reads within one selection or one post-generation recheck."""

    depths: dict = field(default_factory=dict)
    assessments: dict = field(default_factory=dict)
    roots: dict = field(default_factory=dict)

    def assess(self, store, cid):
        if cid not in self.assessments:
            self.assessments[cid] = store.dependencies.assess(cid)
        return self.assessments[cid]


class GeneralizationReviewMixin:
    def _review_premise(self, concept, subject, depth, snapshot, now):
        """Fingerprint the actual claim, applicable scope and surviving alternatives."""
        if concept is None or concept.status != "active" or concept.subject != subject:
            return None
        if (depth > 1) != (concept.evidence_model == "meta"):
            return None
        level = generalization_target_depth(self._concept_store, concept, memo=snapshot.depths)
        if level != depth:
            return None
        cid = concept.concept_id
        store = self._concept_store
        assessment = snapshot.assess(store, cid)
        scope = store.applicability.get(cid)
        if store.support.review(cid).get("outcome") in {"invalidated", "assessed_no_support"}:
            return None
        if not assessment["viable"] or applicability_state(scope, set()) in {
            "superseded", "outside_validity",
        }:
            return None
        groups = [assessment["support_sets"][i] for i in assessment.get("valid_sets", ())]
        base = self._as_tension_base(concept, now)
        hints = [scope_hint(scope)] if scope else []
        if depth > 1:
            # Only traverse known viable alternatives. A legacy premise can
            # participate at depth 1 without certifying an original observation.
            if groups:
                # Show a sample from one surviving alternative. Members of
                # different alternatives do not form a new sufficient set.
                members = groups[0]
                labels = []
                for kind, ident, _ in members[:3]:
                    if kind == "concept" and (child := store.get(int(ident))) is not None:
                        detail = snippet(child.label)
                        if child_scope := store.applicability.get(child.concept_id):
                            detail += " (" + scope_hint(child_scope) + ")"
                        labels.append(detail)
                if labels:
                    hints.append("supporting patterns (sample): " + " / ".join(labels))
            lineage = self._review_roots(groups, snapshot)
            hints.append("recorded source references (sample, overlap may be unknown): "
                         + (", ".join(lineage) or "unknown"))
        revision = revision_hash([POLICY_VERSION, subject, concept.kind, base.label,
                                  base.rationale, round(base.confidence, 1), base.hint,
                                  scope, groups, hints])
        return replace(base, hint=" | ".join(filter(None, (base.hint, *hints)))), revision

    def _review_roots(self, groups, snapshot):
        """Follow a bounded sample of viable conjunctions to known source IDs."""
        pending = deque(member[:2] for group in groups for member in group)
        seen = set()
        roots = set()
        while pending and len(seen) < 24 and len(roots) < 8:
            kind, ident = pending.popleft()
            if (kind, ident) in seen:
                continue
            seen.add((kind, ident))
            if kind == "concept":
                nested = snapshot.assess(self._concept_store, int(ident))
                for index in nested.get("valid_sets", ()):
                    pending.extend(member[:2] for member in nested["support_sets"][index])
            else:
                key = (kind, ident)
                if key not in snapshot.roots:
                    manifest = capture_source_manifest(
                        self._concept_store, self._memory_store, self._topic_graph, key,
                    )
                    snapshot.roots[key] = {revision_hash(root)[:12]
                                          for source in manifest["sources"]
                                          for root in source["roots"]}
                roots.update(snapshot.roots[key])
        return sorted(roots)[:8]

    def _record_reconsidered_premises(self, subject, depth, revisions, outcome):
        key = review_key(subject, depth)
        state = self._load_sigs(key)
        _record_batch(state, "reconsiderations", revisions, outcome)
        self._kv_set(key, json.dumps(state))

    def _review_inputs(self, ids, subject, depth):
        snapshot = _ReviewSnapshot()
        now = timephrase.utcnow()
        result = {}
        for cid in ids:
            item = self._review_premise(self._concept_store.get(cid), subject, depth, snapshot, now)
            if item is None:
                return {}
            result[str(cid)] = item
        return result

    def _run_generalization_levels(self, ctx, spec, stats, force):
        if not getattr(self._agent_settings, "generalization_synthesis_enabled", True):
            return
        for depth in range(1, self._generalization_max_depth() + 1):
            if self._cancel_event.is_set():
                break
            if depth > 1 and not getattr(self._agent_settings, "generalization_stacking_enabled",
                                         True):
                break
            try:
                self._review_level(ctx, spec, depth, stats, force)
            except SynthesisCallFailed:
                log.info("generalization deferred (subject=%s depth=%d)", spec.subject, depth)
            except Exception:
                log.warning("generalization review failed (subject=%s depth=%d)",
                            spec.subject, depth, exc_info=True)

    def _select_review_batch(self, subject, depth, state, force):
        """Choose unreviewed revisions plus comparison partners within the prompt cap."""
        previous = _latest(state.get("reviews", []))
        snapshot = _ReviewSnapshot()
        now = timephrase.utcnow()
        inputs = {}
        rows = []
        for row in self._concept_store.all():
            item = self._review_premise(row, subject, depth, snapshot, now)
            if item is not None:
                inputs[str(row.concept_id)] = item
                rows.append(row)
        if len(rows) < 2:
            return
        dirty = {cid for cid, (_, rev) in inputs.items()
                 if previous.get(cid, {}).get("premises", {}).get(cid) != rev}
        population = revision_hash(sorted(inputs))
        pending = state.get("pending", {})
        if not (force or dirty or pending or state.get("population") != population):
            return
        # New and revised inputs get first choice. Keep familiar premises in
        # the pool for the last partial batch, oldest review first.
        rows.sort(key=lambda row: (str(row.concept_id) not in dirty,
                                   previous.get(str(row.concept_id), {}).get("completed_at", ""),
                                   -row.confidence, row.concept_id))
        cap = self._max_generalization_concepts if depth == 1 else self._max_l2_concepts
        unfinished = pending.get("premises", {})
        if 2 <= len(unfinished) <= cap and all(
            cid in inputs and inputs[cid][1] == rev for cid, rev in unfinished.items()
        ):
            picked = [self._concept_store.get(int(cid)) for cid in unfinished]
        else:
            # Reserve a familiar comparison when a large changed cohort fills
            # the prompt. At cap two, a pair of new premises still fits together.
            if cap > 2 and len(dirty) >= cap and len(rows) > len(dirty):
                rows.insert(cap - 1, rows.pop(len(dirty)))
            picked = self._pick_diverse_concepts(rows, cap)
        if len(picked) < 2:
            return
        ids = [row.concept_id for row in picked]
        pool = [inputs[str(cid)][0] for cid in ids]
        revisions = {str(cid): inputs[str(cid)][1] for cid in ids}
        remaining = len(dirty - revisions.keys()) if len(ids) == cap else 0
        return pool, revisions, population, remaining

    def _review_level(self, ctx, spec, depth, stats, force):
        key = review_key(spec.subject, depth)
        state = self._load_sigs(key)
        batch = self._select_review_batch(spec.subject, depth, state, force)
        if batch is None:
            return
        pool, revisions, population, remaining = batch
        ids = [base.id for base in pool]
        pending = state.get("pending", {})
        prefix = "generalization" if depth == 1 else f"generalization_l{depth}"
        stats[prefix + "_dirty"] = True
        if pending.get("premises") != revisions:
            pending = {"premises": revisions, "handled": {}}
        state["pending"] = pending
        self._kv_set(key, json.dumps(state))
        existing = self._existing_generalizations(spec, depth=depth, concepts=pool)
        proposals = spec.propose(ctx, concepts=pool, target_depth=depth, existing=existing)
        stats[prefix + "_proposed"] = stats.get(prefix + "_proposed", 0) + len(proposals)
        fresh = self._review_inputs(ids, spec.subject, depth)
        if {cid: item[1] for cid, item in fresh.items()} != revisions:
            return
        for proposal in proposals:
            if self._cancel_event.is_set():
                return
            identity = revision_hash([proposal.label, proposal.reinforces_id,
                                      sorted(proposal.evidence), proposal.rationale])
            if identity in pending["handled"]:
                continue
            before = (stats["added"], stats["reinforced"])
            self._persist(proposal, stats)
            self._save_routing_queue()
            pending["handled"][identity] = {
                "added": stats["added"] - before[0], "reinforced": stats["reinforced"] - before[1],
            }
            self._kv_set(key, json.dumps(state))
        if self._cancel_event.is_set():
            return
        if proposals:
            current = self._review_inputs(ids, spec.subject, depth)
            if revisions != {cid: item[1] for cid, item in current.items()}:
                return
        else:
            # A usable empty answer can still have queued unclaimed premises.
            self._save_routing_queue()
        handled = pending["handled"].values()
        outcome = {"proposals": len(pending["handled"]), "added": sum(r["added"] for r in handled),
                   "reinforced": sum(r["reinforced"] for r in handled)}
        _record_batch(state, "reviews", revisions, outcome)
        state.pop("pending")
        state.update(policy_version=POLICY_VERSION, target_depth=depth, population=population,
                     fingerprint=revision_hash(revisions), count=len(ids))
        self._kv_set(key, json.dumps(state))
        if remaining:
            stats["generalization_dirty_remaining"] = (
                stats.get("generalization_dirty_remaining", 0) + remaining
            )
