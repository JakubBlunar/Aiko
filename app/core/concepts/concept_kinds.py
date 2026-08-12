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
  user ``profile_block`` for ``subject=user``). ``subject=aiko`` concepts
  are *not* routed to a named for_target block -- they surface every turn
  through the T3 ``relevant_context`` path (core lane + relevance), so
  they have no entry here. A ``"*"`` key is the wildcard for any other
  subject; an empty map falls back to   ``surfacing_target``. Consumers
  don't read this directly -- they call :func:`kinds_for_target`
  (via ``ConceptView.for_target``), so a new kind auto-flows to its
  declared consumers with no consumer code change.
- ``role`` -- what a concept of this kind *does to a decision*, which is
  orthogonal to both subject and kind. See :data:`ROLES`.

v1 registers only ``identity`` end-to-end scaffolding; every other kind
in the catalogue is a one-line ``register_kind`` call we grow into.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.core.concepts.concept_lifecycle import (
    affective_evidence_gate,
    aspiration_evidence_gate,
    boundary_evidence_gate,
    communication_style_evidence_gate,
    conduct_evidence_gate,
    generalization_evidence_gate,
    identity_evidence_gate,
    narrative_evidence_gate,
    pursuit_evidence_gate,
    ritual_evidence_gate,
    taste_evidence_gate,
    tension_evidence_gate,
    value_evidence_gate,
)

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

# What a concept of this kind does to a decision, as opposed to what it is
# about. The axis exists because a selection ranked on strength alone
# converges on the kinds with the highest stakes priors -- ``boundary``
# (0.9) and ``value`` (0.85) -- and a prompt built only from those is one
# that can restate what Aiko already holds but never reach past it.
#
# - ``anchor``     ground truth. Who someone is, what happened, the
#                  pattern over it. Neither a rail nor a spur; the things
#                  a reply needs to be *about the right person*.
# - ``guide``      constrains action. A line to keep, a principle to
#                  honour, a way to speak. Load-bearing and, in excess,
#                  the thing that makes her careful instead of curious.
# - ``generative`` could move. An enjoyment, an interest of her own, a
#                  direction, an unresolved friction. What she reaches
#                  *with* rather than what she reasons *within*.
#
# Consumed by the openness reserve on the core lane, the generative floor
# on the flex lane, and the concept diets -- all of which ask "is this
# selection all rails?" and none of which should branch on kind names.
ROLE_ANCHOR = "anchor"
ROLE_GUIDE = "guide"
ROLE_GENERATIVE = "generative"
ROLES: tuple[str, ...] = (ROLE_ANCHOR, ROLE_GUIDE, ROLE_GENERATIVE)


@dataclass(frozen=True, slots=True)
class SurfaceWeights:
    """Per-kind blend for the turn-relevant surfacing score (L18).

    The concept fill in ``build_relevant_context`` scores each candidate by a
    normalized blend of three signals rather than raw cosine alone:

    - ``context`` -- cosine of the concept label to the live turn embedding.
    - ``confidence`` -- the concept's stored confidence.
    - ``recency`` -- a decay boost from ``last_reinforced_at`` (see
      :func:`app.core.concepts.concept_surfacing.recency_boost`), with the
      per-kind ``recency_halflife_days`` controlling how fast it fades.

    Defaults are **context-only** (all other weights ``0``), which reproduces
    the pre-L18 cosine-only behaviour, so a kind opts into the blend by setting
    non-zero weights -- no change to any other kind until it is tuned.
    Behaviour-loaded kinds (boundary) weight recency higher: a line she was just
    reminded of should matter more than a stale one, whereas an identity trait
    barely cares about recency.

    The L23 cognitive-surfacing pass adds three more per-kind signals to the
    same blend (all default ``0.0`` -> opt-in, no change until tuned):

    - ``stability`` -- ``confidence * plasticity-adjusted`` (see
      :func:`app.core.concepts.concept_surfacing.stability`): a settled, sticky
      (low-plasticity) core belief is worth asserting on *how held* it is, not
      just cosine. Identity/value opt in.
    - ``salience`` -- an emotional/recent-change charge (affect magnitude +
      recent lifecycle events + fresh reinforcement) that lets a charged concept
      intrude. Behaviour/affective kinds opt in.
    - ``activation`` -- the spreading-activation weight. Unlike the others this
      is **not** part of the sum-normalized blend; it is an additive boost
      applied on top (a concept associated with the turn's hot topics is primed
      even at low direct cosine). ``activation_*`` half-life is unused today.
    - ``standing`` (L38) -- the relationship-local prior earned from whether
      this concept's previous surfacings led to engaged turns. It stays
      separate from confidence (usefulness is not truth) and participates in
      the normalized base rather than stacking as a bonus.

    ``recency_halflife_days`` controls the recency decay; ``salience_halflife_days``
    the recent-change event decay.
    """

    context: float = 1.0
    confidence: float = 0.0
    recency: float = 0.0
    recency_halflife_days: float = 30.0
    stability: float = 0.0
    salience: float = 0.0
    salience_halflife_days: float = 21.0
    activation: float = 0.0
    standing: float = 0.0


DEFAULT_SURFACE_WEIGHTS = SurfaceWeights()


