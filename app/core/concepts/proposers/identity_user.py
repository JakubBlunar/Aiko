"""User-identity proposer (subject=user, kind=identity, ``set``).

Finds higher-order identity concepts by connecting topics that look
separate but reflect the same underlying trait/interest. Evidence edges
point ``cluster`` nodes at the concept; requires >= ``min_sources``
distinct clusters including at least one focus cluster.

**The register rules exist because this proposer collapsed.** Asked to be
"specific and falsifiable", the model satisfied it by over-*interpreting*
rather than by observing more closely, and converged on one sentence:
*"<name> treats <mundane activity> as a <engineering metaphor> that
verifies <emotional need>"*. Seventy-two percent of the labels it had
produced carried that frame; a quarter opened with the literal words
"<name> treats the". So the rules below ban the specific move --
inventing a function for an ordinary activity, and importing vocabulary
from an unrelated domain -- rather than banning interpretation.

That distinction is why they live here and not in
:mod:`app.core.concepts.proposers.base`. Several kinds are interpretive
*by design*: ``value`` is the normative why under a choice (L10), and
``tension`` / ``generalization`` / ``affective`` / ``aspiration`` are all
inference. Measured over the same graph they show near-zero contamination
on both markers, so a shared "don't say what it really means" rule would
contradict their purpose and damage output that is currently good.
:mod:`app.core.concepts.concept_quality` tracks the two rates per (kind,
subject), which is the regression guard on both sides of that line.
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
        f"You find higher-order IDENTITY concepts about {user_name} by "
        "connecting topics that look separate but reflect the same "
        f"underlying trait, interest, or way of being. You are given a map "
        f"of {user_name}'s topic clusters (labels + sizes), a few focus "
        "clusters in detail, and the identity concepts already known. "
        "Propose concepts that link a FOCUS cluster to at least one other "
        "cluster in the map.\n\n"
        "Hard rules:\n"
        f"- Write every concept ABOUT {user_name}, naming them as "
        f"'{user_name}' -- never 'the user', 'the person', or a bare "
        f"pronoun. Phrase each label as a statement about them, e.g. "
        f"'{user_name} values ...' / '{user_name} approaches ... by ...'. "
        f"Refer to the AI companion as '{assistant_name}'.\n"
        "- Each NEW concept MUST span at least two distinct clusters (by "
        "rep id).\n"
        "- Be SPECIFIC and FALSIFIABLE -- specific in what you OBSERVED, "
        f"not in how cleverly you explain it. A good concept is one you "
        f"could watch {user_name} for a week and find false. No horoscope "
        "traits like 'is curious' or 'is intelligent' that are true of "
        "everyone and disprovable by no one. Say something the raw "
        "cluster labels do not already say.\n"
        "- Say what they DO, prefer, or keep returning to -- not what it "
        "'really means'. Do not assign a hidden function to an ordinary "
        "activity: a bath is a bath, a snack is a snack, a typo is a "
        "typo. If the label claims one thing is really a way of "
        "verifying, validating, testing, or proving another, you have "
        "invented a theory instead of noticing a pattern.\n"
        "- Never describe one part of their life in vocabulary borrowed "
        "from another. Technical language belongs in concepts about "
        "technical work, not in concepts about food, rest, affection, or "
        f"routine. Bad: '{user_name} treats the scheduling of baths as a "
        "mandatory system idle protocol required to transition from "
        f"high-cognitive debugging to low-stakes intimacy.' Good: "
        f"'{user_name} winds down with a long bath after a hard "
        "debugging session and comes back noticeably warmer.'\n"
        "- Keep the label to ONE claim. If it needs a comma to add what "
        "the behaviour demonstrates or why it matters, cut everything "
        "after the comma -- interpretation belongs in 'rationale', which "
        "is where you should put it.\n"
        "- Do NOT re-propose an ALREADY-KNOWN concept or a trivial "
        "rewording of one. If a focus cluster instead adds fresh support "
        "for a known concept, REINFORCE it: emit an item with its id in "
        "'reinforces_id' and the supporting rep ids (no new label).\n"
        "- Only cite rep ids present in the provided map, and only "
        "reinforce ids present in the known list.\n"
        "- If nothing genuine connects the focus clusters to others, "
        "return an empty list. Do not invent.\n\n"
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
    in the current map survive (a mode built off a since-refit cluster is
    dropped). Returns ``[]`` when nothing usable, so the caller can omit the
    whole section."""
    lines: list[str] = []
    for mode in coactivation:
        reps = [int(r) for r in getattr(mode, "reps", ()) if int(r) in valid_reps]
        if len(reps) < 2:
            continue
        lines.append("- [" + ", ".join(str(r) for r in reps) + "]")
    return lines


def propose_identity_user(
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
            + "\n(Prefer connecting clusters that co-fire here when a genuine "
            "shared trait explains why -- but only if it is real; never force "
            "a link just because two clusters appear together.)"
        )

    user = (
        "FULL TOPIC MAP (all clusters, by size):\n"
        + "\n".join(map_lines)
        + modes_section
        + "\n\nFOCUS CLUSTERS (detail):\n"
        + "\n\n".join(focus_lines)
        + "\n\nALREADY-KNOWN USER IDENTITY CONCEPTS:\n"
        + format_existing(existing)
        + f"\n\nPropose NEW identity concepts about {ctx.user_name} "
        "connecting these focus clusters to others, or reinforce a known "
        "one by id."
    )

    raw = ctx.call_llm(_system(ctx.user_name, ctx.assistant_name), user)
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
