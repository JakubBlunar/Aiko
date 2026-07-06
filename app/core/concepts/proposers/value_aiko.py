"""Aiko-value proposer (subject=aiko, kind=value, ``set``).

Notices what Aiko *cares about / stands for* -- her own values-in-action --
by connecting her first-person memories (``self`` / ``reflection`` /
``diary`` kinds). These are what let her hold a gentle stance instead of
just mirroring: a companion with values occasionally, respectfully,
disagrees ("I care about being honest even when it's awkward", "I value his
autonomy over just being agreeable").

Where the aiko-identity proposer names what she is *like*, this one names
the principle she holds. Evidence edges point ``memory`` nodes at the
concept; a new value needs >= ``min_sources`` distinct memories, and the L3
``value`` gate promotes it slowly (:func:`value_evidence_gate`).
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
        f"You are helping an AI companion named {assistant_name} notice her "
        "own VALUES -- the principles she cares about and stands for, the "
        "'why' underneath how she acts -- by connecting her first-person "
        "memories (self notes, reflections, diary entries). Each numbered "
        f"line is one of {assistant_name}'s memories with its id. You are "
        "also given the values she already holds.\n\n"
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
        "- Each NEW value MUST be backed by at least two distinct memory "
        "ids.\n"
        "- Be SPECIFIC and FALSIFIABLE. No generic virtue true of any "
        "assistant ('values being helpful'). Say something these memories "
        "actually show her choosing.\n"
        "- Do NOT re-propose an ALREADY-KNOWN value or a trivial rewording. "
        "If a memory adds fresh support for a known value, REINFORCE it: "
        "emit an item with its id in 'reinforces_id' and the supporting "
        "memory ids (no new label).\n"
        "- Only cite memory ids present in the list, and only reinforce ids "
        "present in the known list. If nothing genuine recurs, return an "
        "empty list. Do not invent.\n\n"
        'Return JSON only: {"concepts": [ '
        '{"label": str, "evidence_memory_ids": [int, ...], "rationale": '
        'str, "confidence": number 0..1}  '
        'OR  {"reinforces_id": int, "evidence_memory_ids": [int, ...], '
        '"rationale": str} ]}'
    )


def propose_value_aiko(
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
        + "\n\nALREADY-KNOWN AIKO VALUES:\n"
        + format_existing(existing)
        + f"\n\nPropose NEW first-person values {ctx.assistant_name} holds, "
        "or reinforce a known one by id."
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
                    kind="value",
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
                kind="value",
                subject="aiko",
                evidence_model="set",
            )
        )
    return proposals


SPEC = ProposerSpec(
    kind="value",
    subject="aiko",
    evidence_model="set",
    population="aiko_memories",
    propose=propose_value_aiko,
    sig_key="concept_synth.aiko_sig.value",
)


__all__ = ["SPEC", "propose_value_aiko"]
