"""Shared-value proposer (subject=relationship, kind=value, ``set``).

H12. The "us" sibling of the L10 value proposers: not what he holds and not
what she holds, but what the two of them have come to treat as *mattering*
-- "we say the awkward thing rather than smoothing it", "we protect each
other's quiet", "we finish what we start together".

Reads the same ``shared_moment`` groups the L7 ritual proposer does, and
asks a different question of them. A ritual is what they repeatedly *do*;
a value is the commitment the doing reveals. Because those are easy to
restate as each other, a NEW value here **must draw on moments from at
least two distinct groups**: a principle visible in only one recurring
activity is that activity, named twice. The composition rule is the whole
defence against duplicating L7, so it is enforced here rather than trusted
to the prompt.

The render side was already waiting -- ``_concept_value_header`` has had a
``relationship`` branch ("what you've come to see you and {name} both
value") since L10 -- but nothing ever minted a row for it. Of 30
relationship concepts on the reference install, 25 were ``tension`` and 4
``ritual``: the pair was represented almost entirely as friction and habit,
with nothing standing for what they are *for*.
"""
from __future__ import annotations

from collections.abc import Sequence

from app.core.concepts.proposers.base import (
    CandidateProposal,
    ExistingConcept,
    ProposerContext,
    ProposerSpec,
    clamp01,
    coerce_id_list,
    format_existing,
    resolve_reinforces,
)
from app.core.concepts.ritual_grouping import RitualGroup

# Members shown per group. Lower than L7's 8: this prompt shows every group
# at once (a value has to span them) rather than naming one at a time.
_MAX_MEMBERS_SHOWN = 5
# A value must be visible in this many distinct groups. Two is the smallest
# number that can distinguish a principle from the ritual it appeared in.
_MIN_GROUPS = 2


def _system(user_name: str, assistant_name: str) -> str:
    return (
        f"You find SHARED VALUES between {user_name} and {assistant_name} -- "
        "the things the two of them have come to treat as mattering, the "
        "commitment underneath what they repeatedly do. You are given "
        "numbered GROUPS of their recurring shared moments and the shared "
        "values already known.\n\n"
        "Examples: 'they say the awkward thing to each other rather than "
        "smoothing it over', 'they protect each other's quiet without "
        "needing it explained', 'they finish what they start together', "
        "'they let each other be unimpressive'.\n\n"
        "Hard rules:\n"
        f"- Write every value about the pair -- '{user_name} and "
        f"{assistant_name}' or 'the two of them' -- never about one of them "
        "alone (that would be a personal value).\n"
        "- Name the PRINCIPLE, not the activity. 'Friday evenings watching "
        "anime' is a ritual; 'they guard time that is only theirs' is a "
        "value. If all you can say is what they do, return nothing.\n"
        f"- Each NEW value MUST draw on moments from at least {_MIN_GROUPS} "
        "DIFFERENT groups. A principle that only shows up in one recurring "
        "activity is just that activity described again -- skip it.\n"
        "- Be SPECIFIC and FALSIFIABLE. No generic warmth true of any close "
        "pair ('they value each other'). Say something these moments "
        "actually show the two of them choosing, ideally where choosing it "
        "cost something.\n"
        "- These are quiet impressions held lightly, never announcements. Do "
        "not name something one of them would say out loud as a rule.\n"
        "- Do NOT re-propose an ALREADY-KNOWN value or a trivial rewording. "
        "If fresh moments support a known one, REINFORCE it: emit an item "
        "with its id in 'reinforces_id' and the supporting moment ids (no "
        "new label).\n"
        "- Only cite moment ids present in the groups below, and only "
        "reinforce ids present in the known list. If nothing genuine spans "
        "the groups, return an empty list. Do not invent.\n\n"
        'Return JSON only: {"concepts": [ '
        '{"label": str, "evidence_memory_ids": [int, ...], "rationale": str, '
        '"confidence": number 0..1}  '
        'OR  {"reinforces_id": int, "evidence_memory_ids": [int, ...], '
        '"rationale": str} ]}'
    )


def _group_block(index: int, group: RitualGroup) -> str:
    head = f"GROUP [{index}] (vibe: {group.dominant_vibe}, {group.size} moments):"
    lines = [
        f"  - [{m.id}] {m.text}" for m in group.members[:_MAX_MEMBERS_SHOWN]
    ]
    if group.size > _MAX_MEMBERS_SHOWN:
        lines.append(f"  ... and {group.size - _MAX_MEMBERS_SHOWN} more")
    return head + "\n" + "\n".join(lines)


def propose_value_relationship(
    ctx: ProposerContext,
    *,
    groups: Sequence[RitualGroup],
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    if len(groups) < _MIN_GROUPS:
        return []
    group_of: dict[int, int] = {}
    for index, group in enumerate(groups):
        for mid in group.member_ids:
            group_of.setdefault(int(mid), index)
    existing_ids = {int(e.id) for e in existing}

    user = (
        "RECURRING SHARED-MOMENT GROUPS:\n"
        + "\n\n".join(_group_block(i, g) for i, g in enumerate(groups))
        + "\n\nALREADY-KNOWN SHARED VALUES:\n"
        + format_existing(existing)
        + f"\n\nPropose NEW values {ctx.user_name} and {ctx.assistant_name} "
        "hold together -- each one visible across at least two of the groups "
        "above -- or reinforce a known one by id."
    )

    raw = ctx.call_llm(_system(ctx.user_name, ctx.assistant_name), user)
    proposals: list[CandidateProposal] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ids = [
            mid
            for mid in dict.fromkeys(
                coerce_id_list(item.get("evidence_memory_ids"))
            )
            if mid in group_of
        ]
        if not ids:
            continue
        evidence = [("memory", str(mid)) for mid in ids]
        rationale = str(item.get("rationale") or "").strip()

        reinforces = resolve_reinforces(item.get("reinforces_id"), existing_ids)
        if reinforces is not None:
            proposals.append(
                CandidateProposal(
                    label="",
                    rationale=rationale,
                    confidence=0.0,
                    evidence=evidence,
                    kind="value",
                    subject="relationship",
                    evidence_model="set",
                    reinforces_id=reinforces,
                )
            )
            continue

        label = str(item.get("label") or "").strip()
        if not label or len(ids) < ctx.min_sources:
            continue
        if len({group_of[mid] for mid in ids}) < _MIN_GROUPS:
            continue
        proposals.append(
            CandidateProposal(
                label=label,
                rationale=rationale,
                confidence=clamp01(item.get("confidence")),
                evidence=evidence,
                kind="value",
                subject="relationship",
                evidence_model="set",
            )
        )
    return proposals


SPEC = ProposerSpec(
    kind="value",
    subject="relationship",
    evidence_model="set",
    population="shared_moments",
    propose=propose_value_relationship,
    sig_key="concept_synth.shared_value_sig",
)


__all__ = ["SPEC", "propose_value_relationship"]
