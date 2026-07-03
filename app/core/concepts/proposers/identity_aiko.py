"""Aiko-identity proposer (subject=aiko, kind=identity, ``set``).

Notices higher-order concepts about Aiko *herself* -- her stance, tastes,
values-in-action -- by connecting her own first-person memories (``self``
/ ``reflection`` / ``diary`` kinds). Evidence edges point ``memory``
nodes at the concept; requires >= ``min_sources`` distinct memories.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.core.concepts.proposers.base import (
    CandidateProposal,
    ExistingConcept,
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
        f"You are helping an AI companion named {assistant_name} notice "
        "higher-order concepts about HERSELF -- her own stance, tastes, "
        "values-in-action, and ways of being -- by connecting her own "
        "first-person memories (self notes, reflections, diary entries). "
        f"Each numbered line is one of {assistant_name}'s memories with "
        "its id. You are also given the concepts she already holds about "
        "herself.\n\n"
        "Hard rules:\n"
        f"- Write each NEW concept in FIRST PERSON ('I ...'), as "
        f"{assistant_name} about herself. When a concept involves the "
        f"person she is with, name him as '{user_name}' -- never 'the "
        "user'.\n"
        "- Each NEW concept MUST be backed by at least two distinct memory "
        "ids.\n"
        "- Be SPECIFIC and FALSIFIABLE. No generic self-flattery true of "
        "any assistant. Say something these memories actually show.\n"
        "- Do NOT re-propose an ALREADY-KNOWN concept or a trivial "
        "rewording of one. If a memory instead adds fresh support for a "
        "known concept, REINFORCE it: emit an item with its id in "
        "'reinforces_id' and the supporting memory ids (no new label).\n"
        "- Only cite memory ids present in the list, and only reinforce "
        "ids present in the known list. If nothing genuine recurs, return "
        "an empty list. Do not invent.\n\n"
        'Return JSON only: {"concepts": [ '
        '{"label": str, "evidence_memory_ids": [int, ...], "rationale": '
        'str, "confidence": number 0..1}  '
        'OR  {"reinforces_id": int, "evidence_memory_ids": [int, ...], '
        '"rationale": str} ]}'
    )


def propose_identity_aiko(
    ctx: ProposerContext,
    *,
    memories: Sequence[Any],
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    if not memories:
        return []
    valid_ids: set[int] = set()
    lines: list[str] = []
    for mem in memories:
        try:
            mid = int(mem.id)
        except (TypeError, ValueError, AttributeError):
            continue
        valid_ids.add(mid)
        lines.append(f"[{mid}] {snippet(getattr(mem, 'content', '') or '')}")
    if not valid_ids:
        return []
    existing_ids = {int(e.id) for e in existing}

    user = (
        "Aiko's own memories:\n"
        + "\n".join(lines)
        + "\n\nALREADY-KNOWN AIKO IDENTITY CONCEPTS:\n"
        + format_existing(existing)
        + f"\n\nPropose NEW first-person identity concepts about "
        f"{ctx.assistant_name} herself, or reinforce a known one by id."
    )

    raw = ctx.call_llm(_system(ctx.user_name, ctx.assistant_name), user)
    proposals: list[CandidateProposal] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ids = coerce_id_list(item.get("evidence_memory_ids"))
        ids = list(dict.fromkeys(i for i in ids if i in valid_ids))
        if not ids:
            continue
        evidence = [("memory", str(i)) for i in ids]
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
                    kind="identity",
                    subject="aiko",
                    evidence_model="set",
                    reinforces_id=reinforces,
                )
            )
            continue

        label = str(item.get("label") or "").strip()
        if not label or len(ids) < ctx.min_sources:
            continue
        proposals.append(
            CandidateProposal(
                label=label,
                rationale=rationale,
                confidence=clamp01(item.get("confidence")),
                evidence=evidence,
                kind="identity",
                subject="aiko",
                evidence_model="set",
            )
        )
    return proposals


SPEC = ProposerSpec(
    kind="identity",
    subject="aiko",
    evidence_model="set",
    population="aiko_memories",
    propose=propose_identity_aiko,
)


__all__ = ["SPEC", "propose_identity_aiko"]
