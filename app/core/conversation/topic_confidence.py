"""F10i — per-topic confidence self-model (pure scoring).

A topic-scoped extension of K20 metacognitive calibration. Where F10f
*researches* gaps and F10h reads a topic's emotional charge, this lets
Aiko express **how much she actually knows** about a topic: hedge on a
thin cluster ("I only know a little about your work, but…") and speak with
earned familiarity on a rich, well-studied one.

Confidence is derived from two cheap per-cluster stats (see
``TopicGraph.cluster_knowledge_stats``):

* **size** — how many memories the topic has accumulated. More memories =
  more context, even if none are "studied" facts.
* **learned_count** — how many of those are learned-fact rows
  (``knowledge`` / ``curiosity_finding``). These give *factual* depth on
  top of conversational familiarity.

Both components saturate, so a handful of memories / a couple of learned
facts is already enough to feel familiar, and a single learned fact in a
huge cluster doesn't dominate. The score lands in ``[0, 1]`` and is banded:
**thin** (hedge), **familiar** (don't over-qualify), or silent (the common
middle).

Separation from F10f: F10f fires on *dense-but-unresearched* clusters
(high size, ~0 knowledge) — those score mid/high here (size carries them),
so F10i's **thin** band is reserved for genuinely small clusters, not the
"I keep circling X" beat F10f owns.

Pure + numpy-free: the provider does the embed + cluster match + stat
read; this module only scores the resulting counts.
"""
from __future__ import annotations

from dataclasses import dataclass


# How much each component saturates to 1.0. A topic with ~12 memories is
# well-trodden; ~3 learned facts is solid factual depth.
_SIZE_SATURATION = 12.0
_LEARNED_SATURATION = 3.0

# Confidence = weighted blend. Size (conversational familiarity) carries
# slightly more than learned-fact depth — Aiko can know a lot *about* the
# user's job from talking, even with zero researched facts.
_SIZE_WEIGHT = 0.6
_LEARNED_WEIGHT = 0.4


@dataclass(slots=True, frozen=True)
class ClusterConfidence:
    """Scored confidence in one topic cluster.

    ``confidence`` is in ``[0, 1]``. ``band`` is ``"thin"`` (hedge),
    ``"familiar"`` (speak from what you know), or ``None`` when the
    confidence sits in the silent middle.
    """

    size: int
    learned_count: int
    confidence: float
    band: str | None


def score_confidence(
    size: int,
    learned_count: int,
    *,
    thin_threshold: float = 0.25,
    familiar_threshold: float = 0.7,
) -> ClusterConfidence:
    """Score topic confidence from cluster ``size`` + ``learned_count``.

    Blends a saturating size component and a saturating learned-fact
    component into a ``[0, 1]`` confidence, then bands it: ``familiar``
    wins if confidence is at/above ``familiar_threshold``, else ``thin``
    if at/below ``thin_threshold``, else silent (``None``). Passing equal
    thresholds (the MCP force path) forces a side on every cluster.
    """
    sz = max(0, int(size))
    learned = max(0, int(learned_count))
    size_score = min(1.0, sz / _SIZE_SATURATION) if sz > 0 else 0.0
    learned_score = (
        min(1.0, learned / _LEARNED_SATURATION) if learned > 0 else 0.0
    )
    confidence = _SIZE_WEIGHT * size_score + _LEARNED_WEIGHT * learned_score

    band: str | None = None
    if confidence >= familiar_threshold:
        band = "familiar"
    elif confidence <= thin_threshold:
        band = "thin"

    return ClusterConfidence(
        size=sz,
        learned_count=learned,
        confidence=round(confidence, 4),
        band=band,
    )


def render_block(
    conf: ClusterConfidence, label: str, user_display_name: str,
) -> str:
    """Render the one-line calibration cue, or ``""`` when in the middle.

    A private register nudge (like the relationship-axes block), never a
    line said aloud. The **familiar** band is deliberately about *not
    over-hedging* — it does not push specific facts (that's K61
    knowledge-grounding's job), it just tells her to trust what she knows.
    """
    name = (user_display_name or "them").strip() or "them"
    topic = (label or "this topic").strip() or "this topic"
    if conf.band == "thin":
        return (
            f'Heads-up: "{topic}" is thin ground for you — you\'ve only got a '
            f"little on it. It's fine to say you don't know much here yet and "
            f"let {name} fill you in, rather than bluffing familiarity."
        )
    if conf.band == "familiar":
        return (
            f'Heads-up: "{topic}" is well-trodden ground with {name} — you '
            "actually know this area. Speak from what you know; no need to "
            "hedge or over-qualify."
        )
    return ""


__all__ = ["ClusterConfidence", "score_confidence", "render_block"]