@dataclass(frozen=True, slots=True)
class PlasticityModulation:
    """Per-kind relationship modulation of the L16 plasticity band.

    The L3 engine damps every confidence move by a per-concept ``plasticity``
    (the learning rate). This spec lets a kind's *effective* plasticity be
    raised by the live relationship signal at eval time -- a behaviour boundary
    should *loosen* (become more renegotiable) as trust deepens and the
    relationship matures, but never silently and never below its stored base.

    Both gains are additive lifts (in plasticity units) applied to the base:
    ``trust_gain`` at full positive trust, ``duration_gain`` at "full"
    relationship duration. ``max_plasticity`` is the ceiling the lift is
    clamped to, so a boundary can loosen but never become fully fluid.

    Defaults are **no-op** (both gains ``0.0``, ceiling ``1.0``), which
    reproduces the pre-modulation behaviour, so a kind opts in by setting
    non-zero gains -- no change to any other kind until it is tuned. See
    :func:`app.core.concepts.concept_lifecycle.effective_plasticity`.
    """

    trust_gain: float = 0.0
    duration_gain: float = 0.0
    max_plasticity: float = 1.0


DEFAULT_PLASTICITY_MODULATION = PlasticityModulation()


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
    # Whether a concept of this kind may be *rendered* into the static T3
    # relevant-context block at all. Off means the kind speaks only through
    # a dedicated surface of its own. No kind sets it today -- ``tension``
    # did until H10, and that exclusion cost it every one of 13,800 concept
    # surfacings -- but the hook stays declarative because three separate
    # selection paths have to agree with the renderer: the flex lane, the
    # hypothesis lane, and the L28 openness reserve.
    static_render: bool = True
    # H10: whether this kind may hold a *pinned* slot -- one it occupies
    # regardless of cosine to the live turn, via the L28 openness reserve.
    # Distinct from ``static_render`` (may it render at all) and from
    # ``core_always_on`` (does it opt into the ordinary core lane), because
    # rendering and pinning are different promises and ``tension`` wants
    # opposite answers. A friction should be raised when the turn is
    # actually about it and left alone otherwise: pinning one into every
    # turn is precisely the nagging L12's cooldown was built to prevent,
    # and the openness reserve's own notes flagged it as the failure to
    # avoid if the render carve-out were ever relaxed. It was, so this is
    # the half that stays.
    pinnable: bool = True
    # L27: the per-kind confidence bar for the core lane. Behaviour-loaded
    # kinds (value, boundary) should sit *higher* than tastes; this is the
    # natural companion to the L16 ``plasticity_default`` band (sticky kinds
    # earn a higher bar). ``None`` => fall back to the global
    # ``context_budget_core_min_confidence`` setting.
    core_min_confidence: float | None = None
    # L18: the per-kind blend the turn-relevant surfacing scorer uses (context
    # cosine + confidence + recency). Defaults to context-only, which is exactly
    # the pre-L18 cosine ranking, so this is opt-in per kind.
    surface_weights: SurfaceWeights = DEFAULT_SURFACE_WEIGHTS
    # L16: how the live relationship signal (trust + duration) raises this
    # kind's *effective* plasticity at eval time. Defaults to no-op, so a kind
    # opts in (only ``boundary`` does now) -- the L3 worker reads this to loosen
    # a boundary as the bond deepens, never touching the stored base.
    plasticity_modulation: PlasticityModulation = DEFAULT_PLASTICITY_MODULATION
    # L32: the kind's *stakes* prior in [0, 1] -- how much a belief of this
    # kind matters, which is a different question from how likely it is to
    # be true (``confidence``) and from how much any one signal should
    # weigh for the kind (``surface_weights.salience``). A boundary is
    # high-stakes at any confidence; a tooling preference is low-stakes even
    # when certain. Read at surfacing time by
    # ``concept_importance.kind_importance`` and lifted from there by the
    # emotional charge of the concept's topics; never stored on a row.
    # 0.5 is the no-opinion default, which leaves surfacing untouched.
    importance: float = 0.5
    # L12 meta rule 2, made declarative: how many of a meta concept's base
    # concepts must still be ``active`` for it to stay live. ``None`` => all of
    # them, which is a tension's arity (lose either side of the friction and it
    # is moot). A generalization abstracts several children, so it survives
    # losing one (2). ``0`` says the bases are *history* rather than a live
    # dependency -- the L17d self-correction rule stands on corrections that
    # happened, and a correction does not stop having happened. Only read for
    # ``evidence_model == "meta"`` concepts, so setting it on a kind that also
    # has base-model rows (``communication_style``) is inert for those.
    meta_min_active_bases: int | None = None
    # What this kind does to a decision -- one of :data:`ROLES`. Defaults
    # to ``anchor``, which is the inert setting: an anchor is neither
    # drawn on to open a selection up nor displaced to make room for one
    # that does, so an unclassified kind changes no balance decision until
    # it is deliberately labelled a rail or a spur.
    role: str = ROLE_ANCHOR


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


