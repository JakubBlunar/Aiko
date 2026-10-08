"""Bounded, source-referenced reconsideration for concept synthesis."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import fields, is_dataclass, replace
from datetime import timedelta
from inspect import signature
from typing import Any

import numpy as np

from app.core.concepts.proposers.base import (
    CandidateProposal,
    ExistingConcept,
    FocusCluster,
    NarrativeCandidate,
    ProposerContext,
    ProposerSpec,
    TensionBase,
)
from app.core.concepts.ritual_grouping import MomentLite, RitualGroup, moment_from_memory
from app.core.concepts.surfacing_conduct import ConductFinding, load_conduct_snapshot
from app.core.concepts.concept_support import memory_revision, revision_hash
from app.core.infra import timephrase

ROUTING_KEY = "concept_synth.reconsideration"
DIAGNOSTICS_KEY = "concept_synth.routing_diagnostics"
QUEUE_CAP = 128
QUEUE_BYTES = 262144
MAX_ATTEMPTS = 3
MAX_RETRIES_PER_RUN = 2
TTL_DAYS = 7
RETRY_HOURS = 6
DISTINCTION_COS = 0.78

ROUTING_RULE = """Evidence routing (in addition to the kind-specific rules):
- Matching a topic is not matching a claim. For each reinforcement include
  evidence_relation: "same", "related", or "uncertain". Use "same" only when
  the cited evidence supports the existing claim, not merely its topic.
- Evaluate support AND discovery independently. A source may both reinforce
  a known claim and support a genuinely different new claim; emit both items.
- A full concept is still a valid support target. Never invent a replacement
  or paraphrase because its evidence room is zero.
- For every NEW claim near an existing one include compared_to: its shown id,
  and distinction: the specific falsifiable assertion the old claim does NOT
  make. More emphatic wording, extra adjectives, and evidence capacity are not
  distinctions. Do not assert a stronger claim than the evidence supports.
- Preserve all existing source-count, deliberate-anchor, recurrence, temporal,
  closed-arc, directional and meta-depth requirements. If no distinct claim is
  justified, return no new claim rather than manufacturing novelty.
