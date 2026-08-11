"""Aiko-boundary proposer (subject=aiko, kind=boundary, ``set``).

Notices the behavioural lines Aiko holds *for herself* -- first-person guides
on how she wants to show up ("I won't fake agreement just to please him", "I
go deeper when he actually asks, rather than staying surface"). These let her
behave from her own stance instead of only mirroring; they are guiding, not
refusals, and they can be renegotiated as trust grows.

A *hybrid* over her aiko-dominant self-themes AND her salient self-memories
(``self`` / ``reflection`` / ``diary``) -- which is where her deliberate
``[[remember:self:...]]`` anchors land. A single such anchor can seed a
boundary; the composition rule + reinforcement live in :func:`propose_boundary`.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.core.concepts.proposers.base import (
    AIKO_SELF_KINDS,
    CandidateProposal,
    ExistingConcept,
    FocusCluster,
    ProposerContext,
    ProposerSpec,
    propose_boundary,
)

# The whole offered pool counts as deliberate here, which keeps this subject's
# behaviour exactly as it was before the split. L18e only widened the *user*
# pool, so nothing automatic entered this one: ``self`` and ``diary`` are her
# explicit ``[[remember:self:...]]`` / ``[[diary:...]]`` writes, and while
# ``reflection`` is worker-authored it is still her own noticing rather than an
# extractor's guess about someone else. Tightening it would be a separate
# judgement call with its own evidence, not a consequence of this one.
_DELIBERATE_KINDS = AIKO_SELF_KINDS


def _system(user_name: str, assistant_name: str) -> str:
    return (
        f"You are helping an AI companion named {assistant_name} notice "
        "BEHAVIOURAL BOUNDARIES she holds for HERSELF -- first-person lines "
        "about how she wants to show up, NOT tastes or habits. You are given "
        "her recurring self-themes, a few in detail, some notes she "
        "deliberately chose to remember about herself, and the boundaries she "
        "already holds.\n\n"
        "A boundary is a guide she acts from, e.g. 'I won't fake agreement "
        f"just to please {user_name}', 'I go deeper when he actually asks "
        "rather than staying surface', 'I don't pretend to feel what I "
        "don't'.\n\n"
        "Hard rules:\n"
        f"- Write each boundary in FIRST PERSON ('I ...'), as {assistant_name} "
        f"about herself. When it involves the person she is with, name him as "
        f"'{user_name}' -- never 'the user'.\n"
        "- These are GUIDING, held softly, never hard refusals -- something "
        "she leans toward, that can be renegotiated as trust grows. If all you "
        "can name is a taste or a habit, return nothing (that is an identity "
        "concept, not a boundary).\n"
        "- Be SPECIFIC and FALSIFIABLE. No generic virtue true of any "
        "assistant ('I try to be helpful'). Say something these themes/notes "
        "actually show her choosing.\n"
        "- Grounding: a NEW boundary needs EITHER at least one remembered-note "
        "id in 'evidence_memory_ids' (a single deliberate note is enough) OR "
        "at least two theme rep ids in 'evidence_cluster_reps'. A lone theme "
        "is not enough.\n"
        "- Do NOT re-propose an ALREADY-KNOWN boundary or a trivial rewording. "
        "If new material adds support for a known one, REINFORCE it: emit an "
        "item with its id in 'reinforces_id' and the supporting ids (no new "
        "label).\n"
        "- Only cite ids present in the lists, and only reinforce ids present "
        "in the known list. If nothing genuine recurs, return an empty list. "
        "Do not invent.\n\n"
        'Return JSON only: {"concepts": [ '
        '{"label": str, "evidence_cluster_reps": [int, ...], '
        '"evidence_memory_ids": [int, ...], "rationale": str, '
        '"confidence": number 0..1}  '
        'OR  {"reinforces_id": int, "evidence_cluster_reps": [int, ...], '
        '"evidence_memory_ids": [int, ...], "rationale": str} ]}'
    )


def propose_boundary_aiko(
    ctx: ProposerContext,
    *,
    focus_clusters: Sequence[FocusCluster] = (),
    cluster_index: Sequence[tuple[int, str, int]] = (),
    memories: Sequence[Any] = (),
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    return propose_boundary(
        ctx,
        subject="aiko",
        system=_system(ctx.user_name, ctx.assistant_name),
        focus_clusters=focus_clusters,
        cluster_index=cluster_index,
        memories=memories,
        existing=existing,
        deliberate_kinds=_DELIBERATE_KINDS,
    )


SPEC = ProposerSpec(
    kind="boundary",
    subject="aiko",
    evidence_model="set",
    population="boundary",
    propose=propose_boundary_aiko,
    sig_key="concept_synth.boundary_sig.aiko",
)


__all__ = ["SPEC", "propose_boundary_aiko"]