def kinds_by_role(role: str) -> list[ConceptKind]:
    """Every registered kind carrying ``role``, sorted by name.

    The plug-in seam for the three balance mechanisms (openness reserve,
    generative floor, diet invariants), mirroring :func:`core_lane_kinds`:
    a new kind declares its ``role`` and auto-joins the right side of
    every balance decision with no selector code change. An unrecognised
    role returns ``[]`` rather than raising -- the mechanisms treat "no
    candidates" as "leave the selection alone", which is the safe read of
    a typo'd role name."""
    if not role:
        return []
    return sorted(
        (k for k in CONCEPT_KINDS.values() if k.role == role),
        key=lambda k: k.name,
    )


def renders_in_static_block(kind_name: str) -> bool:
    """Whether concepts of ``kind_name`` may be rendered into the static
    T3 relevant-context block.

    The one place the ``static_render`` carve-out is read, so the three
    selection lanes and the renderer cannot disagree about it. An unknown
    kind answers ``True``: a row whose kind is not registered has no
    dedicated surface to speak through instead, and silently dropping it
    everywhere would make a registry gap look like a cold layer."""
    kind = CONCEPT_KINDS.get(str(kind_name or ""))
    return bool(kind.static_render) if kind is not None else True


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
        # L32: who someone is matters, but it is rarely *urgent* -- above the
        # neutral middle, below the kinds that carry a duty of care.
        importance=0.6,
        role=ROLE_ANCHOR,
        # L3: identity carries its own set-evidence gate (distinct sources +
        # age-stability + confidence), floored at three sources and a real
        # stability delay. It used to ride the bare ``set_evidence_gate`` --
        # which is still the worker's fallback for any kind that supplies no
        # gate -- but on the global settings alone (min age 0.0) that let
        # traits promote the moment a second source appeared.
        promotion_gate=identity_evidence_gate,
        # L24: identity feeds the user profile block for what *he* is like.
        # ``subject=aiko`` identity is not routed to a named block -- it
        # surfaces every turn via the T3 relevant_context path instead.
        surfacing_target="profile_block",
        surfacing_targets={
            "user": "profile_block",
        },
        # L27: identity is the anchor of the always-on core lane — who the
        # user is and how Aiko tends to be, carried into every turn. Its bar
        # falls back to the global ``context_budget_core_min_confidence``.
        core_always_on=True,
        # L23: on the turn-relevant lane, a non-core identity trait ranks on how
        # *settled* it is (stability), not recency -- who someone is barely cares
        # that they were reminded of it. Context stays dominant.
        surface_weights=SurfaceWeights(
            context=0.6, confidence=0.1, stability=0.3, activation=0.15,
            standing=0.1,
        ),
    )
)


# ── L10: value ────────────────────────────────────────────────────────
# The normative *why* under the choices — a shared principle a group of
# clusters/memories reflects ("he values owning his data"; "I value honesty
# over agreeableness"). Same ``set`` machinery as identity, but the deepest,
# hardest-won layer: a stricter promotion gate and lower plasticity, and a
# higher core-lane bar so a value only pins into every turn once it is very
# settled. Subject-parameterized across all three: user + aiko exactly like
# identity, plus relationship (H12) for what the pair holds together, mined
# from the same shared-moment groups the ritual kind reads.
register_kind(
    ConceptKind(
        name="value",
        subject="user",
        evidence_model="set",
        # L16: values are the stickiest concepts of all -> lower plasticity
        # than identity (0.3), so confidence moves (accrual + decay + disproof)
        # are heavily damped once one is held.
        plasticity_default=0.2,
        # L32: what someone stands for is near the top of the stakes ladder.
        # Getting a value wrong costs more than getting a preference wrong,
        # which is the same instinct behind this kind's ``protect_downward``
        # standing and its raised core-lane bar.
        importance=0.85,
        # A value is the normative *why* under a choice, so it constrains
        # the choice -- a rail, however warmly it is phrased.
        role=ROLE_GUIDE,
        # L3: stricter than the plain set gate (more sources, non-instant age,
        # higher confidence) -- values should be slow and hard-won.
        promotion_gate=value_evidence_gate,
        # L24: same per-subject routing as identity -- a user value feeds the
        # user profile view; aiko and relationship values surface via the T3
        # relevant_context path, not a named for_target block.
        surfacing_target="profile_block",
        surfacing_targets={
            "user": "profile_block",
        },
        # L27: values join the always-on core lane (they drive how Aiko wants
        # to behave), but at a *higher* bar than identity -- only assert a
        # value every turn when it is very settled.
        core_always_on=True,
        core_min_confidence=0.85,
        # L23: values lean on stability even more than identity -- a hard-won
        # principle asserts on how firmly it is held.
        surface_weights=SurfaceWeights(
            context=0.55, confidence=0.1, stability=0.35, activation=0.15,
            standing=0.1,
        ),
    )
)


