"""L2 concept proposers package.

Each proposer lives in its own module and exposes a ``SPEC``
(:class:`ProposerSpec`). This package assembles them into the ordered
``CONCEPT_PROPOSERS`` registry the :class:`ConceptSynthesisWorker`
iterates. Adding a kind/subject is a new sibling module plus one line in
``_SPECS`` below -- no change to the worker body.

Ships ``set``-structured proposers (per the L1 structural evidence-model
vocabulary -- node type lives on the edges, not the model). Each kind is
mined for both subjects:

- ``identity_user`` / ``identity_aiko`` -- *identity* (what he is into /
  what she is like).
- ``value_user`` / ``value_aiko`` -- *value* (the principle beneath the
  choices; L10). Value proposers share their subject's population with
  identity but carry their own ``sig_key`` so their dirty-tracking never
  clobbers identity's.

The two ``subject=user`` proposers mine his topic clusters; the two
``subject=aiko`` proposers mine her self-model in one *combined* pass
(L11) -- her aiko-dominant self-themes (clusters) AND her salient
self-memories -- via the shared :func:`propose_aiko_hybrid` body, so a
self-concept can be grounded by a theme, a memory, or a mix.

- ``affective_user`` / ``affective_aiko`` -- *affective* (the durable
  topic->emotion signature; L13). They mine the ``"affect"`` population:
  topic clusters annotated with each subject's typical affect (from the
  per-cluster affect map), plus -- for aiko -- her affect-stamped
  self-memories. The affect *direction* is carried in the concept text, not
  on the edges.
- ``relationship_ritual`` -- *ritual* (the recurring shared pattern; L7,
  ``subject=relationship``). It mines the ``"shared_moments"`` population:
  ``shared_moment`` memories grouped by single-link cosine into recurring
  clusters, each named as a warm relationship ritual. Evidence is the
  constituent moments (``memory`` edges).
- ``narrative_user`` / ``narrative_aiko`` -- *narrative* (a closed causal arc;
  L8). The first ``sequence``-evidence proposers: they mine the
  ``"narrative"`` population -- each subject-dominant cluster's member memories
  loaded in temporal order -- and name any that form a beginning->development
  ->resolution story. Evidence is the ordered chain (``memory`` edges carrying
  ordinals). Only the voice differs (user third-person / aiko first-person).
- ``aspiration_user`` / ``aspiration_aiko`` -- *aspiration* (an open-ended
  direction; L14). The open-ended sibling of narrative and the second
  ``sequence``-evidence kind: they mine the ``"aspiration"`` population (the
  same temporally-ordered candidates, filtered to span real time) and name any
  that show a *sustained direction* rather than a closed arc. Shares the
  :func:`propose_ordered_concept` body with narrative (gate flag
  ``"directional"`` vs ``"closed"``).
- ``boundary_user`` / ``boundary_aiko`` -- *boundary* (a behaviour-gating line;
  L18). The first *hybrid* proposers for both subjects: they mine the
  ``"boundary"`` population -- topic clusters AND Aiko's explicit remembered
  anchors (``self_tagged`` about the user / ``self`` about herself) -- and name
  soft lines that should guide her behaviour. Share the :func:`propose_boundary`
  body, whose composition rule lets a single deliberate anchor seed a boundary
  (a lone cluster needs a sibling). Only the voice differs (user third-person /
  aiko first-person).
- ``communication_style_user`` / ``communication_style_aiko`` -- *communication
  style* (a self-authored delivery-style line bound to context; L23 follow-on).
  Hybrid proposers over the ``"comm_style"`` population -- topic clusters AND the
  remembered anchors (``self_tagged`` about the user / ``self`` about herself) --
  additionally *guided* (not grounded) by a persisted style-signal digest (K13
  labels + the profile ``communication_style`` field). Share the
  :func:`propose_communication_style` body, whose composition rule lets a single
  anchor seed a line. The delivery vehicle for lightening the hard-coded persona.
- ``tension_user`` / ``tension_relationship`` / ``tension_aiko`` -- *tension*
  (L12), the first *meta* proposers. Unlike every other proposer their raw
  material is not clusters/memories but the small set of active BASE (non-meta)
  concepts: they mine the ``"tension"`` population and name two of those
  concepts held in genuine friction (an internal push/pull; a cross-subject
  user-vs-aiko value clash for the relationship lens). Share the
  :func:`propose_tension` body, whose composition rule accepts exactly a pair of
  distinct concept ids, emitting ``("concept", id)`` evidence with
  ``evidence_model="meta"``. They run LAST so their base concepts are already
  ``active`` (the meta dependency-ordering rule).
- ``generalization_user`` / ``generalization_aiko`` -- *generalization* (L20),
  the abstraction meta proposers. Like tension their raw material is the active
  BASE (non-meta) concepts, but they name a higher-order super-concept that 2+
  of those are all facets of ("builds things that last" over several hobbies) --
  is-a / part-of, not friction. Share the :func:`propose_generalization` body,
  whose composition rule accepts 2..N distinct concept ids (capped at
  ``GENERALIZATION_MAX_CHILDREN``), emitting ``("concept", id)`` evidence with
  ``evidence_model="meta"``. They run with the other metas, LAST.
"""
from __future__ import annotations

