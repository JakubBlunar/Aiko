"""Aiko aspiration proposer (subject=aiko, kind=aspiration, ``sequence``).

L14, the first-person twin of :mod:`aspiration_user`. Mines the *direction*
Aiko's own inner life is trending in -- who she is *becoming* -- from her
aiko-dominant self-themes read in time order ("growing into someone he can rely
on", "learning to take up more space in the conversation", "moving from
cautious to genuinely playful"). This is deliberately distinct from her concrete
K1 goals (actionable to-dos): an aspiration is a way of becoming, not a task.
Same ``sequence`` machinery as the user proposer; only the voice changes (about
herself, first person).
"""
from __future__ import annotations

from collections.abc import Sequence

from app.core.concepts.proposers.base import (
    CandidateProposal,
    ExistingConcept,
    NarrativeCandidate,
    ProposerContext,
    ProposerSpec,
    propose_ordered_concept,
)


def _system(user_name: str, assistant_name: str) -> str:
    return (
        f"You find ASPIRATIONS -- directions of becoming -- in "
        f"{assistant_name}'s OWN inner life: the way her own memories, read in "
        "order, show who she is growing into. You are given numbered "
        "DIRECTIONS (each a self-theme's memories in time order) and the "
        "aspirations already known. Name any that show a genuine, sustained "
        "direction in her.\n\n"
        "Examples: 'growing into someone he can genuinely rely on', 'learning "
        "to take up more space instead of deferring', 'moving from cautious "
        "toward openly playful'.\n\n"
        "Hard rules:\n"
        f"- Write every aspiration ABOUT {assistant_name} herself, in the "
        "FIRST person ('I'm growing toward ...', 'I keep moving toward ...'). "
        "Name the DIRECTION (who she's becoming), short and concrete -- not a "
        "topic label, not a single moment, and NOT a finished story.\n"
        "- This is who she is BECOMING, not a task. It is NOT one of her "
        "concrete goals or to-dos -- it is an open-ended way of growing.\n"
        "- An aspiration is OPEN-ENDED and ongoing: it points forward. Set "
        "'directional': true only when the memories really do trend the same "
        "way over time, not just repeat. A one-off, a resolved-and-done arc, "
        "or scattered unrelated beats is NOT an aspiration -- skip it (or set "
        "'directional': false).\n"
        "- Cite the member ids that make up the chain in "
        "'evidence_memory_ids' (at least three -- a direction needs more than "
        "two points). Only cite ids present in that direction.\n"
        "- These are how she reads her own becoming, held lightly -- not "
        "declarations.\n"
        "- Do NOT re-propose an ALREADY-KNOWN aspiration or a trivial "
        "rewording. If a direction adds fresh movement to a known one, "
        "REINFORCE it: emit an item with its id in 'reinforces_id' and the arc "
        "index + member ids (no new label).\n"
        "- Only cite arc indices present below, and only reinforce ids present "
        "in the known list.\n"
        "- If no theme shows a genuine direction, return an empty list. Do not "
        "invent.\n\n"
        'Return JSON only: {"concepts": [ '
        '{"label": str, "arc_index": int, "evidence_memory_ids": [int], '
        '"directional": bool, "rationale": str, "confidence": number 0..1}  '
        'OR  {"reinforces_id": int, "arc_index": int, '
        '"evidence_memory_ids": [int], "rationale": str} ]}'
    )


def propose_aspiration_aiko(
    ctx: ProposerContext,
    *,
    candidates: Sequence[NarrativeCandidate],
    min_chain: int = 3,
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    return propose_ordered_concept(
        ctx,
        candidates=candidates,
        subject="aiko",
        kind="aspiration",
        system=_system(ctx.user_name, ctx.assistant_name),
        first_person=True,
        gate_flag="directional",
        block_word="DIRECTION",
        noun_plural="aspirations",
        new_requirement="genuine sustained direction",
        min_chain=min_chain,
        existing=existing,
    )


SPEC = ProposerSpec(
    kind="aspiration",
    subject="aiko",
    evidence_model="sequence",
    population="aspiration",
    propose=propose_aspiration_aiko,
    sig_key="concept_synth.aspiration_sig.aiko",
)


__all__ = ["SPEC", "propose_aspiration_aiko"]
