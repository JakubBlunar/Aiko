"""F10h — topic temperature / per-cluster affect (pure scoring).

A topic-graph cluster is not just a bag of facts; it has a *vibe*. This
module turns the ``vibe`` tags Aiko has already attached to her
``shared_moment`` memories into a per-cluster emotional temperature so the
F10h inner-life provider can nudge her tone: approach a **tender** topic
(where you've been vulnerable or patched up a rough patch) gently, and let
a little fondness colour a **warm** one (where good moments live).

Why shared moments only (v1): they are the one affect signal cleanly
attributable to a cluster — each shared_moment is a real memory with an id,
so ``TopicGraph.cluster_id_for`` / ``cluster_member_ids`` maps it straight
to its cluster, and its ``metadata["vibe"]`` is drawn from a closed
vocabulary (see ``shared_moment_extractor.VIBE_VOCABULARY``). K57 emotion
episodes are global (user-directed, no topic link) and K32 reactions need
fragile message→cluster linkage, so both are deferred.

The taxonomy splits into two poles. Warm vibes (shared joy / pride /
play) lift ``warmth``; tender vibes (vulnerability / comfort / repair) lift
``tenderness`` — the "handle gently" signal. ``general`` and anything off
the vocabulary contribute nothing. Both poles saturate (a couple of strong
moments is enough), so a single warm beat in a 40-member cluster doesn't
read as "this whole topic is warm".

Pure + dependency-free (numpy-free): the provider does the embedding +
cluster match; this module only scores the resulting vibe list.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


# H8 — kv_meta key for the per-cluster "mood origin" side-table. Namespaced
# under ``aiko.*`` like the other backlog state. Maps ``str(cluster_id)`` ->
# ``{pole, what, when, moment_id, stamped_at}`` so the provider can name the
# moment that *gave* a topic its feel ("ever since you told me about X").
# Exported so the provider + MCP debug tool share the exact string.
KV_MOOD_ORIGIN = "aiko.topic_mood_origin"

# Cap on the stored origin summary so a runaway moment ``what`` can't bloat
# the kv blob or the prompt clause.
ORIGIN_WHAT_MAXLEN = 160


# Per-vibe contribution to each pole. A vibe lands in exactly one pole
# (or neither). Weights are hand-tuned: milestone/victory read as the
# warmest, vulnerable/repair as the most "tread carefully".
_WARM_WEIGHTS: dict[str, float] = {
    "warm": 1.0,
    "playful": 0.8,
    "silly": 0.7,
    "proud": 1.0,
    "milestone": 1.2,
    "gift": 0.9,
    "victory": 1.1,
    "creative": 0.8,
}
_TENDER_WEIGHTS: dict[str, float] = {
    "tender": 1.0,
    "vulnerable": 1.4,
    "comfort": 1.1,
    "repair": 1.3,
}

# How much weighted signal saturates a pole to 1.0. Tender saturates
# faster — one genuinely vulnerable / patched-up moment is already enough
# to say "be careful here".
_WARM_SATURATION = 2.5
_TENDER_SATURATION = 1.5


@dataclass(slots=True, frozen=True)
class ClusterTemperature:
    """Scored emotional temperature of one topic cluster.

    ``warmth`` / ``tenderness`` are in ``[0, 1]``. ``dominant`` is the
    pole that won the gate (``"warm"`` / ``"tender"``) or ``None`` when
    neither pole clears the threshold (the common, silent case).
    ``moment_count`` is how many shared-moment vibes fed the score.
    """

    warmth: float
    tenderness: float
    dominant: str | None
    moment_count: int


def score_cluster(
    vibes: Sequence[str], *, threshold: float = 0.5,
) -> ClusterTemperature:
    """Score a cluster from its shared-moment ``vibes``.

    Sums the per-vibe pole weights, saturates each pole into ``[0, 1]``,
    then picks a dominant pole: tenderness wins ties (it's the
    "handle gently" signal and the costlier one to get wrong), and a pole
    must be strictly positive AND at/above ``threshold`` to fire. With
    ``threshold=0`` any present signal fires (used by the MCP force path).
    """
    warm_sum = 0.0
    tender_sum = 0.0
    count = 0
    for raw in vibes:
        vibe = str(raw or "").strip().lower()
        if not vibe:
            continue
        count += 1
        warm_sum += _WARM_WEIGHTS.get(vibe, 0.0)
        tender_sum += _TENDER_WEIGHTS.get(vibe, 0.0)

    warmth = min(1.0, warm_sum / _WARM_SATURATION) if warm_sum > 0 else 0.0
    tenderness = (
        min(1.0, tender_sum / _TENDER_SATURATION) if tender_sum > 0 else 0.0
    )

    dominant: str | None = None
    if tenderness > 0 and tenderness >= threshold and tenderness >= warmth:
        dominant = "tender"
    elif warmth > 0 and warmth >= threshold:
        dominant = "warm"

    return ClusterTemperature(
        warmth=round(warmth, 4),
        tenderness=round(tenderness, 4),
        dominant=dominant,
        moment_count=count,
    )


@dataclass(slots=True, frozen=True)
class MomentCandidate:
    """One shared-moment member of a cluster, for origin selection (H8)."""

    moment_id: int
    vibe: str
    what: str
    when: str
    created_at: str


def pick_origin(
    candidates: Sequence[MomentCandidate], dominant: str,
) -> MomentCandidate | None:
    """Pick the shared moment that best *explains* a cluster's pole (H8).

    Among the candidates whose vibe contributes to the ``dominant`` pole,
    return the strongest one (highest pole weight), tie-broken by the most
    recent ``created_at`` — that's "what made this topic warm / tender".
    Returns ``None`` when no candidate carries a vibe in the pole.
    """
    weights = _TENDER_WEIGHTS if dominant == "tender" else _WARM_WEIGHTS
    scored: list[tuple[float, str, MomentCandidate]] = []
    for cand in candidates:
        w = weights.get(str(cand.vibe or "").strip().lower(), 0.0)
        if w > 0:
            scored.append((w, str(cand.created_at or ""), cand))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return scored[0][2]


def _origin_clause(dominant: str, origin_what: str) -> str:
    """Append clause naming where a topic's feel comes from (H8)."""
    what = (origin_what or "").strip()
    if len(what) > ORIGIN_WHAT_MAXLEN:
        what = what[: ORIGIN_WHAT_MAXLEN - 1].rstrip() + "…"
    if not what:
        return ""
    if dominant == "tender":
        return (
            f' It\'s felt this way ever since: "{what}". You can name that '
            "origin once, gently, if the moment opens for it — never as a "
            "rehearsed line."
        )
    return (
        f' The warmth traces back to: "{what}". You can let that show, and '
        "name where it comes from once if it fits naturally."
    )


