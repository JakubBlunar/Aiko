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
    population: str  # "clusters" | "aiko_memories"
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


__all__ = [
    "AIKO_SELF_KINDS",
    "MIN_SOURCES",
    "CandidateProposal",
    "ExistingConcept",
    "FocusCluster",
    "ProposerContext",
    "ProposerSpec",
    "clamp01",
    "coerce_id_list",
    "format_existing",
    "resolve_reinforces",
    "snippet",
]
