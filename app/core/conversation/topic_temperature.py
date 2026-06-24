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


def render_block(
    temp: ClusterTemperature, label: str, user_display_name: str,
) -> str:
    """Render the one-line tonal cue, or ``""`` when not charged.

    Mirrors the terse relationship-axes block: a single private
    Heads-up line, never a numeric dashboard. Aiko phrases the actual
    delivery herself; this only sets the register.
    """
    name = (user_display_name or "them").strip() or "them"
    topic = (label or "this topic").strip() or "this topic"
    if temp.dominant == "tender":
        return (
            f'Heads-up: "{topic}" is tender ground with {name} — you\'ve had '
            "vulnerable or patched-up moments here. Meet it gently; don't "
            "open with a joke or a breezy take."
        )
    if temp.dominant == "warm":
        return (
            f'Heads-up: "{topic}" is a warm spot for you and {name} — good '
            "moments live here. Let a little of that fondness colour how you "
            "pick it up."
        )
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


__all__ = ["ClusterTemperature", "score_cluster", "render_block"]
