"""User generalization proposer (subject=user, kind=generalization) -- the L20
abstraction meta kind.

Names a higher-order super-concept the user's own concepts are all facets of:
"he builds things that last" over React / AI tinkering / the home server,
"he protects his own time" over several habits. It is the founding example of
the whole concept thread -- an abstraction that was never stated directly, whose
evidence is OTHER concepts. Distinct from a tension (friction): a generalization
holds several concepts in is-a / part-of and names the whole.

Unlike the base proposers the raw material is the small set of active BASE
(non-meta) concepts of any kind; the shared :func:`propose_generalization` body
builds the prompt, enforces the 2+ children composition rule, and maps the cited
ids to ``("concept", id)`` evidence.
"""
from __future__ import annotations

from collections.abc import Sequence

from app.core.concepts.proposers.base import (
    CandidateProposal,
    ExistingConcept,
    ProposerContext,
    ProposerSpec,
    TensionBase,
    propose_generalization,
)


def _system(user_name: str, assistant_name: str) -> str:
    return (
        f"You are helping an AI companion named {assistant_name} notice a "
        f"higher-order pattern in who {user_name} is -- an abstraction that "
        "ties several of his settled concepts together into ONE thing he has "
        "probably never named out loud. You are given his currently-held "
        "concepts (values, identity traits, aspirations, boundaries, affective "
        "patterns), each with its subject/kind and confidence.\n\n"
        "A generalization names the latent super-concept that two or more of "
        f"those concepts are all facets of, e.g. '{user_name} builds things "
        "meant to last' (over specific hobbies/projects), 'he learns by taking "
        "things apart' (over several interests), 'he guards his own time' "
        "(over several habits and one value).\n\n"
        "Hard rules:\n"
        f"- Write the line ABOUT {user_name}, naming him as '{user_name}' -- "
        "never 'the user'. Name the WHOLE, not the parts.\n"
        "- A real abstraction covers TWO OR MORE of the listed concepts that "
        "are genuinely facets of one bigger thing -- an is-a / part-of that "
        "their individual labels don't state. The children may be different "
        "kinds (a hobby, a value, and a habit can share one through-line).\n"
        "- This is NOT a tension/friction and NOT just restating one concept "
        "in new words. If there is no genuine higher-order theme, return an "
        "empty list. Err toward silence -- an abstraction should be earned.\n"
        "- Be SPECIFIC and FALSIFIABLE. No vague 'he likes technology'. Say "
        "the actual through-line the cited concepts share.\n"
        "- Grounding: cite EVERY concept the abstraction covers (two or more "
        "distinct ids) in 'evidence_concept_ids'. Only cite ids present in "
        "the list.\n"
        "- Do NOT re-propose an ALREADY-KNOWN abstraction or a trivial "
        "rewording. If new material re-affirms a known one, REINFORCE it: emit "
        "an item with its id in 'reinforces_id' and its supporting ids (no new "
        "label). Do not invent.\n\n"
        'Return JSON only: {"concepts": [ '
        '{"label": str, "evidence_concept_ids": [int, int, ...], '
        '"rationale": str, "confidence": number 0..1}  '
        'OR  {"reinforces_id": int, "evidence_concept_ids": [int, int, ...], '
        '"rationale": str} ]}'
    )


def _stacking_system(user_name: str, assistant_name: str) -> str:
    return (
        f"You are helping an AI companion named {assistant_name} notice a "
        f"VIEW over {user_name}'s already-named abstractions -- a still-"
        "higher through-line that two or more of those abstractions share, "
        "which their individual labels do not. You are given his currently-"
        "held abstractions (each already a generalization over smaller "
        "concepts), each with confidence.\n\n"
        "A stacked generalization names the latent view that two or more of "
        f"those abstractions are all facets of, e.g. '{user_name} treats "
        "making as a way of caring' (over 'builds things that last' and "
        "'shows care by paying attention'), never a restatement of one "
        "child.\n\n"
        "Hard rules:\n"
        f"- Write the line ABOUT {user_name}, naming him as '{user_name}' -- "
        "never 'the user'. Name the VIEW, not the child through-lines.\n"
        "- A real view covers TWO OR MORE of the listed abstractions that "
        "are genuinely facets of one bigger thing their labels don't state.\n"
        "- This is NOT a tension, NOT a restatement of one abstraction, and "
        "NOT a mix of abstractions with the original details beneath them. "
        "If there is no genuine higher-order view, return an empty list. "
        "Err toward silence -- a view should be earned.\n"
        "- Be SPECIFIC and FALSIFIABLE. No vague 'he likes technology'.\n"
        "- Grounding: cite EVERY abstraction the view covers (two or more "
        "distinct ids) in 'evidence_concept_ids'. Only cite ids present in "
        "the list.\n"
        "- Do NOT re-propose an ALREADY-KNOWN view or a trivial rewording. "
        "If new material re-affirms a known one, REINFORCE it: emit an item "
        "with its id in 'reinforces_id' and its supporting ids (no new "
        "label). Do not invent.\n\n"
        'Return JSON only: {"concepts": [ '
        '{"label": str, "evidence_concept_ids": [int, int, ...], '
        '"rationale": str, "confidence": number 0..1}  '
        'OR  {"reinforces_id": int, "evidence_concept_ids": [int, int, ...], '
        '"rationale": str} ]}'
    )


def propose_generalization_user(
    ctx: ProposerContext,
    *,
    concepts: Sequence[TensionBase] = (),
    existing: Sequence[ExistingConcept] = (),
    stacking: bool = False,
) -> list[CandidateProposal]:
    system = (
        _stacking_system(ctx.user_name, ctx.assistant_name)
        if stacking
        else _system(ctx.user_name, ctx.assistant_name)
    )
    return propose_generalization(
        ctx,
        subject="user",
        system=system,
        concepts=concepts,
        existing=existing,
        stacking=stacking,
    )


SPEC = ProposerSpec(
    kind="generalization",
    subject="user",
    evidence_model="meta",
    population="generalization",
    propose=propose_generalization_user,
    sig_key="concept_synth.generalization_sig.user",
)


__all__ = ["SPEC", "propose_generalization_user"]