# ── L13: affective ────────────────────────────────────────────────────
# The durable topic->emotion mapping: what energizes vs. drains him, and
# how certain topics move Aiko ("debugging frustrates then satisfies him";
# "explaining systems lifts me"; "I don't like talking about X"). Same
# ``set`` machinery, evidence mixing topic clusters (per-cluster affect map)
# and, for aiko, her affect-stamped self-memories. Distinct from K2 mood
# beliefs (which model *current* mood) -- these are the durable pattern.
register_kind(
    ConceptKind(
        name="affective",
        subject="user",
        evidence_model="set",
        # L16: affect is the *fluid* end of the plasticity spectrum -- a
        # topic's emotional weather shifts faster than an identity trait or a
        # value -> higher plasticity so confidence tracks change more readily.
        plasticity_default=0.5,
        # L32: high stakes on a *separate* axis from confidence -- "he may be
        # struggling with X" is exactly the shaky-but-weighty belief L32
        # exists to stop burying. Note this is the kind whose concepts also
        # attract the largest affect lift, so the two compound.
        importance=0.75,
        # Descriptive, not directive: "debugging drains him" is a fact about
        # the weather over a topic. It shapes tone the way any ground truth
        # does, but it does not tell her what she may or may not do.
        role=ROLE_ANCHOR,
        # L3: the fluid-end gate (a lower age + confidence bar than value,
        # but still >= 2 distinct sources).
        promotion_gate=affective_evidence_gate,
        # L24 / L27: affective concepts are *tone guidance* -- they should
        # surface when the live turn's topic matches, not be pinned every
        # turn. So no ``surfacing_targets`` (they route through the T3
        # relevant_context relevance path for both subjects) and they do NOT
        # join the always-on core lane.
        # L23: a topic's emotional weather is a *recent* thing -- weight recency
        # so a freshly-felt affect outranks a stale one (salience added in the
        # L23 Phase 2 pass). Short-ish half-life like boundary.
        surface_weights=SurfaceWeights(
            context=0.5, recency=0.3, salience=0.2, activation=0.25,
            standing=0.1,
            recency_halflife_days=21.0, salience_halflife_days=21.0,
        ),
    )
)


# ── K81: taste ────────────────────────────────────────────────────────
# The *preference* axis the topic stack never had. The topic graph knows
# frequency (what came up, how often) and cluster_affect knows emotion
# (valence/arousal); neither knows which topics reliably go *well* between
# the two of them. K81 reads that off the L37 surfacing ledger -- per-cluster
# engaged/settled rate -- and stores it as a durable, first-person,
# relationship-scoped enjoyment ("you genuinely light up getting into X with
# him"). Only an ``aiko`` proposer ships: taste is *hers*, coloured by the
# bond, never a claim about what he should like. Same ``set`` machinery as
# affective, and deliberately the same fluid plasticity band so a taste forms
# and drifts through the normal lifecycle rather than being pinned.
register_kind(
    ConceptKind(
        name="taste",
        subject="aiko",
        evidence_model="set",
        # L16: fluid end, like affective -- an enjoyment shifts as the
        # relationship's rhythm shifts, so confidence should track change
        # readily rather than harden into a fixed trait.
        plasticity_default=0.5,
        # L32: the canonical *low*-stakes kind, and the one the L32 sketch
        # names outright -- an enjoyment is worth having and cheap to be
        # wrong about, so it should not crowd out weightier beliefs however
        # confident it gets.
        importance=0.3,
        # The lowest stakes prior in the registry paired with a generative
        # role is the whole reason the role axis exists: on strength alone
        # a taste never outranks a boundary, so without a seat kept for it
        # the openest thing she holds is the first thing dropped.
        role=ROLE_GENERATIVE,
        # K81: the taste gate -- >= 2 clusters of evidence, a short stability
        # delay, a moderate confidence bar (floors the shared set gate).
        promotion_gate=taste_evidence_gate,
        # L24 / L27: a taste is enthusiasm colour, not a pinned fact -- it
        # surfaces when the live turn's topic matches (T3 relevance), never
        # every turn. So no ``surfacing_targets`` (relevance-only route, like
        # affective) and it does NOT join the always-on core lane.
        # L23: an enjoyment is a *recent-leaning* reading -- a topic that has
        # been landing lately outranks one that used to; salience lets a
        # freshly-enjoyed topic intrude, and activation lifts it when its
        # cluster is hot this turn. Mirrors the affective blend.
        surface_weights=SurfaceWeights(
            context=0.5, recency=0.3, salience=0.2, activation=0.25,
            standing=0.1,
            recency_halflife_days=21.0, salience_halflife_days=21.0,
        ),
    )
)


