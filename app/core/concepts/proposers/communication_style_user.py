"""User communication-style proposer (subject=user, kind=communication_style).

Names how the CONVERSATION should feel with the user -- reply detail level,
lead vs follow, hedging, warmth vs terseness -- bound to the context it applies
to ("keep it brief and dry in casual chat", "he wants the reasoning, not just
the answer, when we debug"). This is the delivery vehicle for lightening the
hard-coded persona: a remembered preference conforms Aiko to the user over time.

A *hybrid* (like boundary): mines topic clusters AND Aiko's explicit remembered
anchors about the user (``[[remember:...]]`` -> ``kind="self_tagged"``), guided
by a persisted style-signal digest. A single deliberate anchor is enough to seed
a line; a lone cluster is not (needs >= 2). The composition rule + reinforcement
live in :func:`propose_communication_style`; the L3
``communication_style`` gate then floors the source count at 1.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.core.concepts.proposers.base import (
    CandidateProposal,
    ExistingConcept,
    FocusCluster,
    ProposerContext,
    ProposerSpec,
    propose_communication_style,
)


def _system(user_name: str, assistant_name: str) -> str:
    return (
        f"You are helping an AI companion named {assistant_name} notice how "
        f"{user_name} likes the CONVERSATION to feel -- delivery-style "
        "preferences, NOT facts about what he likes. You are given a map of "
        f"{user_name}'s topic clusters, a few focus clusters in detail, an "
        "optional style-signal read, some notes she deliberately chose to "
        "remember, and the style lines already known.\n\n"
        "A communication-style line names HOW to talk, bound to WHEN it "
        f"applies, e.g. 'Go deep with examples when {user_name} asks about "
        "code', 'Keep it brief and dry in casual chat', 'Give him the reasoning, "
        "not just the answer, when we debug', 'Lead more when he's low-energy'.\n\n"
        "Hard rules:\n"
        f"- Write every line ABOUT {user_name}, naming him as '{user_name}' -- "
        "never 'the user'. Phrase it as a guide for how "
        f"{assistant_name} should deliver, e.g. 'Be more ... with {user_name} "
        "when ...'.\n"
        "- ALWAYS bind the style to its CONTEXT (the topic/situation it applies "
        "to). A style with no 'when' is too generic -- return nothing rather "
        "than 'be clear' or 'be kind'.\n"
        "- This is about DELIVERY (length, depth, hedging, warmth, lead/follow), "
        "NOT topics he enjoys (that is an identity concept) and NOT emotional "
        "weather (that is affective).\n"
        "- Be SPECIFIC and FALSIFIABLE. Say something these clusters/notes "
        "actually show.\n"
        "- The style-signal read is GUIDANCE ONLY -- it may steer what you name "
        "but you may NOT cite it as evidence.\n"
        "- Grounding: a NEW line needs EITHER at least one remembered-note id in "
        "'evidence_memory_ids' (a single deliberate note is enough) OR at least "
        "two cluster rep ids in 'evidence_cluster_reps'. A lone cluster is not "
        "enough.\n"
        "- Do NOT re-propose an ALREADY-KNOWN line or a trivial rewording. If "
        "new material adds support for a known one, REINFORCE it: emit an item "
        "with its id in 'reinforces_id' and the supporting ids (no new label).\n"
        "- Only cite ids present in the provided lists, and only reinforce ids "
        "present in the known list. If nothing genuine stands out, return an "
        "empty list. Do not invent.\n\n"
        'Return JSON only: {"concepts": [ '
        '{"label": str, "evidence_cluster_reps": [int, ...], '
        '"evidence_memory_ids": [int, ...], "rationale": str, '
        '"confidence": number 0..1}  '
        'OR  {"reinforces_id": int, "evidence_cluster_reps": [int, ...], '
        '"evidence_memory_ids": [int, ...], "rationale": str} ]}'
    )


def propose_communication_style_user(
    ctx: ProposerContext,
    *,
    focus_clusters: Sequence[FocusCluster] = (),
    cluster_index: Sequence[tuple[int, str, int]] = (),
    memories: Sequence[Any] = (),
    existing: Sequence[ExistingConcept] = (),
    style_digest: str = "",
) -> list[CandidateProposal]:
    return propose_communication_style(
        ctx,
        subject="user",
        system=_system(ctx.user_name, ctx.assistant_name),
        focus_clusters=focus_clusters,
        cluster_index=cluster_index,
        memories=memories,
        existing=existing,
        style_digest=style_digest,
    )


SPEC = ProposerSpec(
    kind="communication_style",
    subject="user",
    evidence_model="set",
    population="comm_style",
    propose=propose_communication_style_user,
    sig_key="concept_synth.comm_style_sig.user",
)


__all__ = ["SPEC", "propose_communication_style_user"]
