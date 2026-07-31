"""User-affective proposer (subject=user, kind=affective, ``set``).

L13. Where identity names *what* the user is into and value names the *why*
beneath it, this one names the durable *emotional signature* of a topic --
how it tends to make him feel. Each topic cluster is annotated with its
typical affect (from the per-cluster affect map the post-turn sampler
maintains); the proposer looks for a durable emotion that a group of
related topics share ("hands-on building energizes him"; "admin / logistics
topics drain him"; "release-week pressure stresses him out").

Distinct from K2 mood beliefs (which model his *current* mood): these are
the settled pattern. Evidence is cluster-only (``cluster`` edges); a NEW
affective concept must span at least ``min_sources`` distinct clusters that
share the affect, so a one-off mood on a single topic never becomes a
durable claim. The affect *direction* is carried in the label / rationale,
not on the edges (no schema change).
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


def _system(user_name: str, assistant_name: str) -> str:
    return (
        f"You find AFFECTIVE concepts about {user_name} -- the durable "
        "EMOTIONAL signature of a topic, how it reliably tends to make them "
        "feel over time. You are given a map of their topic clusters (labels "
        "+ sizes), each annotated with the emotion it typically carries, a "
        "few focus clusters in detail, and the affective patterns already "
        "known. Propose a durable topic->emotion pattern that a FOCUS "
        "cluster shares with at least one other cluster.\n\n"
        "Examples: 'hands-on building energizes him', 'admin and logistics "
        "drain him', 'release-week pressure stresses him out', 'debugging "
        "frustrates him before it satisfies him'.\n\n"
        "Hard rules:\n"
        f"- Write every pattern ABOUT {user_name}, naming them as "
        f"'{user_name}' -- never 'the user' or a bare pronoun. Refer to the "
        f"AI companion as '{assistant_name}'.\n"
        "- Name a durable EMOTIONAL pattern, not the topic or the activity "
        "(that is an identity concept). The label must state the FEELING "
        "('X energizes him', 'Y drains him', 'Z makes him anxious').\n"
        "- Each NEW pattern MUST span at least two distinct clusters (by rep "
        "id) that genuinely share the same emotional signature. Trust the "
        "per-cluster affect annotations, but only group clusters whose "
        "affect actually agrees.\n"
        "- This is tone guidance, NEVER a stated fact. Do not propose "
        "patterns you would announce out loud ('you always get stressed').\n"
        "- Do NOT re-propose an ALREADY-KNOWN pattern or a trivial rewording. "
        "If a focus cluster adds fresh support for a known one, REINFORCE it: "
        "emit an item with its id in 'reinforces_id' and the supporting rep "
        "ids (no new label).\n"
        "- Only cite rep ids present in the provided map, and only reinforce "
        "ids present in the known list.\n"
        "- If no genuine shared emotional pattern connects the focus "
        "clusters, return an empty list. Do not invent.\n\n"
        'Return JSON only: {"concepts": [ '
        '{"label": str, "evidence_cluster_reps": [int, ...], "rationale": '
        'str, "confidence": number 0..1}  '
        'OR  {"reinforces_id": int, "evidence_cluster_reps": [int, ...], '
        '"rationale": str} ]}'
    )


def propose_affective_user(
    ctx: ProposerContext,
    *,
    focus_clusters: Sequence[FocusCluster],
    cluster_index: Sequence[tuple[int, str, int]],
    affect_by_rep: "dict[int, str] | None" = None,
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    if not focus_clusters or not cluster_index:
        return []
    aff = affect_by_rep or {}
    valid_reps = {int(rep) for rep, _label, _size in cluster_index}
    focus_reps = {int(fc.rep) for fc in focus_clusters}
    existing_ids = {int(e.id) for e in existing}

    def _rep_line(rep: int, label: str, size: int) -> str:
        line = f"- [{rep}] {label} (size {size})"
        if rep in aff:
            line += f"  (feels: {aff[rep]})"
        return line

    map_lines = [
        _rep_line(rep, label, size) for rep, label, size in cluster_index
    ]
    focus_lines = []
    for fc in focus_clusters:
        head = f"[{fc.rep}] {fc.label} (size {fc.size})"
        if fc.rep in aff:
            head += f"  (feels: {aff[fc.rep]})"
        parts = [head]
        if fc.representative:
            parts.append(f"  representative: {snippet(fc.representative)}")
        if fc.digest:
            parts.append(f"  digest: {snippet(fc.digest)}")
        focus_lines.append("\n".join(parts))

    user = (
        "FULL TOPIC MAP (all clusters with affect annotation, by size):\n"
        + "\n".join(map_lines)
        + "\n\nFOCUS CLUSTERS (detail):\n"
        + "\n\n".join(focus_lines)
        + "\n\nALREADY-KNOWN USER AFFECTIVE PATTERNS:\n"
        + format_existing(existing)
        + f"\n\nPropose NEW affective patterns about {ctx.user_name} -- the "
        "durable emotion shared by these focus clusters and others -- or "
        "reinforce a known one by id."
    )

    raw = ctx.call_llm(_system(ctx.user_name, ctx.assistant_name), user)
    proposals: list[CandidateProposal] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        reps = coerce_id_list(item.get("evidence_cluster_reps"))
        reps = list(dict.fromkeys(r for r in reps if r in valid_reps))
        if not reps or not (set(reps) & focus_reps):
            continue
        evidence = [("cluster", str(r)) for r in reps]
        rationale = str(item.get("rationale") or "").strip()

        reinforces = resolve_reinforces(
            item.get("reinforces_id"), existing_ids
        )
        if reinforces is not None:
            proposals.append(
                CandidateProposal(
                    label="",
                    rationale=rationale,
                    confidence=0.0,
                    evidence=evidence,
                    kind="affective",
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
                kind="affective",
                subject="user",
                evidence_model="set",
            )
        )
    return proposals


SPEC = ProposerSpec(
    kind="affective",
    subject="user",
    evidence_model="set",
    population="affect",
    propose=propose_affective_user,
    sig_key="concept_synth.affect_sig.user",
)


__all__ = ["SPEC", "propose_affective_user"]