# ── K85c: pursuit ─────────────────────────────────────────────────────
# The *third subject*. Everything else she can lean on in a lull is about
# him or about the two of them: taste is explicitly bond-scoped ("topics
# she enjoys getting into with {user}"), value and identity are how she
# reasons, and three quarters of them name him outright. So when the room
# goes quiet she has nothing of her own to open with, and the measured
# result is a companion who follows -- an 82% anaphoric-opener rate and
# almost no material in a reply that wasn't in his message first (K90).
#
# A pursuit is something she keeps returning to in her own time: the
# garden, a book series, whatever the away beats and hobby milestones say
# she actually does. It is deliberately NOT called ``interest`` -- T6
# already carries ``interest_drift_block`` (K64b) and
# ``dormant_interest_block`` (K67), and both of those mean "a *shared*
# topic cluster's mass moved", which is a different thing wearing the
# same word.
#
# Mined from the K85b ``pursuit_note`` memories, which is why this kind
# had to wait for them: nothing she did alone used to survive the week.
register_kind(
    ConceptKind(
        name="pursuit",
        subject="aiko",
        evidence_model="set",
        # L16: stickier than taste (0.5) and looser than a value (0.2). An
        # interest of her own should outlive a quiet fortnight -- that is
        # the whole point of it -- but a hobby she has genuinely dropped
        # must be allowed to fade rather than harden into a claim she
        # keeps making about herself.
        plasticity_default=0.35,
        # L32: above taste, below a value. It is worth more than enjoyment
        # colour because she leads with it, and less than a principle
        # because being wrong costs an awkward opener, not a broken trust.
        importance=0.45,
        # An interest of her own is the thing she leads *with* -- the K85c
        # answer to a companion who only ever follows.
        role=ROLE_GENERATIVE,
        promotion_gate=pursuit_evidence_gate,
        # L24 / L27: emphatically NOT core_always_on. A pinned "you are
        # into gardening" every single turn is the canned-hobby failure
        # the backlog warns about; a pursuit earns its way into a turn
        # either through T3 relevance or through the K85e lull block, and
        # both of those are situational by construction.
        # L18e: leans on stability -- an interest is a *settled* thing, so
        # it should not be reshuffled by whichever note landed last week.
        # Recency still gets a small share so a pursuit she has actually
        # been at lately outranks one she has not.
        surface_weights=SurfaceWeights(
            context=0.55, stability=0.25, recency=0.2, activation=0.15,
            standing=0.1, recency_halflife_days=45.0,
        ),
    )
)


# ── L42: surfacing conduct ────────────────────────────────────────────
# A relationship-scoped self-observation about how Aiko allocates attention:
# concentration, neglect, or fixation. It is not pinned identity and never
# exposes ledger mechanics; it surfaces only when relevant.
register_kind(
    ConceptKind(
        name="conduct",
        subject="aiko",
        evidence_model="set",
        plasticity_default=0.4,
        # L32: how she allocates attention shapes behaviour, so it sits above
        # the middle -- but it is a self-observation, not a duty of care.
        importance=0.6,
        # A self-observation phrased as a correction ("you have been
        # fixating on X") reads as an instruction for the next turn, so it
        # constrains even though nobody set it as a line.
        role=ROLE_GUIDE,
        promotion_gate=conduct_evidence_gate,
        surface_weights=SurfaceWeights(
            context=0.6, recency=0.2, stability=0.2, activation=0.15,
            standing=0.1, recency_halflife_days=30.0,
        ),
    )
)


# ── L8: narrative (arc) ───────────────────────────────────────────────
# A referenceable causal arc: an ordered chain of episodic memories collapsed
# into one named story ("The Great 13900KS Investigation"; for aiko, "the
# stretch where I learned to hold a gentle stance"). The first *ordered*
# (``sequence``) evidence kind -- the chain order lives on ``concept_edges.ordinal``
# (already in the schema). Subject-parameterized across ALL THREE subjects --
# user + aiko (L8, arcs over each one's own memories) and relationship (L29a,
# a closed joint project cut out of the ``shared_moment`` stream) -- so the
# per-row ``subject`` varies and the ``subject`` below is only the typical
# default, never an allow-list. Distinct from a rolling recency digest (that's
# the conversation summary's job, not a concept) and from the deferred L29b
# meta-narrative (an arc over other *concepts*).
register_kind(
    ConceptKind(
        name="narrative",
        subject="user",
        evidence_model="sequence",
        # L16: a *closed* story is fairly stable once told -> low-ish plasticity
        # (same band as identity), so a settled arc resists churn but can still
        # decay if never recalled.
        plasticity_default=0.3,
        # L32: a told story is worth remembering but rarely changes what she
        # should *do* -- the no-opinion middle, left explicit so the ladder
        # reads end to end.
        importance=0.5,
        # A *closed* arc is settled history. Its open-ended sibling
        # ``aspiration`` is the generative one; that is the whole
        # difference between the two kinds.
        role=ROLE_ANCHOR,
        # L18e: a closed arc asserts on how *settled* it is, not recency
        # (mirrors identity) -- context stays dominant, stability breaks ties.
        surface_weights=SurfaceWeights(
            context=0.6, confidence=0.1, stability=0.3, standing=0.1,
        ),
        # L3: the sequence gate -- >= 3 chain steps (a story, not an anecdote),
        # a non-instant age, a moderate confidence bar.
        promotion_gate=narrative_evidence_gate,
        # L24 / L27: arcs are recalled when the live turn touches them, not
        # pinned every turn -> no ``surfacing_targets`` (relevance-only for both
        # subjects) and they do NOT join the always-on core lane.
    )
)


