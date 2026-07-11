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

import hashlib
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
    NarrativeCandidate,
    ProposerContext,
    ProposerSpec,
    TensionBase,
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
# Answer-token budget per proposer LLM call. Sized generously because a
# reasoning-capable maintenance model (e.g. qwen3.x) can spend a large,
# variable preamble on visible chain-of-thought *before* the ``{"concepts":
# [...]}`` array, and several proposers emit multiple concepts with full
# rationales in one object -- a tight cap truncates the array mid-object and
# the batch fails to parse (the salvage pass recovers complete leading objects,
# but a roomy budget avoids losing the tail in the first place). This is an
# idle worker, so the extra tokens cost latency we don't feel.
_MAX_TOKENS = 4096
_TEMPERATURE = 0.6

_KV_CLUSTER_SIGS = "concept_synth.cluster_sigs"
_KV_AIKO_SIG = "concept_synth.aiko_sig"
_TOPIC_DIGEST_PREFIX = "aiko.topic_digest."


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    """Best-effort ISO-8601 -> aware ``datetime`` (``None`` on junk). Used by
    the L14 span computation to measure how much time an aspiration covers."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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
        user_profile_store: Any = None,
        style_signal_store: Any = None,
        user_id_provider: Callable[[], str] | None = None,
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
        # L23: optional style-signal sources for the communication-style pass.
        # All optional -> when absent the digest is empty and the pass still
        # runs on anchors + clusters (keeps tests / cold installs working).
        self._user_profile_store = user_profile_store
        self._style_signal_store = style_signal_store
        self._user_id_provider = user_id_provider
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
    def _affect_min_samples(self) -> int:
        """L13: minimum affect-bearing turns a cluster (or self-theme) must
        accrue before it is offered to the affective proposers -- a couple of
        readings, so a one-off mood never becomes a durable topic->affect."""
        return max(
            1,
            int(
                getattr(
                    self._memory_settings,
                    "concept_synthesis_affect_min_samples",
                    3,
                )
            ),
        )

    @property
    def _ritual_min_moments(self) -> int:
        """L7: minimum ``shared_moment`` rows before the ritual pass even runs
        -- below this there aren't enough moments for a recurring pattern to
        exist, so grouping is skipped entirely."""
        return max(
            2,
            int(
                getattr(
                    self._memory_settings,
                    "concept_synthesis_ritual_min_moments",
                    6,
                )
            ),
        )

    @property
    def _ritual_group_min_size(self) -> int:
        """L7: minimum members a moment cluster needs to be a ritual candidate
        (a couple of moments isn't a recurring pattern)."""
        return max(
            2,
            int(
                getattr(
                    self._memory_settings, "concept_synthesis_ritual_group_min_size", 3
                )
            ),
        )

    @property
    def _ritual_group_similarity(self) -> float:
        """L7: single-link cosine threshold for joining two shared moments
        into the same ritual group."""
        raw = float(
            getattr(
                self._memory_settings,
                "concept_synthesis_ritual_group_similarity",
                0.6,
            )
        )
        return max(0.0, min(1.0, raw))

    @property
    def _max_ritual_groups(self) -> int:
        """L7: cap on ritual groups offered to the proposer per run (bounds
        the prompt / LLM cost)."""
        return max(
            1,
            int(
                getattr(
                    self._memory_settings, "concept_synthesis_max_ritual_groups", 3
                )
            ),
        )

    @property
    def _narrative_min_chain(self) -> int:
        """L8: minimum ordered steps a candidate cluster must resolve to before
        it is offered as an arc (a story needs a beginning, a middle, and an
        end -- two beats is an anecdote). Also the proposer's new-arc floor."""
        return max(
            2,
            int(
                getattr(
                    self._memory_settings,
                    "concept_synthesis_narrative_min_chain",
                    3,
                )
            ),
        )

    @property
    def _max_narrative_clusters_per_run(self) -> int:
        """L8: cap on candidate arcs offered to the narrative proposer per run
        (per subject) -- bounds the prompt / LLM cost."""
        return max(
            1,
            int(
                getattr(
                    self._memory_settings,
                    "concept_synthesis_max_narrative_clusters_per_run",
                    3,
                )
            ),
        )

    @property
    def _max_narrative_memories(self) -> int:
        """L8: cap on member memories loaded per candidate arc -- long-running
        themes stay bounded (the proposer further elides the middle)."""
        return max(
            2,
            int(
                getattr(
                    self._memory_settings,
                    "concept_synthesis_max_narrative_memories",
                    40,
                )
            ),
        )

    @property
    def _aspiration_min_chain(self) -> int:
        """L14: minimum ordered steps a candidate cluster must resolve to before
        it is offered as a trajectory. Also the proposer's new-aspiration floor."""
        return max(
            2,
            int(
                getattr(
                    self._memory_settings,
                    "concept_synthesis_aspiration_min_chain",
                    3,
                )
            ),
        )

    @property
    def _aspiration_min_span_days(self) -> float:
        """L14: minimum number of days the ordered evidence must span before a
        cluster is offered as a trajectory -- a *direction* has to persist over
        time, not just accumulate in a single sitting."""
        return max(
            0.0,
            float(
                getattr(
                    self._memory_settings,
                    "concept_synthesis_aspiration_min_span_days",
                    14.0,
                )
            ),
        )

    @property
    def _max_aspiration_clusters_per_run(self) -> int:
        """L14: cap on candidate trajectories offered to the aspiration proposer
        per run (per subject) -- bounds the prompt / LLM cost."""
        return max(
            1,
            int(
                getattr(
                    self._memory_settings,
                    "concept_synthesis_max_aspiration_clusters_per_run",
                    3,
                )
            ),
        )

    @property
    def _max_aspiration_memories(self) -> int:
        """L14: cap on member memories loaded per candidate trajectory."""
        return max(
            2,
            int(
                getattr(
                    self._memory_settings,
                    "concept_synthesis_max_aspiration_memories",
                    40,
                )
            ),
        )

    @property
    def _max_boundary_memories(self) -> int:
        """L18: cap on explicit-anchor memories offered to the boundary
        proposer per run (per subject) -- bounds the prompt / LLM cost."""
        return max(
            1,
            int(
                getattr(
                    self._memory_settings,
                    "concept_synthesis_max_boundary_memories",
                    24,
                )
            ),
        )

    @property
    def _max_comm_style_memories(self) -> int:
        """L23: cap on explicit-anchor memories offered to the
        communication-style proposer per run (per subject) -- bounds prompt /
        LLM cost."""
        return max(
            1,
            int(
                getattr(
                    self._memory_settings,
                    "concept_synthesis_max_comm_style_memories",
                    24,
                )
            ),
        )

    @property
    def _max_tension_concepts(self) -> int:
        """L12: cap on active base concepts offered to the tension proposer per
        run (per subject) -- bounds the prompt / LLM cost. Concept cardinality
        is small by design (tens), so this rarely bites; for the relationship
        lens each side gets roughly half."""
        return max(
            2,
            int(
                getattr(
                    self._memory_settings,
                    "concept_synthesis_max_tension_concepts",
                    24,
                )
            ),
        )

    @property
    def _max_generalization_concepts(self) -> int:
        """L20: cap on active base concepts offered to the generalization
        proposer per run (per subject) -- bounds the prompt / LLM cost, mirrors
        the tension cap."""
        return max(
            2,
            int(
                getattr(
                    self._memory_settings,
                    "concept_synthesis_max_generalization_concepts",
                    24,
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
            "affect_dirty": False,
            "ritual_dirty": False,
            "narrative_dirty": False,
            "aspiration_dirty": False,
            "boundary_dirty": False,
            "comm_style_dirty": False,
            "tension_dirty": False,
            "generalization_dirty": False,
        }

        for spec in CONCEPT_PROPOSERS:
            if self._cancel_event.is_set():
                break
            try:
                if spec.population == "clusters":
                    proposals = self._run_cluster_pass(ctx, spec, stats, force)
                elif spec.population == "aiko_memories":
                    proposals = self._run_aiko_pass(ctx, spec, stats, force)
                elif spec.population == "affect":
                    proposals = self._run_affect_pass(ctx, spec, stats, force)
                elif spec.population == "shared_moments":
                    proposals = self._run_ritual_pass(ctx, spec, stats, force)
                elif spec.population == "narrative":
                    proposals = self._run_narrative_pass(ctx, spec, stats, force)
                elif spec.population == "aspiration":
                    proposals = self._run_aspiration_pass(ctx, spec, stats, force)
                elif spec.population == "boundary":
                    proposals = self._run_boundary_pass(ctx, spec, stats, force)
                elif spec.population == "comm_style":
                    proposals = self._run_comm_style_pass(ctx, spec, stats, force)
                elif spec.population == "tension":
                    proposals = self._run_tension_pass(ctx, spec, stats, force)
                elif spec.population == "generalization":
                    proposals = self._run_generalization_pass(
                        ctx, spec, stats, force
                    )
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

    # ── affect pass (L13) ───────────────────────────────────────────────

    def _run_affect_pass(
        self,
        ctx: ProposerContext,
        spec: ProposerSpec,
        stats: dict[str, Any],
        force: bool = False,
    ) -> list[CandidateProposal]:
        """L13 affect pass: annotate topic clusters with the subject's typical
        affect (from the per-cluster affect map) and, for ``subject=aiko``,
        also her self-themes + affect-stamped self-memories, so the affective
        proposers can name durable topic->emotion patterns. Evidence is the
        cluster reps / memory ids; the affect *direction* is carried in the
        concept text (no edge-schema change)."""
        from app.core.concepts import cluster_affect as _ca

        subject = spec.subject
        try:
            clusters = self._topic_graph.topic_clusters()
        except Exception:
            log.debug("topic_clusters failed (affect pass)", exc_info=True)
            clusters = []
        by_cid = {int(c.cluster_id): c for c in clusters}
        min_samples = self._affect_min_samples

        # rep -> (label, size, phrase, bucket, samples)
        annotated: dict[int, tuple[str, int, str, str, int]] = {}

        # (a) conversation-topic affect from the subject's per-cluster map.
        affect_map = _ca.load_map(self._kv_get, _ca.kv_key_for(subject))
        for cid_str, st in affect_map.items():
            if int(st.samples) < min_samples:
                continue
            try:
                cid = int(cid_str)
            except (TypeError, ValueError):
                continue
            c = by_cid.get(cid)
            if c is None:
                continue
            label = (c.summary or "").strip()
            if not label:
                continue
            annotated[int(c.representative_id)] = (
                label,
                int(c.size),
                _ca.affect_phrase(st.valence, st.arousal),
                "%s/%s" % _ca.affect_bucket(st.valence, st.arousal),
                int(st.samples),
            )

        # (b) aiko-only: self-themes (aggregated self-memory affect) +
        # affect-stamped self-memory specifics.
        memories_batch: list[Any] = []
        memory_affect: dict[int, str] = {}
        if subject == "aiko":
            aiko_kinds = set(AIKO_SELF_KINDS)
            self_mems = self._memory_store.iter_by_kinds(AIKO_SELF_KINDS)
            mem_aff: dict[int, tuple[float, float]] = {}
            for m in self_mems:
                aff = (getattr(m, "metadata", None) or {}).get("affect")
                if not isinstance(aff, dict):
                    continue
                try:
                    mem_aff[int(m.id)] = (
                        float(aff["valence"]), float(aff["arousal"])
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            # Aggregate per self-theme (aiko-dominant cluster).
            for c in clusters:
                kinds = tuple(c.member_kinds or ())
                if not kinds:
                    continue
                if sum(1 for k in kinds if k in aiko_kinds) / len(kinds) <= 0.5:
                    continue
                label = (c.summary or "").strip()
                if not label:
                    continue
                vals = [
                    mem_aff[int(mid)]
                    for mid in c.member_ids
                    if int(mid) in mem_aff
                ]
                if len(vals) < min_samples:
                    continue
                v = sum(x[0] for x in vals) / len(vals)
                a = sum(x[1] for x in vals) / len(vals)
                annotated.setdefault(
                    int(c.representative_id),
                    (
                        label,
                        int(c.size),
                        _ca.affect_phrase(v, a),
                        "%s/%s" % _ca.affect_bucket(v, a),
                        len(vals),
                    ),
                )
            # Self-memory specifics (with affect), salience desc, minus reps.
            reps = set(annotated.keys())
            memories_batch = [
                m
                for m in sorted(
                    self_mems,
                    key=lambda m: float(getattr(m, "salience", 0.0)),
                    reverse=True,
                )
                if int(m.id) in mem_aff and int(m.id) not in reps
            ][: self._max_aiko_memories]
            for m in memories_batch:
                v, a = mem_aff[int(m.id)]
                memory_affect[int(m.id)] = _ca.affect_phrase(v, a)

        if not annotated and not memory_affect:
            stats["affect_dirty"] = False
            return []

        # Combined dirty-tracking under the subject's affect sig.
        sig_key = spec.sig_key or ("concept_synth.affect_sig." + subject)
        prev = self._load_sigs(sig_key)
        prev_ann = prev.get("annotated", {}) if prev else {}
        delta = self._dirty_size_delta
        dirty: list[tuple[int, int, bool]] = []  # rep, samples, is_new
        for rep, (_label, _size, _phrase, bucket, samples) in annotated.items():
            p = prev_ann.get(str(rep))
            if p is None:
                dirty.append((rep, samples, True))
                continue
            if (
                force
                or str(p.get("bucket", "")) != bucket
                or abs(samples - int(p.get("samples", 0))) >= delta
            ):
                dirty.append((rep, samples, False))
        mem_dirty = False
        if subject == "aiko":
            prev_mc = int(prev.get("mem_affect_count", 0)) if prev else 0
            mem_dirty = (
                force or (not prev)
                or abs(len(memory_affect) - prev_mc) >= delta
            )

        is_dirty = force or bool(dirty) or mem_dirty
        stats["affect_dirty"] = bool(is_dirty)
        if not is_dirty:
            return []

        cluster_index = [
            (rep, ann[0], ann[1]) for rep, ann in annotated.items()
        ]
        affect_by_rep = {rep: ann[2] for rep, ann in annotated.items()}
        dirty.sort(key=lambda d: (0 if d[2] else 1, -d[1]))
        focus_rows = dirty[: self._max_clusters_per_run]
        focus_reps = {rep for rep, _s, _n in focus_rows}
        focus_clusters = [
            FocusCluster(
                rep=rep,
                label=annotated[rep][0],
                size=annotated[rep][1],
                representative=self._memory_content(rep),
                digest=self._digest_for_rep(rep),
            )
            for rep, _s, _n in focus_rows
        ]

        if subject == "aiko":
            proposals = spec.propose(
                ctx,
                focus_clusters=focus_clusters,
                cluster_index=cluster_index,
                affect_by_rep=affect_by_rep,
                memories=memories_batch,
                memory_affect=memory_affect,
                existing=self._existing_for(spec),
            )
        else:
            proposals = spec.propose(
                ctx,
                focus_clusters=focus_clusters,
                cluster_index=cluster_index,
                affect_by_rep=affect_by_rep,
                existing=self._existing_for(spec),
            )

        # Persist sig: processed focus reps fresh; unprocessed dirty reps keep
        # their old signature so they stay dirty and drain next run.
        new_ann: dict[str, dict[str, Any]] = {}
        for rep, (_label, _size, _phrase, bucket, samples) in annotated.items():
            if rep in focus_reps:
                new_ann[str(rep)] = {"bucket": bucket, "samples": samples}
            elif str(rep) in prev_ann:
                new_ann[str(rep)] = prev_ann[str(rep)]
        sig: dict[str, Any] = {"annotated": new_ann}
        if subject == "aiko":
            sig["mem_affect_count"] = len(memory_affect)
        self._save_sigs(sig_key, sig)
        return proposals

    # ── ritual pass (L7) ────────────────────────────────────────────────

    def _run_ritual_pass(
        self,
        ctx: ProposerContext,
        spec: ProposerSpec,
        stats: dict[str, Any],
        force: bool = False,
    ) -> list[CandidateProposal]:
        """L7 ritual pass: group recurring ``shared_moment`` memories into
        candidate relationship rituals. Evidence is the constituent moments;
        the recurrence itself lives in the grouping (single-link cosine), not
        in an edge-schema change. Count + max-id watermark dirty-tracking so a
        settled corpus is a fast no-op."""
        if not bool(
            getattr(self._agent_settings, "ritual_synthesis_enabled", True)
        ):
            stats["ritual_dirty"] = False
            return []

        from app.core.concepts import ritual_grouping as _rg

        rows = self._memory_store.iter_by_kind("shared_moment")
        count = len(rows)
        if count < self._ritual_min_moments:
            stats["ritual_dirty"] = False
            return []
        max_id = max((int(m.id) for m in rows), default=0)

        sig_key = spec.sig_key or "concept_synth.ritual_sig"
        prev = self._load_sigs(sig_key)
        delta = self._dirty_size_delta
        prev_count = int(prev.get("count", 0)) if prev else 0
        prev_max = int(prev.get("max_id", 0)) if prev else 0
        is_dirty = (
            force
            or (not prev)
            or abs(count - prev_count) >= delta
            or max_id != prev_max
        )
        stats["ritual_dirty"] = bool(is_dirty)
        if not is_dirty:
            return []

        moments = [
            mi
            for mi in (_rg.moment_from_memory(m) for m in rows)
            if mi is not None
        ]
        groups = _rg.group_moments(
            moments,
            min_size=self._ritual_group_min_size,
            similarity=self._ritual_group_similarity,
        )[: self._max_ritual_groups]

        # Always advance the watermark, even when nothing grouped: otherwise an
        # unchanged, ungroupable corpus would re-run (and re-call the LLM)
        # every idle tick.
        self._save_sigs(sig_key, {"count": count, "max_id": max_id})
        if not groups:
            return []

        return spec.propose(
            ctx,
            groups=groups,
            existing=self._existing_for(spec),
        )

    # ── ordered-sequence passes (L8 narrative + L14 aspiration) ─────────

    def _ordered_candidates(
        self,
        spec: ProposerSpec,
        stats: dict[str, Any],
        *,
        dirty_stat_key: str,
        force: bool,
        min_chain: int,
        max_clusters: int,
        max_memories: int,
        min_span_days: float = 0.0,
    ) -> list[NarrativeCandidate]:
        """Shared candidate-builder for the ``sequence`` passes.

        Offers each subject-dominant topic cluster's member memories in
        temporal order as a :class:`NarrativeCandidate`, with per-subject
        size/label dirty-tracking so a settled corpus is a fast no-op. L8
        narrative calls it with ``min_span_days=0`` (closure, not span, is its
        bar); L14 aspiration passes a span floor (a trajectory must cover
        time). ``dirty_stat_key`` is OR-in'd True (never reset) because the two
        subject passes share one stat, so an empty aiko pass must not clobber a
        dirty user pass."""
        subject = spec.subject
        subject_clusters = self._dominant_clusters(subject)
        if not subject_clusters:
            return []
        try:
            clusters = self._topic_graph.topic_clusters()
        except Exception:
            log.debug("topic_clusters failed (sequence pass)", exc_info=True)
            clusters = []
        by_rep = {int(c.representative_id): c for c in clusters}

        sig_key = spec.sig_key or (
            "concept_synth." + spec.kind + "_sig." + subject
        )
        sigs = self._load_sigs(sig_key)
        delta = self._dirty_size_delta
        dirty: list[tuple[int, str, int, int, bool]] = []  # rep,label,size,drift,new
        for rep, label, size, _kinds in subject_clusters:
            prev = sigs.get(str(rep))
            if prev is None:
                dirty.append((rep, label, size, size, True))
                continue
            prev_size = int(prev.get("size", 0))
            prev_label = str(prev.get("label", ""))
            drift = abs(size - prev_size)
            if force or prev_label != label or drift >= delta:
                dirty.append((rep, label, size, drift, False))

        if not dirty:
            return []
        stats[dirty_stat_key] = True

        # Never-processed first, then largest drift.
        dirty.sort(key=lambda d: (0 if d[4] else 1, -d[3]))
        focus_rows = dirty[:max_clusters]

        candidates: list[NarrativeCandidate] = []
        for rep, label, _size, _drift, _new in focus_rows:
            cluster = by_rep.get(int(rep))
            if cluster is None:
                continue
            mems = self._ordered_memories(cluster.member_ids)
            if len(mems) < min_chain:
                # Too short to be a chain yet -- still marked processed below so
                # it doesn't re-run until it actually grows.
                continue
            if min_span_days > 0 and self._span_days(mems) < min_span_days:
                # Not enough time covered for a *sustained* direction (L14).
                continue
            candidates.append(
                NarrativeCandidate(
                    rep=int(rep),
                    label=label,
                    subject=subject,
                    memories=mems[:max_memories],
                )
            )

        # Persist sigs: processed focus reps fresh (even the skipped ones, so
        # they only re-run once they grow); unprocessed dirty reps keep their
        # old signature so they drain next run (mirrors the cluster pass).
        processed = {rep for rep, _l, _s, _d, _n in focus_rows}
        current = {rep: (label, size) for rep, label, size, _k in subject_clusters}
        new_sigs: dict[str, dict[str, Any]] = {}
        for rep, (label, size) in current.items():
            if rep in processed:
                new_sigs[str(rep)] = {"size": size, "label": label}
            elif str(rep) in sigs:
                new_sigs[str(rep)] = sigs[str(rep)]
        self._save_sigs(sig_key, new_sigs)
        return candidates

    def _run_narrative_pass(
        self,
        ctx: ProposerContext,
        spec: ProposerSpec,
        stats: dict[str, Any],
        force: bool = False,
    ) -> list[CandidateProposal]:
        """L8 narrative pass: offer each subject-dominant topic cluster's member
        memories, in temporal order, as a candidate *closed* causal arc. The
        chain order is written to ``concept_edges.ordinal`` (the first
        ``sequence`` kind). Subject-parameterized (``spec.subject`` = user /
        aiko). Per-subject size/label dirty-tracking via the shared builder."""
        if not bool(
            getattr(self._agent_settings, "narrative_synthesis_enabled", True)
        ):
            return []
        min_chain = self._narrative_min_chain
        candidates = self._ordered_candidates(
            spec,
            stats,
            dirty_stat_key="narrative_dirty",
            force=force,
            min_chain=min_chain,
            max_clusters=self._max_narrative_clusters_per_run,
            max_memories=self._max_narrative_memories,
        )
        if not candidates:
            return []
        return spec.propose(
            ctx,
            candidates=candidates,
            min_chain=min_chain,
            existing=self._existing_for(spec),
        )

    def _run_aspiration_pass(
        self,
        ctx: ProposerContext,
        spec: ProposerSpec,
        stats: dict[str, Any],
        force: bool = False,
    ) -> list[CandidateProposal]:
        """L14 aspiration pass: the open-ended sibling of narrative. Offers the
        same temporally-ordered candidates, but with a minimum evidence *span*
        (a trajectory must cover time) and lets the proposer name a *direction*
        rather than a closed arc. Gated by ``agent.aspiration_synthesis_enabled``."""
        if not bool(
            getattr(self._agent_settings, "aspiration_synthesis_enabled", True)
        ):
            return []
        min_chain = self._aspiration_min_chain
        candidates = self._ordered_candidates(
            spec,
            stats,
            dirty_stat_key="aspiration_dirty",
            force=force,
            min_chain=min_chain,
            max_clusters=self._max_aspiration_clusters_per_run,
            max_memories=self._max_aspiration_memories,
            min_span_days=self._aspiration_min_span_days,
        )
        if not candidates:
            return []
        return spec.propose(
            ctx,
            candidates=candidates,
            min_chain=min_chain,
            existing=self._existing_for(spec),
        )

    # ── boundary pass (L18) ─────────────────────────────────────────────

    def _run_boundary_pass(
        self,
        ctx: ProposerContext,
        spec: ProposerSpec,
        stats: dict[str, Any],
        force: bool = False,
    ) -> list[CandidateProposal]:
        """L18 boundary pass: mine behaviour-gating lines for ``spec.subject``
        from a *hybrid* of topic clusters AND Aiko's explicit remembered
        anchors -- ``self_tagged`` notes about the user for ``subject="user"``,
        ``self`` / ``reflection`` / ``diary`` notes about herself for
        ``subject="aiko"``. L18e also folds ``preference`` memories into the
        user pool when ``agent.boundary_evidence_broadening_enabled`` (stated
        tastes/limits that never became a deliberate anchor). The proposer's
        composition rule lets a single deliberate anchor seed a boundary.
        Combined cluster + memory dirty-tracking (mirrors :meth:`_run_aiko_pass`)
        so a settled corpus is a fast no-op. Gated by
        ``agent.boundary_synthesis_enabled``."""
        if not bool(
            getattr(self._agent_settings, "boundary_synthesis_enabled", True)
        ):
            return []
        subject = spec.subject
        if subject == "aiko":
            anchor_kinds: tuple[str, ...] = AIKO_SELF_KINDS
        else:
            # L18e: broaden past deliberate anchors to stated preferences/limits
            # (the proposer rejects non-boundary tastes), unless disabled.
            broaden = bool(
                getattr(
                    self._agent_settings,
                    "boundary_evidence_broadening_enabled",
                    True,
                )
            )
            anchor_kinds = (
                ("self_tagged", "preference") if broaden else ("self_tagged",)
            )
        pop = self._memory_store.iter_by_kinds(anchor_kinds)
        mem_count = len(pop)
        clusters = self._dominant_clusters(subject)
        if mem_count == 0 and not clusters:
            return []
        mem_max_id = max((int(m.id) for m in pop), default=0)

        sig_key = spec.sig_key or ("concept_synth.boundary_sig." + subject)
        prev = self._load_sigs(sig_key)
        delta = self._dirty_size_delta

        prev_count = int(prev.get("mem_count", 0)) if prev else 0
        mem_dirty = force or (not prev) or abs(mem_count - prev_count) >= delta

        prev_clusters = prev.get("clusters", {}) if prev else {}
        dirty_clusters: list[tuple[int, str, int, int, bool]] = []
        for rep, label, size, _kinds in clusters:
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
        stats["boundary_dirty"] = bool(is_dirty)
        if not is_dirty:
            return []

        cluster_index = [
            (rep, label, size) for rep, label, size, _k in clusters
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

        # Anchor specifics: salience-sorted, minus any that are a cluster's
        # representative (so a theme and its headline note aren't offered
        # twice), capped.
        cluster_reps = {int(rep) for rep, _l, _s, _k in clusters}
        batch = [
            m
            for m in sorted(
                pop,
                key=lambda m: float(getattr(m, "salience", 0.0)),
                reverse=True,
            )
            if int(m.id) not in cluster_reps
        ][: self._max_boundary_memories]

        proposals = spec.propose(
            ctx,
            focus_clusters=focus_clusters,
            cluster_index=cluster_index,
            memories=batch,
            existing=self._existing_for(spec),
        )

        # Persist combined signature. Processed focus reps go fresh; unprocessed
        # dirty clusters keep their old signature so they stay dirty and drain
        # next run (mirrors the aiko pass).
        current = {rep: (label, size) for rep, label, size, _k in clusters}
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

    def _run_comm_style_pass(
        self,
        ctx: ProposerContext,
        spec: ProposerSpec,
        stats: dict[str, Any],
        force: bool = False,
    ) -> list[CandidateProposal]:
        """L23 communication-style pass: mine self-authored delivery-style lines
        for ``spec.subject`` from a *hybrid* of topic clusters AND the remembered
        anchors (``self_tagged`` about the user / ``self`` / ``reflection`` /
        ``diary`` about herself), additionally *guided* by a persisted
        style-signal digest (K13 labels + the profile ``communication_style``
        field). The digest steers labeling only -- it is never evidence -- so a
        concept still needs real cluster/memory grounding (the proposer's
        composition rule lets a single anchor seed a line). Combined cluster +
        memory dirty-tracking, with a digest hash folded in so a material shift
        in the observed style re-fires the pass. Gated by
        ``agent.communication_style_synthesis_enabled``."""
        if not bool(
            getattr(
                self._agent_settings,
                "communication_style_synthesis_enabled",
                True,
            )
        ):
            return []
        subject = spec.subject
        anchor_kinds = (
            AIKO_SELF_KINDS if subject == "aiko" else ("self_tagged",)
        )
        pop = self._memory_store.iter_by_kinds(anchor_kinds)
        mem_count = len(pop)
        clusters = self._dominant_clusters(subject)
        if mem_count == 0 and not clusters:
            return []
        mem_max_id = max((int(m.id) for m in pop), default=0)

        # Style digest guides labeling (not evidence). Its hash is part of the
        # dirty key so a material style shift re-fires an otherwise-settled pass.
        digest = self._build_style_digest(subject)
        digest_hash = hashlib.sha1(
            digest.encode("utf-8", "ignore")
        ).hexdigest()[:12]

        sig_key = spec.sig_key or ("concept_synth.comm_style_sig." + subject)
        prev = self._load_sigs(sig_key)
        delta = self._dirty_size_delta

        prev_count = int(prev.get("mem_count", 0)) if prev else 0
        prev_digest = str(prev.get("digest_hash", "")) if prev else ""
        mem_dirty = force or (not prev) or abs(mem_count - prev_count) >= delta
        digest_dirty = bool(digest_hash) and digest_hash != prev_digest

        prev_clusters = prev.get("clusters", {}) if prev else {}
        dirty_clusters: list[tuple[int, str, int, int, bool]] = []
        for rep, label, size, _kinds in clusters:
            p = prev_clusters.get(str(rep))
            if p is None:
                dirty_clusters.append((rep, label, size, size, True))
                continue
            prev_size = int(p.get("size", 0))
            prev_label = str(p.get("label", ""))
            drift = abs(size - prev_size)
            if force or prev_label != label or drift >= delta:
                dirty_clusters.append((rep, label, size, drift, False))

        is_dirty = force or mem_dirty or digest_dirty or bool(dirty_clusters)
        stats["comm_style_dirty"] = bool(is_dirty)
        if not is_dirty:
            return []

        cluster_index = [
            (rep, label, size) for rep, label, size, _k in clusters
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

        cluster_reps = {int(rep) for rep, _l, _s, _k in clusters}
        batch = [
            m
            for m in sorted(
                pop,
                key=lambda m: float(getattr(m, "salience", 0.0)),
                reverse=True,
            )
            if int(m.id) not in cluster_reps
        ][: self._max_comm_style_memories]

        proposals = spec.propose(
            ctx,
            focus_clusters=focus_clusters,
            cluster_index=cluster_index,
            memories=batch,
            existing=self._existing_for(spec),
            style_digest=digest,
        )

        current = {rep: (label, size) for rep, label, size, _k in clusters}
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
                "digest_hash": digest_hash,
                "clusters": new_clusters,
            },
        )
        return proposals

    def _run_tension_pass(
        self,
        ctx: ProposerContext,
        spec: ProposerSpec,
        stats: dict[str, Any],
        force: bool = False,
    ) -> list[CandidateProposal]:
        """L12 tension pass -- the first *meta* proposer. Unlike every other
        pass the raw material is not clusters/memories but the small set of
        active BASE (non-meta) concepts for ``spec.subject``: the ``user`` /
        ``aiko`` lenses read that subject's own actives, while ``relationship``
        pairs across both subjects for a cross-subject value clash. The proposer
        names two of those concepts held in friction and cites them as
        ``("concept", id)`` evidence.

        Offering only non-meta actives is what keeps the meta depth cap (no
        meta-of-meta) true by construction, and reading ``status="active"`` is
        the dependency-ordering guarantee (a tension can only be built on
        promoted bases). Dirty-tracked on a fingerprint of the offered pool (ids
        + rounded confidence + live/quiet hint) so a settled graph is a fast
        no-op, but a base going quiet or shifting confidence re-fires the pass.
        Gated by ``agent.tension_synthesis_enabled``."""
        if not bool(
            getattr(self._agent_settings, "tension_synthesis_enabled", True)
        ):
            return []
        subject = spec.subject
        cap = self._max_tension_concepts
        if subject == "relationship":
            half = max(2, cap // 2)
            pool = (
                self._active_tension_bases("user", half)
                + self._active_tension_bases("aiko", half)
            )
        else:
            pool = self._active_tension_bases(subject, cap)

        # A tension needs at least two concepts to hold in friction; for the
        # relationship lens it needs at least one of each subject. Leave the
        # shared ``tension_dirty`` flag untouched on an early-out (the three
        # tension specs share it, so it accumulates via OR below).
        if len(pool) < 2:
            return []
        if subject == "relationship" and not (
            any(b.subject == "user" for b in pool)
            and any(b.subject == "aiko" for b in pool)
        ):
            return []

        sig_key = spec.sig_key or ("concept_synth.tension_sig." + subject)
        prev = self._load_sigs(sig_key)
        fingerprint = self._tension_fingerprint(pool)
        prev_fp = str(prev.get("fingerprint", "")) if prev else ""
        is_dirty = force or fingerprint != prev_fp
        # OR-accumulate: user / relationship / aiko all share the flag, so a
        # later empty lens must not clear an earlier dirty one.
        stats["tension_dirty"] = bool(stats.get("tension_dirty")) or bool(
            is_dirty
        )
        if not is_dirty:
            return []

        proposals = spec.propose(
            ctx,
            concepts=pool,
            existing=self._existing_for(spec),
        )
        self._save_sigs(
            sig_key, {"fingerprint": fingerprint, "count": len(pool)}
        )
        return proposals

    def _run_generalization_pass(
        self,
        ctx: ProposerContext,
        spec: ProposerSpec,
        stats: dict[str, Any],
        force: bool = False,
    ) -> list[CandidateProposal]:
        """L20 generalization pass -- the abstraction *meta* proposer. Like the
        tension pass its raw material is the active BASE (non-meta) concepts for
        ``spec.subject`` (``user`` / ``aiko`` only -- an abstraction is over one
        subject's own concepts, never cross-subject), and the proposer names a
        higher-order super-concept 2+ of them are facets of, cited as
        ``("concept", id)`` evidence.

        Offering only non-meta actives keeps the meta depth cap (no
        abstraction-of-abstraction) true by construction; reading
        ``status="active"`` is the dependency-ordering guarantee. Dirty-tracked
        on the same base-pool fingerprint as tension (ids + rounded confidence +
        live/quiet hint) so a settled graph is a fast no-op. Gated by
        ``agent.generalization_synthesis_enabled``."""
        if not bool(
            getattr(
                self._agent_settings, "generalization_synthesis_enabled", True
            )
        ):
            return []
        subject = spec.subject
        pool = self._active_tension_bases(
            subject, self._max_generalization_concepts
        )
        # An abstraction needs at least two concepts to generalise over.
        if len(pool) < 2:
            return []

        sig_key = spec.sig_key or (
            "concept_synth.generalization_sig." + subject
        )
        prev = self._load_sigs(sig_key)
        fingerprint = self._tension_fingerprint(pool)
        prev_fp = str(prev.get("fingerprint", "")) if prev else ""
        is_dirty = force or fingerprint != prev_fp
        stats["generalization_dirty"] = bool(
            stats.get("generalization_dirty")
        ) or bool(is_dirty)
        if not is_dirty:
            return []

        proposals = spec.propose(
            ctx,
            concepts=pool,
            existing=self._existing_for(spec),
        )
        self._save_sigs(
            sig_key, {"fingerprint": fingerprint, "count": len(pool)}
        )
        return proposals

    def _active_tension_bases(
        self, subject: str, limit: int
    ) -> list[TensionBase]:
        """The active, non-meta concepts for ``subject``, highest-confidence
        first and capped at ``limit``, rendered as :class:`TensionBase` rows for
        the tension proposer. Excluding ``evidence_model=="meta"`` is the meta
        depth cap (a tension can never reference another tension)."""
        try:
            rows = [
                c
                for c in self._concept_store.list_by(
                    status="active", subject=subject
                )
                if c.evidence_model != "meta"
            ]
        except Exception:
            log.debug("tension base list failed (%s)", subject, exc_info=True)
            return []
        rows.sort(key=lambda c: float(c.confidence), reverse=True)
        rows = rows[: max(2, int(limit))]
        now = _parse_iso(_now_iso())
        return [
            TensionBase(
                id=int(c.concept_id),
                subject=str(c.subject),
                kind=str(c.kind),
                label=str(c.label),
                rationale=str(c.rationale or ""),
                confidence=float(c.confidence),
                hint=self._activity_hint(c, now),
            )
            for c in rows
        ]

    @staticmethod
    def _activity_hint(concept: Concept, now: "datetime | None") -> str:
        """A coarse 'live vs quiet' hint from ``last_reinforced_at`` -- the L4
        'one pattern hot while a normally-paired one has gone dormant' signal in
        cheap form. Empty when it can't be dated or sits in the middle band."""
        ts = _parse_iso(getattr(concept, "last_reinforced_at", None) or "")
        if ts is None or now is None:
            return ""
        days = (now - ts).total_seconds() / 86400.0
        if days <= 7.0:
            return "live lately"
        if days >= 30.0:
            return "gone quiet lately"
        return ""

    @staticmethod
    def _tension_fingerprint(pool: list[TensionBase]) -> str:
        """Stable hash of the offered base pool (ids + rounded confidence +
        live/quiet hint). Folding the hint in means a concept going quiet -- a
        new tension signal -- re-fires an otherwise-settled pass."""
        key = "|".join(
            f"{b.id}:{round(float(b.confidence), 1)}:{b.hint}"
            for b in sorted(pool, key=lambda b: int(b.id))
        )
        return hashlib.sha1(key.encode("utf-8", "ignore")).hexdigest()[:16]

    def _build_style_digest(self, subject: str) -> str:
        """Compact, persisted read of how the user communicates -- the K13
        style-signal labels + the distilled profile ``communication_style``
        field -- rendered as one short line for the proposer prompt.

        This is *guidance only* (never evidence). For ``subject="user"`` it reads
        as "how {user} writes lately"; for ``subject="aiko"`` the same user read
        is framed as "what he responds to", so Aiko's self-authored style adapts
        to him. Returns ``""`` when the sources are absent (cold install / tests)
        or too thin to have warmed up -- the pass then runs on anchors + clusters
        alone."""
        uid = ""
        try:
            uid = (self._user_id_provider() if self._user_id_provider else "") or ""
        except Exception:
            uid = ""
        if not uid:
            return ""

        parts: list[str] = []

        # K13 style-signal labels from the persisted window (no live analyzer).
        if self._style_signal_store is not None:
            try:
                blob = self._style_signal_store.load(uid)
            except Exception:
                blob = None
            if isinstance(blob, dict):
                try:
                    from app.core.persona.style_signal import StyleSignalAnalyzer

                    analyzer = StyleSignalAnalyzer(
                        agent_settings=self._agent_settings
                    )
                    analyzer.from_dict(blob)
                    sig = analyzer.current_signal()
                    labels = (
                        analyzer.labels_for_signal(sig) if sig else []
                    )
                except Exception:
                    labels = []
                if labels:
                    parts.append("writes: " + ", ".join(labels))

        # Distilled profile communication_style field.
        if self._user_profile_store is not None:
            try:
                entry = self._user_profile_store.fields(uid).get(
                    "communication_style"
                )
            except Exception:
                entry = None
            value = str(getattr(entry, "value", "") or "").strip()
            if value:
                parts.append("noted style: " + value)

        if not parts:
            return ""

        who = "How he writes / what he responds to" if subject == "aiko" else "How the user writes lately"
        return who + " -- " + "; ".join(parts)

    @staticmethod
    def _span_days(mems: list[Any]) -> float:
        """Days between the earliest and latest member's effective timestamp
        (``event_time`` when set, else ``created_at``). ``0`` when it can't be
        parsed. ``mems`` is assumed already in temporal order."""
        def _eff(m: Any) -> str:
            et = getattr(m, "event_time", None)
            if isinstance(et, str) and et.strip():
                return et
            return str(getattr(m, "created_at", "") or "")

        if len(mems) < 2:
            return 0.0
        first = _parse_iso(_eff(mems[0]))
        last = _parse_iso(_eff(mems[-1]))
        if first is None or last is None:
            return 0.0
        return abs((last - first).total_seconds()) / 86400.0

    def _ordered_memories(self, member_ids: "tuple[int, ...] | list[int]") -> list[Any]:
        """Resolve ``member_ids`` to Memory rows in **temporal order** -- by
        ``event_time`` when set, else ``created_at`` (both ISO-8601, so string
        order is chronological). Unresolvable ids are dropped."""
        rows = self._memory_store.get_many([int(m) for m in member_ids])
        mems = list(rows.values())

        def _key(m: Any) -> str:
            et = getattr(m, "event_time", None)
            if isinstance(et, str) and et.strip():
                return et
            return str(getattr(m, "created_at", "") or "")

        mems.sort(key=_key)
        return mems

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

        # Meta depth cap + cycle guard (L12, rule 4), enforced at persist time:
        # a meta concept may reference only EXISTING, non-meta concepts. Drops
        # any ``("concept", id)`` edge whose target vanished (retired between
        # propose and persist) or is itself meta; a tension that loses either
        # side of its pair is no longer a tension, so we reject it.
        if proposal.evidence_model == "meta":
            proposal.evidence = self._filter_meta_evidence(proposal.evidence)
            if len({(t, i) for t, i in proposal.evidence}) < 2:
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
        self._add_evidence_edges(
            cid, proposal.evidence, evidence_model=proposal.evidence_model
        )
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
        self._add_evidence_edges(
            concept.concept_id,
            proposal.evidence,
            evidence_model=concept.evidence_model,
        )
        ev = self._concept_store.evidence_of(concept.concept_id)
        concept.evidence_count = len(ev)
        concept.distinct_source_count = len(
            {(e.src_type, e.src_id) for e in ev}
        )
        concept.last_reinforced_at = _now_iso()
        # confidence / plasticity / status intentionally left to L3.
        self._concept_store.update(concept)

    def _filter_meta_evidence(
        self, evidence: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        """Keep only ``("concept", id)`` edges whose target is a live, non-meta
        concept (plus any non-concept edges untouched). Enforces the L12 meta
        depth cap / cycle guard at persist time: a base that turned out to be
        meta or has vanished is dropped, so a tension can never reference
        another tension or a ghost."""
        kept: list[tuple[str, str]] = []
        for node_type, node_id in evidence:
            if node_type != "concept":
                kept.append((node_type, node_id))
                continue
            try:
                target = self._concept_store.get(int(node_id))
            except (TypeError, ValueError):
                target = None
            if target is None or target.evidence_model == "meta":
                continue
            kept.append((node_type, node_id))
        return kept

    def _add_evidence_edges(
        self,
        concept_id: int,
        evidence: list[tuple[str, str]],
        *,
        evidence_model: str = "set",
    ) -> None:
        # For a ``sequence`` concept (L8 narrative) the ``evidence`` list is the
        # arc in chain order, so we stamp each edge's ``ordinal`` by position
        # (``evidence_of`` returns edges ordered by ordinal). ``set`` kinds are
        # unordered, so ordinal stays ``None``.
        ordered = evidence_model == "sequence"
        for i, (node_type, node_id) in enumerate(evidence):
            self._concept_store.add_edge(
                ConceptEdge(
                    src_type=node_type,
                    src_id=str(node_id),
                    dst_type="concept",
                    dst_id=str(concept_id),
                    relation="evidence",
                    polarity=1,
                    strength=1.0,
                    ordinal=(i if ordered else None),
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
