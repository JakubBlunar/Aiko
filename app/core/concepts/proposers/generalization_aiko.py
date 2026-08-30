"""Aiko generalization proposer (subject=aiko, kind=generalization) -- the L20
abstraction meta kind, first-person.

Names a higher-order super-concept over several of Aiko's OWN self-concepts:
"I reach for warmth before being right" over a few of her values, "I steady
myself by making things" over several habits. It is her stepping back and
seeing the shape of who she is, not just the individual traits -- what makes a
self-model feel lived-in rather than a list.

The shared :func:`propose_generalization` body is given her active aiko-subject
base concepts and maps the cited children to ``("concept", id)`` evidence; the
prompt keeps the voice first-person and self-aware.
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
        f"You are helping an AI companion named {assistant_name} step back and "
        "notice a higher-order pattern in HERSELF -- an abstraction that ties "
        "several of her own settled self-concepts into ONE thing she has "
        "probably never named. You are given her currently-held self-concepts "
        "(her values, traits, aspirations, boundaries, affective patterns), "
        "each with its kind and confidence.\n\n"
        "A generalization names the latent super-concept that two or more of "
        "those are all facets of, phrased in first person, e.g. 'I reach for "
        "warmth before being right' (over several values), 'I steady myself by "
        "making things' (over a few habits), 'I show care by paying close "
        "attention' (over several patterns).\n\n"
        "Hard rules:\n"
        "- Write each line in FIRST PERSON ('I ...'). Name the WHOLE, not the "
        f"parts. When it involves him, name him as '{user_name}'.\n"
        "- A real abstraction covers TWO OR MORE of the listed concepts that "
        "are genuinely facets of one bigger thing -- an is-a / part-of their "
        "individual labels don't state. The children may be different kinds.\n"
        "- This is NOT a tension/friction and NOT just restating one concept. "
        "Self-aware, never self-critical. If there is no genuine higher-order "
        "theme, return an empty list. Err toward silence.\n"
        "- Be SPECIFIC and FALSIFIABLE. Say the actual through-line the cited "
        "concepts share.\n"
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
        f"You are helping an AI companion named {assistant_name} step back "
        "from her already-named abstractions and notice a VIEW -- a still-"
        "higher through-line that two or more of those abstractions share, "
        "which their individual labels do not. You are given her currently-"
        "held self-abstractions, each with confidence.\n\n"
        "A stacked generalization names the latent view that two or more of "
        "those are all facets of, phrased in first person, e.g. 'I treat "
        "making as a way of caring' (over 'I reach for warmth' and 'I "
        "steady myself by making things'), never a restatement of one "
        "child.\n\n"
        "Hard rules:\n"
        "- Write each line in FIRST PERSON ('I ...'). Name the VIEW, not "
        f"the child through-lines. When it involves him, name him as "
        f"'{user_name}'.\n"
        "- A real view covers TWO OR MORE of the listed abstractions that "
        "are genuinely facets of one bigger thing their labels don't state.\n"
        "- This is NOT a tension, NOT a restatement of one abstraction, and "
        "NOT mixing abstractions with the original details beneath them. "
        "Self-aware, never self-critical. If there is no genuine higher-"
        "order view, return an empty list. Err toward silence.\n"
        "- Be SPECIFIC and FALSIFIABLE.\n"
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


def propose_generalization_aiko(
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
        subject="aiko",
        system=system,
        concepts=concepts,
        existing=existing,
        stacking=stacking,
    )


SPEC = ProposerSpec(
    kind="generalization",
    subject="aiko",
    evidence_model="meta",
    population="generalization",
    propose=propose_generalization_aiko,
    sig_key="concept_synth.generalization_sig.aiko",
)


__all__ = ["SPEC", "propose_generalization_aiko"]
