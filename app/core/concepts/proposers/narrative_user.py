"""User narrative proposer (subject=user, kind=narrative, ``sequence``).

L8. The first *ordered*-evidence proposer: where identity/value concepts name
what he is like, this one names a *story* -- a closed causal arc mined from a
topic cluster's episodic memories in time order ("The Great 13900KS
Investigation", "learning to trust the new team", "the long road to shipping
voice mode"). The worker's ``_run_narrative_pass`` loads each user-dominant
cluster's members in temporal order and hands them in as
:class:`~app.core.concepts.proposers.base.NarrativeCandidate`\\s; this proposer
names any that form a genuine beginning->development->resolution arc, or
reinforces a known one. Evidence is the chain of member memories in order
(``sequence`` edges carrying ordinals); an open or too-short arc is dropped.
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
        f"You find NARRATIVE ARCS in {user_name}'s life -- the closed, "
        "self-contained little stories that a run of related memories tells "
        "when you read them in order. You are given numbered ARCS (each a "
        "theme's memories in time order) and the arcs already known. Name any "
        "that form a genuine story.\n\n"
        "Examples: 'the drawn-out hunt for the flaky CPU that turned out to be "
        "RAM', 'settling into the new apartment', 'the falling-out with a "
        "friend and the slow repair'.\n\n"
        "Hard rules:\n"
        f"- Write every arc ABOUT {user_name} (third person). Name the STORY "
        "(what happened, start to finish), short and concrete -- not a topic "
        "label and not a single moment.\n"
        "- Only name an arc that is CLOSED: it has a beginning, a development, "
        "and a resolution/outcome you can see in the memories. Set 'closed': "
        "true only when the story has actually landed. An ongoing, unresolved "
        "thread is NOT a narrative -- skip it (or set 'closed': false).\n"
        "- Cite the member ids that make up the chain in "
        "'evidence_memory_ids' (at least three -- a story needs more than two "
        "beats). Only cite ids present in that arc.\n"
        "- These are impressions of how things unfolded, held lightly -- not "
        "announcements.\n"
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


def propose_narrative_user(
    ctx: ProposerContext,
    *,
    candidates: Sequence[NarrativeCandidate],
    min_chain: int = 3,
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    return propose_narrative(
        ctx,
        candidates=candidates,
        subject="user",
        system=_system(ctx.user_name, ctx.assistant_name),
        first_person=False,
        min_chain=min_chain,
        existing=existing,
    )


SPEC = ProposerSpec(
    kind="narrative",
    subject="user",
    evidence_model="sequence",
    population="narrative",
    propose=propose_narrative_user,
    sig_key="concept_synth.narrative_sig.user",
)


__all__ = ["SPEC", "propose_narrative_user"]
