"""User-boundary proposer (subject=user, kind=boundary, ``set``).

Where identity names *what* the user is into and value the *why* underneath,
this one names a **behavioural line to be gentle about** -- a soft constraint
on how Aiko should act around him, not a trait ("go gentler about work when
he's stressed", "he'd rather not be pushed to decide on the spot"). These are
guiding, not refusals.

Unlike the trait/value proposers (clusters only) this one is a *hybrid*: it
mines topic clusters AND Aiko's explicit remembered anchors about the user
(``[[remember:...]]`` -> ``kind="self_tagged"``), plus -- since L18e --
automatically-extracted ``preference`` rows. A single *deliberate* anchor is
enough to seed a boundary; anything else needs two sources. The composition
rule + reinforcement handling live in :func:`propose_boundary`; the L3
``boundary`` gate (:func:`boundary_evidence_gate`) then floors the source count
at 1 for anchor-grounded lines.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.core.concepts.proposers.base import (
    CandidateProposal,
    ExistingConcept,
    FocusCluster,
    ProposerContext,
    ProposerSpec,
    propose_boundary,
)

# Which memory kinds in the offered pool count as a line the user *chose* to
# have remembered, and can therefore ground a boundary on their own.
#
# Only ``self_tagged`` (a ``[[remember:...]]`` annotation) qualifies. L18e also
# feeds this pass ``preference`` rows so a stated limit that was never anchored
# can still be seen -- but those are the extractor's reading of a passing
# sentence, and granting them the single-source path let one automatic guess
# mint a standing behavioural line. See :func:`propose_boundary` for the
# measurement that prompted splitting the two apart.
_DELIBERATE_KINDS = ("self_tagged",)


def _system(user_name: str, assistant_name: str) -> str:
    return (
        f"You are helping an AI companion named {assistant_name} notice "
        f"BEHAVIOURAL BOUNDARIES about {user_name} -- soft lines that should "
        "guide how she acts around him, NOT facts about what he likes. You are "
        f"given a map of {user_name}'s topic clusters, a few focus clusters in "
        "detail, notes she deliberately chose to remember, preferences that "
        "were picked up automatically, and the boundaries already known.\n\n"
        "A boundary is a *guide*, e.g. 'go gentler about his work when he's "
        "stressed', 'he'd rather not be teased about being wrong', 'don't push "
        "him to decide on the spot', 'ease off pet names for now'. It is "
        "something she should be mindful of in HER behaviour.\n\n"
        "Hard rules:\n"
        f"- Write every boundary ABOUT {user_name}, naming him as "
        f"'{user_name}' -- never 'the user'. Phrase it as a gentle guide for "
        f"how {assistant_name} should act, e.g. 'Be gentle with {user_name} "
        "about ...'.\n"
        "- These are GUIDING, never hard refusals. Do NOT propose content-"
        "policy limits ('won't discuss X') -- only relationship/interaction "
        "preferences. If all you can name is a topic he likes, return nothing "
        "(that is an identity concept, not a boundary).\n"
        "- Be SPECIFIC and FALSIFIABLE. No boundary true of everyone ('be "
        "kind'). Say something these clusters/notes actually show.\n"
        "- Grounding: a NEW boundary needs EITHER one id from NOTABLE "
        "REMEMBERED NOTES (a single deliberate note is enough) OR at least "
        "TWO ids in total across 'evidence_cluster_reps' and "
        "'evidence_memory_ids'. One lone cluster is not enough, and neither "
        "is one lone automatically-picked-up preference.\n"
        "- Do NOT re-propose an ALREADY-KNOWN boundary or a trivial rewording. "
        "If new material adds support for a known one, REINFORCE it: emit an "
        "item with its id in 'reinforces_id' and the supporting ids (no new "
        "label).\n"
        "- Only cite ids present in the provided lists, and only reinforce ids "
        "present in the known list. If nothing genuine stands out, return an "
        "empty list. Do not invent.\n\n"
        'Return JSON only: {"concepts": [ '
        '{"label": str, "evidence_cluster_reps": [int, ...], '
        '"evidence_memory_ids": [int, ...], "rationale": str, '
        '"confidence": number 0..1}  '
        'OR  {"reinforces_id": int, "evidence_cluster_reps": [int, ...], '
        '"evidence_memory_ids": [int, ...], "rationale": str} ]}'
    )


def propose_boundary_user(
    ctx: ProposerContext,
    *,
    focus_clusters: Sequence[FocusCluster] = (),
    cluster_index: Sequence[tuple[int, str, int]] = (),
    memories: Sequence[Any] = (),
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    return propose_boundary(
        ctx,
        subject="user",
        system=_system(ctx.user_name, ctx.assistant_name),
        focus_clusters=focus_clusters,
        cluster_index=cluster_index,
        memories=memories,
        existing=existing,
        deliberate_kinds=_DELIBERATE_KINDS,
    )


SPEC = ProposerSpec(
    kind="boundary",
    subject="user",
    evidence_model="set",
    population="boundary",
    propose=propose_boundary_user,
    sig_key="concept_synth.boundary_sig.user",
)


__all__ = ["SPEC", "propose_boundary_user"]
