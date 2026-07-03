"""The ``ConceptKind`` registry (L1).

The concept layer is *kind-parameterized*: one shared store + lifecycle
+ surfacing, plus a small registry where each kind contributes only the
handful of things that actually differ between kinds. Adding a new kind
(value, narrative, tension, self, ...) is a registry entry here, not a
schema migration or an ``if/elif`` ladder in the store/worker/prompt
layers -- those all dispatch through this registry.

Each kind declares four things:

- ``subject`` -- the *typical* subject the kind is about (``user`` /
  ``aiko`` / ``relationship``). Subject is orthogonal to kind: most
  kinds exist for more than one subject, so this is a default, not a
  constraint -- the per-row ``concepts.subject`` column is what actually
  varies.
- ``evidence_model`` -- the *structure* of a concept's evidence, NOT a
  node type: ``set`` (unordered set of sources) / ``sequence`` (ordered
  chain) / ``recurring`` (periodic pattern) / ``meta`` (references other
  concepts). The node type of each piece of evidence (memory / cluster /
  concept) lives per-edge on ``concept_edges.src_type`` and is freely
  mixable, so e.g. a ``set`` concept can draw on both clusters and
  memories at once. This describes shape for the L3 gate and debug/UI;
  it does not constrain which edges a concept may have.
- ``proposer`` / ``promotion_gate`` -- callables supplied by later
  L-entries (L2 mines candidates, L3 decides promotion). They are
  ``None`` in v1: L1 is the substrate only, so nothing proposes or
  promotes yet.
- ``surfacing_target`` -- the name of the prompt block / subsystem an
  active concept of this kind is routed to (L5). A hint string in v1;
  the actual provider wiring lands with L5.

v1 registers only ``identity`` end-to-end scaffolding; every other kind
in the catalogue is a one-line ``register_kind`` call we grow into.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# Recognised values for the orthogonal axes. Kept as plain tuples (not
# enums) so callers can validate without importing heavy machinery, and
# so the ``kind`` axis stays an *open* enum -- unknown kinds are allowed,
# these are just the ones known at v1.
SUBJECTS: tuple[str, ...] = ("user", "aiko", "relationship")
# Structural evidence models -- node-type-agnostic. Node type (memory /
# cluster / concept) is carried per-edge on ``concept_edges.src_type``,
# so these describe *shape* only and evidence may mix node types.
EVIDENCE_MODELS: tuple[str, ...] = (
    "set",
    "sequence",
    "recurring",
    "meta",
)


@dataclass(frozen=True, slots=True)
class ConceptKind:
    """Declarative spec for one concept kind.

    ``proposer`` / ``promotion_gate`` stay ``None`` until L2 / L3 supply
    them; the store and (future) worker read the registry rather than
    branching per kind.
    """

    name: str
    subject: str = "user"
    evidence_model: str = "set"
    proposer: Callable[..., object] | None = None
    promotion_gate: Callable[..., object] | None = None
    surfacing_target: str | None = None


CONCEPT_KINDS: dict[str, ConceptKind] = {}


def register_kind(kind: ConceptKind) -> ConceptKind:
    """Register (or replace) a kind by name. Returns the kind for
    convenient module-level use."""
    CONCEPT_KINDS[kind.name] = kind
    return kind


def get_kind(name: str) -> ConceptKind | None:
    """Look up a registered kind, or ``None`` for an unknown kind."""
    return CONCEPT_KINDS.get(name)


# ── v1: identity end-to-end ───────────────────────────────────────────
# Traits/interests spanning clusters ("he enjoys understanding
# systems"). Homed on the user profile; the proposer/gate arrive with
# L2/L3.
register_kind(
    ConceptKind(
        name="identity",
        subject="user",
        evidence_model="set",
        surfacing_target="profile_block",
    )
)


__all__ = [
    "CONCEPT_KINDS",
    "EVIDENCE_MODELS",
    "SUBJECTS",
    "ConceptKind",
    "get_kind",
    "register_kind",
]
