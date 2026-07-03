"""User-identity proposer (subject=user, kind=identity, ``set``).

Finds higher-order identity concepts by connecting topics that look
separate but reflect the same underlying trait/interest. Evidence edges
point ``cluster`` nodes at the concept; requires >= ``min_sources``
distinct clusters including at least one focus cluster.
"""
from __future__ import annotations

from collections.abc import Sequence

from app.core.concepts.proposers.base import (
    CandidateProposal,
    ExistingConcept,
    FocusCluster,
    ProposerContext,
    ProposerSpec,
    clamp01,
    coerce_id_list,
    format_existing,
    resolve_reinforces,
    snippet,
)

_SYSTEM = (
    "You find higher-order IDENTITY concepts about a person by connecting "
    "topics that look separate but reflect the same underlying trait, "
    "interest, or way of being. You are given a map of their topic "
    "clusters (labels + sizes), a few focus clusters in detail, and the "
    "identity concepts already known. Propose concepts that link a FOCUS "
    "cluster to at least one other cluster in the map.\n\n"
    "Hard rules:\n"
    "- Each NEW concept MUST span at least two distinct clusters (by rep "
    "id).\n"
    "- Be SPECIFIC and FALSIFIABLE. No horoscope traits like 'is curious' "
    "or 'is intelligent' that are true of everyone and disprovable by "
    "no one. Say something the raw cluster labels do not already say.\n"
    "- Do NOT re-propose an ALREADY-KNOWN concept or a trivial rewording "
    "of one. If a focus cluster instead adds fresh support for a known "
    "concept, REINFORCE it: emit an item with its id in 'reinforces_id' "
    "and the supporting rep ids (no new label).\n"
    "- Only cite rep ids present in the provided map, and only reinforce "
    "ids present in the known list.\n"
    "- If nothing genuine connects the focus clusters to others, return "
    "an empty list. Do not invent.\n\n"
    'Return JSON only: {"concepts": [ '
    '{"label": str, "evidence_cluster_reps": [int, ...], "rationale": '
    'str, "confidence": number 0..1}  '
    'OR  {"reinforces_id": int, "evidence_cluster_reps": [int, ...], '
    '"rationale": str} ]}'
)


def propose_identity_user(
    ctx: ProposerContext,
    *,
    focus_clusters: Sequence[FocusCluster],
    cluster_index: Sequence[tuple[int, str, int]],
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    if not focus_clusters or not cluster_index:
        return []
    valid_reps = {int(rep) for rep, _label, _size in cluster_index}
    focus_reps = {int(fc.rep) for fc in focus_clusters}
    existing_ids = {int(e.id) for e in existing}

    map_lines = [
        f"- [{rep}] {label} (size {size})"
        for rep, label, size in cluster_index
    ]
    focus_lines = []
    for fc in focus_clusters:
        parts = [f"[{fc.rep}] {fc.label} (size {fc.size})"]
        if fc.representative:
            parts.append(f"  representative: {snippet(fc.representative)}")
        if fc.digest:
            parts.append(f"  digest: {snippet(fc.digest)}")
        focus_lines.append("\n".join(parts))

    user = (
        "FULL TOPIC MAP (all clusters, by size):\n"
        + "\n".join(map_lines)
        + "\n\nFOCUS CLUSTERS (detail):\n"
        + "\n\n".join(focus_lines)
        + "\n\nALREADY-KNOWN USER IDENTITY CONCEPTS:\n"
        + format_existing(existing)
        + "\n\nPropose NEW identity concepts connecting these focus "
        "clusters to others, or reinforce a known one by id."
    )

    raw = ctx.call_llm(_SYSTEM, user)
    proposals: list[CandidateProposal] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        reps = coerce_id_list(item.get("evidence_cluster_reps"))
        # Keep only known reps, dedupe, require at least one focus cluster.
        reps = list(dict.fromkeys(r for r in reps if r in valid_reps))
        if not reps or not (set(reps) & focus_reps):
            continue
        evidence = [("cluster", str(r)) for r in reps]
        rationale = str(item.get("rationale") or "").strip()

        reinforces = resolve_reinforces(
            item.get("reinforces_id"), existing_ids
        )
        if reinforces is not None:
            # Reinforcement: existing concept, >=1 new source is enough.
            proposals.append(
                CandidateProposal(
                    label="",
                    rationale=rationale,
                    confidence=0.0,
                    evidence=evidence,
                    kind="identity",
                    subject="user",
                    evidence_model="set",
                    reinforces_id=reinforces,
                )
            )
            continue

        label = str(item.get("label") or "").strip()
        if not label or len(reps) < ctx.min_sources:
            continue
        proposals.append(
            CandidateProposal(
                label=label,
                rationale=rationale,
                confidence=clamp01(item.get("confidence")),
                evidence=evidence,
                kind="identity",
                subject="user",
                evidence_model="set",
            )
        )
    return proposals


SPEC = ProposerSpec(
    kind="identity",
    subject="user",
    evidence_model="set",
    population="clusters",
    propose=propose_identity_user,
)


__all__ = ["SPEC", "propose_identity_user"]
