"""Aiko-affective proposer (subject=aiko, kind=affective, ``set``).

L13, first-person half. Notices how certain topics durably *move Aiko* --
what lifts her, what she shrinks from, what makes her flustered -- from two
signals combined: her recurring self-themes / conversation topics annotated
with her typical affect (the per-cluster affect map + aggregated self-memory
affect) AND her salient affect-stamped self-memories. Examples:
"explaining systems lifts me", "talking about love makes me flustered",
"I don't like talking about X".

Reuses the L11 :func:`propose_aiko_hybrid` combined-evidence body (mixed
``cluster`` + ``memory`` edges; a NEW concept needs >= ``min_sources``
distinct sources), with an affect-aware prompt and per-source affect
annotations. The affect *direction* lives in the label / rationale, not on
the edges. Surfaces as first-person tone guidance, never a stated fact.
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
        f"You are helping an AI companion named {assistant_name} notice how "
        "certain topics durably AFFECT HER -- what lifts her, what drains "
        "her, what she shrinks from or gets flustered by. You are given her "
        "recurring self-themes and conversation topics (clusters, each with a "
        "rep id and the emotion it typically carries for her) and a set of "
        "notable affect-stamped self-memories (each with an id and how it "
        "felt), plus the affective patterns she already holds.\n\n"
        "Examples: 'explaining systems lifts me', 'talking about love makes "
        "me flustered', 'conflict leaves me tense', 'quiet evenings with "
        f"{user_name} feel cozy', 'I don't enjoy talking about X'.\n\n"
        "Hard rules:\n"
        f"- Write each NEW pattern in FIRST PERSON ('X lifts me', 'Y makes me "
        f"anxious'), as {assistant_name} about herself. When it involves the "
        f"person she is with, name him as '{user_name}' -- never 'the "
        "user'.\n"
        "- Name a durable EMOTIONAL pattern tied to a topic, not a taste or a "
        "habit (those are identity concepts) and not her current mood. The "
        "label must state the FEELING the topic reliably brings.\n"
        "- Each NEW pattern MUST be backed by at least two distinct sources "
        "-- theme/topic rep ids and/or memory ids, in any mix. Trust the "
        "affect annotations, but only assert a feeling the sources agree on.\n"
        "- This is private tone guidance, NEVER a stated fact she announces.\n"
        "- Do NOT re-propose an ALREADY-KNOWN pattern or a trivial rewording. "
        "If a theme or memory adds fresh support for a known one, REINFORCE "
        "it: emit an item with its id in 'reinforces_id' and the supporting "
        "rep/memory ids (no new label).\n"
        "- Only cite rep ids and memory ids present in the lists, and only "
        "reinforce ids present in the known list. If nothing genuine recurs, "
        "return an empty list. Do not invent.\n\n"
        'Return JSON only: {"concepts": [ '
        '{"label": str, "evidence_cluster_reps": [int, ...], '
        '"evidence_memory_ids": [int, ...], "rationale": str, '
        '"confidence": number 0..1}  '
        'OR  {"reinforces_id": int, "evidence_cluster_reps": [int, ...], '
        '"evidence_memory_ids": [int, ...], "rationale": str} ]}'
    )


def propose_affective_aiko(
    ctx: ProposerContext,
    *,
    focus_clusters: Sequence[FocusCluster] = (),
    cluster_index: Sequence[tuple[int, str, int]] = (),
    affect_by_rep: "dict[int, str] | None" = None,
    memories: Sequence[Any] = (),
    memory_affect: "dict[int, str] | None" = None,
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    return propose_aiko_hybrid(
        ctx,
        kind="affective",
        system=_system(ctx.user_name, ctx.assistant_name),
        noun_plural="affective patterns",
        known_label="ALREADY-KNOWN AIKO AFFECTIVE PATTERNS",
        focus_clusters=focus_clusters,
        cluster_index=cluster_index,
        memories=memories,
        existing=existing,
        affect_by_rep=affect_by_rep,
        memory_affect=memory_affect,
        prompt_tail=(
            f"Propose NEW first-person affective patterns about "
            f"{ctx.assistant_name} -- how these topics durably move her -- "
            "grounded in the themes/topics and/or self-memories above (cite "
            "rep ids in 'evidence_cluster_reps' and/or memory ids in "
            "'evidence_memory_ids'), or reinforce a known one by id."
        ),
    )


SPEC = ProposerSpec(
    kind="affective",
    subject="aiko",
    evidence_model="set",
    population="affect",
    propose=propose_affective_aiko,
    sig_key="concept_synth.affect_sig.aiko",
)


__all__ = ["SPEC", "propose_affective_aiko"]
