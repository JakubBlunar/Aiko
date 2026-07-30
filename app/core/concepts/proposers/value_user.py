"""User-value proposer (subject=user, kind=value, ``set``).

Where the identity proposer names *what* the user is into, this one names
the normative *why* underneath -- the principle a group of otherwise
separate topics all express ("he values owning his data" under
self-hosting + local-first AI + privacy tools). Values are the deepest
"gets me" layer: they predict reactions to topics never discussed. Same
``set``/cluster machinery as identity, but the prompt asks for the shared
principle rather than the activity, and the L3 gate for ``value`` promotes
it more slowly (:func:`value_evidence_gate`).

Evidence edges point ``cluster`` nodes at the concept; a new value must
span at least ``min_sources`` distinct clusters including one focus
cluster.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from app.core.conversation.topic_graph import CoactivationMode


def _system(user_name: str, assistant_name: str) -> str:
    return (
        f"You find higher-order VALUE concepts about {user_name} -- the "
        "normative PRINCIPLE underneath their choices, the 'why' a group of "
        "otherwise-separate topics all express. You are given a map of "
        f"{user_name}'s topic clusters (labels + sizes), a few focus "
        "clusters in detail, and the values already known. Propose a value "
        "that a FOCUS cluster shares with at least one other cluster.\n\n"
        "A value is NOT an interest or an activity -- it is the principle "
        "that would predict how they'd react to a NEW topic. Examples: "
        "'owning and controlling his own data' (under self-hosting + "
        "local-first tools + privacy), 'craftsmanship over speed', "
        "'self-reliance', 'honesty even when it's costly'.\n\n"
        "Hard rules:\n"
        f"- Write every value ABOUT {user_name}, naming them as "
        f"'{user_name}' -- never 'the user' or a bare pronoun. Phrase each "
        f"label as a principle they hold, e.g. '{user_name} values ...' / "
        f"'{user_name} cares more about ... than ...'. Refer to the AI "
        f"companion as '{assistant_name}'.\n"
        "- Name the PRINCIPLE, not the topic. Do NOT restate an interest or "
        "trait (that is an identity concept, not a value). If all you can "
        "say is what they DO or LIKE, return nothing -- go deeper or skip.\n"
        "- Each NEW value MUST span at least two distinct clusters (by rep "
        "id) that the principle genuinely explains.\n"
        "- Be SPECIFIC and FALSIFIABLE. No values true of everyone ('values "
        "happiness'). Say something a person could actually NOT hold.\n"
        "- State the principle and STOP. One clause, no trailing "
        "', treating X as Y' / ', viewing X as Y' / ', believing that ...' "
        "-- that is a second claim smuggled onto the first, and it belongs "
        "in 'rationale'. A value reads best bare: "
        f"'{user_name} values craftsmanship over speed.'\n"
        "- Do NOT re-propose an ALREADY-KNOWN value or a trivial rewording. "
        "If a focus cluster adds fresh support for a known value, REINFORCE "
        "it: emit an item with its id in 'reinforces_id' and the supporting "
        "rep ids (no new label).\n"
        "- Only cite rep ids present in the provided map, and only reinforce "
        "ids present in the known list.\n"
        "- If nothing genuine connects the focus clusters under a shared "
        "principle, return an empty list. Do not invent.\n\n"
        'Return JSON only: {"concepts": [ '
        '{"label": str, "evidence_cluster_reps": [int, ...], "rationale": '
        'str, "confidence": number 0..1}  '
        'OR  {"reinforces_id": int, "evidence_cluster_reps": [int, ...], '
        '"rationale": str} ]}'
    )


def _coactivation_lines(
    coactivation: "Sequence[CoactivationMode]",
    valid_reps: set[int],
) -> list[str]:
    """Render the L4 "TOPIC MODES" hint: clusters that keep lighting up in
    the same conversations, as ``[rep, rep, ...]`` groups. Only reps present
    in the current map survive. Returns ``[]`` when nothing usable."""
    lines: list[str] = []
    for mode in coactivation:
        reps = [int(r) for r in getattr(mode, "reps", ()) if int(r) in valid_reps]
        if len(reps) < 2:
            continue
        lines.append("- [" + ", ".join(str(r) for r in reps) + "]")
    return lines


def propose_value_user(
    ctx: ProposerContext,
    *,
    focus_clusters: Sequence[FocusCluster],
    cluster_index: Sequence[tuple[int, str, int]],
    existing: Sequence[ExistingConcept] = (),
    coactivation: "Sequence[CoactivationMode]" = (),
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
    mode_lines = _coactivation_lines(coactivation, valid_reps)
    focus_lines = []
    for fc in focus_clusters:
        parts = [f"[{fc.rep}] {fc.label} (size {fc.size})"]
        if fc.representative:
            parts.append(f"  representative: {snippet(fc.representative)}")
        if fc.digest:
            parts.append(f"  digest: {snippet(fc.digest)}")
        focus_lines.append("\n".join(parts))

    modes_section = ""
    if mode_lines:
        modes_section = (
            "\n\nTOPIC MODES (clusters that tend to light up together in the "
            "same conversations -- a soft hint, not a rule):\n"
            + "\n".join(mode_lines)
            + "\n(A shared principle often explains why clusters co-fire -- "
            "prefer these when a genuine value connects them, but never force "
            "a link.)"
        )

    user = (
        "FULL TOPIC MAP (all clusters, by size):\n"
        + "\n".join(map_lines)
        + modes_section
        + "\n\nFOCUS CLUSTERS (detail):\n"
        + "\n\n".join(focus_lines)
        + "\n\nALREADY-KNOWN USER VALUES:\n"
        + format_existing(existing)
        + f"\n\nPropose NEW values about {ctx.user_name} -- the principle "
        "beneath these focus clusters and others -- or reinforce a known "
        "one by id."
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
                    kind="value",
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
                kind="value",
                subject="user",
                evidence_model="set",
            )
        )
    return proposals


SPEC = ProposerSpec(
    kind="value",
    subject="user",
    evidence_model="set",
    population="clusters",
    propose=propose_value_user,
    sig_key="concept_synth.cluster_sigs.value",
)


__all__ = ["SPEC", "propose_value_user"]
