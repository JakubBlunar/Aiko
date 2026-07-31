"""Relationship-ritual proposer (subject=relationship, kind=ritual, ``set``).

L7. Where identity/value/affective concepts are about a *person*, this one is
about the *pair*: the recurring "this is a thing you two do" pattern -- a
named relationship ritual mined from ``shared_moment`` memories ("Friday
debugging evenings", "the pre-release nerves-and-tea", "the end-of-day
check-in").

The worker's ``_run_ritual_pass`` does the grouping (single-link cosine over
the moments), and hands each :class:`~app.core.concepts.ritual_grouping.RitualGroup`
in -- a cluster of recurring moments annotated with a dominant vibe + weekday
hint. This proposer names each group as a warm, concrete ritual, or reinforces
a known one. Evidence is the constituent shared moments (``memory`` edges); a
NEW ritual must draw on at least ``min_sources`` distinct moments so a one-off
evening never becomes a "ritual".
"""
from __future__ import annotations

from collections.abc import Sequence

from app.core.concepts.proposers.base import (
    CandidateProposal,
    ExistingConcept,
    ProposerContext,
    ProposerSpec,
    clamp01,
    format_existing,
    resolve_reinforces,
)
from app.core.concepts.ritual_grouping import RitualGroup

# How many member moments to show per group -- bounds the prompt regardless of
# how many moments a long-running ritual has accrued.
_MAX_MEMBERS_SHOWN = 8


def _system(user_name: str, assistant_name: str) -> str:
    return (
        f"You find RELATIONSHIP RITUALS between {user_name} and "
        f"{assistant_name} -- the recurring 'this is a thing the two of you "
        "do' patterns. You are given numbered GROUPS of shared moments that "
        "cluster together (each with its dominant vibe and, when there is "
        "one, the weekday it tends to happen), and the rituals already known. "
        "Name a durable, specific ritual that a group represents.\n\n"
        "Examples: 'Friday late-night debugging sessions', 'winding down "
        "together at the end of a hard day', 'celebrating small wins with "
        "silly victory dances', 'talking through nerves before a release'.\n\n"
        "Hard rules:\n"
        f"- Write every ritual ABOUT the pair -- '{user_name} and "
        f"{assistant_name}' or 'the two of you' -- never about one person "
        "alone (that would be an identity concept).\n"
        "- Name the RITUAL (the recurring shared activity + its feel), warm "
        "and concrete, not a one-off event and not a generic label. Fold in "
        "the vibe / weekday when it sharpens the name.\n"
        "- Each NEW ritual MUST come from a single group (by its index) that "
        "genuinely recurs. Only propose a ritual when the moments really are "
        "the same recurring thing; if a group is just unrelated moments that "
        "happen to cluster, skip it.\n"
        "- These are warm impressions of the relationship, held lightly -- "
        "not stated facts. Do not name something you would announce ('we "
        "always do X').\n"
        "- Do NOT re-propose an ALREADY-KNOWN ritual or a trivial rewording. "
        "If a group adds fresh support for a known one, REINFORCE it: emit an "
        "item with its id in 'reinforces_id' and the group index (no new "
        "label).\n"
        "- Only cite group indices present below, and only reinforce ids "
        "present in the known list.\n"
        "- If no group is a genuine recurring ritual, return an empty list. "
        "Do not invent.\n\n"
        'Return JSON only: {"concepts": [ '
        '{"label": str, "group_index": int, "rationale": str, '
        '"confidence": number 0..1}  '
        'OR  {"reinforces_id": int, "group_index": int, "rationale": str} ]}'
    )


def _group_block(index: int, group: RitualGroup) -> str:
    bits = [f"vibe: {group.dominant_vibe}"]
    if group.weekday_hint:
        bits.append(f"usually {group.weekday_hint}")
    bits.append(f"{group.size} moments")
    head = f"GROUP [{index}] ({', '.join(bits)}):"
    lines = [
        f"  - [{m.id}] {m.text}" for m in group.members[:_MAX_MEMBERS_SHOWN]
    ]
    if group.size > _MAX_MEMBERS_SHOWN:
        lines.append(f"  ... and {group.size - _MAX_MEMBERS_SHOWN} more")
    return head + "\n" + "\n".join(lines)


def propose_relationship_ritual(
    ctx: ProposerContext,
    *,
    groups: Sequence[RitualGroup],
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    if not groups:
        return []
    by_index = {i: g for i, g in enumerate(groups)}
    existing_ids = {int(e.id) for e in existing}

    user = (
        "RECURRING SHARED-MOMENT GROUPS (candidate rituals):\n"
        + "\n\n".join(_group_block(i, g) for i, g in by_index.items())
        + "\n\nALREADY-KNOWN RELATIONSHIP RITUALS:\n"
        + format_existing(existing)
        + f"\n\nPropose NEW rituals shared by {ctx.user_name} and "
        f"{ctx.assistant_name} -- one per genuinely recurring group -- or "
        "reinforce a known one by id."
    )

    raw = ctx.call_llm(_system(ctx.user_name, ctx.assistant_name), user)
    proposals: list[CandidateProposal] = []
    seen_new: set[int] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            gi = int(item.get("group_index"))
        except (TypeError, ValueError):
            continue
        group = by_index.get(gi)
        if group is None:
            continue
        evidence = [("memory", str(mid)) for mid in group.member_ids]

        rationale = str(item.get("rationale") or "").strip()
        reinforces = resolve_reinforces(item.get("reinforces_id"), existing_ids)
        if reinforces is not None:
            proposals.append(
                CandidateProposal(
                    label="",
                    rationale=rationale,
                    confidence=0.0,
                    evidence=evidence,
                    kind="ritual",
                    subject="relationship",
                    evidence_model="set",
                    reinforces_id=reinforces,
                )
            )
            continue

        # One NEW ritual per group at most -- guards an LLM that proposes two
        # near-duplicate labels for the same cluster.
        if gi in seen_new:
            continue
        label = str(item.get("label") or "").strip()
        if not label or len(group.member_ids) < ctx.min_sources:
            continue
        seen_new.add(gi)
        proposals.append(
            CandidateProposal(
                label=label,
                rationale=rationale,
                confidence=clamp01(item.get("confidence")),
                evidence=evidence,
                kind="ritual",
                subject="relationship",
                evidence_model="set",
            )
        )
    return proposals


SPEC = ProposerSpec(
    kind="ritual",
    subject="relationship",
    evidence_model="set",
    population="shared_moments",
    propose=propose_relationship_ritual,
    sig_key="concept_synth.ritual_sig",
)


__all__ = ["SPEC", "propose_relationship_ritual"]
