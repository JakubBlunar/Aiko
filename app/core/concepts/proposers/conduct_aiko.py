"""L42 proposer for relationship-scoped Aiko conduct observations."""
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
from app.core.concepts.surfacing_conduct import ConductFinding


def _system(user_name: str, assistant_name: str) -> str:
    return (
        f"You are helping {assistant_name}, an AI companion, notice durable "
        f"patterns in how she has been showing up with {user_name}. The input "
        "findings were already computed from a long relationship history. Your "
        "job is only to name them naturally, not to calculate or exaggerate.\n\n"
        "Hard rules:\n"
        "- This is Aiko's first-person self-model, but write the stored label "
        "in SECOND PERSON addressed to her ('you keep...', 'you tend...') so "
        "it reads naturally inside her private prompt.\n"
        f"- Make it relationship-scoped and name '{user_name}', never 'the user'.\n"
        "- Be specific, gentle, falsifiable, and open to being wrong.\n"
        "- Never mention ledgers, prompts, surfacing, scores, rates, counts, "
        "algorithms, or percentages.\n"
        "- Concentration means attention keeps leaning toward one region; "
        "neglect means parts of her understanding stay unused; fixation means "
        "she returns to one interpretation more than it opens conversation.\n"
        "- Do not turn an observation into a rule for what he may discuss.\n"
        "- Return at most one item per supplied finding. Copy its finding_key "
        "exactly. Reinforce a known conduct observation when appropriate; do "
        "not create paraphrase twins.\n\n"
        'Return JSON only: {"concepts": ['
        '{"finding_key": str, "label": str, "rationale": str, '
        '"confidence": number 0..1} OR '
        '{"finding_key": str, "reinforces_id": int, "rationale": str}]}'
    )


def propose_conduct_aiko(
    ctx: ProposerContext,
    *,
    findings: Sequence[ConductFinding] = (),
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    if not findings:
        return []
    finding_by_key = {finding.key: finding for finding in findings}
    finding_lines = "\n".join(
        f"[{finding.key}] shape={finding.shape}; observation={finding.summary}"
        for finding in findings
    )
    user = (
        "PRE-COMPUTED CONDUCT FINDINGS\n"
        f"{finding_lines}\n\n"
        "ALREADY-KNOWN CONDUCT OBSERVATIONS\n"
        f"{format_existing(existing)}\n\n"
        "Name only findings that make a durable, useful self-observation."
    )
    raw_items = ctx.call_llm(
        _system(ctx.user_name, ctx.assistant_name),
        user,
    )
    existing_ids = {item.id for item in existing}
    proposals: list[CandidateProposal] = []
    used_keys: set[str] = set()
    if not raw_items:
        return _fallback(findings, min_sources=int(ctx.min_sources))
    for raw in raw_items:
        key = str(raw.get("finding_key", "") or "").strip()
        finding = finding_by_key.get(key)
        if finding is None or key in used_keys:
            continue
        evidence = [
            (kind, str(item_id))
            for kind, item_id in finding.evidence
            if kind in {"cluster", "memory", "concept"} and int(item_id) > 0
        ]
        if len(set(evidence)) < max(2, int(ctx.min_sources)):
            continue
        reinforces_id = resolve_reinforces(
            raw.get("reinforces_id"), existing_ids,
        )
        label = str(raw.get("label", "") or "").strip()
        if reinforces_id is None and not label:
            continue
        rationale = str(raw.get("rationale", "") or "").strip()
        rationale = f"Conduct shape={finding.shape}. {rationale}".strip()
        proposals.append(
            CandidateProposal(
                label=label,
                rationale=rationale,
                confidence=clamp01(raw.get("confidence"), default=0.65),
                evidence=evidence,
                kind="conduct",
                subject="aiko",
                evidence_model="set",
                reinforces_id=reinforces_id,
            )
        )
        used_keys.add(key)
    if not proposals:
        return _fallback(findings, min_sources=int(ctx.min_sources))
    return proposals


def _fallback(
    findings: Sequence[ConductFinding],
    *,
    min_sources: int,
) -> list[CandidateProposal]:
    """Mint conduct concepts straight from the findings, no model involved.

    Detection is the expensive, careful half of L42 — the LLM pass only
    puts the observation into words, and the detector already wrote a
    perfectly serviceable sentence. Losing a real self-observation because
    a local model returned an empty object is a bad trade, and it is not
    hypothetical: one empty return in August left ``kind='conduct'`` at
    zero rows with the detector working correctly the whole time.

    Confidence is held below the LLM path's default because nothing judged
    whether the observation is worth making — the lifecycle worker can
    raise it later if the pattern keeps showing up.
    """
    out: list[CandidateProposal] = []
    for finding in findings:
        label = (finding.second_person or "").strip()
        if not label:
            continue
        evidence = [
            (kind, str(item_id))
            for kind, item_id in finding.evidence
            if kind in {"cluster", "memory", "concept"} and int(item_id) > 0
        ]
        if len(set(evidence)) < max(2, min_sources):
            continue
        out.append(
            CandidateProposal(
                label=label,
                rationale=(
                    f"Conduct shape={finding.shape}. Named from the detector's "
                    "own summary because the naming pass returned nothing."
                ),
                confidence=0.5,
                evidence=evidence,
                kind="conduct",
                subject="aiko",
                evidence_model="set",
            )
        )
    return out


SPEC = ProposerSpec(
    kind="conduct",
    subject="aiko",
    evidence_model="set",
    population="conduct",
    propose=propose_conduct_aiko,
    sig_key="concept_synth.conduct_sig.aiko",
)


__all__ = ["SPEC", "propose_conduct_aiko"]
