"""Shared substrate for L2 concept proposers.

A proposer is a pure ``build prompt -> call LLM -> parse -> map to
evidence`` function. It does **not** own cadence, dirty-tracking, or
batch selection -- the :class:`ConceptSynthesisWorker` does that and
hands each proposer a small, pre-selected, bounded input. That keeps all
the batching logic in one place and lets proposers be unit-tested with a
fake ``call_llm``.

This module holds only the pieces every proposer shares: the input /
output dataclasses, the :class:`ProposerSpec` registry-entry type, and a
few parsing helpers. One proposer per sibling module (``identity_user``,
``identity_aiko``, ...); the package ``__init__`` assembles them into
``CONCEPT_PROPOSERS``.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

MIN_SOURCES = 2
AIKO_SELF_KINDS: tuple[str, ...] = ("self", "reflection", "diary")

# How much per-source text to show the model. Keeps the focus/aiko
# prompt bounded regardless of how long individual memories are.
_MAX_SNIPPET_CHARS = 240


# ── data ──────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ExistingConcept:
    """A concept already in the store, handed to a proposer so the LLM can
    avoid re-proposing it (and instead reinforce it by id). Kept minimal
    -- id + label is all the prompt needs."""

    id: int
    label: str


@dataclass(slots=True)
class CandidateProposal:
    """One proposer output, already mapped onto concrete evidence node
    refs. ``evidence`` is a list of ``(node_type, node_id)`` where
    ``node_type`` is ``"cluster"`` / ``"memory"`` / ``"concept"``.

    ``reinforces_id`` distinguishes the two output shapes: ``None`` means
    a brand-new concept (``label`` is authoritative); a concept id means
    "this evidence supports that existing concept" -- the worker attaches
    the edges to it instead of creating a row, keeping the reinforcement
    signal alive without duplicate concepts."""

    label: str
    rationale: str
    confidence: float
    evidence: list[tuple[str, str]]
    kind: str
    subject: str
    evidence_model: str = "set"
    reinforces_id: int | None = None


@dataclass(slots=True)
class FocusCluster:
    """A user-dominant topic cluster selected for full-content synthesis
    this run. ``rep`` is the stable representative-member id."""

    rep: int
    label: str
    size: int
    representative: str = ""
    digest: str = ""


@dataclass(slots=True)
class NarrativeCandidate:
    """A subject-dominant topic cluster whose member memories have been
    loaded and put in **temporal order** (by ``event_time``, falling back to
    ``created_at``), offered to the narrative proposer (L8) as a candidate
    causal arc.

    ``rep`` is the stable cluster representative id (used by the worker for
    dirty-tracking); ``memories`` is the ordered chain the proposer renders
    and, if it names a *closed* arc, cites as ordered ``sequence`` evidence.
    ``subject`` (``user`` / ``aiko``) selects third- vs first-person framing."""

    rep: int
    label: str
    subject: str
    memories: list[Any]


@dataclass(slots=True)
class ProposerContext:
    """Shared plumbing handed to every proposer. ``call_llm`` is the
    worker's bound LLM helper ``(system, user) -> list[dict]`` (already
    JSON-parsed into the ``concepts`` array)."""

    call_llm: Callable[[str, str], list[dict[str, Any]]]
    min_sources: int = MIN_SOURCES
    clock: Callable[[], datetime] | None = None
    # Personalisation: concepts read better (and age better) when they
    # name the people involved instead of "the user" / "the AI companion".
    user_name: str = "the user"
    assistant_name: str = "Aiko"


@dataclass(frozen=True, slots=True)
class ProposerSpec:
    """Registry entry binding a (kind, subject) to its proposer + the
    batch population the worker must select for it.

    ``sig_key`` namespaces this proposer's dirty-tracking signature in
    ``kv_meta`` so two proposers over the *same* population (e.g. identity
    and value both mining topic clusters) don't clobber each other's
    "which clusters have I already synthesised?" state. Empty => the worker
    falls back to the legacy per-population key (kept for identity so its
    existing signature survives an upgrade)."""

    kind: str
    subject: str
    evidence_model: str
    # "clusters" | "aiko_memories" | "affect" | "shared_moments" | "narrative"
    population: str
    propose: Callable[..., list[CandidateProposal]]
    sig_key: str = ""


# ── helpers ─────────────────────────────────────────────────────────────


def clamp01(value: Any, default: float = 0.5) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


def coerce_id_list(raw: Any) -> list[int]:
    """Best-effort coercion of an LLM-returned id list into ``int``s,
    tolerating strings and junk entries."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[int] = []
    for item in raw:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def snippet(text: str) -> str:
    text = " ".join((text or "").split())
    if len(text) > _MAX_SNIPPET_CHARS:
        return text[: _MAX_SNIPPET_CHARS - 1].rstrip() + "\u2026"
    return text


