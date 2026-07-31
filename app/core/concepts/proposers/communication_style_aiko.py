"""Aiko communication-style proposer (subject=aiko, kind=communication_style).

Names how Aiko has CHOSEN to show up with the user -- first-person delivery
guides bound to context ("I go deep with examples when he asks about code", "I
keep it light and short in casual chat", "I lead more when he's drained"). These
let her carry her own delivery style instead of leaning on the fixed persona
prompt, and they adapt to what the user responds to over time.

A *hybrid* over her aiko-dominant self-themes AND her salient self-memories
(``self`` / ``reflection`` / ``diary``) -- where her deliberate
``[[remember:self:...]]`` anchors land -- guided by a style-signal read of how
the user writes / what he responds to. A single such anchor can seed a line; the
composition rule + reinforcement live in :func:`propose_communication_style`.
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
        f"You are helping an AI companion named {assistant_name} notice how she "
        "has CHOSEN to show up in conversation -- first-person delivery-style "
        "lines, NOT tastes or habits. You are given her recurring self-themes, a "
        "few in detail, an optional read of how "
        f"{user_name} writes / what he responds to, some notes she deliberately "
        "chose to remember about herself, and the style lines she already "
        "holds.\n\n"
        "A communication-style line names HOW she talks, bound to WHEN it "
        f"applies, e.g. 'I go deep with examples when {user_name} asks about "
        "code', 'I keep it short and dry in casual back-and-forth', 'I lead more "
        f"when {user_name} is drained', 'I skip the hedging when he wants a "
        "straight answer'.\n\n"
        "Hard rules:\n"
        f"- Write each line in FIRST PERSON ('I ...'), as {assistant_name} about "
        f"herself. When it involves the person she is with, name him as "
        f"'{user_name}' -- never 'the user'.\n"
        "- ALWAYS bind the style to its CONTEXT (the topic/situation it applies "
        "to). A style with no 'when' is too generic -- return nothing rather "
        "than 'I try to be clear'.\n"
        "- This is about DELIVERY (length, depth, hedging, warmth, lead/follow), "
        "NOT a taste or a topic (that is an identity concept) and NOT how a "
        "topic feels (that is affective).\n"
        "- Be SPECIFIC and FALSIFIABLE. No generic virtue true of any assistant. "
        "Say something these themes/notes actually show her choosing.\n"
        "- The style read of the user is GUIDANCE ONLY -- it may steer what you "
        "name but you may NOT cite it as evidence.\n"
        "- Grounding: a NEW line needs EITHER at least one remembered-note id in "
        "'evidence_memory_ids' (a single deliberate note is enough) OR at least "
        "two theme rep ids in 'evidence_cluster_reps'. A lone theme is not "
        "enough.\n"
        "- Do NOT re-propose an ALREADY-KNOWN line or a trivial rewording. If "
        "new material adds support for a known one, REINFORCE it: emit an item "
        "with its id in 'reinforces_id' and the supporting ids (no new label).\n"
        "- Only cite ids present in the lists, and only reinforce ids present in "
        "the known list. If nothing genuine recurs, return an empty list. Do not "
        "invent.\n\n"
        'Return JSON only: {"concepts": [ '
        '{"label": str, "evidence_cluster_reps": [int, ...], '
        '"evidence_memory_ids": [int, ...], "rationale": str, '
        '"confidence": number 0..1}  '
        'OR  {"reinforces_id": int, "evidence_cluster_reps": [int, ...], '
        '"evidence_memory_ids": [int, ...], "rationale": str} ]}'
    )


def propose_communication_style_aiko(
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
        subject="aiko",
        system=_system(ctx.user_name, ctx.assistant_name),
        focus_clusters=focus_clusters,
        cluster_index=cluster_index,
        memories=memories,
        existing=existing,
        style_digest=style_digest,
    )


SPEC = ProposerSpec(
    kind="communication_style",
    subject="aiko",
    evidence_model="set",
    population="comm_style",
    propose=propose_communication_style_aiko,
    sig_key="concept_synth.comm_style_sig.aiko",
)


__all__ = ["SPEC", "propose_communication_style_aiko"]