# ── L14: aspiration (trajectory) ──────────────────────────────────────
# The open-ended sibling of narrative: not a *closed* arc but a *direction*
# someone is moving in ("building toward a fully self-hosted life"; for aiko,
# first-person "growing into someone he can rely on"). Reuses the ``sequence``
# evidence model (ordered chain on ``concept_edges.ordinal``) but promotes on a
# consistent direction sustained over time rather than a resolution. Subject-
# parameterized (user + aiko) exactly like narrative; the per-row ``subject``
# varies. Distinct from Aiko's concrete K1 goals (actionable to-dos) -- an
# aspiration is who she is *becoming*, not a task.
register_kind(
    ConceptKind(
        name="aspiration",
        subject="user",
        evidence_model="sequence",
        # L16: a direction is durable but *evolves* as progress happens -> a
        # mid band, more fluid than a settled narrative arc (0.3) but stickier
        # than affect (0.5).
        plasticity_default=0.4,
        # L32: what someone is reaching for is worth protecting -- above a
        # closed arc, since a trajectory is still live and can be helped or
        # hindered by how she responds.
        importance=0.6,
        # A direction is unfinished by definition, which is exactly what
        # makes it something to reach with.
        role=ROLE_GENERATIVE,
        # L3: the aspiration gate -- >= 3 ordered steps, a *higher* age floor
        # than narrative (a trajectory must be sustained), moderate confidence.
        promotion_gate=aspiration_evidence_gate,
        # L24 / L27: aspirations surface when the live turn touches them (T3
        # relevance) AND via the proactive momentum worker -> no
        # ``surfacing_targets`` and NOT in the always-on core lane.
        # L18e: a trajectory is a *moving* thing -- weight recency so a
        # freshly-advanced aspiration outranks a stale one; context still leads.
        surface_weights=SurfaceWeights(
            context=0.6, confidence=0.15, recency=0.25,
            standing=0.1,
            recency_halflife_days=21.0,
        ),
    )
)


# ── L7: ritual (relationship) ─────────────────────────────────────────
# The recurring "this is a thing you two do" pattern -- a named relationship
# ritual mined from shared_moment memories ("Friday debugging evenings",
# "the pre-release nerves-and-tea"). Subject is *relationship*: it's about
# the pair, not either person alone. Uses the ``set`` machinery (evidence =
# the constituent shared moments) with a recurrence-flavoured gate. Distinct
# from catchphrases (a recurring *phrase*) and from the relationship-phase
# block (the arc, not a specific ritual). Surfaces via the T3 relevance path,
# not a named for_target block.
register_kind(
    ConceptKind(
        name="ritual",
        subject="relationship",
        evidence_model="set",
        # L16: rituals are mid-band -- warmer/softer than a value, but a
        # settled shared pattern shouldn't churn every time a moment lands.
        plasticity_default=0.4,
        # L32: relationship colour. Warm and worth having, low cost to be
        # wrong about -- just above taste.
        importance=0.4,
        # An established pattern the two of them already have. Warm, but
        # it describes what is rather than opening what could be.
        role=ROLE_ANCHOR,
        # L3: recurrence gate (>= 3 distinct moments / non-instant age /
        # moderate confidence) -- a one-off evening isn't a ritual.
        promotion_gate=ritual_evidence_gate,
        # L24 / L27: rituals are relationship colour -- they should surface
        # when the live turn touches the shared pattern, not be pinned every
        # turn. So no ``surfacing_targets`` (they route through the T3
        # relevant_context relevance path) and they do NOT join the core lane.
        # L18e: a settled shared pattern leans on stability, with a light
        # recency nudge so a recently-enacted ritual reads a touch warmer.
        surface_weights=SurfaceWeights(
            context=0.65, stability=0.2, recency=0.15,
            standing=0.1,
            recency_halflife_days=30.0,
        ),
    )
)


# ── L18: boundary ─────────────────────────────────────────────────────
# The behaviour-*gating* kind: a line to be gentle about rather than a trait
# ("go gentler about work with him", first-person "I won't fake agreement just
# to please him"). Unlike the other set kinds it is mined from topic clusters
# AND Aiko's explicit remembered anchors (``self_tagged`` about the user /
# ``self`` about herself), and a SINGLE deliberate anchor can seed one (the L18
# gate floors the source count at 1; the proposer guarantees a one-source
# boundary is anchor-grounded). Subject-parameterized (user + aiko). These are
# guiding, not refusals -- the rendering keeps them soft. Because a boundary is
# behaviorally load-bearing it joins the always-on core lane, and it weights
# *recency* in surfacing (a line she was just reminded of matters more).
register_kind(
    ConceptKind(
        name="boundary",
        subject="user",
        evidence_model="set",
        # L16: the canonical *medium* plasticity band -- stickier than affect
        # (0.5) so a boundary doesn't churn, more fluid than a value (0.2) so it
        # can be renegotiated. This is the base the L16 trust modulation lifts
        # from as the bond deepens (see ``plasticity_modulation`` below).
        plasticity_default=0.45,
        # L32: the top of the stakes ladder. A boundary is the one kind that
        # *gates behaviour*, and crossing one costs more than any amount of
        # being right elsewhere -- so it outranks even a value, and stays
        # weighty at confidence levels that would bury an ordinary belief.
        importance=0.9,
        # The archetypal rail: the one kind whose entire job is to gate
        # behaviour. Highest stakes prior in the registry, which is why
        # the balance mechanisms have to name it explicitly.
        role=ROLE_GUIDE,
        # L3: the boundary gate -- floors the source count at 1 so a single
        # deliberate anchor can promote (cluster-only boundaries still need >= 2,
        # enforced by the proposer), with medium age + confidence bars.
        promotion_gate=boundary_evidence_gate,
        # L24 / L27: a boundary is a behaviour guide, not a user-profile fact, so
        # it is NOT routed to ``profile_block`` -- it surfaces via the T3
        # relevant_context path (core-lane pin + relevance) for both subjects.
        core_always_on=True,
        core_min_confidence=0.8,
        # L18: recency-heavy surfacing blend -- a behaviour line she was just
        # reminded of should outrank a stale one; still weighted by context and
        # confidence, and short half-life so old boundaries fade in ranking.
        surface_weights=SurfaceWeights(
            context=0.45, confidence=0.15, recency=0.25, salience=0.15,
            activation=0.2, standing=0.1,
            recency_halflife_days=14.0, salience_halflife_days=14.0,
        ),
        # L16: boundary is the first consumer of relationship modulation -- its
        # effective plasticity rises from the 0.45 base toward 0.75 as trust and
        # duration grow (loosens, never fully fluid). Never touches the stored
        # base; applied live at eval time by the L3 worker.
        plasticity_modulation=PlasticityModulation(
            trust_gain=0.25, duration_gain=0.1, max_plasticity=0.75
        ),
    )
)