def resolve_reinforces(raw: Any, existing_ids: set[int]) -> int | None:
    """Return a valid existing concept id to reinforce, or ``None`` (a
    brand-new proposal, or an LLM-hallucinated id we ignore)."""
    if raw is None:
        return None
    try:
        rid = int(raw)
    except (TypeError, ValueError):
        return None
    return rid if rid in existing_ids else None


def format_existing(existing: Sequence[ExistingConcept]) -> str:
    """Render the existing-concepts block for a proposer prompt. Returns
    ``"(none yet)"`` when empty so the instruction still reads cleanly."""
    if not existing:
        return "(none yet)"
    return "\n".join(f"[{e.id}] {e.label}" for e in existing)


def propose_aiko_hybrid(
    ctx: ProposerContext,
    *,
    kind: str,
    system: str,
    noun_plural: str,
    known_label: str,
    focus_clusters: Sequence[FocusCluster],
    cluster_index: Sequence[tuple[int, str, int]],
    memories: Sequence[Any],
    existing: Sequence[ExistingConcept] = (),
    affect_by_rep: "dict[int, str] | None" = None,
    memory_affect: "dict[int, str] | None" = None,
    prompt_tail: str | None = None,
) -> list[CandidateProposal]:
    """Shared body for the subject=aiko proposers (L11).

    Unlike the user path (clusters only), Aiko's self-model is mined from a
    *combined* pass: her aiko-dominant self-themes (``focus_clusters`` +
    ``cluster_index``) AND her salient individual self-memories
    (``memories``). One concept may cite theme rep ids, memory ids, or a
    mix; a NEW concept needs ``>= ctx.min_sources`` total distinct sources,
    and evidence edges carry mixed ``("cluster", rep)`` / ``("memory", id)``
    nodes (the ``set`` model allows it). When she has no aiko-dominant
    clusters yet this degrades cleanly to memories-only (cold start).

    ``affect_by_rep`` / ``memory_affect`` (L13) optionally annotate each
    theme / memory line with its typical affect phrase, and ``prompt_tail``
    overrides the closing instruction — the affective proposer reuses this
    body with an affect-aware prompt."""
    aff_reps = affect_by_rep or {}
    aff_mems = memory_affect or {}
    valid_reps = {int(rep) for rep, _label, _size in cluster_index}

    valid_mem_ids: set[int] = set()
    mem_lines: list[str] = []
    for mem in memories:
        try:
            mid = int(mem.id)
        except (TypeError, ValueError, AttributeError):
            continue
        valid_mem_ids.add(mid)
        line = f"[{mid}] {snippet(getattr(mem, 'content', '') or '')}"
        if mid in aff_mems:
            line += f"  (felt: {aff_mems[mid]})"
        mem_lines.append(line)

    if not valid_reps and not valid_mem_ids:
        return []
    existing_ids = {int(e.id) for e in existing}

    def _rep_line(rep: int, label: str, size: int, *, bullet: bool) -> str:
        head = f"- [{rep}] {label} (size {size})" if bullet else (
            f"[{rep}] {label} (size {size})"
        )
        if rep in aff_reps:
            head += f"  (feels: {aff_reps[rep]})"
        return head

    sections: list[str] = []
    if cluster_index:
        map_lines = [
            _rep_line(rep, label, size, bullet=True)
            for rep, label, size in cluster_index
        ]
        sections.append(
            "RECURRING SELF-THEMES (clusters of her own memories, by size):\n"
            + "\n".join(map_lines)
        )
    if focus_clusters:
        focus_lines: list[str] = []
        for fc in focus_clusters:
            parts = [_rep_line(fc.rep, fc.label, fc.size, bullet=False)]
            if fc.representative:
                parts.append(f"  representative: {snippet(fc.representative)}")
            if fc.digest:
                parts.append(f"  digest: {snippet(fc.digest)}")
            focus_lines.append("\n".join(parts))
        sections.append("FOCUS THEMES (detail):\n" + "\n\n".join(focus_lines))
    if mem_lines:
        sections.append("NOTABLE SELF-MEMORIES:\n" + "\n".join(mem_lines))
    sections.append(f"{known_label}:\n" + format_existing(existing))
    sections.append(
        prompt_tail
        or (
            f"Propose NEW first-person {noun_plural} about "
            f"{ctx.assistant_name} herself, grounded in the self-themes "
            "and/or self-memories above (cite theme rep ids in "
            "'evidence_cluster_reps' and/or memory ids in "
            "'evidence_memory_ids'), or reinforce a known one by id."
        )
    )
    user = "\n\n".join(sections)

    raw = ctx.call_llm(system, user)
    proposals: list[CandidateProposal] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        reps = list(
            dict.fromkeys(
                r
                for r in coerce_id_list(item.get("evidence_cluster_reps"))
                if r in valid_reps
            )
        )
        mids = list(
            dict.fromkeys(
                i
                for i in coerce_id_list(item.get("evidence_memory_ids"))
                if i in valid_mem_ids
            )
        )
        total = len(reps) + len(mids)
        if total == 0:
            continue
        evidence = [("cluster", str(r)) for r in reps]
        evidence += [("memory", str(i)) for i in mids]
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
                    kind=kind,
                    subject="aiko",
                    evidence_model="set",
                    reinforces_id=reinforces,
                )
            )
            continue

        label = str(item.get("label") or "").strip()
        if not label or total < ctx.min_sources:
            continue
        proposals.append(
            CandidateProposal(
                label=label,
                rationale=rationale,
                confidence=clamp01(item.get("confidence")),
                evidence=evidence,
                kind=kind,
                subject="aiko",
                evidence_model="set",
            )
        )
    return proposals


