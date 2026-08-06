"""L17d self-correction proposer (subject=aiko, kind=communication_style).

The feature the whole L17 record exists to enable: Aiko noticing a pattern
in her own mistakes and changing how she works, not just what she believes.
Its raw material is neither clusters nor memories but her *corrections* --
L17c learning events, grouped by :func:`cluster_corrections` when several
of them happened for the same sort of reason across different beliefs.

It deliberately lands as ``communication_style`` rather than a new
``self_correction`` kind (the backlog's open question 1). That kind already
has a promotion gate, ``SurfaceWeights``, and a live steering path into the
T3 relevant-context region -- which is the entire point of the feature: a
rule she has learned about herself has to be able to *change her
behaviour*. A new kind would need all of that wiring rebuilt for no
behavioural gain.

What is different from the ordinary comm-style proposer is the evidence:
``evidence_model="meta"``, with one ``("concept", prior_concept_id)`` edge
per belief the pattern was learned from. So the rule rides the L12/L20 meta
rails (cascade, confidence bounding, the depth cap that stops a meta
standing on a meta) with one deliberate exception -- the
``meta_min_active_bases=0`` on the kind, because a self-correction stands
on beliefs she *stopped* holding and a correction does not stop having
happened.

Grounding rule: the prompt sees only the stored ``because`` prose and the
labels the beliefs wore. It is asked to name the habit those reasons share
and nothing else -- no counts, no salience, no mention of the machinery.
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
from app.core.concepts.self_correction import CorrectionCluster


def _system(user_name: str, assistant_name: str) -> str:
    return (
        f"You are helping {assistant_name}, an AI companion, learn something "
        f"about HOW SHE WORKS from times she got {user_name} wrong and had to "
        "change her mind. Each group below is several separate corrections "
        "that happened for a similar reason. The grouping is already done; "
        "your only job is to name the habit those reasons share, as a rule "
        "she can actually follow next time.\n\n"
        "Hard rules:\n"
        "- The rule is about HER OWN CONDUCT -- how she reads him, how fast "
        "she commits, how she checks -- never a new claim about what he is "
        f"like. If a group only says something about {user_name}, skip it.\n"
        "- Write the stored label in SECOND PERSON addressed to her ('you "
        "commit to a first read before...', 'wait for him to...'), so it "
        "reads naturally inside her private prompt.\n"
        f"- Name '{user_name}', never 'the user'.\n"
        "- Make it ACTIONABLE and falsifiable: something that would visibly "
        "change a reply. 'be more careful' is worthless; 'you decide what he "
        "means from one message when he is being brief -- ask instead' is a "
        "rule.\n"
        "- Ground it ONLY in the reasons given. Do not invent a mistake that "
        "is not in the group, and do not moralise about it.\n"
        "- Never mention concepts, beliefs, corrections, records, learning, "
        "confidence, scores, counts, or any machinery. She is noticing a "
        "habit, not reading a report.\n"
        "- Do not turn a correction into self-criticism. This is a working "
        "adjustment, said plainly.\n"
        "- At most ONE item per group. Copy its group_key exactly. If a "
        "group's reasons share nothing worth acting on, leave it out -- an "
        "empty list is the right answer more often than not.\n"
        "- If she already holds an equivalent rule, REINFORCE it: emit its id "
        "in 'reinforces_id' with no new label. Do not create paraphrase "
        "twins.\n\n"
        'Return JSON only: {"concepts": ['
        '{"group_key": str, "label": str, "rationale": str, '
        '"confidence": number 0..1} OR '
        '{"group_key": str, "reinforces_id": int, "rationale": str}]}'
    )


def _render(cluster: CorrectionCluster) -> str:
    lines = [f"[{cluster.key}]"]
    for member in cluster.members:
        parts = [f"  - reason: {member.because}"]
        if member.old_label and member.new_label:
            parts.append(
                f"    (thought: {member.old_label} -> instead: "
                f"{member.new_label})"
            )
        elif member.old_label:
            parts.append(f"    (thought: {member.old_label})")
        lines.extend(parts)
    return "\n".join(lines)


def propose_self_correction_aiko(
    ctx: ProposerContext,
    *,
    clusters: Sequence[CorrectionCluster] = (),
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    if not clusters:
        return []
    by_key = {cluster.key: cluster for cluster in clusters}
    user = (
        "GROUPED CORRECTIONS\n"
        + "\n".join(_render(cluster) for cluster in clusters)
        + "\n\nRULES SHE ALREADY HOLDS ABOUT HERSELF\n"
        + format_existing(existing)
        + "\n\nName only a habit that is genuinely in the reasons above and "
        "that would change how she replies."
    )
    raw_items = ctx.call_llm(_system(ctx.user_name, ctx.assistant_name), user)
    existing_ids = {item.id for item in existing}
    proposals: list[CandidateProposal] = []
    used_keys: set[str] = set()
    for raw in raw_items:
        key = str(raw.get("group_key", "") or "").strip()
        cluster = by_key.get(key)
        if cluster is None or key in used_keys:
            continue
        used_keys.add(key)
        reinforces_id = resolve_reinforces(raw.get("reinforces_id"), existing_ids)
        label = str(raw.get("label", "") or "").strip()
        if reinforces_id is None and not label:
            continue
        rationale = str(raw.get("rationale", "") or "").strip()
        proposals.append(
            CandidateProposal(
                label=label,
                rationale=rationale,
                confidence=clamp01(raw.get("confidence"), default=0.6),
                evidence=[
                    ("concept", str(cid)) for cid in cluster.concept_ids
                ],
                kind="communication_style",
                subject="aiko",
                evidence_model="meta",
                reinforces_id=reinforces_id,
            )
        )
    return proposals


SPEC = ProposerSpec(
    kind="communication_style",
    subject="aiko",
    evidence_model="meta",
    population="self_correction",
    propose=propose_self_correction_aiko,
    sig_key="concept_synth.self_correction_sig.aiko",
)


__all__ = ["SPEC", "propose_self_correction_aiko"]
