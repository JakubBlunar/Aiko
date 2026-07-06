"""User aspiration proposer (subject=user, kind=aspiration, ``sequence``).

L14, the open-ended sibling of :mod:`narrative_user`. Where a narrative names a
*closed* arc (a story that has landed), an aspiration names a *direction* -- the
way a run of memories, read in time order, shows someone moving toward
something ("building toward a fully self-hosted life", "moving from tinkering to
shipping", "growing more deliberate about health"). The worker's
``_run_aspiration_pass`` hands each user-dominant cluster's members in temporal
order (already filtered to cover a real span of time) as
:class:`~app.core.concepts.proposers.base.NarrativeCandidate`\\s; this proposer
names any that show a sustained direction, or reinforces a known one. Evidence
is the ordered chain (``sequence`` edges carrying ordinals).
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
        f"You find ASPIRATIONS -- directions of travel -- in {user_name}'s "
        "life: the way a run of related memories, read in order, shows him "
        "moving from one place toward another. You are given numbered "
        "DIRECTIONS (each a theme's memories in time order) and the "
        "aspirations already known. Name any that show a genuine, sustained "
        "direction.\n\n"
        "Examples: 'building toward a fully self-hosted, private setup', "
        "'moving from hobby tinkering toward actually shipping', 'getting "
        "steadily more deliberate about his health'.\n\n"
        "Hard rules:\n"
        f"- Write every aspiration ABOUT {user_name} (third person). Name the "
        "DIRECTION (where he's heading, from roughly where), short and "
        "concrete -- not a topic label, not a single moment, and NOT a "
        "finished story.\n"
        "- An aspiration is OPEN-ENDED and ongoing: it points forward. Set "
        "'directional': true only when the memories really do trend the same "
        "way over time (a consistent pull), not just repeat. A one-off, a "
        "resolved-and-done arc, or scattered unrelated beats is NOT an "
        "aspiration -- skip it (or set 'directional': false).\n"
        "- Cite the member ids that make up the chain in "
        "'evidence_memory_ids' (at least three -- a direction needs more than "
        "two points). Only cite ids present in that direction.\n"
        "- These are impressions of where he's going, held lightly -- not "
        "predictions or announcements.\n"
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


def propose_aspiration_user(
    ctx: ProposerContext,
    *,
    candidates: Sequence[NarrativeCandidate],
    min_chain: int = 3,
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    return propose_ordered_concept(
        ctx,
        candidates=candidates,
        subject="user",
        kind="aspiration",
        system=_system(ctx.user_name, ctx.assistant_name),
        first_person=False,
        gate_flag="directional",
        block_word="DIRECTION",
        noun_plural="aspirations",
        new_requirement="genuine sustained direction",
        min_chain=min_chain,
        existing=existing,
    )


SPEC = ProposerSpec(
    kind="aspiration",
    subject="user",
    evidence_model="sequence",
    population="aspiration",
    propose=propose_aspiration_user,
    sig_key="concept_synth.aspiration_sig.user",
)


__all__ = ["SPEC", "propose_aspiration_user"]