"""


class SynthesisCallFailed(RuntimeError):
    """A proposer did not produce a usable answer; its inputs remain pending."""


def _encode(value: Any) -> Any:
    if isinstance(value, ExistingConcept):
        return {"$existing": int(value.id), "reinforce": value.reinforce}
    if isinstance(value, TensionBase):
        return {"$concept": int(value.id)}
    if isinstance(value, FocusCluster):
        return {"$cluster": int(value.rep), "size": int(value.size), "label": value.label}
    if isinstance(value, MomentLite):
        return {"$moment": int(value.id)}
    if isinstance(value, ConductFinding):
        return {
            "$conduct": value.key,
            "shape": value.shape,
            "nodes": [[kind, str(ident)] for kind, ident in value.evidence],
        }
    if hasattr(value, "id") and hasattr(value, "content") and hasattr(value, "kind"):
        return {"$memory": int(value.id)}
    if is_dataclass(value):
        if not isinstance(value, (NarrativeCandidate, RitualGroup)):
            raise ValueError("unsupported synthesis input")
        return {
            "$type": type(value).__name__,
            "fields": {field.name: _encode(getattr(value, field.name)) for field in fields(value)},
        }
    if isinstance(value, dict):
        return {
            "$dict": [
                [_encode(key), {"$coactivation": True} if key == "coactivation" else _encode(item)]
                for key, item in value.items()
            ]
        }
    if isinstance(value, tuple):
        return {"$tuple": [_encode(item) for item in value]}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("unsupported synthesis input")


def manifest_nodes(value: Any) -> set[tuple[str, str]]:
    if isinstance(value, list):
        return set().union(*(manifest_nodes(item) for item in value))
    if not isinstance(value, dict):
        return set()
    if "$conduct" in value:
        return {tuple(node) for node in value["nodes"]}
    for marker, kind in (
        ("$memory", "memory"),
        ("$moment", "memory"),
        ("$cluster", "cluster"),
        ("$concept", "concept"),
    ):
        if marker in value:
            return {(kind, str(value[marker]))}
    return set().union(*(manifest_nodes(item) for item in value.values()))


def _match_output(proposal: CandidateProposal, outputs: list[dict[str, Any]]) -> dict[str, Any]:
    matches = []
    for output in outputs:
        if not isinstance(output, dict):
            continue
        if proposal.reinforces_id is not None:
            try:
                match = int(output.get("reinforces_id")) == proposal.reinforces_id
            except (TypeError, ValueError):
                match = False
        else:
            match = str(output.get("label") or "").strip() == proposal.label
        if match and str(output.get("rationale") or "").strip() == proposal.rationale:
            matches.append(output)
    return matches[0] if len(matches) == 1 else {}


class SynthesisRoutingMixin:
    def _support_revisions(self, evidence):
        result = {}
        clusters = None
        for node in set(evidence):
            kind, ident = node
            cached = self._support_revision_cache.get(node)
            if cached is not None and kind != "concept":
                result[node] = cached
                continue
            if kind in {"memory", "cluster", "moment"}:
                memory = self._memory_store.get(int(ident))
                revision = memory_revision(memory) if memory is not None else "ref"
                if kind == "cluster":
                    if clusters is None:
                        clusters = {
                            int(cluster.representative_id): cluster
                            for cluster in self._topic_graph.topic_clusters()
                        }
                    cluster = clusters.get(int(ident))
                    if cluster is not None:
                        members = sorted({int(member) for member in cluster.member_ids})
                        rows = self._memory_store.get_many(members)
                        revision = revision_hash(
                            [
                                revision,
                                int(cluster.size),
                                [
                                    (
                                        member,
                                        memory_revision(rows[member]) if member in rows else "ref",
                                    )
                                    for member in members
                                ],
                            ]
                        )
                result[node] = revision
            elif kind == "concept":
                concept = self._concept_store.get(int(ident))
                result[node] = (
                    revision_hash(
                        [
                            concept.last_reinforced_at,
                            concept.distinct_source_count,
                        ]
                    )
                    if concept is not None
                    else "ref"
                )
            else:
                result[node] = "ref"
            if kind != "concept":
                self._support_revision_cache[node] = result[node]
        return result

    def _observe_support(self, concept, nodes, held):
        revisions = self._support_revisions(nodes)
        fresh_nodes = [
            node
            for node, revision in revisions.items()
            if self._concept_store.support.observe(
                concept.concept_id,
                node,
                revision=revision,
                previously_held=node in held,
                manifest=self._support_manifest_cache.get(node),
            )
        ]
        fresh = len(fresh_nodes)
        if fresh:
            self._concept_store.support.record_inputs(concept.concept_id, fresh_nodes)
        self._routing_stats["fresh_support"] += fresh
        self._routing_stats["historical_recitations"] += len(revisions) - fresh
        if concept.status != "active":
            self._concept_store.support.set_state(
                concept.concept_id,
                "supported_pending",
                "lifecycle_due" if fresh else "fresh_observation_required",
            )
        return bool(fresh)

    def _select_existing(self, rows, query_vectors, *, scope, limit=40):
        archived = {"dormant", "retired", "contradicted"}
        best = {}
        for dimension in {int(vector.size) for vector in query_vectors}:
            queries = [
                vector
                for vector in query_vectors
                if vector.size == dimension
                and np.isfinite(vector).all()
                and float(np.linalg.norm(vector)) > 0
            ]
            embedded = []
            for concept in rows:
                vector = np.asarray(concept.embedding, dtype=np.float32).ravel()
                if vector.size != dimension or not np.isfinite(vector).all():
                    continue
                norm = float(np.linalg.norm(vector))
                if norm > 0:
                    embedded.append((concept, vector / norm))
            if not queries or not embedded:
                continue
            matrix = np.vstack([vector for _concept, vector in embedded])
            scores = np.full(len(embedded), -1.0)
            for query in queries:
                scores = np.maximum(scores, matrix @ (query / np.linalg.norm(query)))
            best.update(
                {
                    concept.concept_id: float(score)
                    for (concept, _vector), score in zip(embedded, scores, strict=True)
                }
            )
        if best:
            ranked = sorted(
                (concept for concept in rows if concept.concept_id in best),
                key=lambda concept: (-best[concept.concept_id], concept.concept_id),
            )
            reserve = [
                concept
                for concept in ranked
                if concept.status in archived
                and best[concept.concept_id] >= self._admission_cosine()
            ][:8]
            selected = {concept.concept_id for concept in reserve}
            result = (
                reserve
                + [concept for concept in ranked if concept.concept_id not in selected][
                    : limit - len(reserve)
                ]
            )
        else:
            key = "concept_synth.comparison_cursor:" + scope
            try:
                cursors = json.loads(self._kv_get(key) or "{}")
                if not isinstance(cursors, dict):
                    cursors = {}
            except (TypeError, ValueError):
                cursors = {}
            pools = {
                "archive": sorted(
                    (concept for concept in rows if concept.status in archived),
                    key=lambda concept: concept.concept_id,
                ),
                "working": sorted(
                    (concept for concept in rows if concept.status not in archived),
                    key=lambda concept: concept.concept_id,
                ),
            }
            for name, pool in pools.items():
                offset = int(cursors.get(name, 0)) % len(pool) if pool else 0
                pools[name] = pool[offset:] + pool[:offset]
            reserve = pools["archive"][:8]
            working = pools["working"][: limit - len(reserve)]
            reserve += pools["archive"][len(reserve) : limit - len(working)]
            result = reserve + working
            for name, used in (("archive", len(reserve)), ("working", len(working))):
                cursors[name] = (int(cursors.get(name, 0)) + used) % max(1, len(pools[name]))
            self._kv_set(key, json.dumps(cursors))
        if hasattr(self, "_routing_stats"):
            self._routing_stats["comparison_eligible"] += len(rows)
            self._routing_stats["comparison_offered"] += len(result)
            self._routing_stats["comparison_not_assessed"] += max(0, len(rows) - len(result))
            self._routing_stats["archive_offered"] += sum(
                concept.status in archived for concept in result
            )
        return result

    def _routing_begin(self, stats: dict[str, Any]) -> None:
        raw = self._kv_get(ROUTING_KEY)
        try:
            rows = json.loads(raw) if raw else []
        except (TypeError, ValueError):
            rows = []
        self._reconsiderations = (
            {str(row["key"]): row for row in rows if isinstance(row, dict) and "key" in row}
            if isinstance(rows, list)
            else {}
        )
        self._routing_stats = Counter()
        self._support_revision_cache = {}
        self._support_manifest_cache = {}
        self._routing_sources: dict[str, set[tuple[str, str]]] = {}
        self._retrying = False
        self._reconsider_due = []
        now = timephrase.utcnow()
        for key, row in list(self._reconsiderations.items()):
            expires = timephrase.parse_iso(str(row.get("expires_at", "")))
            if expires is None or expires <= now:
                del self._reconsiderations[key]
                self._routing_stats["expired"] += 1
                continue
            next_at = timephrase.parse_iso(str(row.get("next_at", "")))
            if row.get("state") == "pending" and next_at is not None and next_at <= now:
                self._reconsider_due.append(key)
        self._reconsider_due.sort(
            key=lambda key: (
                self._reconsiderations[key]["next_at"],
                self._reconsiderations[key]["first_at"],
                key,
            )
        )
        stats["routing"] = {}

    def _capture_proposer(self, spec: ProposerSpec) -> ProposerSpec:
        def propose(ctx: ProposerContext, **kwargs: Any) -> list[CandidateProposal]:
            outputs: list[dict[str, Any]] = []
            self._synthesis_call_failed = False

            def call(system: str, user: str) -> list[dict[str, Any]]:
                raw = ctx.call_llm(system, user)
                outputs.extend(raw)
                return raw

            try:
                manifest = _encode(kwargs)
                manifest["$proposer"] = spec.sig_key
                if len(json.dumps(manifest)) > 32768:
                    manifest = None
                    self._routing_stats["manifest_oversize"] += 1
            except ValueError:
                manifest = None
                self._routing_stats["manifest_unsupported"] += 1
            if manifest is not None:
                self._support_revisions(manifest_nodes(manifest))
                from app.core.concepts.concept_evidence_lineage import capture_source_manifest

                for node in manifest_nodes(manifest):
                    if node[0] == "concept" or node not in self._support_manifest_cache:
                        self._support_manifest_cache[node] = capture_source_manifest(
                            self._concept_store, self._memory_store, self._topic_graph, node,
                        )
            proposals = spec.propose(replace(ctx, call_llm=call), **kwargs)
            for proposal in proposals:
                output = _match_output(proposal, outputs)
                relation = str(output.get("evidence_relation") or "unspecified")
                proposal.evidence_relation = (
                    relation
                    if relation
                    in {
                        "same",
                        "related",
                        "uncertain",
                        "unspecified",
                    }
                    else "uncertain"
                )
                proposal.distinction = str(output.get("distinction") or "").strip()
                try:
                    proposal.compared_to = int(output["compared_to"])
                except (KeyError, TypeError, ValueError):
                    proposal.compared_to = None
                proposal.input_manifest = manifest
                self._routing_stats["proposals"] += 1
                if proposal.reinforces_id is not None and relation == "unspecified":
                    self._routing_stats["relation_unspecified"] += 1
            claimed = {node for proposal in proposals for node in proposal.evidence}
            if manifest is not None:
                self._track_sources("offered", manifest_nodes(manifest))
            if manifest is not None and not self._retrying:
                unclaimed = manifest_nodes(manifest) - claimed
                if unclaimed:
                    self._queue_reconsideration(
                        spec.subject,
                        spec.kind,
                        manifest,
                        sorted(unclaimed),
                        reason="unclaimed",
                        targets=[],
                    )
            if self._synthesis_call_failed:
                raise SynthesisCallFailed("no usable concept synthesis answer")
            return proposals

        return replace(spec, propose=propose)

    def _queue_reconsideration(
        self,
        subject: str,
        kind: str,
        manifest: Any,
        nodes: list[tuple[str, str]],
        *,
        reason: str,
        targets: list[int],
    ) -> None:
        if self._retrying or manifest is None or not nodes:
            return
        identity = [subject, kind, sorted(set(nodes)), sorted(set(targets))]
        if isinstance(manifest, dict) and "$proposer" in manifest:
            identity.append(manifest["$proposer"])
        key = hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()[:24]
        if key in self._reconsiderations:
            if self._reconsiderations[key]["state"] == "pending":
                refreshed = dict(self._reconsiderations[key], manifest=manifest)
                if not self._retain_reconsideration(refreshed):
                    self._routing_stats["manifest_refresh_deferred"] += 1
            self._routing_stats["queue_repeats"] += 1
            return
        now = timephrase.utcnow()
        row = {
            "key": key,
            "subject": subject,
            "kind": kind,
            "nodes": sorted(set(nodes)),
            "targets": sorted(set(targets)),
            "reason": reason,
            "manifest": manifest,
            "state": "pending",
            "assessment_state": "not_assessed",
            "attempts": 0,
            "first_at": now.isoformat(),
            "next_at": (now + timedelta(hours=RETRY_HOURS)).isoformat(),
            "expires_at": (now + timedelta(days=TTL_DAYS)).isoformat(),
        }
        if self._retain_reconsideration(row):
            self._routing_stats["queued"] += 1
        else:
            self._routing_stats["capacity_declined"] += 1

    def _retain_reconsideration(self, row: dict[str, Any]) -> bool:
        retained = dict(self._reconsiderations)
        retained[row["key"]] = row
        evicted = 0
        while len(retained) > QUEUE_CAP or len(json.dumps(list(retained.values()))) > QUEUE_BYTES:
            terminal = next(
                (key for key, entry in retained.items()
                 if key != row["key"] and entry["state"] != "pending"),
                None,
            )
            if terminal is None:
                return False
            del retained[terminal]
            evicted += 1
        self._reconsiderations = retained
        self._routing_stats["evicted"] += evicted
        return True

    def _defer_proposal(self, proposal: CandidateProposal, reason: str) -> None:
        self._routing_stats[reason] += 1
        self._queue_reconsideration(
            proposal.subject,
            proposal.kind,
            proposal.input_manifest,
            proposal.evidence,
            reason=reason,
            targets=[proposal.reinforces_id] if proposal.reinforces_id is not None else [],
        )

    def _track_sources(self, name: str, nodes: Any) -> None:
        self._routing_sources.setdefault(name, set()).update(nodes)

    def _decode_input(self, value: Any) -> Any:
        if isinstance(value, list):
            return [decoded for item in value if (decoded := self._decode_input(item)) is not None]
        if not isinstance(value, dict):
            return value
        if "$tuple" in value:
            return tuple(self._decode_input(item) for item in value["$tuple"])
        if "$coactivation" in value:
            return self._coactivation_modes()
        if "$conduct" in value:
            current = next(
                (
                    row
                    for row in load_conduct_snapshot(self._kv_get)
                    if row.get("key") == value["$conduct"] and row.get("shape") == value["shape"]
                ),
                None,
            )
            if current is None:
                raise ValueError("conduct finding no longer current")
            return ConductFinding(
                shape=current["shape"],
                key=current["key"],
                summary=current["summary"],
                evidence=tuple((str(kind), int(ident)) for kind, ident in current["evidence"]),
                score=float(current["score"]),
            )
        if "$dict" in value:
            return {
                self._decode_input(key): self._decode_input(item) for key, item in value["$dict"]
            }
        if "$memory" in value or "$moment" in value:
            row = self._memory_store.get(int(value.get("$memory", value.get("$moment"))))
            if row is None:
                raise ValueError("evidence source vanished")
            if "$memory" in value:
                return row
            moment = moment_from_memory(row)
            if moment is None:
                raise ValueError("evidence is no longer a shared moment")
            when = timephrase.parse_iso(moment.when)
            return MomentLite(
                id=moment.id,
                text=moment.text,
                vibe=moment.vibe,
                weekday=when.strftime("%A") if when is not None else None,
            )
        if "$cluster" in value:
            rep = int(value["$cluster"])
            if self._memory_store.get(rep) is None:
                raise ValueError("cluster representative vanished")
            return FocusCluster(
                rep=rep,
                label=value["label"],
                size=int(value["size"]),
                representative=self._memory_content(rep),
                digest=self._digest_for_rep(rep),
            )
        if "$concept" in value or "$existing" in value:
            ident = int(value.get("$concept", value.get("$existing")))
            concept = self._concept_store.get(ident)
            if concept is None:
                if "$existing" in value:
                    return None
                raise ValueError("concept source vanished")
            if "$existing" in value:
                return replace(self._existing_row(concept), reinforce=value.get("reinforce", True))
            if concept.status != "active":
                raise ValueError("concept source no longer active")
            return TensionBase(
                id=concept.concept_id,
                subject=concept.subject,
                kind=concept.kind,
                label=concept.label,
                rationale=concept.rationale,
                confidence=concept.confidence,
                hint=self._activity_hint(concept, None),
            )
        if value.get("$type") in {"NarrativeCandidate", "RitualGroup"}:
            cls = NarrativeCandidate if value["$type"] == "NarrativeCandidate" else RitualGroup
            return cls(**{key: self._decode_input(item) for key, item in value["fields"].items()})
        raise ValueError("invalid synthesis manifest")

    def _retry_enabled(self, spec: ProposerSpec, kwargs: dict[str, Any]) -> bool:
        flags = {
            "affect": "affective_synthesis_enabled",
            "taste": "taste_synthesis_enabled",
            "pursuit": "pursuit_synthesis_enabled",
            "conduct": "surfacing_conduct_enabled",
            "shared_moments": "ritual_synthesis_enabled",
            "shared_arc": "shared_arc_synthesis_enabled",
            "narrative": "narrative_synthesis_enabled",
            "aspiration": "aspiration_synthesis_enabled",
            "boundary": "boundary_synthesis_enabled",
            "comm_style": "communication_style_synthesis_enabled",
            "tension": "tension_synthesis_enabled",
            "generalization": "generalization_synthesis_enabled",
            "self_correction": "concept_self_correction_enabled",
        }
        flag = flags.get(spec.population, "")
        if spec.population == "shared_moments":
            flag = self._SHARED_MOMENT_PASSES.get(
                spec.kind,
                ("ritual_synthesis_enabled",),
            )[0]
        enabled = bool(getattr(self._agent_settings, flag, True))
        if kwargs.get("stacking"):
            enabled = enabled and bool(
                getattr(
                    self._agent_settings,
                    "generalization_stacking_enabled",
                    True,
                )
            )
        return enabled

    def _run_reconsiderations(
        self,
        ctx: ProposerContext,
        specs: Any,
        stats: dict[str, Any],
    ) -> None:
        by_kind: dict[tuple[str, str], list[ProposerSpec]] = {}
        for spec in specs:
            by_kind.setdefault((spec.subject, spec.kind), []).append(spec)
        calls = 0
        for key in self._reconsider_due:
            if calls >= MAX_RETRIES_PER_RUN or self._cancel_event.is_set():
                break
            row = self._reconsiderations[key]
            try:
                kwargs = self._decode_input(row["manifest"])
            except (TypeError, ValueError, KeyError):
                row["state"] = "source_unavailable"
                self._routing_stats["source_unavailable"] += 1
                continue
            proposer_key = row["manifest"].get("$proposer")
            compatible = []
            for candidate in by_kind.get((row["subject"], row["kind"]), []):
                if proposer_key is not None and candidate.sig_key != proposer_key:
                    continue
                try:
                    signature(candidate.propose).bind(ctx, **kwargs)
                except (TypeError, ValueError):
                    continue
                compatible.append(candidate)
            if len(compatible) != 1:
                row["state"] = "unavailable"
                row["assessment_state"] = "not_assessed"
                self._routing_stats["proposer_unavailable"] += 1
                continue
            spec = compatible[0]
            if not self._retry_enabled(spec, kwargs):
                continue
            old = kwargs.get("existing", [])
            if spec.population == "generalization":
                fresh = self._existing_generalizations(
                    spec,
                    depth=2 if kwargs.get("stacking") else 1,
                    concepts=kwargs.get("concepts", ()),
                )
                old = []
            else:
                fresh = self._existing_for(
                    spec,
                    focus=kwargs.get("focus_clusters", kwargs.get("candidates", [])),
                    memories=kwargs.get("memories", []),
                )
            known = {item.id: item for item in old}
            known.update({item.id: item for item in fresh})
            known.update({item.id: item for item in old if not item.reinforce})
            for target in row.get("targets", []):
                concept = self._concept_store.get(int(target))
                if (
                    concept is not None
                    and (concept.concept_id not in known or known[concept.concept_id].reinforce)
                    and (spec.population != "generalization" or concept.concept_id in known)
                ):
                    known[concept.concept_id] = self._existing_row(concept)
            pinned = {int(target) for target in row.get("targets", [])}
            refreshed = {item.id for item in fresh}
            kwargs["existing"] = sorted(
                known.values(),
                    key=lambda item, pinned=pinned, refreshed=refreshed: (
                        item.id not in pinned, item.id not in refreshed
                    ),
            )[:40]
            known = {item.id: item for item in kwargs["existing"]}
            if spec.population == "comm_style":
                kwargs["style_digest"] = self._build_style_digest(spec.subject)
            nodes = {tuple(node) for node in row["nodes"]}

            def retry_call(system: str, user: str, *, focus_nodes=nodes) -> list[dict[str, Any]]:
                focus = ", ".join(f"{kind}:{ident}" for kind, ident in sorted(focus_nodes))
                return ctx.call_llm(
                    system + "\n\nRECONSIDERATION: distinguish repetition from a new claim. "
                    "All NEW items must include compared_to and a concrete distinction "
                    "when a known claim is related. Return [] if nothing new is justified.",
                    user + "\n\nReconsider these sources specifically: " + focus,
                )

            self._retrying = True
            self._synthesis_call_failed = False
            try:
                proposals = self._capture_proposer(spec).propose(
                    replace(ctx, call_llm=retry_call),
                    **kwargs,
                )
            except SynthesisCallFailed:
                proposals = []
                self._synthesis_call_failed = True
            finally:
                self._retrying = False
            if self._cancel_event.is_set():
                break
            calls += 1
            row["attempts"] += 1
            self._routing_stats["reconsidered"] += 1
            supported = False
            created = False
            unresolved = self._synthesis_call_failed
            for proposal in proposals:
                if not nodes.intersection(proposal.evidence):
                    self._routing_stats["retry_outside_focus"] += 1
                    continue
                if proposal.reinforces_id is not None:
                    if proposal.evidence_relation != "same":
                        unresolved = True
                        continue
                elif known and (
                    len(proposal.distinction) < 12 or proposal.compared_to not in known
                ):
                    unresolved = True
                    continue
                before = int(stats.get("added", 0))
                reinforced_before = int(stats.get("reinforced", 0))
                deferred_before = self._routing_stats["missing_distinction"]
                self._retrying = True
                try:
                    self._persist(proposal, stats)
                finally:
                    self._retrying = False
                created = created or int(stats.get("added", 0)) > before
                supported = supported or int(stats.get("reinforced", 0)) > reinforced_before
                unresolved = (
                    unresolved or self._routing_stats["missing_distinction"] > deferred_before
                )
            if created:
                row["state"] = "new_claim"
            elif unresolved:
                row["state"] = "pending" if row["attempts"] < MAX_ATTEMPTS else "exhausted"
            else:
                row["state"] = "same_claim" if supported else "no_claim"
            row["assessment_state"] = "supported_pending" if supported else "not_assessed"
            row["next_at"] = (timephrase.utcnow() + timedelta(hours=RETRY_HOURS)).isoformat()
            self._routing_stats[row["state"]] += 1

    def _routing_finish(self, stats: dict[str, Any]) -> None:
        rows = list(self._reconsiderations.values())
        while len(rows) > QUEUE_CAP or len(json.dumps(rows)) > QUEUE_BYTES:
            terminal = next(
                (index for index, row in enumerate(rows) if row["state"] != "pending"),
                len(rows) - 1,
            )
            rows.pop(terminal)
            self._routing_stats["evicted"] += 1
        self._kv_set(ROUTING_KEY, json.dumps(rows))
        for name, nodes in self._routing_sources.items():
            self._routing_stats[f"unique_{name}"] = len(nodes)
        self._routing_stats["repeat_source_attempts"] = self._routing_stats[
            "evidence_attempts"
        ] - len(self._routing_sources.get("attempted", set()))
        self._routing_stats["pending"] = sum(row["state"] == "pending" for row in rows)
        now = timephrase.utcnow()
        self._routing_stats["due"] = sum(
            row["state"] == "pending"
            and (timephrase.parse_iso(row["next_at"]) or now) <= now
            for row in rows
        )
        stats["routing"] = dict(self._routing_stats)
        try:
            raw = self._kv_get(DIAGNOSTICS_KEY)
            cumulative = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            cumulative = {}
        if not isinstance(cumulative, dict):
            cumulative = {}
        for name, count in self._routing_stats.items():
            if name not in {"pending", "due"} and not name.startswith("unique_"):
                cumulative[name] = int(cumulative.get(name, 0)) + count
        self._kv_set(DIAGNOSTICS_KEY, json.dumps(cumulative))

    def synthesis_diagnostics(self) -> dict[str, Any]:
        def read(key: str, fallback: Any) -> Any:
            try:
                return json.loads(self._kv_get(key) or json.dumps(fallback))
            except (TypeError, ValueError):
                return fallback

        rows = read(ROUTING_KEY, [])
        return {
            "last_run": dict((self._last_stats or {}).get("routing", {})),
            "cumulative": read(DIAGNOSTICS_KEY, {}),
            "queue_states": dict(Counter(row.get("state", "invalid") for row in rows)),
            "revival": self._concept_store.support.diagnostics(),
            "assessment_states": dict(
                Counter(row.get("assessment_state", "not_assessed") for row in rows)
            ),
            "limits": {
                "entries": QUEUE_CAP,
                "bytes": QUEUE_BYTES,
                "attempts": MAX_ATTEMPTS,
                "calls_per_run": MAX_RETRIES_PER_RUN,
                "retry_hours": RETRY_HOURS,
                "ttl_days": TTL_DAYS,
            },
        }