# Cap on how many steps of one arc we show the model -- long arcs stay bounded
# in the prompt; the middle is elided so the beginning + end (the parts that
# make it a *closed* story) always survive.
_MAX_STEPS_SHOWN = 12


def _arc_block(index: int, cand: NarrativeCandidate) -> str:
    """Render one candidate arc as a numbered, temporally-ordered block."""
    mems = cand.memories
    lines: list[str] = []
    if len(mems) <= _MAX_STEPS_SHOWN:
        shown = list(enumerate(mems))
    else:
        head = _MAX_STEPS_SHOWN // 2
        tail = _MAX_STEPS_SHOWN - head
        shown = list(enumerate(mems[:head]))
        shown.append((-1, None))
        shown += list(zip(range(len(mems) - tail, len(mems)), mems[-tail:]))
    for pos, mem in shown:
        if mem is None:
            lines.append(
                f"  ... ({len(mems) - _MAX_STEPS_SHOWN} step(s) elided) ..."
            )
            continue
        mid = int(mem.id)
        lines.append(f"  {pos + 1}. [{mid}] {snippet(getattr(mem, 'content', '') or '')}")
    return f'ARC [{index}] -- theme "{cand.label}":\n' + "\n".join(lines)


def propose_narrative(
    ctx: ProposerContext,
    *,
    candidates: Sequence[NarrativeCandidate],
    subject: str,
    system: str,
    first_person: bool,
    min_chain: int = 3,
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    """Shared body for the L8 narrative proposers (user + aiko).

    Each :class:`NarrativeCandidate` is a subject-dominant cluster whose
    member memories are already in temporal order. The model names any
    candidate that forms a genuine **closed causal arc** (a beginning, a
    development, and a resolution) and cites the member ids that make up the
    chain; ``first_person`` only shapes the prompt voice (aiko's arcs are
    about herself). We re-derive the chain order from the candidate's own
    ordering (not the LLM's id order) and emit ``sequence`` evidence so the
    worker stamps ordinals 0..n. An *open* arc (``closed`` false) or a chain
    shorter than ``min_chain`` is dropped; reinforcement of a known arc has
    neither requirement (it just adds fresh support to an existing story)."""
    if not candidates:
        return []
    by_index = {i: c for i, c in enumerate(candidates)}
    existing_ids = {int(e.id) for e in existing}

    voice = (
        f"about {ctx.assistant_name} herself (first person -- 'I ...', 'the "
        "stretch where I ...')"
        if first_person
        else f"about {ctx.user_name} (third person)"
    )
    user = (
        "CANDIDATE ARCS (each theme's memories in time order):\n"
        + "\n\n".join(_arc_block(i, c) for i, c in by_index.items())
        + "\n\nALREADY-KNOWN ARCS:\n"
        + format_existing(existing)
        + f"\n\nName NEW narrative arcs {voice} -- one per genuinely closed "
        "arc -- or reinforce a known one by id."
    )

    raw = ctx.call_llm(system, user)
    proposals: list[CandidateProposal] = []
    seen_new: set[int] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            ai = int(item.get("arc_index"))
        except (TypeError, ValueError):
            continue
        cand = by_index.get(ai)
        if cand is None:
            continue
        pos_of = {int(m.id): idx for idx, m in enumerate(cand.memories)}
        cited = [
            i for i in coerce_id_list(item.get("evidence_memory_ids"))
            if i in pos_of
        ]
        # Chain order is the candidate's temporal order, not the LLM's -- so
        # ordinals are correct even if the model lists ids out of sequence.
        ordered_ids = sorted(dict.fromkeys(cited), key=lambda i: pos_of[i])
        if not ordered_ids:
            continue
        evidence = [("memory", str(i)) for i in ordered_ids]
        rationale = str(item.get("rationale") or "").strip()

        reinforces = resolve_reinforces(item.get("reinforces_id"), existing_ids)
        if reinforces is not None:
            proposals.append(
                CandidateProposal(
                    label="",
                    rationale=rationale,
                    confidence=0.0,
                    evidence=evidence,
                    kind="narrative",
                    subject=subject,
                    evidence_model="sequence",
                    reinforces_id=reinforces,
                )
            )
            continue

        # A NEW arc must be closed (resolved) and long enough to be a story.
        if ai in seen_new:
            continue
        if not bool(item.get("closed")):
            continue
        label = str(item.get("label") or "").strip()
        if not label or len(ordered_ids) < max(int(min_chain), 1):
            continue
        seen_new.add(ai)
        proposals.append(
            CandidateProposal(
                label=label,
                rationale=rationale,
                confidence=clamp01(item.get("confidence")),
                evidence=evidence,
                kind="narrative",
                subject=subject,
                evidence_model="sequence",
            )
        )
    return proposals


__all__ = [
    "AIKO_SELF_KINDS",
    "MIN_SOURCES",
    "CandidateProposal",
    "ExistingConcept",
    "FocusCluster",
    "NarrativeCandidate",
    "ProposerContext",
    "ProposerSpec",
    "clamp01",
    "coerce_id_list",
    "format_existing",
    "propose_aiko_hybrid",
    "propose_narrative",
    "resolve_reinforces",
    "snippet",
]
