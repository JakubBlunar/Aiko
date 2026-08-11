"""L13 - per-cluster affect maps (pure estimator + bucketing).

Affective concepts (topic -> durable emotion) need a signal that today is
barely persisted: user affect is a per-turn ephemeral K37 estimate, Aiko's
is the scalar ``AffectState``, and neither is attributed to a topic. This
module is the light, pure core of a rolling per-topic-cluster affect
aggregate that fixes that.

Two maps are kept (one per *subject*): ``concept.cluster_affect.user``
tracks how the **user** tends to feel around a topic cluster, and
``concept.cluster_affect.aiko`` tracks how **Aiko** tends to feel around it
(the "topics that move her" half of L13). Both are
``cluster_id -> ClusterAffectState`` maps persisted in ``kv_meta`` (same
shape + discipline as K75 ``user_expertise``): the post-turn
:class:`AffectClusterSampler` folds the live turn's affect into the active
cluster's EWMA, and the L2 ``_run_affect_pass`` reads the map back (joining
``cluster_id -> representative_id`` at synthesis time) to annotate clusters
for the affective proposers.

Pure + dependency-free: the sampler does the embedding + cluster match;
this module only blends samples and buckets the result into words.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from app.core.infra import timephrase


log = logging.getLogger("app.cluster_affect")


# kv_meta keys, one map per subject. Namespaced under ``concept.*`` (the L1-L3
# concept layer owns them), not ``aiko.*``.
KV_CLUSTER_AFFECT_USER = "concept.cluster_affect.user"
KV_CLUSTER_AFFECT_AIKO = "concept.cluster_affect.aiko"


def kv_key_for(subject: str) -> str:
    """Map a concept subject to its per-cluster affect kv key."""
    return (
        KV_CLUSTER_AFFECT_AIKO
        if str(subject) == "aiko"
        else KV_CLUSTER_AFFECT_USER
    )


@dataclass(slots=True, frozen=True)
class ClusterAffectState:
    """Running affect estimate for one topic cluster (one subject).

    ``valence`` is an EWMA in ``[-1, 1]`` and ``arousal`` an EWMA in
    ``[0, 1]`` (matching the ``AffectState`` scales). ``samples`` is how
    many affect-bearing turns have been folded in; ``updated_at`` is ISO-8601
    for the age-out sweep.

    ``valence_samples`` counts only the turns that actually carried a
    *valence* read. The two differ because most turns are readable on one
    axis only: message length gives arousal on almost every turn, while
    valence needs a mood word or a venting act. Counting them together let
    a cluster clear the annotation floor on arousal evidence alone and then
    be described with a valence nobody measured.
    """

    valence: float
    arousal: float
    samples: int
    updated_at: str
    valence_samples: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "valence": round(float(self.valence), 4),
            "arousal": round(float(self.arousal), 4),
            "samples": int(self.samples),
            "valence_samples": int(self.valence_samples),
            "updated_at": self.updated_at,
        }


def update_state(
    prev: ClusterAffectState | None,
    valence: "float | None",
    arousal: "float | None",
    *,
    learning_rate: float = 0.2,
    now_iso: str,
) -> ClusterAffectState:
    """Blend one affect sample into the per-cluster EWMA, per axis.

    ``None`` on an axis means *this turn said nothing about it*: the stored
    value carries forward untouched rather than being pulled toward a
    neutral default. Folding an unread axis as "neutral" is not a small
    inaccuracy — it is the majority case, and it flattens the map toward
    the population mean, which is exactly the signal the map exists to
    depart from.

    An axis with no samples yet has nothing to blend against, so the first
    real read seeds it. That also lets a map written under the old
    fold-everything behaviour heal: those rows carry
    ``valence_samples == 0``, so their first measured valence replaces the
    smeared value instead of averaging with it.
    """
    lr = max(0.01, min(1.0, float(learning_rate)))
    v = None if valence is None else max(-1.0, min(1.0, float(valence)))
    a = None if arousal is None else max(0.0, min(1.0, float(arousal)))
    if prev is None:
        return ClusterAffectState(
            valence=0.0 if v is None else v,
            arousal=0.4 if a is None else a,
            samples=1,
            updated_at=now_iso,
            valence_samples=0 if v is None else 1,
        )
    if v is None:
        new_v = float(prev.valence)
    elif int(prev.valence_samples) <= 0:
        new_v = v
    else:
        new_v = max(-1.0, min(1.0, (1.0 - lr) * float(prev.valence) + lr * v))
    new_a = (
        float(prev.arousal)
        if a is None
        else max(0.0, min(1.0, (1.0 - lr) * float(prev.arousal) + lr * a))
    )
    return ClusterAffectState(
        valence=new_v,
        arousal=new_a,
        samples=int(prev.samples) + 1,
        updated_at=now_iso,
        valence_samples=int(prev.valence_samples) + (0 if v is None else 1),
    )


# ── bucketing (valence x arousal -> stable key + human phrase) ───────────

_VAL_POS = 0.2
_VAL_NEG = -0.2
_AR_HIGH = 0.6
_AR_LOW = 0.35


def affect_bucket(valence: float, arousal: float) -> tuple[str, str]:
    """Coarse ``(valence_band, arousal_band)`` key, stable for dirty-tracking.

    ``valence_band`` in ``{pos, neu, neg}``; ``arousal_band`` in
    ``{high, mid, low}``. A concept is only re-proposed when a cluster's
    bucket shifts (or samples accrue), not on every tiny EWMA wobble."""
    if valence >= _VAL_POS:
        vb = "pos"
    elif valence <= _VAL_NEG:
        vb = "neg"
    else:
        vb = "neu"
    if arousal >= _AR_HIGH:
        ab = "high"
    elif arousal <= _AR_LOW:
        ab = "low"
    else:
        ab = "mid"
    return vb, ab


_PHRASES: dict[tuple[str, str], str] = {
    ("pos", "high"): "energizing and upbeat",
    ("pos", "mid"): "warm and positive",
    ("pos", "low"): "calm and content",
    ("neu", "high"): "keyed-up but neutral",
    ("neu", "mid"): "neutral",
    ("neu", "low"): "quiet and flat",
    ("neg", "high"): "tense and agitated",
    ("neg", "mid"): "downbeat and heavy",
    ("neg", "low"): "low and drained",
}


def affect_phrase(valence: float, arousal: float) -> str:
    """Human phrase for a ``(valence, arousal)`` point, for the proposer
    prompt annotation ("this topic tends to feel <phrase>")."""
    return _PHRASES.get(affect_bucket(valence, arousal), "neutral")


# ── kv map helpers ──────────────────────────────────────────────────────


def load_map(
    kv_get: Callable[[str], "str | None"], key: str
) -> dict[str, ClusterAffectState]:
    """Return the persisted ``cluster_id -> ClusterAffectState`` map."""
    try:
        raw = kv_get(key)
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        blob = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(blob, dict):
        return {}
    out: dict[str, ClusterAffectState] = {}
    for cid, row in blob.items():
        if not isinstance(row, dict):
            continue
        try:
            out[str(cid)] = ClusterAffectState(
                valence=float(row.get("valence", 0.0)),
                arousal=float(row.get("arousal", 0.4)),
                samples=int(row.get("samples", 0)),
                updated_at=str(row.get("updated_at", "")),
                # Absent on rows written before the split. Reading it as 0
                # rather than as ``samples`` is deliberate: those valences
                # were folded from unread axes, so they have not earned the
                # annotation floor and should re-earn it.
                valence_samples=int(row.get("valence_samples", 0)),
            )
        except Exception:
            continue
    return out


def save_map(
    kv_set: Callable[[str, str], None],
    key: str,
    state_map: dict[str, ClusterAffectState],
    *,
    cap: int = 200,
    max_age_days: float = 120.0,
) -> None:
    """Persist the map (best-effort), bounding growth: drop stale entries
    older than ``max_age_days`` then keep the ``cap`` most-recently-updated."""
    try:
        pruned = _prune(state_map, cap=cap, max_age_days=max_age_days)
        payload = {cid: st.to_dict() for cid, st in pruned.items()}
        kv_set(key, json.dumps(payload))
    except Exception:
        log.debug("cluster_affect store write failed", exc_info=True)


def _prune(
    state_map: dict[str, ClusterAffectState],
    *,
    cap: int,
    max_age_days: float,
) -> dict[str, ClusterAffectState]:
    if not state_map:
        return state_map
    now = timephrase.utcnow()

    def _age_days(st: ClusterAffectState) -> float:
        try:
            ts = datetime.fromisoformat(st.updated_at)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            return (now - ts).total_seconds() / 86400.0
        except Exception:
            return 0.0

    fresh = {
        cid: st
        for cid, st in state_map.items()
        if _age_days(st) <= float(max_age_days)
    }
    if len(fresh) <= int(cap):
        return fresh
    ranked = sorted(
        fresh.items(), key=lambda kv: kv[1].updated_at, reverse=True
    )
    return dict(ranked[: int(cap)])


__all__ = [
    "KV_CLUSTER_AFFECT_AIKO",
    "KV_CLUSTER_AFFECT_USER",
    "ClusterAffectState",
    "affect_bucket",
    "affect_phrase",
    "kv_key_for",
    "load_map",
    "save_map",
    "update_state",
]
