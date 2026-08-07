"""Shared-arc proposer (subject=relationship, kind=narrative, ``sequence``).

L29(a). The "both of us" sibling of the L8 narrative proposers: a *closed
joint project* compressed into one named story -- "the month we rebuilt the
memory system", "our push to get voice mode working". Where the user/aiko
arcs read one person's own memories, this one reads the ``shared_moment``
stream, which is the only evidence that is about the pair by construction.

The worker's ``_run_shared_arc_pass`` groups moments into temporally
contiguous, topically coherent episodes (see
:mod:`app.core.concepts.shared_arc_grouping`) and hands each in as a
:class:`~app.core.concepts.proposers.base.NarrativeCandidate`. Everything
downstream is ordinary L8 machinery: ``kind="narrative"`` keeps the closed-arc
promotion gate, the 0.3 plasticity, and the relevance-only surfacing, and the
``relationship`` branch of ``_concept_narrative_header`` already knows how to
render the result.

The one thing that is not shared is the voice. ``propose_ordered_concept``
derives it from ``first_person``, which only spans "about him" and "about
me"; a shared arc is about the two of them at once, so this proposer passes
an explicit ``voice`` override.
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
        f"You find SHARED STORY-ARCS in what {user_name} and {assistant_name} "
        "have lived through together -- the closed, self-contained little "
        "stories that a run of shared moments tells when you read them in "
        "order. You are given numbered ARCS (each a stretch of their shared "
        "moments in time order) and the arcs already known. Name any that "
        "form a genuine story the two of them went through together.\n\n"
        "Examples: 'the month they rebuilt the memory system together', 'the "
        "long push to get voice mode working', 'the argument about being "
        "interrupted and the repair that followed'.\n\n"
        "Hard rules:\n"
        f"- Write every arc about {user_name} AND {assistant_name} together "
        "(third person plural -- 'they', 'the two of them'). Name the STORY "
        "(what they went through, start to finish), short and concrete -- not "
        "a topic label, not a mood, and not a single moment.\n"
        "- It must be genuinely JOINT. A stretch of moments that is really "
        f"just {user_name}'s own story, or just a mood the two of them shared, "
        "is NOT a shared arc -- skip it.\n"
        "- Only name an arc that is CLOSED: it has a beginning, a "
        "development, and a resolution/outcome you can see in the moments. "
        "Set 'closed': true only when the story has actually landed. An "
        "ongoing thread is NOT a narrative -- skip it (or set 'closed': "
        "false).\n"
        "- A run of moments that merely share a FEELING (all tender, all "
        "playful) is not a story. There has to be something that happened and "
        "then finished.\n"
        "- Cite the member ids that make up the chain in "
        "'evidence_memory_ids' (at least three -- a story needs more than two "
        "beats). Only cite ids present in that arc.\n"
        "- These are warm recollections of how things went, held lightly -- "
        "not announcements and not a summary of the relationship.\n"
        "- Do NOT re-propose an ALREADY-KNOWN arc or a trivial rewording. If "
        "an arc adds fresh beats to a known story, REINFORCE it: emit an item "
        "with its id in 'reinforces_id' and the arc index + member ids (no "
        "new label).\n"
        "- Only cite arc indices present below, and only reinforce ids "
        "present in the known list.\n"
        "- If no arc is a genuine closed shared story, return an empty list. "
        "Do not invent.\n\n"
        'Return JSON only: {"concepts": [ '
        '{"label": str, "arc_index": int, "evidence_memory_ids": [int], '
        '"closed": bool, "rationale": str, "confidence": number 0..1}  '
        'OR  {"reinforces_id": int, "arc_index": int, '
        '"evidence_memory_ids": [int], "rationale": str} ]}'
    )


def propose_narrative_relationship(
    ctx: ProposerContext,
    *,
    candidates: Sequence[NarrativeCandidate],
    min_chain: int = 3,
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    return propose_narrative(
        ctx,
        candidates=candidates,
        subject="relationship",
        system=_system(ctx.user_name, ctx.assistant_name),
        first_person=False,
        min_chain=min_chain,
        existing=existing,
        voice=(
            f"about {ctx.user_name} and {ctx.assistant_name} together "
            "(third person plural -- 'the two of them ...')"
        ),
    )


SPEC = ProposerSpec(
    kind="narrative",
    subject="relationship",
    evidence_model="sequence",
    population="shared_arc",
    propose=propose_narrative_relationship,
    sig_key="concept_synth.narrative_sig.relationship",
)


__all__ = ["SPEC", "propose_narrative_relationship"]
