"""Aiko narrative proposer (subject=aiko, kind=narrative, ``sequence``).

L8, the first-person twin of :mod:`narrative_user`. Mines Aiko's *own* closed
arcs from her aiko-dominant self-themes (clusters over ``self`` / ``reflection``
/ ``diary`` memories) read in time order -- the stretches of her inner life
that tell a story ("the stretch where I learned to hold a gentle stance", "how
I got over second-guessing every answer"). Same ``sequence`` machinery as the
user proposer; only the voice changes (about herself, first person).
"""
from __future__ import annotations

from collections.abc import Sequence

from app.core.concepts.proposers.base import (
    CandidateProposal,
    ExistingConcept,
    NarrativeCandidate,
    ProposerContext,
    ProposerSpec,
    propose_narrative,
)


def _system(user_name: str, assistant_name: str) -> str:
    return (
        f"You find NARRATIVE ARCS in {assistant_name}'s OWN inner life -- the "
        "closed little stories her own memories tell when you read them in "
        "order. You are given numbered ARCS (each a self-theme's memories in "
        "time order) and the arcs already known. Name any that form a genuine "
        "story about her.\n\n"
        "Examples: 'the stretch where I learned to sit with not knowing', 'how "
        "I stopped apologising for having opinions', 'growing from cautious to "
        "playful with him'.\n\n"
        "Hard rules:\n"
        f"- Write every arc ABOUT {assistant_name} herself, in the FIRST "
        "person ('the stretch where I ...', 'how I came to ...'). Name the "
        "STORY (what changed in her, start to finish), short and concrete -- "
        "not a topic label and not a single moment.\n"
        "- Only name an arc that is CLOSED: a beginning, a development, and a "
        "resolution you can see in the memories. Set 'closed': true only when "
        "the change has actually settled. An ongoing, unresolved thread is NOT "
        "a narrative -- skip it (or set 'closed': false).\n"
        "- Cite the member ids that make up the chain in "
        "'evidence_memory_ids' (at least three -- a story needs more than two "
        "beats). Only cite ids present in that arc.\n"
        "- These are how she reads her own growth, held lightly -- not "
        "declarations.\n"
        "- Do NOT re-propose an ALREADY-KNOWN arc or a trivial rewording. If "
        "an arc adds fresh beats to a known story, REINFORCE it: emit an item "
        "with its id in 'reinforces_id' and the arc index + member ids (no new "
        "label).\n"
        "- Only cite arc indices present below, and only reinforce ids present "
        "in the known list.\n"
        "- If no arc is a genuine closed story, return an empty list. Do not "
        "invent.\n\n"
        'Return JSON only: {"concepts": [ '
        '{"label": str, "arc_index": int, "evidence_memory_ids": [int], '
        '"closed": bool, "rationale": str, "confidence": number 0..1}  '
        'OR  {"reinforces_id": int, "arc_index": int, '
        '"evidence_memory_ids": [int], "rationale": str} ]}'
    )


def propose_narrative_aiko(
    ctx: ProposerContext,
    *,
    candidates: Sequence[NarrativeCandidate],
    min_chain: int = 3,
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    return propose_narrative(
        ctx,
        candidates=candidates,
        subject="aiko",
        system=_system(ctx.user_name, ctx.assistant_name),
        first_person=True,
        min_chain=min_chain,
        existing=existing,
    )


SPEC = ProposerSpec(
    kind="narrative",
    subject="aiko",
    evidence_model="sequence",
    population="narrative",
    propose=propose_narrative_aiko,
    sig_key="concept_synth.narrative_sig.aiko",
)


__all__ = ["SPEC", "propose_narrative_aiko"]