from app.core.concepts.proposers import (
    affective_aiko,
    affective_user,
    aspiration_aiko,
    aspiration_user,
    boundary_aiko,
    boundary_user,
    communication_style_aiko,
    communication_style_user,
    generalization_aiko,
    generalization_user,
    identity_aiko,
    identity_user,
    narrative_aiko,
    narrative_user,
    relationship_ritual,
    tension_aiko,
    tension_relationship,
    tension_user,
    value_aiko,
    value_user,
)
from app.core.concepts.proposers.base import (
    AIKO_SELF_KINDS,
    GENERALIZATION_MAX_CHILDREN,
    MIN_SOURCES,
    CandidateProposal,
    ExistingConcept,
    FocusCluster,
    NarrativeCandidate,
    ProposerContext,
    ProposerSpec,
    TensionBase,
    clamp01,
    coerce_id_list,
    format_existing,
    propose_aiko_hybrid,
    propose_boundary,
    propose_communication_style,
    propose_generalization,
    propose_narrative,
    propose_ordered_concept,
    propose_tension,
    resolve_reinforces,
    snippet,
)
from app.core.concepts.proposers.affective_aiko import propose_affective_aiko
from app.core.concepts.proposers.affective_user import propose_affective_user
from app.core.concepts.proposers.aspiration_aiko import propose_aspiration_aiko
from app.core.concepts.proposers.aspiration_user import propose_aspiration_user
from app.core.concepts.proposers.boundary_aiko import propose_boundary_aiko
from app.core.concepts.proposers.boundary_user import propose_boundary_user
from app.core.concepts.proposers.communication_style_aiko import (
    propose_communication_style_aiko,
)
from app.core.concepts.proposers.communication_style_user import (
    propose_communication_style_user,
)
from app.core.concepts.proposers.generalization_aiko import (
    propose_generalization_aiko,
)
from app.core.concepts.proposers.generalization_user import (
    propose_generalization_user,
)
from app.core.concepts.proposers.identity_aiko import propose_identity_aiko
from app.core.concepts.proposers.identity_user import propose_identity_user
from app.core.concepts.proposers.narrative_aiko import propose_narrative_aiko
from app.core.concepts.proposers.narrative_user import propose_narrative_user
from app.core.concepts.proposers.relationship_ritual import (
    propose_relationship_ritual,
)
from app.core.concepts.proposers.tension_aiko import propose_tension_aiko
from app.core.concepts.proposers.tension_relationship import (
    propose_tension_relationship,
)
from app.core.concepts.proposers.tension_user import propose_tension_user
from app.core.concepts.proposers.value_aiko import propose_value_aiko
from app.core.concepts.proposers.value_user import propose_value_user

CONCEPT_PROPOSERS: tuple[ProposerSpec, ...] = (
    identity_user.SPEC,
    identity_aiko.SPEC,
    value_user.SPEC,
    value_aiko.SPEC,
    affective_user.SPEC,
    affective_aiko.SPEC,
    relationship_ritual.SPEC,
    narrative_user.SPEC,
    narrative_aiko.SPEC,
    aspiration_user.SPEC,
    aspiration_aiko.SPEC,
    boundary_user.SPEC,
    boundary_aiko.SPEC,
    communication_style_user.SPEC,
    communication_style_aiko.SPEC,
    # Meta proposers run LAST: their base concepts must already be ``active``
    # (the L1 meta dependency-ordering rule).
    tension_user.SPEC,
    tension_relationship.SPEC,
    tension_aiko.SPEC,
    # L20 abstraction metas -- also over active base concepts, so they sit
    # with the other metas at the end of the pass order.
    generalization_user.SPEC,
    generalization_aiko.SPEC,
)


__all__ = [
    "AIKO_SELF_KINDS",
    "CONCEPT_PROPOSERS",
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
    "propose_affective_aiko",
    "propose_affective_user",
    "propose_aiko_hybrid",
    "propose_aspiration_aiko",
    "propose_aspiration_user",
    "propose_boundary",
    "propose_boundary_aiko",
    "propose_boundary_user",
    "propose_communication_style",
    "propose_communication_style_aiko",
    "propose_communication_style_user",
    "propose_generalization",
    "propose_generalization_aiko",
    "propose_generalization_user",
    "propose_identity_aiko",
    "propose_identity_user",
    "propose_narrative",
    "propose_narrative_aiko",
    "propose_narrative_user",
    "propose_ordered_concept",
    "propose_relationship_ritual",
    "propose_tension",
    "propose_tension_aiko",
    "propose_tension_relationship",
    "propose_tension_user",
    "propose_value_aiko",
    "propose_value_user",
    "resolve_reinforces",
    "snippet",
]
