"""L32 - concept *importance*, the second strength axis (derived, not stored).

A concept has always had one strength axis, ``confidence`` ("how likely is
this true?"), and surfacing ranked on it. That conflates two different
questions. "User likes TypeScript" can be high confidence and low stakes;
"user might be struggling emotionally" can be low confidence and high
stakes -- something to hold gently but weight heavily. Ranking on
confidence alone chatters about certain-but-trivial facts and stays quiet
on uncertain-but-critical ones.

Importance is that missing axis, in ``[0, 1]``. Two properties make it
cheap enough to compute at read time rather than storing it:

- **Derived.** It is a pure function of the concept's ``kind`` and the
  emotional charge of the topic clusters it is grounded in. No column, no
  migration, no writer, and no decay policy of its own -- it moves when
  its inputs move. (That resolves the "stored or derived?" and "what
  lowers importance?" questions in the L32 sketch by dissolving them.)
- **Status-agnostic.** Nothing here reads ``status``, so a ``candidate``
  scores exactly like an ``active`` row with the same inputs. That is what
  lets the L30 hypothesis lane rank by importance with no extra work.

Two inputs, in order of how much they carry:

1. **The per-kind prior** (``ConceptKind.importance``) -- a ``boundary``
   or a ``value`` starts more important than a tooling preference. This
   makes explicit a stakes notion the registry already expressed
   implicitly through ``plasticity_default``, ``core_min_confidence`` and
   the ``protect_downward`` list in ``earned_standing``.
2. **The affect lift** -- how strongly the user tends to feel around the
   topics the concept is grounded in, from the L13 per-cluster affect
   EWMAs. The lift **only ever raises** importance above the prior. Affect
   is sparse (roughly 40% of concepts resolve to no affect-bearing
   cluster), so a symmetric blend would read "no data" as "trivial" and
   penalise the majority. Same convention as ``recency_boost``, where a
   missing timestamp returns the neutral ``1.0`` rather than a penalty.

**Where it may be applied.** The T3 ``relevant_context`` scorer, and any
off-prompt ranking (curiosity, workers). It must **never** reach
``ConceptView._stable_rank``, which feeds the T0 ``profile_block``: that
lane is deliberately ranked on *quantised* confidence with a
``concept_id`` tie-break so per-tick jitter cannot reorder bullets and
break the prompt-cache prefix. A live importance term there would put
that churn straight back.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.concepts.cluster_affect import ClusterAffectState
from app.core.concepts.concept_kinds import get_kind
from app.core.infra import timephrase

log = logging.getLogger("app.concept_importance")


#: The no-opinion point. An importance of exactly this leaves a surfacing
#: score untouched (:func:`importance_factor` returns ``1.0``), so it is
#: both the default for an unregistered kind and the value to pass when
#: the feature is disabled.
IMPORTANCE_NEUTRAL = 0.5


def _c01(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def kind_importance(kind_name: str | None) -> float:
    """The per-kind stakes prior, or :data:`IMPORTANCE_NEUTRAL` for an
    unregistered kind (the ``kind`` axis is an open enum)."""
    kind = get_kind(str(kind_name or ""))
    if kind is None:
        return IMPORTANCE_NEUTRAL
    return _c01(getattr(kind, "importance", IMPORTANCE_NEUTRAL))


def state_charge(state: ClusterAffectState) -> float:
    """One cluster's emotional charge in ``[0, 1]``.

    ``abs(valence) * (0.5 + 0.5 * arousal)``: **valence magnitude is what
    makes a topic matter** -- felt strongly in either direction, loved or
    dreaded -- and arousal only scales how hot it runs. A neutral-valence
    cluster is not high-stakes however energetic it is (``valence=0``
    yields ``0``), while a strongly-felt but quiet topic ("low and
    drained" -- exactly the wellbeing case L32 exists for) keeps half its
    charge rather than being zeroed by a bare ``|v| * a`` product.
    """
    valence = max(-1.0, min(1.0, float(state.valence)))
    arousal = _c01(state.arousal)
    return _c01(abs(valence) * (0.5 + 0.5 * arousal))


def affect_charge(
    states: Iterable[ClusterAffectState],
    *,
    min_samples: int = 3,
    max_age_days: float = 120.0,
    now: datetime | None = None,
) -> float:
    """Reduce a concept's grounded clusters to one ``[0, 1]`` charge.

    ``max`` rather than a mean: a concept touching one charged topic and
    three neutral ones is about the charged one, and averaging would wash
    that out. Clusters below ``min_samples`` or older than
    ``max_age_days`` are skipped -- ``cluster_affect.load_map`` prunes
    only on *write*, so a read can see entries the sweep has not reached
    yet. No qualifying cluster returns ``0.0``, which means "no lift",
    never a penalty.
    """
    moment = now or timephrase.utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    best = 0.0
    for state in states:
        if int(getattr(state, "samples", 0)) < int(min_samples):
            continue
        if _age_days(state, moment) > float(max_age_days):
            continue
        charge = state_charge(state)
        if charge > best:
            best = charge
    return best


def _age_days(state: ClusterAffectState, now: datetime) -> float:
    """Age of an affect entry in days; ``0.0`` when the stamp is junk, so
    an unparseable timestamp reads as fresh rather than silently dropping
    a real signal."""
    raw = str(getattr(state, "updated_at", "") or "")
    if not raw:
        return 0.0
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (now - ts).total_seconds() / 86_400.0)


def blend_importance(prior: float, charge: float, *, lift: float) -> float:
    """Raise ``prior`` toward ``1.0`` in proportion to ``charge``.

    ``prior + (1 - prior) * lift * charge``, clamped. Monotonic in both
    arguments and **never below the prior**, so a concept with no affect
    data keeps its kind's stake instead of being read as trivial. ``lift``
    caps how far the strongest possible charge can carry a concept: at
    ``0.5`` a fully-charged taste (prior ``0.3``) reaches ``0.65``, still
    short of an uncharged boundary.
    """
    base = _c01(prior)
    return _c01(base + (1.0 - base) * _c01(lift) * _c01(charge))


def importance_factor(importance: float, *, strength: float) -> float:
    """Turn an importance into the multiplier ``surface_score`` applies.

    ``1 + strength * (importance - 0.5)``: neutral importance is exactly
    ``1.0`` (no change), and ``strength=0.0`` disables the axis for every
    concept at once. A modulator rather than another sum-normalised term
    on purpose -- a normalised term would dilute cosine and would need a
    weight tuned on all twelve kinds, whereas this tilts the existing
    blend by a single knob and leaves the default behaviour intact.
    """
    return 1.0 + float(strength) * (_c01(importance) - IMPORTANCE_NEUTRAL)


@dataclass(frozen=True, slots=True)
class ImportanceDetail:
    """The importance of one concept with its inputs kept separable, so
    the debug surfaces can show *why* rather than one opaque number."""

    importance: float
    prior: float
    charge: float
    #: How many affect-bearing clusters the concept resolved to. ``0``
    #: means the value is the bare kind prior.
    clusters: int


def memory_ids_from_edges(edges: Iterable[object]) -> tuple[int, ...]:
    """The memory ids behind a concept's ``cluster`` evidence edges.

    A cluster evidence edge stores the cluster's **representative memory
    id** in ``src_id``, not a ``cluster_id`` -- the two id spaces are
    unrelated and overlap only by accident. Resolving one to a cluster is
    :class:`ImportanceContext`'s job; this only pulls the raw ids out of
    whatever edge list the caller already had in hand.
    """
    out: list[int] = []
    for edge in edges:
        if str(getattr(edge, "src_type", "")) != "cluster":
            continue
        if str(getattr(edge, "relation", "")) != "evidence":
            continue
        try:
            out.append(int(getattr(edge, "src_id")))
        except (TypeError, ValueError):
            continue
    return tuple(out)


class ImportanceContext:
    """Everything needed to score importance for a batch of concepts
    without further I/O.

    Built once per turn (or per API page) and passed down, so the L30
    hypothesis lane -- which ranks in its own lane rather than through
    ``surface_score`` -- can call :meth:`for_concept` directly instead of
    re-deriving the affect join.

    ``cluster_by_memory`` bridges the two id spaces: a cluster evidence
    edge names a representative *memory*, while the affect maps are keyed
    by ``cluster_id``. Build it from the live topic graph's cluster
    membership, which is the current truth about where a memory sits --
    an edge written months ago may name a memory whose cluster has since
    been rebuilt, and following the memory is what keeps that edge useful.
    """

    __slots__ = (
        "_affect_aiko",
        "_affect_user",
        "_cache",
        "_cluster_by_memory",
        "_lift",
        "_max_age_days",
        "_memory_ids",
        "_min_samples",
        "_now",
    )

    def __init__(
        self,
        *,
        affect_user: Mapping[str, ClusterAffectState] | None = None,
        affect_aiko: Mapping[str, ClusterAffectState] | None = None,
        cluster_by_memory: Mapping[int, int] | None = None,
        memory_ids_by_concept: Mapping[int, Sequence[int]] | None = None,
        lift: float = 0.5,
        min_samples: int = 3,
        max_age_days: float = 120.0,
        now: datetime | None = None,
    ) -> None:
        self._affect_user = dict(affect_user or {})
        self._affect_aiko = dict(affect_aiko or {})
        self._cluster_by_memory = dict(cluster_by_memory or {})
        self._memory_ids = dict(memory_ids_by_concept or {})
        self._lift = float(lift)
        self._min_samples = int(min_samples)
        self._max_age_days = float(max_age_days)
        self._now = now or timephrase.utcnow()
        self._cache: dict[int, ImportanceDetail] = {}

    def for_concept(self, concept: object) -> float:
        """This concept's importance in ``[0, 1]``."""
        return self.detail(concept).importance

    def detail(self, concept: object) -> ImportanceDetail:
        """Importance plus the prior and charge that produced it."""
        cid = int(getattr(concept, "concept_id", 0) or 0)
        cached = self._cache.get(cid) if cid else None
        if cached is not None:
            return cached
        prior = kind_importance(getattr(concept, "kind", ""))
        states = self._states_for(
            str(getattr(concept, "subject", "user") or "user"),
            self._memory_ids.get(cid, ()),
        )
        charge = affect_charge(
            states,
            min_samples=self._min_samples,
            max_age_days=self._max_age_days,
            now=self._now,
        )
        out = ImportanceDetail(
            importance=blend_importance(prior, charge, lift=self._lift),
            prior=prior,
            charge=charge,
            clusters=len(states),
        )
        if cid:
            self._cache[cid] = out
        return out

    def _states_for(
        self, subject: str, memory_ids: Sequence[int]
    ) -> list[ClusterAffectState]:
        """The affect rows for the clusters this concept is grounded in.

        Deduped by cluster: several evidence edges commonly resolve to the
        same cluster, and ``affect_charge`` takes a max, so duplicates are
        harmless but wasteful.
        """
        if not memory_ids:
            return []
        affect = (
            self._affect_aiko if subject == "aiko" else self._affect_user
        )
        if not affect:
            return []
        seen: set[int] = set()
        out: list[ClusterAffectState] = []
        for mid in memory_ids:
            cluster_id = self._cluster_by_memory.get(int(mid))
            if cluster_id is None or cluster_id in seen:
                continue
            seen.add(cluster_id)
            state = affect.get(str(cluster_id))
            if state is not None:
                out.append(state)
        return out


def membership_from_clusters(clusters: Iterable[object]) -> dict[int, int]:
    """A ``memory_id -> cluster_id`` map from already-read cluster rows.

    In-memory: a ``TopicCluster`` already carries its full member list, so
    the bridge needs no query of its own. Takes rows rather than the graph
    so the surfacing path -- which reads ``topic_clusters()`` anyway to
    bridge hot clusters for spreading activation -- can share the one read.
    """
    out: dict[int, int] = {}
    for cluster in clusters:
        try:
            cid = int(cluster.cluster_id)  # type: ignore[attr-defined]
            for mid in cluster.member_ids:  # type: ignore[attr-defined]
                out[int(mid)] = cid
        except Exception:
            continue
    return out


def cluster_membership(topic_graph: object) -> dict[int, int]:
    """:func:`membership_from_clusters` straight off a topic graph.

    Best-effort -- an empty map simply means no concept gets an affect
    lift, which is a missing lift rather than a penalty.
    """
    if topic_graph is None:
        return {}
    try:
        clusters = topic_graph.topic_clusters()  # type: ignore[attr-defined]
    except Exception:
        log.debug("importance: cluster membership read failed", exc_info=True)
        return {}
    return membership_from_clusters(clusters)


__all__ = [
    "IMPORTANCE_NEUTRAL",
    "ImportanceContext",
    "ImportanceDetail",
    "affect_charge",
    "blend_importance",
    "cluster_membership",
    "importance_factor",
    "kind_importance",
    "membership_from_clusters",
    "memory_ids_from_edges",
    "state_charge",
]
