"""The ``ConceptKind`` registry (L1).

The concept layer is *kind-parameterized*: one shared store + lifecycle
+ surfacing, plus a small registry where each kind contributes only the
handful of things that actually differ between kinds. Adding a new kind
(value, narrative, tension, self, ...) is a registry entry here, not a
schema migration or an ``if/elif`` ladder in the store/worker/prompt
layers -- those all dispatch through this registry.

Each kind declares:

- ``subject`` -- the *typical* subject the kind is about (``user`` /
  ``aiko`` / ``relationship``). Subject is orthogonal to kind: most
  kinds exist for more than one subject, so this is a default, not a
  constraint -- the per-row ``concepts.subject`` column is what actually
  varies.
- ``plasticity_default`` -- the kind's default *inertia band* (L16), the
  per-concept learning rate the L3 engine reads to damp confidence
  movement in both directions (accrual, decay, disproof, revision).
  Low = sticky / core (identity, value); high = fluid (taste,
  affective); medium (boundary). ``None`` => the worker falls back to
  the ``concept_default_plasticity`` setting. The L3 worker stamps this
  onto a concept on its first evaluation.
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
- ``surfacing_target`` -- the *default* prompt block / subsystem an
  active concept of this kind is routed to (L5). Kept as the
  subject-agnostic fallback.
- ``surfacing_targets`` -- the **authoritative** (L24) per-subject
  routing map (``subject -> target``), because the same kind can feed
  different consumers depending on subject (e.g. ``identity`` feeds the
  user ``profile_block`` for ``subject=user`` but the ``self_image_block``
  for ``subject=aiko``). A ``"*"`` key is the wildcard for any other
  subject; an empty map falls back to ``surfacing_target``. Consumers
  don't read this directly -- they call :func:`kinds_for_target`
  (via ``ConceptView.for_target``), so a new kind auto-flows to its
  declared consumers with no consumer code change.

v1 registers only ``identity`` end-to-end scaffolding; every other kind
in the catalogue is a one-line ``register_kind`` call we grow into.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.concepts.concept_lifecycle import set_evidence_gate

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
    # L16: default plasticity band for the kind (the L3 learning rate).
    # ``None`` => fall back to the ``concept_default_plasticity`` setting.
    plasticity_default: float | None = None
    proposer: Callable[..., object] | None = None
    promotion_gate: Callable[..., object] | None = None
    surfacing_target: str | None = None
    # L24: authoritative per-subject routing (``subject -> target``); a
    # ``"*"`` key is the wildcard. Empty => fall back to ``surfacing_target``.
    surfacing_targets: dict[str, str] = field(default_factory=dict)
    # L27: whether this kind participates in the *always-on core lane* — the
    # high-confidence concepts pinned into the prompt every turn regardless
    # of cosine to the live turn (who the user is, what they + Aiko value,
    # how she wants to behave). Off by default: a kind opts in here so it
    # auto-joins the balanced core selection with no selector code change.
    core_always_on: bool = False
    # L27: the per-kind confidence bar for the core lane. Behaviour-loaded
    # kinds (value, boundary) should sit *higher* than tastes; this is the
    # natural companion to the L16 ``plasticity_default`` band (sticky kinds
    # earn a higher bar). ``None`` => fall back to the global
    # ``context_budget_core_min_confidence`` setting.
    core_min_confidence: float | None = None


CONCEPT_KINDS: dict[str, ConceptKind] = {}


def register_kind(kind: ConceptKind) -> ConceptKind:
    """Register (or replace) a kind by name. Returns the kind for
    convenient module-level use."""
    CONCEPT_KINDS[kind.name] = kind
    return kind


def get_kind(name: str) -> ConceptKind | None:
    """Look up a registered kind, or ``None`` for an unknown kind."""
    return CONCEPT_KINDS.get(name)


# ── surfacing-target routing (L24) ────────────────────────────────────
# ``surfacing_targets`` is authoritative; ``surfacing_target`` is the
# subject-agnostic fallback. These resolvers are the one place that logic
# lives -- consumers ask "which kinds feed my block?" via
# :func:`kinds_for_target`, never by branching on kind names.

def target_for(kind: ConceptKind, subject: str | None = None) -> str | None:
    """The single target a concept of this ``kind`` / ``subject`` routes
    to: the per-subject entry, then the ``"*"`` wildcard, then the scalar
    ``surfacing_target`` fallback."""
    targets = kind.surfacing_targets
    if targets:
        if subject is not None and subject in targets:
            return targets[subject]
        if "*" in targets:
            return targets["*"]
    return kind.surfacing_target


def targets_of(kind: ConceptKind, subject: str | None = None) -> set[str]:
    """Every target a concept of this ``kind`` may route to. With
    ``subject`` given this is at most one target; without it, the union of
    all per-subject targets plus the scalar fallback."""
    if subject is not None:
        t = target_for(kind, subject)
        return {t} if t else set()
    out: set[str] = set(kind.surfacing_targets.values())
    if kind.surfacing_target:
        out.add(kind.surfacing_target)
    return out


def core_lane_kinds() -> list[ConceptKind]:
    """Every registered kind that opts into the L27 always-on core lane.

    The plug-in seam consumed by ``ConceptView.core_lane``: a new kind sets
    ``core_always_on=True`` (plus an optional ``core_min_confidence`` bar)
    and auto-joins the balanced core selection — no selector code change.
    Sorted by name for deterministic bucket ordering downstream."""
    return sorted(
        (k for k in CONCEPT_KINDS.values() if k.core_always_on),
        key=lambda k: k.name,
    )


def kinds_for_target(target: str, subject: str | None = None) -> set[str]:
    """The set of registered kind names that route to ``target`` (for the
    given ``subject`` when provided, else across all subjects).

    The plug-in seam consumed by ``ConceptView.for_target``: a new kind
    declares its ``surfacing_targets`` and auto-appears here for the
    matching consumer -- no consumer code change."""
    if not target:
        return set()
    return {
        name
        for name, kind in CONCEPT_KINDS.items()
        if target in targets_of(kind, subject)
    }


# ── v1: identity end-to-end ───────────────────────────────────────────
# Traits/interests spanning clusters ("he enjoys understanding
# systems"). Homed on the user profile; the proposer/gate arrive with
# L2/L3.
register_kind(
    ConceptKind(
        name="identity",
        subject="user",
        evidence_model="set",
        # L16: identity is a *core* trait band -> low plasticity (sticky
        # for decay, disproof, *and* accrual). The ``concept_identity_plasticity``
        # setting still overrides this in the worker for back-compat/tuning.
        plasticity_default=0.3,
        # L3: identity uses the set-evidence promotion gate (distinct
        # sources + age-stability + confidence). The worker falls back to
        # this same gate for any kind that doesn't supply its own.
        promotion_gate=set_evidence_gate,
        # L24: identity is the same kind for both subjects but feeds
        # different consumers -- the user profile block for what *he* is
        # like, Aiko's self-image block for what *she* is like.
        surfacing_target="profile_block",
        surfacing_targets={
            "user": "profile_block",
            "aiko": "self_image_block",
        },
        # L27: identity is the anchor of the always-on core lane — who the
        # user is and how Aiko tends to be, carried into every turn. Its bar
        # falls back to the global ``context_budget_core_min_confidence``.
        core_always_on=True,
    )
)


__all__ = [
    "CONCEPT_KINDS",
    "EVIDENCE_MODELS",
    "SUBJECTS",
    "ConceptKind",
    "core_lane_kinds",
    "get_kind",
    "kinds_for_target",
    "register_kind",
    "target_for",
    "targets_of",
]
