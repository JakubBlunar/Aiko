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
from app.core.concepts.concept_event_store import ConceptEvent
from app.core.concepts.concept_store import Concept, ConceptEdge
from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.concepts.concept_event_store import ConceptEventStore
    from app.core.concepts.concept_store import ConceptStore
    from app.core.conversation.topic_graph import TopicGraph
    from app.core.infra.agent_settings import AgentSettings
    from app.core.memory.memory_store import MemoryStore

log = logging.getLogger("app.concept_synthesis_worker")

_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)

# Cosine threshold above which a fresh proposal is treated as the same
# candidate as an existing concept (dedupe -> reinforce instead of add).
_DEDUPE_COS = 0.9
_MAX_TOKENS = 1600
_TEMPERATURE = 0.6

_KV_CLUSTER_SIGS = "concept_synth.cluster_sigs"
_KV_AIKO_SIG = "concept_synth.aiko_sig"
_TOPIC_DIGEST_PREFIX = "aiko.topic_digest."


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _salvage_concepts(text: str) -> list[dict[str, Any]]:
    """Recover complete concept objects from a truncated JSON response.

    When the proposer hits its ``num_predict`` cap the ``"concepts": [...]``
    array is cut off inside the last object, so :func:`json.loads` on the
    whole blob fails. This walks the characters after the array's opening
    ``[``, tracks brace depth (string-/escape-aware), and parses each
    fully-closed ``{...}`` object on its own. The trailing incomplete
    object is dropped; everything before it is preserved.
    """
    if not text:
        return []
    key_pos = text.find('"concepts"')
    bracket = text.find("[", key_pos if key_pos >= 0 else 0)
    if bracket < 0:
        return []
    out: list[dict[str, Any]] = []
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i in range(bracket + 1, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                fragment = text[start : i + 1]
                try:
                    obj = json.loads(fragment)
                    if isinstance(obj, dict):
                        out.append(obj)
                except Exception:
                    pass
                start = -1
        elif ch == "]" and depth == 0:
            break
    return out


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
        user_display_name_provider: Callable[[], str] | None = None,
        assistant_display_name_provider: Callable[[], str] | None = None,
        concept_event_store: "ConceptEventStore | None" = None,
    ) -> None:
        self._concept_store = concept_store
        self._concept_event_store = concept_event_store
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
        self._user_name_provider = user_display_name_provider
        self._assistant_name_provider = assistant_display_name_provider
        self._llm_calls = 0

    @staticmethod
    def _resolve_name(
        provider: Callable[[], str] | None, fallback: str
    ) -> str:
        """Best-effort display-name lookup; keeps synthesis running with a
        generic fallback if the provider errors or returns blank."""
        if provider is None:
            return fallback
        try:
            name = (provider() or "").strip()
        except Exception:
            return fallback
        return name or fallback

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
        # L21 cold-start guard: don't propose abstractions from a graph
        # too sparse / too young to support them. A manual ``force`` run
        # (button / MCP) still bypasses this by calling ``run`` directly.
        if not self._graph_mature(now=now):
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at
        )

    def _graph_mature(self, *, now: datetime | None = None) -> bool:
        """L21 maturity predicate: enough distinct clusters AND enough
        calendar history before any concept is proposed. A
        ``concept_min_clusters`` of 0 disables the cluster floor; a
        ``concept_min_history_days`` of 0 disables the history floor.
        Computed straight off ``topic_clusters()`` so it works uniformly
        against the real graph and lean stubs."""
        min_clusters = int(
            getattr(self._memory_settings, "concept_min_clusters", 6)
        )
        if min_clusters > 0:
            try:
                cluster_count = len(self._topic_graph.topic_clusters())
            except Exception:
                log.debug("graph maturity check failed", exc_info=True)
                return False
            if cluster_count < min_clusters:
                return False
        min_history_days = float(
            getattr(self._memory_settings, "concept_min_history_days", 3.0)
        )
        if min_history_days <= 0.0:
            return True
        return self._history_days() >= min_history_days

    def _history_days(self) -> float:
        """Calendar days since the oldest memory (0.0 when unknown)."""
        try:
            earliest = self._memory_store.earliest_created_at()
        except Exception:
            return 0.0
        if not earliest:
            return 0.0
        try:
            first = datetime.fromisoformat(earliest)
        except (TypeError, ValueError):
            return 0.0
        if first.tzinfo is None:
            first = first.replace(tzinfo=timezone.utc)
        now = self._clock()
        return max(0.0, (now - first).total_seconds() / 86_400.0)

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

    @property
    def _max_tokens(self) -> int:
        return max(
            256,
            int(
                getattr(
                    self._memory_settings,
                    "concept_synthesis_max_tokens",
                    _MAX_TOKENS,
                )
            ),
        )

    # ── run ────────────────────────────────────────────────────────────

    def run(self, *, force: bool = False) -> dict[str, Any]:
        """Run one synthesis pass.

        ``force=True`` (manual "Run synthesis" button / MCP tool) ignores
        the incremental dirty-tracking so a pass is guaranteed even when
        nothing changed -- e.g. after concepts were manually deleted, the
        source-memory signatures are unchanged, so an incremental run
        would otherwise short-circuit and propose nothing. Scheduled idle
        runs leave ``force=False`` and stay incremental.
        """
        if not bool(getattr(self._agent_settings, "concepts_enabled", False)):
            return {"skipped": True, "reason": "disabled"}
        if self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}
        # L21 cold-start guard. Scheduled runs are already gated by
        # ``is_ready``; this belt-and-braces check keeps an immature graph
        # from proposing even if ``run`` is called directly. ``force``
        # (manual button / MCP) always bypasses.
        if not force and not self._graph_mature():
            return {"skipped": True, "reason": "immature_graph"}

        self._llm_calls = 0
        started = time.monotonic()
        ctx = ProposerContext(
            call_llm=self._call_llm,
            user_name=self._resolve_name(self._user_name_provider, "the user"),
            assistant_name=self._resolve_name(
                self._assistant_name_provider, "Aiko"
            ),
        )

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
                    proposals = self._run_cluster_pass(ctx, spec, stats, force)
                elif spec.population == "aiko_memories":
                    proposals = self._run_aiko_pass(ctx, spec, stats, force)
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
        force: bool = False,
    ) -> list[CandidateProposal]:
        clusters = self._dominant_clusters("user")
        if not clusters:
            return []
        cluster_index = [
            (rep, label, size) for rep, label, size, _kinds in clusters
        ]
        # Per-proposer signature key so multiple proposers over the cluster
        # population (identity, value, ...) each track their own dirty state.
        sig_key = spec.sig_key or _KV_CLUSTER_SIGS
        sigs = self._load_sigs(sig_key)
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
            if force or prev_label != label or drift >= delta:
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
            coactivation=self._coactivation_modes(),
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
        self._save_sigs(sig_key, new_sigs)
        return proposals

    def _coactivation_modes(self) -> list[Any]:
        """L4 grouping hint for the user-identity proposer: the co-activation
        modes (clusters that co-fire in the same sessions), computed once per
        run off the topic graph. Empty (and cheap) when the graph doesn't
        expose the signal or raises -- the proposer treats an empty hint as
        "no bias", so this never blocks synthesis."""
        graph = self._topic_graph
        fn = getattr(graph, "cluster_coactivation", None)
        if not callable(fn):
            return []
        ms = self._memory_settings
        try:
            return list(
                fn(
                    bucket_by="session",
                    min_pair_support=int(
                        getattr(ms, "coactivation_min_pair_support", 2)
                    ),
                    min_strength=float(
                        getattr(ms, "coactivation_min_strength", 0.25)
                    ),
                    max_modes=int(getattr(ms, "coactivation_max_modes", 4)),
                    max_reps_per_mode=int(
                        getattr(ms, "coactivation_max_reps_per_mode", 4)
                    ),
                )
            )
        except Exception:
            log.debug("coactivation modes unavailable", exc_info=True)
            return []

    # ── aiko pass ──────────────────────────────────────────────────────

    def _run_aiko_pass(
        self,
        ctx: ProposerContext,
        spec: ProposerSpec,
        stats: dict[str, Any],
        force: bool = False,
    ) -> list[CandidateProposal]:
        """L11 combined self-model pass: mine BOTH her aiko-dominant
        self-themes (clusters the graph already produces) AND her salient
        individual self-memories, so a self-concept can be grounded by a
        recurring theme, a specific memory, or a mix. Degrades cleanly to
        memories-only when she has no self-themes yet (cold start)."""
        pop = self._memory_store.iter_by_kinds(AIKO_SELF_KINDS)
        mem_count = len(pop)
        aiko_clusters = self._dominant_clusters("aiko")
        if mem_count == 0 and not aiko_clusters:
            stats["aiko_dirty"] = False
            return []
        mem_max_id = max((int(m.id) for m in pop), default=0)

        # Per-proposer signature key (identity vs value over the aiko pop).
        sig_key = spec.sig_key or _KV_AIKO_SIG
        prev = self._load_sigs(sig_key)
        delta = self._dirty_size_delta

        # Memory watermark drift (fall back to the legacy "count"/"max_id"
        # shape so an in-flight upgrade re-proposes once, then settles).
        prev_count = int(prev.get("mem_count", prev.get("count", 0)))
        mem_dirty = force or (not prev) or abs(mem_count - prev_count) >= delta

        # Cluster drift: new / relabelled / size-drifted aiko clusters.
        prev_clusters = prev.get("clusters", {}) if prev else {}
        dirty_clusters: list[tuple[int, str, int, int, bool]] = []
        for rep, label, size, _kinds in aiko_clusters:
            p = prev_clusters.get(str(rep))
            if p is None:
                dirty_clusters.append((rep, label, size, size, True))
                continue
            prev_size = int(p.get("size", 0))
            prev_label = str(p.get("label", ""))
            drift = abs(size - prev_size)
            if force or prev_label != label or drift >= delta:
                dirty_clusters.append((rep, label, size, drift, False))

        is_dirty = force or mem_dirty or bool(dirty_clusters)
        stats["aiko_dirty"] = bool(is_dirty)
        if not is_dirty:
            return []

        # Themes: full index (context) + a bounded focus set (detail).
        cluster_index = [
            (rep, label, size) for rep, label, size, _kinds in aiko_clusters
        ]
        dirty_clusters.sort(key=lambda d: (0 if d[4] else 1, -d[3]))
        focus_rows = dirty_clusters[: self._max_clusters_per_run]
        focus_reps = {rep for rep, _l, _s, _d, _n in focus_rows}
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

        # Specifics: salience-sorted self-memories, minus any that are a
        # cluster's representative (so a theme and its headline memory aren't
        # offered as two separate sources), capped.
        cluster_reps = {int(rep) for rep, _l, _s, _k in aiko_clusters}
        batch = [
            m
            for m in sorted(
                pop,
                key=lambda m: float(getattr(m, "salience", 0.0)),
                reverse=True,
            )
            if int(m.id) not in cluster_reps
        ][: self._max_aiko_memories]

        proposals = spec.propose(
            ctx,
            focus_clusters=focus_clusters,
            cluster_index=cluster_index,
            memories=batch,
            existing=self._existing_for(spec),
        )

        # Persist combined signature. Mark processed focus reps fresh; keep
        # unprocessed dirty clusters on their old signature so they stay
        # dirty and drain next run (mirrors the user pass).
        current = {rep: (label, size) for rep, label, size, _k in aiko_clusters}
        new_clusters: dict[str, dict[str, Any]] = {}
        for rep, (label, size) in current.items():
            if rep in focus_reps:
                new_clusters[str(rep)] = {"size": size, "label": label}
            elif str(rep) in prev_clusters:
                new_clusters[str(rep)] = prev_clusters[str(rep)]
        self._save_sigs(
            sig_key,
            {
                "mem_count": mem_count,
                "mem_max_id": mem_max_id,
                "clusters": new_clusters,
            },
        )
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
        # despite seeing the existing list. ``top_sim`` is the cosine to
        # the nearest existing concept of this (subject, kind) -- reused
        # below as ``novelty = 1 - top_sim`` for the discovery event.
        match, top_sim = self._find_duplicate(proposal, vec)
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
        self._record_discovery(concept, proposal, top_sim, now)
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
    ) -> tuple[Concept | None, float]:
        """Return ``(duplicate_or_none, top_cosine)``.

        The top cosine to the nearest existing concept of this
        (subject, kind) is surfaced even when it is below the dedupe
        threshold, so the caller can derive the discovery ``novelty``
        from the same nearest-neighbour lookup (no extra embed / query).
        """
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
            return None, 0.0
        if not hits:
            return None, 0.0
        top_sim = float(hits[0][1])
        if top_sim >= _DEDUPE_COS:
            return hits[0][0], top_sim
        return None, top_sim

    def _record_discovery(
        self,
        concept: Concept,
        proposal: CandidateProposal,
        top_sim: float,
        created_at: str,
    ) -> None:
        """Append a ``discovered`` event to the timeline (best-effort).

        Never fatal: a logging failure must not lose the concept that was
        just persisted. ``novelty`` is ``1 - top_sim`` (1.0 for the first
        concept of its subject/kind, where the nearest lookup was empty).
        """
        store = self._concept_event_store
        if store is None:
            return
        source_kinds = sorted({t for t, _ in proposal.evidence})
        distinct = int(concept.distinct_source_count)
        novelty = round(max(0.0, min(1.0, 1.0 - top_sim)), 4)
        try:
            store.add(
                ConceptEvent(
                    concept_id=concept.concept_id or None,
                    event_type="discovered",
                    kind=concept.kind,
                    subject=concept.subject,
                    label=concept.label,
                    confidence=float(concept.confidence),
                    novelty=novelty,
                    evidence_count=int(concept.evidence_count),
                    distinct_source_count=distinct,
                    source_kinds=",".join(source_kinds),
                    reason=self._discovery_reason(
                        concept.subject, source_kinds, distinct, novelty
                    ),
                    created_at=created_at,
                )
            )
        except Exception:
            log.debug("record discovery event failed", exc_info=True)

    @staticmethod
    def _discovery_reason(
        subject: str,
        source_kinds: list[str],
        distinct: int,
        novelty: float,
    ) -> str:
        """Generated, factual one-liner describing the discovery."""
        first = "First " if novelty >= 0.6 else ""
        if source_kinds == ["cluster"]:
            noun = "topic cluster" if distinct == 1 else "topic clusters"
            verb = "abstraction connecting" if first else "connects"
            lead = f"{first}{verb}" if first else "Connects"
            return f"{lead} {distinct} {noun}."
        if source_kinds == ["memory"]:
            noun = "memory" if distinct == 1 else "memories"
            src = (
                "reflection/diary memories"
                if subject == "aiko" and distinct != 1
                else noun
            )
            if first:
                kind_word = (
                    "self-concept" if subject == "aiko" else "concept"
                )
                return f"First {kind_word} linking {distinct} {src}."
            return f"Links {distinct} {src}."
        # Mixed / other source kinds: stay generic but still factual.
        joined = " + ".join(source_kinds) if source_kinds else "sources"
        lead = "First abstraction over" if first else "Draws on"
        return f"{lead} {distinct} sources ({joined})."

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

    def _dominant_clusters(
        self, subject: str
    ) -> list[tuple[int, str, int, tuple[str, ...]]]:
        """Return ``(rep, label, size, member_kinds)`` for the clusters that
        belong to ``subject``.

        The single topic graph clusters *all* memories together; a cluster's
        subject is decided by whether its members are majority aiko-self
        kinds (``self`` / ``reflection`` / ``diary``). ``subject="user"``
        keeps clusters with ``aiko_share <= 0.5`` (including clusters with no
        kind labels, treated as user); ``subject="aiko"`` keeps clusters with
        ``aiko_share > 0.5``. This is what lets the user pass exclude her
        self-themes while the aiko pass (L11) mines exactly those."""
        try:
            clusters = self._topic_graph.topic_clusters()
        except Exception:
            log.warning("topic_clusters failed", exc_info=True)
            return []
        aiko_kinds = set(AIKO_SELF_KINDS)
        want_aiko = subject == "aiko"
        out: list[tuple[int, str, int, tuple[str, ...]]] = []
        for c in clusters:
            kinds = tuple(c.member_kinds or ())
            if kinds:
                aiko_share = sum(1 for k in kinds if k in aiko_kinds) / len(
                    kinds
                )
                is_aiko = aiko_share > 0.5
            else:
                is_aiko = False
            if is_aiko != want_aiko:
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
                    "num_predict": self._max_tokens,
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
        if match is not None:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict):
                    concepts = parsed.get("concepts")
                    if isinstance(concepts, list):
                        return concepts
            except json.JSONDecodeError:
                pass
        # Truncated response: the aiko-identity pass emits several
        # concepts with full rationales in one object, so hitting the
        # token cap cuts the "concepts": [...] array mid-object and the
        # whole blob fails to parse. Recover the complete leading objects
        # instead of dropping the entire batch.
        salvaged = _salvage_concepts(raw or "")
        if salvaged:
            log.info(
                "concept synthesis: salvaged %d complete concept(s) from a "
                "truncated/invalid response",
                len(salvaged),
            )
        return salvaged


__all__ = ["ConceptSynthesisWorker"]
