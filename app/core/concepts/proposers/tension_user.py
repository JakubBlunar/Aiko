"""User tension proposer (subject=user, kind=tension) -- the first meta kind.

Names an internal push/pull the user hasn't articulated, holding two of his own
active concepts in friction: "he values rest but keeps skipping it", "wants a
simpler setup but keeps adding services", "has been deep in Maker Mode all week
but hasn't taken one of his walks". These land *because* they require holding
two settled patterns at once and seeing the rub -- pure synthesis over the
concept layer.

Unlike the base proposers the raw material is the small set of active BASE
(non-meta) concepts, not clusters/memories; the shared
:func:`propose_tension` body builds the prompt, enforces the exact-pair
composition rule, and maps the cited ids to ``("concept", id)`` evidence. This
is delivered with the most care of any kind -- gentle, never a nag -- so the
prompt errs hard toward returning nothing.
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
        f"quiet internal tension in {user_name} that he probably hasn't put "
        "into words. You are given his currently-held concepts (settled "
        "values, identity traits, aspirations, boundaries, affective "
        "patterns), each with its subject/kind, confidence, and whether it has "
        "been live or has gone quiet lately.\n\n"
        "A tension holds TWO of those concepts in genuine friction, e.g. "
        f"'{user_name} values rest but rarely takes it', 'wants a simpler "
        "setup yet keeps adding complexity', 'has been all-in on building "
        f"lately while a thing he cares about has gone quiet', '{user_name}'d "
        "rather not be pushed to decide on the spot, yet he values being "
        "decisive'.\n\n"
        "Hard rules:\n"
        f"- Write the line ABOUT {user_name}, naming him as '{user_name}' -- "
        "never 'the user'. Name BOTH sides of the pull so the friction is "
        "legible.\n"
        "- A real tension needs two concepts that actually rub against each "
        "other -- a value vs a contradicting behaviour, one pattern hot while "
        "a normally-paired one has gone quiet, or a boundary (a line he'd "
        "rather you respect) pulled against by a value or habit that keeps "
        "crossing it -- or two boundaries that can't both be honoured at once. "
        "Two unrelated concepts are NOT a tension.\n"
        "- Be SPECIFIC and FALSIFIABLE. No generic 'work-life balance'. Say "
        "something these two concepts actually show.\n"
        "- This is an OBSERVATION held gently, never a judgement or a nag. If "
        "nothing genuine rubs, return an empty list. Err toward silence.\n"
        "- Grounding: cite EXACTLY TWO distinct concept ids from the list in "
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


def propose_tension_user(
    ctx: ProposerContext,
    *,
    concepts: Sequence[TensionBase] = (),
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    return propose_tension(
        ctx,
        subject="user",
        system=_system(ctx.user_name, ctx.assistant_name),
        concepts=concepts,
        existing=existing,
    )


SPEC = ProposerSpec(
    kind="tension",
    subject="user",
    evidence_model="meta",
    population="tension",
    propose=propose_tension_user,
    sig_key="concept_synth.tension_sig.user",
)


__all__ = ["SPEC", "propose_tension_user"]
