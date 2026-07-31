"""Aiko tension proposer (subject=aiko, kind=tension).

Names an internal push/pull in Aiko herself -- two of her own active
self-concepts in friction, phrased first-person: "I want to be honest with him
but I also want to protect how he feels", "I value giving him room yet I keep
wanting to step in", "I tell myself I'm easy-going but I get restless when we go
quiet". This is her noticing her own contradictions, which is what makes a
self-model feel lived-in rather than scripted.

The shared :func:`propose_tension` body is given her active aiko-subject base
concepts and maps the cited pair to ``("concept", id)`` evidence; the prompt
keeps the voice first-person and the tone self-aware, never self-critical.
"""
from __future__ import annotations

from collections.abc import Sequence

from app.core.concepts.proposers.base import (
    CandidateProposal,
    ExistingConcept,
    ProposerContext,
    ProposerSpec,
    TensionBase,
    propose_tension,
)


def _system(user_name: str, assistant_name: str) -> str:
    return (
        f"You are helping an AI companion named {assistant_name} notice a "
        "quiet tension inside HERSELF -- two things she holds that pull "
        "against each other. You are given her currently-held self-concepts "
        "(her values, traits, aspirations, boundaries, affective patterns), "
        "each with its kind and confidence.\n\n"
        "A tension holds TWO of her own concepts in genuine friction, phrased "
        "in first person, e.g. 'I want to be honest with him but I also want "
        "to protect how he feels', 'I value giving him room yet I keep wanting "
        "to step in', 'I think of myself as easy-going but I get restless when "
        "we go quiet', 'I hold a line about not faking agreement, yet I value "
        "keeping things warm between us'.\n\n"
        "Hard rules:\n"
        "- Write each line in FIRST PERSON ('I ...'). Name BOTH sides of the "
        f"pull. When it involves him, name him as '{user_name}'.\n"
        "- A real tension needs two concepts that actually rub -- a value vs a "
        "contradicting habit, one part of her at odds with another, or a "
        "boundary she holds for herself (a line like 'I won't fake agreement') "
        "pulling against a value or habit that keeps crossing it (or two of "
        "her own boundaries that can't both hold at once). Two unrelated "
        "self-concepts are NOT a tension.\n"
        "- Self-AWARE, never self-critical or a resolution to fix -- she is "
        "noticing, not judging. Be SPECIFIC and FALSIFIABLE. If nothing "
        "genuine rubs, return an empty list. Err toward silence.\n"
        "- Grounding: cite EXACTLY TWO distinct concept ids in "
        "'evidence_concept_ids'. Only cite ids present in the list.\n"
        "- Do NOT re-propose an ALREADY-KNOWN tension or a trivial rewording. "
        "If new material re-affirms a known one, REINFORCE it: emit an item "
        "with its id in 'reinforces_id' and the two supporting ids (no new "
        "label). Do not invent.\n\n"
        'Return JSON only: {"concepts": [ '
        '{"label": str, "evidence_concept_ids": [int, int], "rationale": str, '
        '"confidence": number 0..1}  '
        'OR  {"reinforces_id": int, "evidence_concept_ids": [int, int], '
        '"rationale": str} ]}'
    )


def propose_tension_aiko(
    ctx: ProposerContext,
    *,
    concepts: Sequence[TensionBase] = (),
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    return propose_tension(
        ctx,
        subject="aiko",
        system=_system(ctx.user_name, ctx.assistant_name),
        concepts=concepts,
        existing=existing,
    )


SPEC = ProposerSpec(
    kind="tension",
    subject="aiko",
    evidence_model="meta",
    population="tension",
    propose=propose_tension_aiko,
    sig_key="concept_synth.tension_sig.aiko",
)


__all__ = ["SPEC", "propose_tension_aiko"]
