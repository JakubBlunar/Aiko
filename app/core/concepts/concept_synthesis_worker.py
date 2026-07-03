"""L2 concept synthesis worker (the incremental proposer).

An :class:`~app.core.proactive.idle_worker.IdleWorker` that mines
candidate identity concepts for two subjects -- the user (from topic
clusters) and Aiko herself (from her ``self`` / ``reflection`` /
``diary`` memories) -- and writes them as ``status="candidate"`` concepts
(promotion to ``active`` is L3's job; this worker never promotes).

**Incremental by design.** Aiko runs intermittently (off overnight), so
a weekly single pass would fire unpredictably and, when it did, process
the whole corpus in one long run. Instead this worker runs regularly
(default 30 min) and does a small bounded batch each time, using
``kv_meta`` signatures to only (re)process material that actually
changed:

- ``concept_synth.cluster_sigs`` -> ``{rep_id: {"size", "label"}}`` keyed
  by the stable representative-member id (survives topic-graph refits).
- ``concept_synth.aiko_sig`` -> ``{"count", "max_id"}`` over the aiko-self
  population.

Each run pulls full content only for up to ``max_clusters_per_run`` dirty
clusters (the rest of the map rides along as cheap labels for
cross-cluster reasoning) and caps the aiko batch at ``max_aiko_memories``.
Once caught up, ``run()`` is a fast no-op with zero LLM calls. Selection
is dispatched by ``ProposerSpec.population`` so the worker body stays
kind/subject-agnostic.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.core.concepts.proposers import (
    AIKO_SELF_KINDS,
    CONCEPT_PROPOSERS,
    CandidateProposal,
    ExistingConcept,
    FocusCluster,
    ProposerContext,
    ProposerSpec,
)
from app.core.concepts.concept_store import Concept, ConceptEdge
from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.concepts.concept_store import ConceptStore
    from app.core.conversation.topic_graph import TopicGraph
    from app.core.infra.agent_settings import AgentSettings
    from app.core.memory.memory_store import MemoryStore

log = logging.getLogger("app.concept_synthesis_worker")

_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)

# Cosine threshold above which a fresh proposal is treated as the same
# candidate as an existing concept (dedupe -> reinforce instead of add).
_DEDUPE_COS = 0.9
_MAX_TOKENS = 900
_TEMPERATURE = 0.6

_KV_CLUSTER_SIGS = "concept_synth.cluster_sigs"
_KV_AIKO_SIG = "concept_synth.aiko_sig"
_TOPIC_DIGEST_PREFIX = "aiko.topic_digest."


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConceptSynthesisWorker:
    """Idle worker that proposes candidate identity concepts."""

    name = "concept_synthesis"

    def __init__(
        self,
        *,
        concept_store: "ConceptStore",
        topic_graph: "TopicGraph",
        memory_store: "MemoryStore",
        embedder: Any,
        ollama: Any,
        chat_model: str,
        cancel_event: threading.Event,
        agent_settings: "AgentSettings",
        memory_settings: Any,
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        clock: Callable[[], datetime] | None = None,
        notify_concept_added: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._concept_store = concept_store
        self._topic_graph = topic_graph
        self._memory_store = memory_store
        self._embedder = embedder
        self._ollama = ollama
        self._chat_model = chat_model
        self._cancel_event = cancel_event
        self._agent_settings = agent_settings
        self._memory_settings = memory_settings
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._notify_concept_added = notify_concept_added
        self._llm_calls = 0

    # ── idle worker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(
                self._memory_settings,
                "concept_synthesis_interval_seconds",
                1800,
            )
        )

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None
    ) -> bool:
        if not bool(getattr(self._agent_settings, "concepts_enabled", False)):
            return False
        if not bool(
            getattr(self._agent_settings, "concept_synthesis_enabled", True)
        ):
            return False
        if not bool(getattr(self._topic_graph, "persistent", False)):
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at
        )

    # ── config knobs ──────────────────────────────────────────────────

    @property
    def _max_clusters_per_run(self) -> int:
        return max(
            1,
            int(
                getattr(
                    self._memory_settings,
                    "concept_synthesis_max_clusters_per_run",
                    5,
                )
            ),
        )

    @property
    def _max_aiko_memories(self) -> int:
        return max(
            1,
            int(
                getattr(
                    self._memory_settings,
                    "concept_synthesis_max_aiko_memories",
                    40,
                )
            ),
        )

    @property
    def _dirty_size_delta(self) -> int:
        return max(
            1,
            int(
                getattr(
                    self._memory_settings,
                    "concept_synthesis_dirty_size_delta",
                    3,
                )
            ),
        )

    # ── run ────────────────────────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        if not bool(getattr(self._agent_settings, "concepts_enabled", False)):
            return {"skipped": True, "reason": "disabled"}
        if self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}

        self._llm_calls = 0
        started = time.monotonic()
        ctx = ProposerContext(call_llm=self._call_llm)

        stats: dict[str, Any] = {
            "added": 0,
            "reinforced": 0,
            "by_subject": {},
            "user_dirty_total": 0,
            "user_processed": 0,
            "user_dirty_remaining": 0,
            "aiko_dirty": False,
        }

        for spec in CONCEPT_PROPOSERS:
            if self._cancel_event.is_set():
                break
            try:
                if spec.population == "clusters":
                    proposals = self._run_cluster_pass(ctx, spec, stats)
                elif spec.population == "aiko_memories":
                    proposals = self._run_aiko_pass(ctx, spec, stats)
                else:
                    proposals = []
            except Exception:
                log.warning(
                    "concept proposer failed (kind=%s subject=%s)",
                    spec.kind, spec.subject, exc_info=True,
                )
                continue
            for proposal in proposals:
                self._persist(proposal, stats)

        stats["llm_calls"] = self._llm_calls
        stats["llm_ms"] = int((time.monotonic() - started) * 1000)
        return stats

    # ── cluster (user) pass ────────────────────────────────────────────

    def _run_cluster_pass(
        self,
        ctx: ProposerContext,
        spec: ProposerSpec,
        stats: dict[str, Any],
    ) -> list[CandidateProposal]:
        clusters = self._user_dominant_clusters()
        if not clusters:
            return []
        cluster_index = [
            (rep, label, size) for rep, label, size, _kinds in clusters
        ]
        sigs = self._load_sigs(_KV_CLUSTER_SIGS)
        delta = self._dirty_size_delta

        dirty: list[tuple[int, str, int, int, bool]] = []  # rep,label,size,drift,is_new
        for rep, label, size, _kinds in clusters:
            prev = sigs.get(str(rep))
            if prev is None:
                dirty.append((rep, label, size, size, True))
                continue
            prev_size = int(prev.get("size", 0))
            prev_label = str(prev.get("label", ""))
            drift = abs(size - prev_size)
            if prev_label != label or drift >= delta:
                dirty.append((rep, label, size, drift, False))

        stats["user_dirty_total"] = len(dirty)
        if not dirty:
            stats["user_dirty_remaining"] = 0
            return []

        # Never-processed first, then largest drift.
        dirty.sort(key=lambda d: (0 if d[4] else 1, -d[3]))
        focus_rows = dirty[: self._max_clusters_per_run]
        stats["user_processed"] = len(focus_rows)
        stats["user_dirty_remaining"] = max(0, len(dirty) - len(focus_rows))

        focus_clusters = [
            FocusCluster(
                rep=rep,
                label=label,
                size=size,
                representative=self._memory_content(rep),
                digest=self._digest_for_rep(rep),
            )
            for rep, label, size, _drift, _new in focus_rows
        ]

        proposals = spec.propose(
            ctx,
            focus_clusters=focus_clusters,
            cluster_index=cluster_index,
            existing=self._existing_for(spec),
        )

        # Persist signatures: keep entries for current reps only (bounds
        # growth across refits) and mark processed focus reps as fresh.
        processed = {rep for rep, _l, _s, _d, _n in focus_rows}
        current = {rep: (label, size) for rep, label, size, _k in clusters}
        new_sigs: dict[str, dict[str, Any]] = {}
        for rep, (label, size) in current.items():
            if rep in processed:
                new_sigs[str(rep)] = {"size": size, "label": label}
            elif str(rep) in sigs:
                new_sigs[str(rep)] = sigs[str(rep)]
        self._save_sigs(_KV_CLUSTER_SIGS, new_sigs)
        return proposals

    # ── aiko pass ──────────────────────────────────────────────────────

    def _run_aiko_pass(
        self,
        ctx: ProposerContext,
        spec: ProposerSpec,
        stats: dict[str, Any],
    ) -> list[CandidateProposal]:
        pop = self._memory_store.iter_by_kinds(AIKO_SELF_KINDS)
        count = len(pop)
        if count == 0:
            return []
        max_id = max(int(m.id) for m in pop)
        prev = self._load_sigs(_KV_AIKO_SIG)
        delta = self._dirty_size_delta
        prev_count = int(prev.get("count", 0)) if prev else 0
        is_dirty = (not prev) or abs(count - prev_count) >= delta
        stats["aiko_dirty"] = bool(is_dirty)
        if not is_dirty:
            return []

        batch = sorted(
            pop, key=lambda m: float(getattr(m, "salience", 0.0)), reverse=True
        )[: self._max_aiko_memories]
        proposals = spec.propose(
            ctx, memories=batch, existing=self._existing_for(spec)
        )
        self._save_sigs(_KV_AIKO_SIG, {"count": count, "max_id": max_id})
        return proposals

    def _existing_for(self, spec: ProposerSpec) -> list[ExistingConcept]:
        """Concepts already stored for this proposer's (subject, kind) --
        both candidate and active -- so the LLM can avoid re-proposing
        them and reinforce by id instead. Cardinality is low by design,
        so passing them all is cheap."""
        return [
            ExistingConcept(id=c.concept_id, label=c.label)
            for c in self._concept_store.list_by(
                subject=spec.subject, kind=spec.kind
            )
        ]

    # ── persistence (shared) ───────────────────────────────────────────

    def _persist(
        self, proposal: CandidateProposal, stats: dict[str, Any]
    ) -> None:
        if not proposal.evidence:
            return

        # LLM-directed reinforcement: attach the new evidence to the named
        # existing concept (the proposer already validated the id against
        # the list it was given; get() guards the store-race).
        if proposal.reinforces_id is not None:
            concept = self._concept_store.get(proposal.reinforces_id)
            if concept is not None:
                self._reinforce(concept, proposal)
                stats["reinforced"] += 1
                self._bump_subject(stats, proposal.subject, "reinforced")
            return

        try:
            vec = self._embedder.embed(proposal.label)
        except Exception:
            log.warning(
                "concept label embed failed: %s", proposal.label,
                exc_info=True,
            )
            return

        # Embedding safety net: catches paraphrase dupes the LLM missed
        # despite seeing the existing list.
        match = self._find_duplicate(proposal, vec)
        if match is not None:
            self._reinforce(match, proposal)
            stats["reinforced"] += 1
            self._bump_subject(stats, proposal.subject, "reinforced")
            return

        now = _now_iso()
        distinct = len({(t, i) for t, i in proposal.evidence})
        concept = Concept(
            label=proposal.label,
            kind=proposal.kind,
            subject=proposal.subject,
            evidence_model=proposal.evidence_model,
            status="candidate",
            confidence=proposal.confidence,
            evidence_count=distinct,
            distinct_source_count=distinct,
            rationale=proposal.rationale,
            embedding=vec,
            first_evidence_at=now,
            last_reinforced_at=now,
        )
        cid = self._concept_store.add(concept)
        self._add_evidence_edges(cid, proposal.evidence)
        stats["added"] += 1
        self._bump_subject(stats, proposal.subject, "added")
        if self._notify_concept_added is not None:
            try:
                self._notify_concept_added(
                    {
                        "id": cid,
                        "label": proposal.label,
                        "subject": proposal.subject,
                        "kind": proposal.kind,
                    }
                )
            except Exception:
                log.debug("notify_concept_added failed", exc_info=True)

    def _find_duplicate(
        self, proposal: CandidateProposal, vec: Any
    ) -> Concept | None:
        try:
            hits = self._concept_store.nearest(
                vec,
                subject=proposal.subject,
                kind=proposal.kind,
                status=None,
                k=5,
            )
        except Exception:
            log.debug("concept nearest failed", exc_info=True)
            return None
        if hits and hits[0][1] >= _DEDUPE_COS:
            return hits[0][0]
        return None

    def _reinforce(
        self, concept: Concept, proposal: CandidateProposal
    ) -> None:
        self._add_evidence_edges(concept.concept_id, proposal.evidence)
        ev = self._concept_store.evidence_of(concept.concept_id)
        concept.evidence_count = len(ev)
        concept.distinct_source_count = len(
            {(e.src_type, e.src_id) for e in ev}
        )
        concept.last_reinforced_at = _now_iso()
        # confidence / plasticity / status intentionally left to L3.
        self._concept_store.update(concept)

    def _add_evidence_edges(
        self, concept_id: int, evidence: list[tuple[str, str]]
    ) -> None:
        for node_type, node_id in evidence:
            self._concept_store.add_edge(
                ConceptEdge(
                    src_type=node_type,
                    src_id=str(node_id),
                    dst_type="concept",
                    dst_id=str(concept_id),
                    relation="evidence",
                    polarity=1,
                    strength=1.0,
                )
            )

    @staticmethod
    def _bump_subject(
        stats: dict[str, Any], subject: str, action: str
    ) -> None:
        by = stats["by_subject"].setdefault(
            subject, {"added": 0, "reinforced": 0}
        )
        by[action] += 1

    # ── topic-graph helpers ────────────────────────────────────────────

    def _user_dominant_clusters(
        self,
    ) -> list[tuple[int, str, int, tuple[str, ...]]]:
        """Return ``(rep, label, size, member_kinds)`` for clusters whose
        members are majority non-aiko-self kinds."""
        try:
            clusters = self._topic_graph.topic_clusters()
        except Exception:
            log.warning("topic_clusters failed", exc_info=True)
            return []
        aiko_kinds = set(AIKO_SELF_KINDS)
        out: list[tuple[int, str, int, tuple[str, ...]]] = []
        for c in clusters:
            kinds = tuple(c.member_kinds or ())
            if kinds:
                aiko_share = sum(1 for k in kinds if k in aiko_kinds) / len(
                    kinds
                )
                if aiko_share > 0.5:
                    continue
            label = (c.summary or "").strip()
            if not label:
                continue
            out.append((int(c.representative_id), label, int(c.size), kinds))
        return out

    def _memory_content(self, memory_id: int) -> str:
        try:
            mem = self._memory_store.get(int(memory_id))
        except Exception:
            return ""
        return (getattr(mem, "content", "") or "") if mem else ""

    def _digest_for_rep(self, rep: int) -> str:
        raw = self._kv_get(_TOPIC_DIGEST_PREFIX + str(rep))
        if not raw:
            return ""
        try:
            parsed = json.loads(raw)
            mem_id = int(parsed.get("memory_id"))
        except (json.JSONDecodeError, TypeError, ValueError):
            return ""
        return self._memory_content(mem_id)

    # ── kv signature helpers ───────────────────────────────────────────

    def _load_sigs(self, key: str) -> dict[str, Any]:
        raw = self._kv_get(key)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _save_sigs(self, key: str, value: dict[str, Any]) -> None:
        try:
            self._kv_set(key, json.dumps(value))
        except Exception:
            log.debug("save sigs failed (%s)", key, exc_info=True)

    # ── llm ─────────────────────────────────────────────────────────────

    def _call_llm(self, system: str, user: str) -> list[dict[str, Any]]:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        chunks: list[str] = []
        self._llm_calls += 1
        try:
            stream = self._ollama.chat_stream(
                messages,
                options={
                    "num_predict": _MAX_TOKENS,
                    "temperature": _TEMPERATURE,
                },
                model=self._chat_model,
                stop_event=self._cancel_event,
                format_json=True,
                surface="concept_synthesis_worker",
            )
            for chunk in stream:
                chunks.append(chunk)
        except Exception:
            log.warning("concept synthesis LLM call failed", exc_info=True)
            return []
        return self._parse("".join(chunks))

    @staticmethod
    def _parse(raw: str) -> list[dict[str, Any]]:
        match = _JSON_OBJECT_RE.search(raw or "")
        if match is None:
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, dict):
            return []
        concepts = parsed.get("concepts")
        return concepts if isinstance(concepts, list) else []


__all__ = ["ConceptSynthesisWorker"]
