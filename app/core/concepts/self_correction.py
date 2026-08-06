"""L17d: clustering Aiko's own corrections into a pattern (pure core).

L17c records each time she came to believe something else, and why. This
module answers the next question up: do those corrections *rhyme*? Three
separate times she over-committed to a first read and had to walk it back
is not three facts about three beliefs -- it is one fact about how she
works, and it is the only kind of fact that can change her behaviour
rather than her content.

The clustering is single-link cosine over the ``because`` clauses (the
same shape as :mod:`app.core.concepts.ritual_grouping`), because the
``because`` is the causal sentence the classifier wrote about *why* the
belief moved. Two corrections land in the same cluster when their reasons
read alike, not when their subjects do.

Two gates make the difference between a pattern and noise, and both are
here rather than in the prompt:

- **Distinct beliefs, not distinct events.** The floor counts distinct
  ``prior_concept_id``s. Three corrections to the *same* belief is her
  wobbling on one thing; the same reason arriving from three different
  beliefs is a habit. This is the gate that stops one oscillating concept
  from manufacturing a rule about her character.
- **Spread over time.** A cluster confined to one afternoon is one
  conversation's mood. ``min_span_days`` requires the corrections to have
  happened far enough apart to be a tendency.

Pure + dependency-light (numpy only, for the cosine): no store, settings
or LLM imports, so it unit-tests in isolation. The worker does the I/O and
hands in :class:`CorrectionInput` rows.
"""
from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

import numpy as np

# Defaults; the worker overrides all of them from settings.
_DEFAULT_SIMILARITY = 0.55
_DEFAULT_MIN_BELIEFS = 3
_DEFAULT_MIN_SPAN_DAYS = 7.0

# How much of a ``because`` clause to keep on a member line for the prompt.
_MAX_TEXT_CHARS = 220


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _trim(text: str) -> str:
    text = " ".join((text or "").split())
    if len(text) > _MAX_TEXT_CHARS:
        return text[: _MAX_TEXT_CHARS - 1].rstrip() + "\u2026"
    return text


@dataclass(slots=True)
class CorrectionInput:
    """One L17c learning event, normalised for clustering.

    ``embedding`` is a vector over the ``because`` clause (the worker
    embeds it; the text itself is what carries the pattern). Kept separate
    from :class:`LearningEvent` so the clustering can be unit-tested
    without the store or an embedder.
    """

    event_id: int
    embedding: Any
    because: str
    prior_concept_id: int
    kind: str = ""
    shape: str = ""
    old_label: str = ""
    new_label: str = ""
    at: str = ""
    salience: float = 0.0


@dataclass(frozen=True, slots=True)
class CorrectionLite:
    """A cluster member, trimmed for the proposer prompt."""

    event_id: int
    prior_concept_id: int
    because: str
    old_label: str
    new_label: str
    at: str


@dataclass(frozen=True, slots=True)
class CorrectionCluster:
    """A group of corrections that happened for the same sort of reason."""

    key: str
    members: tuple[CorrectionLite, ...]
    # Distinct prior-concept ids, which become the ``("concept", id)``
    # evidence edges: the beliefs this pattern was learned from.
    concept_ids: tuple[int, ...]
    span_days: float
    kinds: tuple[str, ...]
    shapes: tuple[str, ...]
    salience_max: float

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def belief_count(self) -> int:
        return len(self.concept_ids)


def _normalise(vec: np.ndarray) -> np.ndarray:
    v = np.asarray(vec, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(v))
    if norm <= 0.0:
        return v
    return v / norm


def _cluster_key(concept_ids: Sequence[int]) -> str:
    """A stable id for a cluster, derived from the beliefs it covers.

    Keyed on the beliefs rather than the events so the same pattern picking
    up one more corroborating correction is recognisably the same pattern
    to the cooldown, while a genuinely different set of beliefs is a
    different key.
    """
    joined = ",".join(str(int(cid)) for cid in sorted(set(concept_ids)))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


def _span_days(moments: Sequence[datetime]) -> float:
    if len(moments) < 2:
        return 0.0
    return (max(moments) - min(moments)).total_seconds() / 86400.0


def cluster_corrections(
    rows: Sequence[CorrectionInput],
    *,
    min_beliefs: int = _DEFAULT_MIN_BELIEFS,
    min_span_days: float = _DEFAULT_MIN_SPAN_DAYS,
    similarity: float = _DEFAULT_SIMILARITY,
    max_clusters: int = 2,
) -> list[CorrectionCluster]:
    """Single-link cosine grouping of corrections into candidate patterns.

    Two corrections link when their ``because`` embeddings clear
    ``similarity``; a cluster is a connected component of that graph that
    covers at least ``min_beliefs`` *distinct* prior beliefs and spans at
    least ``min_span_days``. Returned best-first (most beliefs, then widest
    span), capped at ``max_clusters`` -- a run that proposes several rules
    about her own conduct at once is not learning, it is a rewrite.
    """
    usable = [
        row
        for row in rows
        if row is not None
        and int(row.prior_concept_id or 0) > 0
        and str(row.because or "").strip()
        and row.embedding is not None
    ]
    n = len(usable)
    min_beliefs = max(2, int(min_beliefs))
    if n < min_beliefs:
        return []
    thr = max(-1.0, min(1.0, float(similarity)))

    mat = np.vstack([_normalise(row.embedding) for row in usable])
    mat = mat.astype(np.float32)
    if mat.shape[1] == 0:
        return []
    sims = mat @ mat.T

    parent = list(range(n))

    def _find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if float(sims[i, j]) >= thr:
                _union(i, j)

    comps: dict[int, list[int]] = {}
    for idx in range(n):
        comps.setdefault(_find(idx), []).append(idx)

    clusters: list[CorrectionCluster] = []
    for member_idx in comps.values():
        members = [usable[k] for k in member_idx]
        concept_ids = sorted({int(m.prior_concept_id) for m in members})
        if len(concept_ids) < min_beliefs:
            continue
        moments = [m for m in (_parse(row.at) for row in members) if m]
        span = _span_days(moments)
        if span < float(min_span_days):
            continue
        # Most salient first: the correction that mattered most is the one
        # the prompt should lead with.
        members.sort(key=lambda r: (-float(r.salience), int(r.event_id)))
        clusters.append(
            CorrectionCluster(
                key=_cluster_key(concept_ids),
                members=tuple(
                    CorrectionLite(
                        event_id=int(row.event_id),
                        prior_concept_id=int(row.prior_concept_id),
                        because=_trim(row.because),
                        old_label=_trim(row.old_label),
                        new_label=_trim(row.new_label),
                        at=str(row.at or ""),
                    )
                    for row in members
                ),
                concept_ids=tuple(concept_ids),
                span_days=round(span, 1),
                kinds=tuple(
                    kind
                    for kind, _count in Counter(
                        m.kind for m in members if m.kind
                    ).most_common()
                ),
                shapes=tuple(
                    shape
                    for shape, _count in Counter(
                        m.shape for m in members if m.shape
                    ).most_common()
                ),
                salience_max=round(
                    max((float(m.salience) for m in members), default=0.0), 4
                ),
            )
        )

    clusters.sort(key=lambda c: (-c.belief_count, -c.span_days, c.key))
    return clusters[: max(1, int(max_clusters))]


__all__ = [
    "CorrectionCluster",
    "CorrectionInput",
    "CorrectionLite",
    "cluster_corrections",
]
