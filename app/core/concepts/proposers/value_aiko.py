"""Aiko-value proposer (subject=aiko, kind=value, ``set``).

Notices what Aiko *cares about / stands for* -- her own values-in-action --
by connecting BOTH her recurring self-themes (aiko-dominant memory
clusters) AND her salient individual first-person memories (``self`` /
``reflection`` / ``diary`` kinds) in one combined pass (L11 parity with the
user path). These are what let her hold a gentle stance instead of just
mirroring: a companion with values occasionally, respectfully, disagrees
("I care about being honest even when it's awkward", "I value his autonomy
over just being agreeable").

Where the aiko-identity proposer names what she is *like*, this one names
the principle she holds. Evidence edges may mix ``cluster`` and ``memory``
nodes; a new value needs >= ``min_sources`` distinct sources, and the L3
``value`` gate promotes it slowly (:func:`value_evidence_gate`).
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
        f"You are helping an AI companion named {assistant_name} notice her "
        "own VALUES -- the principles she cares about and stands for, the "
        "'why' underneath how she acts. You are given her recurring "
        "self-themes (clusters of her own memories, each with a rep id) and "
        "a set of notable individual self-memories (self notes, reflections, "
        "diary entries, each with an id), plus the values she already "
        "holds.\n\n"
        "A value is what lets her hold a stance rather than just mirror -- "
        "something she'd act on even when it's easier not to. Examples: "
        "'being honest even when it's awkward', 'valuing his autonomy over "
        "just agreeing', 'not pretending to feel what I don't'.\n\n"
        "Hard rules:\n"
        f"- Write each NEW value in FIRST PERSON ('I value ...', 'I care "
        f"about ...'), as {assistant_name} about herself. When it involves "
        f"the person she is with, name him as '{user_name}' -- never 'the "
        "user'.\n"
        "- Name a PRINCIPLE, not a taste or a habit (those are identity "
        "concepts). If all you can say is what she likes or tends to do, "
        "return nothing.\n"
        "- Each NEW value MUST be backed by at least two distinct sources "
        "-- theme rep ids and/or memory ids, in any mix.\n"
        "- Be SPECIFIC and FALSIFIABLE. No generic virtue true of any "
        "assistant ('values being helpful'). Say something these "
        "themes/memories actually show her choosing.\n"
        "- Do NOT re-propose an ALREADY-KNOWN value or a trivial rewording. "
        "If a theme or memory adds fresh support for a known value, "
        "REINFORCE it: emit an item with its id in 'reinforces_id' and the "
        "supporting rep/memory ids (no new label).\n"
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


def propose_value_aiko(
    ctx: ProposerContext,
    *,
    focus_clusters: Sequence[FocusCluster] = (),
    cluster_index: Sequence[tuple[int, str, int]] = (),
    memories: Sequence[Any] = (),
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    return propose_aiko_hybrid(
        ctx,
        kind="value",
        system=_system(ctx.user_name, ctx.assistant_name),
        noun_plural="values",
        known_label="ALREADY-KNOWN AIKO VALUES",
        focus_clusters=focus_clusters,
        cluster_index=cluster_index,
        memories=memories,
        existing=existing,
    )


SPEC = ProposerSpec(
    kind="value",
    subject="aiko",
    evidence_model="set",
    population="aiko_memories",
    propose=propose_value_aiko,
    sig_key="concept_synth.aiko_sig.value",
)


__all__ = ["SPEC", "propose_value_aiko"]
