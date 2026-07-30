"""Aiko-identity proposer (subject=aiko, kind=identity, ``set``).

Notices higher-order concepts about Aiko *herself* -- her stance, tastes,
values-in-action, and ways of being -- by connecting BOTH her recurring
self-themes (aiko-dominant memory clusters) AND her salient individual
first-person memories (``self`` / ``reflection`` / ``diary`` kinds), in one
combined pass (L11 parity with the user path). Evidence edges may mix
``cluster`` and ``memory`` nodes; a NEW concept requires >= ``min_sources``
total distinct sources. When she has no self-themes yet this degrades to
memories-only (cold start).
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
    propose_aiko_hybrid,
)


def _system(user_name: str, assistant_name: str) -> str:
    return (
        f"You are helping an AI companion named {assistant_name} notice "
        "higher-order concepts about HERSELF -- her own stance, tastes, "
        "values-in-action, and ways of being. You are given her recurring "
        "self-themes (clusters of her own memories, each with a rep id) and "
        "a set of notable individual self-memories (self notes, reflections, "
        "diary entries, each with an id), plus the concepts she already "
        "holds about herself.\n\n"
        "Hard rules:\n"
        f"- Write each NEW concept in FIRST PERSON ('I ...'), as "
        f"{assistant_name} about herself. When a concept involves the "
        f"person she is with, name him as '{user_name}' -- never 'the "
        "user'.\n"
        "- Each NEW concept MUST be backed by at least two distinct sources "
        "-- theme rep ids and/or memory ids, in any mix.\n"
        "- Be SPECIFIC and FALSIFIABLE. No generic self-flattery true of "
        "any assistant. Say something these themes/memories actually "
        "show -- not a theory about what it signifies. Naming what you "
        "feel and want is the point here; dressing an ordinary moment in "
        "borrowed technical vocabulary is not.\n"
        "- Keep the label to ONE claim. If it needs a comma to add what "
        "the pattern reveals, cut everything after the comma and put it "
        "in 'rationale' instead.\n"
        "- Do NOT re-propose an ALREADY-KNOWN concept or a trivial "
        "rewording of one. If a theme or memory instead adds fresh support "
        "for a known concept, REINFORCE it: emit an item with its id in "
        "'reinforces_id' and the supporting rep/memory ids (no new label).\n"
        "- Only cite rep ids and memory ids present in the lists, and only "
        "reinforce ids present in the known list. If nothing genuine "
        "recurs, return an empty list. Do not invent.\n\n"
        'Return JSON only: {"concepts": [ '
        '{"label": str, "evidence_cluster_reps": [int, ...], '
        '"evidence_memory_ids": [int, ...], "rationale": str, '
        '"confidence": number 0..1}  '
        'OR  {"reinforces_id": int, "evidence_cluster_reps": [int, ...], '
        '"evidence_memory_ids": [int, ...], "rationale": str} ]}'
    )


def propose_identity_aiko(
    ctx: ProposerContext,
    *,
    focus_clusters: Sequence[FocusCluster] = (),
    cluster_index: Sequence[tuple[int, str, int]] = (),
    memories: Sequence[Any] = (),
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    return propose_aiko_hybrid(
        ctx,
        kind="identity",
        system=_system(ctx.user_name, ctx.assistant_name),
        noun_plural="identity concepts",
        known_label="ALREADY-KNOWN AIKO IDENTITY CONCEPTS",
        focus_clusters=focus_clusters,
        cluster_index=cluster_index,
        memories=memories,
        existing=existing,
    )


SPEC = ProposerSpec(
    kind="identity",
    subject="aiko",
    evidence_model="set",
    population="aiko_memories",
    propose=propose_identity_aiko,
    sig_key="concept_synth.aiko_sig.identity",
)


__all__ = ["SPEC", "propose_identity_aiko"]
