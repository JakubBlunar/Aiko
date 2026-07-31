"""Relationship tension proposer (subject=relationship, kind=tension).

Names a cross-subject friction between a concept the USER holds and one AIKO
holds -- most often a value clash ("he values blunt directness; she values
softening the hard truths", "he wants her to lead; she holds back to leave him
room"). An aligned pair is a *shared* value (bonding, not a tension); a clashing
pair is exactly where a real relationship lives -- so this is delivered as a
gentle, held observation, NEVER as a grievance or an accusation.

The shared :func:`propose_tension` body is given BOTH subjects' active base
concepts (each line tagged with its subject) and maps the cited pair to
``("concept", id)`` evidence. The prompt's job is to keep the clash tender.
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
        f"gentle friction between something {user_name} holds and something "
        "she holds herself. You are given both of their currently-held "
        "concepts, each tagged with its subject (user or aiko), kind, and "
        "confidence.\n\n"
        "A relationship tension pairs ONE user concept with ONE aiko concept "
        "that pull against each other, e.g. "
        f"'{user_name} wants the unvarnished truth; {assistant_name} leans "
        "toward softening it', 'he likes to be left room to figure things out; "
        f"she wants to jump in and help', '{user_name}'d rather not be pushed "
        f"to open up; {assistant_name} values drawing him out'. This is the "
        "texture of a real relationship, not a problem to fix.\n\n"
        "Hard rules:\n"
        "- The pair MUST be cross-subject: exactly one user concept and one "
        f"aiko concept. Name both sides, {user_name} by name and "
        f"{assistant_name} in first person or by name.\n"
        "- Aligned values are a SHARED value, not a tension -- skip them. Only "
        "name a pair that genuinely clashes -- including a boundary one holds "
        "rubbing against a value or habit the other holds.\n"
        "- Frame it as tender and mutual, NEVER as a grievance, a fault, or a "
        "demand that either side change. Held lightly.\n"
        "- Be SPECIFIC and FALSIFIABLE -- something these two concepts "
        "actually show. If nothing genuine clashes, return an empty list. Err "
        "toward silence.\n"
        "- Grounding: cite EXACTLY TWO distinct concept ids in "
        "'evidence_concept_ids' -- one user, one aiko. Only cite ids present "
        "in the list.\n"
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


def propose_tension_relationship(
    ctx: ProposerContext,
    *,
    concepts: Sequence[TensionBase] = (),
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    return propose_tension(
        ctx,
        subject="relationship",
        system=_system(ctx.user_name, ctx.assistant_name),
        concepts=concepts,
        existing=existing,
    )


SPEC = ProposerSpec(
    kind="tension",
    subject="relationship",
    evidence_model="meta",
    population="tension",
    propose=propose_tension_relationship,
    sig_key="concept_synth.tension_sig.relationship",
)


__all__ = ["SPEC", "propose_tension_relationship"]