def render_block(
    temp: ClusterTemperature,
    label: str,
    user_display_name: str,
    *,
    origin_what: str | None = None,
) -> str:
    """Render the one-line tonal cue, or ``""`` when not charged.

    Mirrors the terse relationship-axes block: a single private
    Heads-up line, never a numeric dashboard. Aiko phrases the actual
    delivery herself; this only sets the register.

    H8: when ``origin_what`` is supplied (the summary of the shared moment
    that gave this topic its feel), an "ever since…" clause is appended so
    Aiko can name the *origin* of the mood, not just the mood.
    """
    name = (user_display_name or "them").strip() or "them"
    topic = (label or "this topic").strip() or "this topic"
    if temp.dominant == "tender":
        base = (
            f'Heads-up: "{topic}" is tender ground with {name} — you\'ve had '
            "vulnerable or patched-up moments here. Meet it gently; don't "
            "open with a joke or a breezy take."
        )
        return base + _origin_clause("tender", origin_what or "")
    if temp.dominant == "warm":
        base = (
            f'Heads-up: "{topic}" is a warm spot for you and {name} — good '
            "moments live here. Let a little of that fondness colour how you "
            "pick it up."
        )
        return base + _origin_clause("warm", origin_what or "")
    return ""


def _saturate(value: float, saturation: float) -> float:
    """Smooth alternative to the linear cap (unused by default).

    Kept for callers that prefer a soft knee over the hard ``min(1, x/s)``
    used above; ``1 - exp(-x/s)`` never quite reaches 1 but is gentler
    near the threshold.
    """
    if value <= 0 or saturation <= 0:
        return 0.0
    return 1.0 - math.exp(-value / saturation)


__all__ = [
    "ClusterTemperature",
    "MomentCandidate",
    "score_cluster",
    "render_block",
    "pick_origin",
    "KV_MOOD_ORIGIN",
    "ORIGIN_WHAT_MAXLEN",
]