# The self-authored *delivery-style* kind (L23 north-star follow-on): how the
# conversation should feel rather than what it is about -- reply detail level,
# lead vs follow, hedging/confidence, warmth vs terseness -- bound to the context
# it applies to ("explain code in depth with examples when we talk programming").
# The delivery vehicle for lightening the hard-coded persona: mined from the
# conversation and surfaced through the same T3 relevant_context region so Aiko
# conforms to the user over time instead of being fixed in the persona file.
# Like boundary it is mined from topic clusters AND explicit remembered anchors
# (``self`` about herself / ``self_tagged`` about the user), a SINGLE deliberate
# anchor can seed one (the gate floors sources at 1; the proposer keeps a
# one-source concept anchor-grounded), and it is subject-parameterized (user +
# aiko). Unlike boundary it does NOT pin to the always-on core lane -- a style
# line is only relevant when its context is live -- so it surfaces purely by
# relevance + spreading activation (it cites the topic cluster it applies to, so
# ``ConceptView.activated`` lights it up when that topic is hot this turn).
register_kind(
    ConceptKind(
        name="communication_style",
        subject="user",
        evidence_model="set",
        # L16: medium plasticity -- a style preference adapts as the bond and the
        # user's habits shift, but shouldn't churn turn to turn. Slightly stickier
        # than affect (0.5), on par with boundary (0.45) as a behaviour guide.
        plasticity_default=0.4,
        # L32: how to talk to someone shapes every turn, but getting it wrong
        # is recoverable in a way a boundary is not -- the neutral middle.
        importance=0.5,
        # A softer rail than boundary -- it governs delivery rather than
        # permission -- but still a rule about how she may speak.
        role=ROLE_GUIDE,
        # L3: boundary-like gate -- a single self-authored anchor promotes
        # ("tell her once and it sticks"); cluster-only inference still needs >= 2
        # (enforced by the proposer composition rule), with medium age/confidence.
        promotion_gate=communication_style_evidence_gate,
        # L24 / L27: a delivery-style line is a behaviour guide, not a user-profile
        # fact, so it is NOT routed to ``profile_block`` -- both subjects surface
        # through the T3 relevant_context path. It is intentionally NOT on the core
        # lane (``core_always_on=False``): style should surface only when its
        # context is active, not pinned every turn.
        surface_weights=SurfaceWeights(
            context=0.5, confidence=0.15, stability=0.25, recency=0.1,
            activation=0.15, standing=0.1,
            recency_halflife_days=21.0,
        ),
        # L17d: the kind also carries the ``evidence_model="meta"`` self-
        # correction rules, whose bases are the beliefs she *stopped* holding.
        # Under the default meta rule those retired bases would make every such
        # rule permanently moot, so this kind's bases are history, not a live
        # dependency. Inert for the ordinary ``set`` comm-style rows.
        meta_min_active_bases=0,
    )
)


