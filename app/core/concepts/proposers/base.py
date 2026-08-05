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

from app.core.infra import timephrase

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
class TensionBase:
    """One active *base* concept offered to the L12 tension proposer as raw
    material. Unlike :class:`ExistingConcept` (id + label, for dedup), a
    tension base carries the extra context the model needs to spot a
    push/pull between two of them: its ``subject`` / ``kind`` (so a value can
    be told apart from an activity), a ``rationale`` snippet, its
    ``confidence``, and an optional ``hint`` about whether it has been live or
    has gone quiet lately (the "hot while a normally-paired concept is
    dormant" signal). Only non-meta actives are ever passed, which is what
    keeps the meta depth cap (no meta-of-meta) true by construction."""

    id: int
    subject: str
    kind: str
    label: str
    rationale: str = ""
    confidence: float = 0.5
    hint: str = ""


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
    # "clusters" | "aiko_memories" | "affect" | "shared_moments"
    # | "narrative" | "aspiration" | "boundary"
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


def mem_line(mem: Any, now: datetime | None = None) -> str:
    """``[id] content (3 days ago)`` -- one evidence row for a proposer.

    K-time10: the age is load-bearing evidence here, not decoration. A
    proposer is judging whether a handful of notes add up to a standing
    trait, and "he was anxious" three times last May is a different claim
    from three times this week. Undated, the two are indistinguishable --
    and a note worded "currently" reads as though it still were.
    """
    when = now or timephrase.utcnow()
    body = snippet(getattr(mem, "content", "") or "")
    created_at = str(getattr(mem, "created_at", "") or "")
    if timephrase.parse_iso(created_at) is not None:
        body = f"{body} ({timephrase.humanize_past(created_at, when)})"
    return f"[{int(mem.id)}] {body}"


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
    rep_annotation_label: str = "feels",
    mem_annotation_label: str = "felt",
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
    theme / memory line with a per-source phrase, and ``prompt_tail``
    overrides the closing instruction — the affective proposer reuses this
    body with an affect-aware prompt. ``rep_annotation_label`` /
    ``mem_annotation_label`` name what that phrase *is* (``"feels"`` /
    ``"felt"`` for affect; K81 taste passes ``"lands well"`` to render the
    per-cluster engagement instead), so the annotation reads correctly for
    whichever signal the caller is threading through."""
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
        line = mem_line(mem)
        if mid in aff_mems:
            line += f"  ({mem_annotation_label}: {aff_mems[mid]})"
        mem_lines.append(line)

    if not valid_reps and not valid_mem_ids:
        return []
    existing_ids = {int(e.id) for e in existing}

    def _rep_line(rep: int, label: str, size: int, *, bullet: bool) -> str:
        head = f"- [{rep}] {label} (size {size})" if bullet else (
            f"[{rep}] {label} (size {size})"
        )
        if rep in aff_reps:
            head += f"  ({rep_annotation_label}: {aff_reps[rep]})"
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


# Cap on how many steps of one chain we show the model -- long chains stay
# bounded in the prompt; the middle is elided so the beginning + end (the parts
# that make it a *closed* story / a clear *direction*) always survive.
_MAX_STEPS_SHOWN = 12


def _ordered_block(index: int, cand: "NarrativeCandidate", block_word: str) -> str:
    """Render one candidate chain as a numbered, temporally-ordered block."""
    mems = cand.memories
    lines: list[str] = []
    if len(mems) <= _MAX_STEPS_SHOWN:
        shown = list(enumerate(mems))
    else:
        head = _MAX_STEPS_SHOWN // 2
        tail = _MAX_STEPS_SHOWN - head
        shown = list(enumerate(mems[:head]))
        shown.append((-1, None))
        shown += list(zip(range(len(mems) - tail, len(mems)), mems[-tail:], strict=False))
    for pos, mem in shown:
        if mem is None:
            lines.append(
                f"  ... ({len(mems) - _MAX_STEPS_SHOWN} step(s) elided) ..."
            )
            continue
        lines.append(f"  {pos + 1}. {mem_line(mem)}")
    return f'{block_word} [{index}] -- theme "{cand.label}":\n' + "\n".join(lines)


def propose_ordered_concept(
    ctx: ProposerContext,
    *,
    candidates: Sequence["NarrativeCandidate"],
    subject: str,
    kind: str,
    system: str,
    first_person: bool,
    gate_flag: str,
    block_word: str,
    noun_plural: str,
    new_requirement: str,
    min_chain: int = 3,
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    """Shared body for the ``sequence``-evidence proposers (L8 narrative +
    L14 aspiration).

    Each :class:`NarrativeCandidate` is a subject-dominant cluster whose
    member memories are already in temporal order. The model names any
    candidate that clears the kind's bar -- a **closed causal arc** for
    narrative, a **sustained direction** for aspiration -- and cites the
    member ids that make up the chain; ``first_person`` only shapes the prompt
    voice (Aiko's are about herself). We re-derive the chain order from the
    candidate's own ordering (not the LLM's id order) and emit ``sequence``
    evidence so the worker stamps ordinals 0..n.

    The kind-specific bits are parameterized: ``gate_flag`` is the JSON boolean
    a NEW concept must set true (``"closed"`` for narrative, ``"directional"``
    for aspiration); ``block_word`` / ``noun_plural`` / ``new_requirement``
    shape the prompt vocabulary. A NEW concept failing the flag, or a chain
    shorter than ``min_chain``, is dropped; a reinforcement of a known concept
    has neither requirement (it just adds fresh support)."""
    if not candidates:
        return []
    by_index = {i: c for i, c in enumerate(candidates)}
    existing_ids = {int(e.id) for e in existing}

    voice = (
        f"about {ctx.assistant_name} herself (first person -- 'I ...')"
        if first_person
        else f"about {ctx.user_name} (third person)"
    )
    user = (
        f"CANDIDATE {block_word}S (each theme's memories in time order):\n"
        + "\n\n".join(
            _ordered_block(i, c, block_word) for i, c in by_index.items()
        )
        + f"\n\nALREADY-KNOWN {block_word}S:\n"
        + format_existing(existing)
        + f"\n\nName NEW {noun_plural} {voice} -- one per {new_requirement} "
        "-- or reinforce a known one by id."
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
                    kind=kind,
                    subject=subject,
                    evidence_model="sequence",
                    reinforces_id=reinforces,
                )
            )
            continue

        # A NEW concept must clear the kind's gate flag and be long enough.
        if ai in seen_new:
            continue
        if not bool(item.get(gate_flag)):
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
                kind=kind,
                subject=subject,
                evidence_model="sequence",
            )
        )
    return proposals


def propose_narrative(
    ctx: ProposerContext,
    *,
    candidates: Sequence["NarrativeCandidate"],
    subject: str,
    system: str,
    first_person: bool,
    min_chain: int = 3,
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    """L8 narrative body -- a thin wrapper over :func:`propose_ordered_concept`
    fixing the narrative vocabulary (a *closed* arc)."""
    return propose_ordered_concept(
        ctx,
        candidates=candidates,
        subject=subject,
        kind="narrative",
        system=system,
        first_person=first_person,
        gate_flag="closed",
        block_word="ARC",
        noun_plural="narrative arcs",
        new_requirement="genuinely closed arc",
        min_chain=min_chain,
        existing=existing,
    )


def propose_boundary(
    ctx: ProposerContext,
    *,
    subject: str,
    system: str,
    focus_clusters: Sequence[FocusCluster] = (),
    cluster_index: Sequence[tuple[int, str, int]] = (),
    memories: Sequence[Any] = (),
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    """Shared body for the L18 boundary proposers (both subjects).

    A boundary *gates behaviour*, so unlike the trait/value kinds it is mined
    from topic clusters AND Aiko's explicit remembered anchors (``memories`` --
    ``self_tagged`` notes about the user for ``subject="user"``, ``self`` /
    ``reflection`` / ``diary`` notes about herself for ``subject="aiko"``). One
    concept may cite cluster rep ids, memory ids, or a mix, and evidence edges
    carry mixed ``("cluster", rep)`` / ``("memory", id)`` nodes (the ``set``
    model allows it).

    **Composition rule.** A NEW boundary is accepted when it is grounded by at
    least ONE explicit anchor memory OR by at least TWO clusters -- a single
    deliberate anchor is enough (that is the whole point of the anchor path),
    but a lone cluster is not (that would be a stray topic, not a line). This
    upstream rule is what lets the L3 :func:`boundary_evidence_gate` safely
    floor the source count at 1. ``subject`` shapes the prompt voice only (the
    system prompt already carries the first-/third-person framing)."""
    valid_reps = {int(rep) for rep, _label, _size in cluster_index}
    valid_mem_ids: set[int] = set()
    mem_lines: list[str] = []
    for mem in memories:
        try:
            mid = int(mem.id)
        except (TypeError, ValueError, AttributeError):
            continue
        valid_mem_ids.add(mid)
        mem_lines.append(mem_line(mem))

    if not valid_reps and not valid_mem_ids:
        return []
    existing_ids = {int(e.id) for e in existing}

    sections: list[str] = []
    if cluster_index:
        map_lines = [
            f"- [{rep}] {label} (size {size})"
            for rep, label, size in cluster_index
        ]
        sections.append(
            "TOPIC MAP (recurring patterns, by size):\n" + "\n".join(map_lines)
        )
    if focus_clusters:
        focus_lines: list[str] = []
        for fc in focus_clusters:
            parts = [f"[{fc.rep}] {fc.label} (size {fc.size})"]
            if fc.representative:
                parts.append(f"  representative: {snippet(fc.representative)}")
            if fc.digest:
                parts.append(f"  digest: {snippet(fc.digest)}")
            focus_lines.append("\n".join(parts))
        sections.append("FOCUS CLUSTERS (detail):\n" + "\n\n".join(focus_lines))
    if mem_lines:
        sections.append(
            "NOTABLE REMEMBERED NOTES (deliberate anchors -- a single one can "
            "ground a boundary):\n" + "\n".join(mem_lines)
        )
    sections.append("ALREADY-KNOWN BOUNDARIES:\n" + format_existing(existing))
    sections.append(
        "Propose NEW boundaries grounded in the material above (cite cluster "
        "rep ids in 'evidence_cluster_reps' and/or remembered-note ids in "
        "'evidence_memory_ids'), or reinforce a known one by id."
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
        if not reps and not mids:
            continue
        evidence = [("cluster", str(r)) for r in reps]
        evidence += [("memory", str(i)) for i in mids]
        rationale = str(item.get("rationale") or "").strip()

        reinforces = resolve_reinforces(item.get("reinforces_id"), existing_ids)
        if reinforces is not None:
            proposals.append(
                CandidateProposal(
                    label="",
                    rationale=rationale,
                    confidence=0.0,
                    evidence=evidence,
                    kind="boundary",
                    subject=subject,
                    evidence_model="set",
                    reinforces_id=reinforces,
                )
            )
            continue

        label = str(item.get("label") or "").strip()
        # Composition rule: one deliberate anchor OR >= 2 clusters.
        if not label or not (len(mids) >= 1 or len(reps) >= 2):
            continue
        proposals.append(
            CandidateProposal(
                label=label,
                rationale=rationale,
                confidence=clamp01(item.get("confidence")),
                evidence=evidence,
                kind="boundary",
                subject=subject,
                evidence_model="set",
            )
        )
    return proposals


def propose_communication_style(
    ctx: ProposerContext,
    *,
    subject: str,
    system: str,
    focus_clusters: Sequence[FocusCluster] = (),
    cluster_index: Sequence[tuple[int, str, int]] = (),
    memories: Sequence[Any] = (),
    existing: Sequence[ExistingConcept] = (),
    style_digest: str = "",
) -> list[CandidateProposal]:
    """Shared body for the L23 communication-style proposers (both subjects).

    A communication-style concept guides *how the conversation should feel* --
    reply detail level, lead vs follow, hedging/confidence, warmth vs terseness
    -- **bound to the context it applies to** ("explain code in depth with
    examples when we talk programming"). Like a boundary it gates delivery, so it
    is a *hybrid* mined from topic clusters AND explicit remembered anchors
    (``memories`` -- ``self_tagged`` about the user for ``subject="user"``,
    ``self`` / ``reflection`` / ``diary`` about herself for ``subject="aiko"``).

    A ``style_digest`` (persisted K13 style-signal labels + the distilled profile
    ``communication_style`` field) may be woven in as *guidance* -- it steers what
    style to name, but is NOT evidence: a proposed concept must still cite real
    cluster / memory ids, so the digest never grounds a concept on its own.

    **Composition rule.** A NEW style concept is accepted when grounded by at
    least ONE explicit anchor memory OR by at least TWO clusters -- a single
    deliberate anchor is enough ("tell her once"), a lone cluster is not. This is
    what lets the L3 :func:`communication_style_evidence_gate` floor the source
    count at 1. ``subject`` shapes the prompt voice only.
    """
    valid_reps = {int(rep) for rep, _label, _size in cluster_index}
    valid_mem_ids: set[int] = set()
    mem_lines: list[str] = []
    for mem in memories:
        try:
            mid = int(mem.id)
        except (TypeError, ValueError, AttributeError):
            continue
        valid_mem_ids.add(mid)
        mem_lines.append(mem_line(mem))

    if not valid_reps and not valid_mem_ids:
        return []
    existing_ids = {int(e.id) for e in existing}

    sections: list[str] = []
    digest = (style_digest or "").strip()
    if digest:
        sections.append(
            "STYLE SIGNAL (observed lately -- guidance only, NOT evidence you "
            "may cite):\n" + snippet(digest)
        )
    if cluster_index:
        map_lines = [
            f"- [{rep}] {label} (size {size})"
            for rep, label, size in cluster_index
        ]
        sections.append(
            "TOPIC MAP (recurring patterns, by size):\n" + "\n".join(map_lines)
        )
    if focus_clusters:
        focus_lines: list[str] = []
        for fc in focus_clusters:
            parts = [f"[{fc.rep}] {fc.label} (size {fc.size})"]
            if fc.representative:
                parts.append(f"  representative: {snippet(fc.representative)}")
            if fc.digest:
                parts.append(f"  digest: {snippet(fc.digest)}")
            focus_lines.append("\n".join(parts))
        sections.append("FOCUS CLUSTERS (detail):\n" + "\n\n".join(focus_lines))
    if mem_lines:
        sections.append(
            "NOTABLE REMEMBERED NOTES (deliberate anchors -- a single one can "
            "ground a style line):\n" + "\n".join(mem_lines)
        )
    sections.append(
        "ALREADY-KNOWN COMMUNICATION-STYLE LINES:\n" + format_existing(existing)
    )
    sections.append(
        "Propose NEW communication-style lines grounded in the material above "
        "(cite cluster rep ids in 'evidence_cluster_reps' and/or remembered-note "
        "ids in 'evidence_memory_ids'), or reinforce a known one by id."
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
        if not reps and not mids:
            continue
        evidence = [("cluster", str(r)) for r in reps]
        evidence += [("memory", str(i)) for i in mids]
        rationale = str(item.get("rationale") or "").strip()

        reinforces = resolve_reinforces(item.get("reinforces_id"), existing_ids)
        if reinforces is not None:
            proposals.append(
                CandidateProposal(
                    label="",
                    rationale=rationale,
                    confidence=0.0,
                    evidence=evidence,
                    kind="communication_style",
                    subject=subject,
                    evidence_model="set",
                    reinforces_id=reinforces,
                )
            )
            continue

        label = str(item.get("label") or "").strip()
        # Composition rule: one deliberate anchor OR >= 2 clusters.
        if not label or not (len(mids) >= 1 or len(reps) >= 2):
            continue
        proposals.append(
            CandidateProposal(
                label=label,
                rationale=rationale,
                confidence=clamp01(item.get("confidence")),
                evidence=evidence,
                kind="communication_style",
                subject=subject,
                evidence_model="set",
            )
        )
    return proposals


def _tension_base_line(base: TensionBase) -> str:
    """Render one active base concept for the tension prompt: id, its
    subject/kind and confidence, the label, a rationale snippet, and the
    optional live/quiet hint that helps the model spot a push/pull."""
    head = f"[{base.id}] ({base.subject}/{base.kind}, conf {base.confidence:.2f}) {base.label}"
    rat = snippet(base.rationale or "")
    if rat:
        head += f" -- {rat}"
    if base.hint:
        head += f"  [{base.hint}]"
    return head


def propose_tension(
    ctx: ProposerContext,
    *,
    subject: str,
    system: str,
    concepts: Sequence[TensionBase] = (),
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    """Shared body for the L12 tension proposers -- the first *meta* kind.

    A tension is a concept whose evidence is two OTHER active concepts held in
    friction: an internal push/pull the person hasn't articulated ("values rest
    but rarely takes it"; "wants simplicity but keeps adding complexity"), or --
    for ``subject="relationship"`` -- a user value clashing with an aiko value.
    Unlike every other proposer the raw material is not clusters/memories but
    the small set of active *base* (non-meta) concepts (``concepts``), so the
    evidence it emits is ``("concept", id)`` and its ``evidence_model`` is
    ``"meta"``.

    **Composition rule.** A tension holds exactly TWO distinct base concepts, so
    a NEW proposal is accepted only when it cites exactly two distinct ids from
    the offered set (this is the arity the L3 :func:`tension_evidence_gate`
    floors at 2). Because only non-meta actives are ever offered, a tension can
    never reference another tension (the meta depth cap) and cannot form a
    cycle. ``subject`` shapes the prompt lens only.
    """
    valid_ids = {int(b.id) for b in concepts}
    if not valid_ids:
        return []
    existing_ids = {int(e.id) for e in existing}

    base_lines = [_tension_base_line(b) for b in concepts]
    sections = [
        "ACTIVE CONCEPTS (each already an established, settled belief -- cite "
        "their ids):\n" + "\n".join(base_lines),
        "ALREADY-KNOWN TENSIONS:\n" + format_existing(existing),
        "Name a NEW tension ONLY when two of the concepts above are genuinely "
        "in friction (cite exactly two ids in 'evidence_concept_ids'), or "
        "reinforce a known one by id. Return nothing rather than forcing a "
        "clash that isn't there.",
    ]
    user = "\n\n".join(sections)

    raw = ctx.call_llm(system, user)
    proposals: list[CandidateProposal] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cids = list(
            dict.fromkeys(
                c
                for c in coerce_id_list(item.get("evidence_concept_ids"))
                if c in valid_ids
            )
        )
        rationale = str(item.get("rationale") or "").strip()

        reinforces = resolve_reinforces(item.get("reinforces_id"), existing_ids)
        if reinforces is not None:
            # A reinforcement re-affirms an existing tension: it still needs the
            # two sides present so the edges (and the source count) stay whole.
            if len(cids) != 2:
                continue
            evidence = [("concept", str(c)) for c in cids]
            proposals.append(
                CandidateProposal(
                    label="",
                    rationale=rationale,
                    confidence=0.0,
                    evidence=evidence,
                    kind="tension",
                    subject=subject,
                    evidence_model="meta",
                    reinforces_id=reinforces,
                )
            )
            continue

        label = str(item.get("label") or "").strip()
        # Composition rule: a tension is exactly a pair of distinct base concepts.
        if not label or len(cids) != 2:
            continue
        evidence = [("concept", str(c)) for c in cids]
        proposals.append(
            CandidateProposal(
                label=label,
                rationale=rationale,
                confidence=clamp01(item.get("confidence")),
                evidence=evidence,
                kind="tension",
                subject=subject,
                evidence_model="meta",
            )
        )
    return proposals


# The most children a single generalization may abstract over. Keeps the meta
# compact (and its ``distinct_source_count`` sane); an abstraction that wants to
# span more than this is usually two abstractions.
GENERALIZATION_MAX_CHILDREN = 6


def propose_generalization(
    ctx: ProposerContext,
    *,
    subject: str,
    system: str,
    concepts: Sequence[TensionBase] = (),
    existing: Sequence[ExistingConcept] = (),
) -> list[CandidateProposal]:
    """Shared body for the L20 generalization proposers -- the abstraction meta
    kind.

    A generalization is a concept whose evidence is 2+ OTHER active concepts (of
    any kind, same subject) that it names a latent super-concept over: "he
    builds things that last" over React / AI / home-server tinkering, or "she
    reaches for warmth over being right" over several of her own values. Like a
    tension its raw material is the small set of active *base* (non-meta)
    concepts (``concepts``), so the evidence it emits is ``("concept", id)`` and
    its ``evidence_model`` is ``"meta"``. Unlike a tension it holds them in
    *is-a / part-of*, not friction, and its arity is a RANGE (2..N), not a fixed
    pair.

    **Composition rule.** A NEW proposal is accepted only when it cites at least
    two distinct ids from the offered set (the arity the L3
    :func:`generalization_evidence_gate` floors at 2), capped at
    :data:`GENERALIZATION_MAX_CHILDREN`. Because only non-meta actives are ever
    offered, a generalization can never abstract another meta (the depth cap)
    and cannot form a cycle. ``subject`` shapes the prompt lens only.
    """
    valid_ids = {int(b.id) for b in concepts}
    if not valid_ids:
        return []
    existing_ids = {int(e.id) for e in existing}

    base_lines = [_tension_base_line(b) for b in concepts]
    sections = [
        "ACTIVE CONCEPTS (each already an established, settled belief -- cite "
        "their ids):\n" + "\n".join(base_lines),
        "ALREADY-KNOWN ABSTRACTIONS:\n" + format_existing(existing),
        "Name a NEW abstraction ONLY when several of the concepts above (two "
        "or more, of any kind) are really facets of ONE higher-order thing "
        "their individual labels don't name -- an is-a / part-of super-concept "
        "('builds things that last' over specific hobbies; 'protects his own "
        "time' over several habits). Cite every concept it covers in "
        "'evidence_concept_ids', or reinforce a known one by id. This is NOT a "
        "friction or a restatement of one concept -- return nothing rather "
        "than forcing an abstraction that isn't really there.",
    ]
    user = "\n\n".join(sections)

    raw = ctx.call_llm(system, user)
    proposals: list[CandidateProposal] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cids = list(
            dict.fromkeys(
                c
                for c in coerce_id_list(item.get("evidence_concept_ids"))
                if c in valid_ids
            )
        )[:GENERALIZATION_MAX_CHILDREN]
        rationale = str(item.get("rationale") or "").strip()

        reinforces = resolve_reinforces(item.get("reinforces_id"), existing_ids)
        if reinforces is not None:
            # A reinforcement re-affirms an existing abstraction: it still needs
            # at least two children so the edges (and source count) stay whole.
            if len(cids) < 2:
                continue
            evidence = [("concept", str(c)) for c in cids]
            proposals.append(
                CandidateProposal(
                    label="",
                    rationale=rationale,
                    confidence=0.0,
                    evidence=evidence,
                    kind="generalization",
                    subject=subject,
                    evidence_model="meta",
                    reinforces_id=reinforces,
                )
            )
            continue

        label = str(item.get("label") or "").strip()
        # Composition rule: an abstraction covers 2+ distinct base concepts.
        if not label or len(cids) < 2:
            continue
        evidence = [("concept", str(c)) for c in cids]
        proposals.append(
            CandidateProposal(
                label=label,
                rationale=rationale,
                confidence=clamp01(item.get("confidence")),
                evidence=evidence,
                kind="generalization",
                subject=subject,
                evidence_model="meta",
            )
        )
    return proposals


__all__ = [
    "AIKO_SELF_KINDS",
    "GENERALIZATION_MAX_CHILDREN",
    "MIN_SOURCES",
    "CandidateProposal",
    "ExistingConcept",
    "FocusCluster",
    "NarrativeCandidate",
    "ProposerContext",
    "ProposerSpec",
    "TensionBase",
    "clamp01",
    "coerce_id_list",
    "format_existing",
    "propose_aiko_hybrid",
    "propose_boundary",
    "propose_communication_style",
    "propose_generalization",
    "propose_narrative",
    "propose_ordered_concept",
    "propose_tension",
    "resolve_reinforces",
    "snippet",
]
