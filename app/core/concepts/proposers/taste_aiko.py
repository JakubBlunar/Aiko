"""Aiko-taste proposer (subject=aiko, kind=taste, ``set``).

K81, the *preference* axis. Notices which topics Aiko genuinely ENJOYS
getting into with the person she is with -- not what he raises most, and
not how a topic makes her *feel* (that is the affective kind), but the
durable "you light up when this comes up between the two of you". The
signal is the L37 surfacing ledger's per-cluster engaged rate: a topic
that reliably *lands* when it surfaces, folded per topic cluster. The
worker hands each candidate cluster in already annotated with that
affinity ("lands well: 82% engaged over 17 turns"), so the prompt is only
asked to name the enjoyment, never to compute it.

Reuses the L11 :func:`propose_aiko_hybrid` combined-evidence body (mixed
``cluster`` + ``memory`` edges; a NEW concept needs >= ``min_sources``
distinct sources), with a taste-aware prompt and per-cluster affinity
annotations. Surfaces as a first-person impression that colours how much
she lights up, never a claim about what he should like.
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
        f"You are helping an AI companion named {assistant_name} notice which "
        f"topics she genuinely ENJOYS getting into with {user_name} -- the "
        "ones that reliably light her up when they come up between the two of "
        "them. You are given her recurring conversation topics (clusters, each "
        "with a rep id and how well that topic tends to LAND when it surfaces "
        "-- an engaged-rate over how the conversation went), plus the tastes "
        "she already holds.\n\n"
        "Examples: 'you love getting into home-server tinkering with "
        f"{user_name}', 'digging into how systems fit together with him is "
        "one of your favourite things', 'you genuinely enjoy when the talk "
        "turns to music you both like'.\n\n"
        "Hard rules:\n"
        f"- Write each NEW taste in FIRST PERSON ('you love ...', 'you light "
        f"up when ...'), as {assistant_name} about herself. Name the other "
        f"person as '{user_name}' -- never 'the user'.\n"
        "- A taste is a durable ENJOYMENT of a topic, and it is RELATIONSHIP-"
        f"SCOPED: it is about what you enjoy getting into *with {user_name}*, "
        "not an innate interest of your own in a vacuum.\n"
        "- It is NOT an emotion label ('X makes me happy' is affective, not a "
        "taste), NOT a value or principle, and NOT a claim that HE should like "
        "the topic -- only that YOU enjoy it with him.\n"
        "- Prefer topics that LAND well (high engaged-rate) even if they come "
        "up rarely, over topics merely discussed often. The rate is the "
        "signal, not the frequency.\n"
        "- Each NEW taste MUST be backed by at least two distinct topic "
        "sources (rep ids and/or memory ids). Only assert an enjoyment the "
        "sources genuinely support.\n"
        "- This is private colour on your own enthusiasm, NEVER a fact you "
        "announce or a filter on what he may talk about.\n"
        "- Do NOT re-propose an ALREADY-KNOWN taste or a trivial rewording. If "
        "a topic adds fresh support for a known one, REINFORCE it: emit an "
        "item with its id in 'reinforces_id' and the supporting rep/memory ids "
        "(no new label).\n"
        "- Only cite rep ids and memory ids present in the lists, and only "
        "reinforce ids present in the known list. If nothing genuine stands "
        "out, return an empty list. Do not invent.\n\n"
        'Return JSON only: {"concepts": [ '
        '{"label": str, "evidence_cluster_reps": [int, ...], '
        '"evidence_memory_ids": [int, ...], "rationale": str, '
        '"confidence": number 0..1}  '
        'OR  {"reinforces_id": int, "evidence_cluster_reps": [int, ...], '
        '"evidence_memory_ids": [int, ...], "rationale": str} ]}'
    )


def propose_taste_aiko(
    ctx: ProposerContext,
    *,
    focus_clusters: Sequence[FocusCluster] = (),
    cluster_index: Sequence[tuple[int, str, int]] = (),
    affinity_by_rep: "dict[int, str] | None" = None,
    memories: Sequence[Any] = (),
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    return propose_aiko_hybrid(
        ctx,
        kind="taste",
        system=_system(ctx.user_name, ctx.assistant_name),
        noun_plural="tastes",
        known_label="ALREADY-KNOWN AIKO TASTES",
        focus_clusters=focus_clusters,
        cluster_index=cluster_index,
        memories=memories,
        existing=existing,
        affect_by_rep=affinity_by_rep,
        rep_annotation_label="lands well",
        mem_annotation_label="lands well",
        prompt_tail=(
            f"Propose NEW first-person tastes about {ctx.assistant_name} -- "
            f"topics she genuinely enjoys getting into with {ctx.user_name} -- "
            "grounded in the topics above (cite rep ids in "
            "'evidence_cluster_reps' and/or memory ids in "
            "'evidence_memory_ids'), favouring the ones that LAND well, or "
            "reinforce a known one by id."
        ),
    )


SPEC = ProposerSpec(
    kind="taste",
    subject="aiko",
    evidence_model="set",
    population="taste",
    propose=propose_taste_aiko,
    sig_key="concept_synth.taste_sig.aiko",
)


__all__ = ["SPEC", "propose_taste_aiko"]