# The first *meta* kind (L12): a tension is a concept whose evidence is two OTHER
# active concepts held in friction -- an internal push/pull the person hasn't
# articulated ("values rest but rarely takes it"), or, for ``subject=relationship``,
# a user value clashing with an aiko value (never a grievance, the place a real
# relationship lives). ``evidence_model="meta"`` -- its two ``("concept", id)``
# evidence edges are what light up the (previously dormant) ``dependents_of``
# activation path and the L3 cascade. Delivered "with the most care of any kind":
# it is never pinned to the core lane, and it reaches the static T3 render only
# through the flex lane's single generative slot. The store-dependent meta rules
# (both bases must stay ``active``; confidence bounded by ``min`` of the base
# confidences) are enforced in the L3 worker, not the pure gate.
#
# H10: it used to be filtered out of T3 entirely, on the reasoning that a
# standing friction with a rendering surface would nag. What the exclusion
# actually bought was silence: 99 active tensions against **zero** of 13,800
# concept-lane surfacings, with the T6 cue reaching 25 turns in 231 and 89% of
# turns carrying eight boundaries and no ambivalence at all. Ambivalence -- 
# wanting two incompatible things and knowing it -- is most of what makes a
# character read as having an interior, and it was the one register she never
# got to use. The nag guards are the ones every kind has: the flex lane's
# generative floor admits at most one per turn, L40 habituation rotates which,
# and the T6 cue keeps its own six-day per-tension cooldown and steps aside
# when this lane has already claimed the same row.
register_kind(
    ConceptKind(
        name="tension",
        subject="user",
        evidence_model="meta",
        # L16: medium-fluid. A tension is a live reading of two concepts in
        # friction, not a hard-won trait -- it should ease as the underlying
        # concepts shift, so it drifts a little faster than a boundary (0.45)
        # but is stickier than raw affect (0.5) since holding two patterns at
        # once is a considered observation, not a mood.
        plasticity_default=0.35,
        # L32: an unresolved friction is weighty -- it is the kind delivered
        # "with the most care of any kind". Read by the T6 cue producer's
        # ripeness signal and, since H10, by the flex lane's scoring too.
        importance=0.7,
        # An unresolved friction is the most generative thing in the
        # registry: it is the one kind that exists *because* something has
        # not settled yet. Which is what earns it the flex lane's generative
        # floor -- the turn whose whole selection came out as rails is
        # exactly the turn a tension should be in.
        role=ROLE_GENERATIVE,
        # It renders, but it is never pinned: a friction earns its place by
        # being live in the turn, never by standing there every turn.
        pinnable=False,
        # L3: the meta gate -- floors the source count at 2 (both sides of the
        # friction), with a higher age + confidence bar than the fluid kinds
        # because a tension asserts with care.
        promotion_gate=tension_evidence_gate,
        # L12: never *pinned* every turn (``core_always_on=False``) -- it has
        # to earn its slot against the live turn, which is the difference
        # between raising something and harping on it. Activation-heavy: a
        # tension is worth revisiting exactly when its base concepts are live
        # this turn. The same weights drive the T6 cue producer's ripeness
        # signal, which reads them through spreading activation.
        surface_weights=SurfaceWeights(
            context=0.5, confidence=0.1, stability=0.2, recency=0.1,
            activation=0.3,
            recency_halflife_days=14.0,
        ),
    )
)


# The abstraction meta kind (L20): a generalization is a concept whose evidence
# is 2+ OTHER active concepts (of any kind, same subject) that it names a latent
# super-concept over -- "he builds things that last" abstracting React / AI /
# home-server tinkering, or "she reaches for warmth over being right" over
# several of her own values. Distinct from a tension (which holds two concepts in
# *friction*): a generalization holds several in *is-a / part-of* and names the
# whole. ``evidence_model="meta"`` -- its ``("concept", id)`` evidence edges ride
# the same lifecycle rails as tension (cascade, confidence bounding, depth cap),
# with an arity-aware moot rule in the L3 worker (still live while >= 2 children
# stay active, so it survives losing one). UNLIKE tension it DOES render in the
# static T3 block: the whole point is that Aiko speaks the abstraction and its
# children step aside (the suppression in ``build_relevant_context``), so it
# joins the always-on core lane at a HIGH confidence bar -- a settled abstraction
# is exactly the "who they are" the core lane is for.
register_kind(
    ConceptKind(
        name="generalization",
        subject="user",
        evidence_model="meta",
        # L16: low plasticity -- an abstraction is the most hard-won, settled
        # thing in the layer (it sits above a whole cluster of beliefs), so it
        # should drift the slowest of the metas, near identity/value.
        plasticity_default=0.25,
        # L32: an abstraction inherits weight from the beliefs beneath it, so
        # it sits a little above the identity traits it usually generalizes.
        importance=0.65,
        # It names a pattern over concepts that already exist, so it
        # inherits their ground-truth character rather than opening
        # anything new -- an anchor even when its children are rails.
        role=ROLE_ANCHOR,
        # L3: the abstraction gate -- floors sources at 2 with age + confidence
        # bars a notch above tension, because a generalization should be slow
        # and well-supported before it speaks for the concepts beneath it.
        promotion_gate=generalization_evidence_gate,
        # L20 / L27: joins the always-on core lane at a high bar so a settled
        # abstraction pins every turn and its children are suppressed beneath
        # it. Confidence/stability-leaning (an abstraction is a settled belief,
        # not a live reading), with a light activation term so a parent still
        # lifts when its children are hot this turn.
        core_always_on=True,
        core_min_confidence=0.8,
        surface_weights=SurfaceWeights(
            context=0.5, confidence=0.3, stability=0.2,
            activation=0.1, standing=0.1,
            recency_halflife_days=30.0,
        ),
        # L20: an abstraction survives losing one child -- it speaks for the
        # group, so it only goes moot when fewer than two remain.
        meta_min_active_bases=2,
    )
)


__all__ = [
    "CONCEPT_KINDS",
    "DEFAULT_PLASTICITY_MODULATION",
    "DEFAULT_SURFACE_WEIGHTS",
    "EVIDENCE_MODELS",
    "ROLES",
    "ROLE_ANCHOR",
    "ROLE_GENERATIVE",
    "ROLE_GUIDE",
    "SUBJECTS",
    "ConceptKind",
    "PlasticityModulation",
    "SurfaceWeights",
    "core_lane_kinds",
    "get_kind",
    "kinds_by_role",
    "kinds_for_target",
    "renders_in_static_block",
    "register_kind",
    "target_for",
    "targets_of",
]
