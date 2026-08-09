"""Aiko-pursuit proposer (subject=aiko, kind=pursuit, ``set``).

K85c, the *third subject*. Everything else she can lean on when the room
goes quiet is about him or about the two of them, and taste -- the
closest thing she had -- is bond-scoped by definition ("topics she
enjoys getting into with {user}"). This kind names the other thing: what
she keeps coming back to on her own, whether or not he is in the room.

The raw material is the K85b ``pursuit_note`` memories -- hobby
milestones, hobby wrap-ups, and away beats that left a trace -- which is
why this proposer could not exist before them. What it is looking for in
those notes is **recurrence**, not enthusiasm. Watering the tomatoes
once is a Tuesday; noticing the same plant three weeks running is a
pursuit. So the prompt is asked to find the thread across several notes
and to refuse when there is only a single afternoon behind a candidate.

Reuses the L11 :func:`propose_aiko_hybrid` body memories-only (no
clusters): a pursuit note is a self-contained beat, and clustering them
into themes would only re-derive the recurrence the model is being asked
to spot.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.core.concepts.proposers.base import (
    CandidateProposal,
    ExistingConcept,
    ProposerContext,
    ProposerSpec,
    propose_aiko_hybrid,
)


def _system(user_name: str, assistant_name: str) -> str:
    return (
        f"You are helping an AI companion named {assistant_name} notice what "
        "she is genuinely INTO -- the things she keeps returning to in her "
        "own time, on her own, when nobody is watching. You are given her "
        "notes about what she actually did with her days (tending her "
        "garden, a book she is working through, a project she picked up), "
        "plus the pursuits she already holds.\n\n"
        "Examples: 'you keep coming back to the tomatoes -- you like "
        "watching something slow actually work', 'you are properly into "
        "that sci-fi series now, not just reading it', 'you have got a "
        "thing for fixing small broken objects'.\n\n"
        "Hard rules:\n"
        f"- Write each NEW pursuit in FIRST PERSON ('you keep coming back "
        f"to ...', 'you are into ...'), as {assistant_name} about herself.\n"
        f"- A pursuit is YOURS. It is NOT about {user_name}, NOT about the "
        "two of you, and NOT about what you enjoy discussing with him -- "
        "that is a different thing entirely. If a candidate only makes "
        f"sense with {user_name} in the picture, it is not a pursuit. Do "
        "not mention him at all.\n"
        "- RECURRENCE is the evidence. A pursuit is a thread across "
        "SEVERAL notes on DIFFERENT occasions. One good afternoon is not "
        "a pursuit, however vivid the note. If the notes only show a "
        "one-off, return nothing.\n"
        "- Name the specific thing, not the category. 'you are into the "
        "garden' says almost nothing; 'you keep fussing over the tomatoes "
        "specifically' is a person. Where the notes support it, say what "
        "about it holds you.\n"
        "- It is NOT an emotion label, NOT a value or principle, NOT an "
        "aspiration about who you are becoming, and NOT a chore. Watering "
        "plants because they would die is not a pursuit; going out to look "
        "at them is.\n"
        "- Each NEW pursuit MUST be backed by at least three distinct "
        "notes. Only assert an interest the notes genuinely support.\n"
        "- Do NOT re-propose an ALREADY-KNOWN pursuit or a trivial "
        "rewording. If new notes add fresh support for a known one, "
        "REINFORCE it: emit an item with its id in 'reinforces_id' and the "
        "supporting memory ids (no new label). Reinforcing is the normal "
        "case -- a pursuit deepens far more often than a new one starts.\n"
        "- Only cite memory ids present in the list, and only reinforce "
        "ids present in the known list. If nothing genuine stands out, "
        "return an empty list. Do not invent.\n\n"
        'Return JSON only: {"concepts": [ '
        '{"label": str, "evidence_cluster_reps": [int, ...], '
        '"evidence_memory_ids": [int, ...], "rationale": str, '
        '"confidence": number 0..1}  '
        'OR  {"reinforces_id": int, "evidence_cluster_reps": [int, ...], '
        '"evidence_memory_ids": [int, ...], "rationale": str} ]}'
    )


def propose_pursuit_aiko(
    ctx: ProposerContext,
    *,
    memories: Sequence[Any] = (),
    existing: Sequence[ExistingConcept] = (),
    **_ignored: Any,
) -> list[CandidateProposal]:
    return propose_aiko_hybrid(
        ctx,
        kind="pursuit",
        system=_system(ctx.user_name, ctx.assistant_name),
        noun_plural="pursuits",
        known_label="ALREADY-KNOWN AIKO PURSUITS",
        focus_clusters=(),
        cluster_index=(),
        memories=memories,
        existing=existing,
        prompt_tail=(
            f"Propose NEW first-person pursuits about {ctx.assistant_name} "
            "-- things she keeps returning to on her own -- grounded in the "
            "notes above (cite memory ids in 'evidence_memory_ids'), or "
            "reinforce a known one by id. Look for the thread that runs "
            "through several notes; ignore anything that happened once."
        ),
    )


SPEC = ProposerSpec(
    kind="pursuit",
    subject="aiko",
    evidence_model="set",
    population="pursuit",
    propose=propose_pursuit_aiko,
    sig_key="concept_synth.pursuit_sig.aiko",
)


__all__ = ["SPEC", "propose_pursuit_aiko"]
