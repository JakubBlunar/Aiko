"""L2 concept proposers package.

Each proposer lives in its own module and exposes a ``SPEC``
(:class:`ProposerSpec`). This package assembles them into the ordered
``CONCEPT_PROPOSERS`` registry the :class:`ConceptSynthesisWorker`
iterates. Adding a kind/subject is a new sibling module plus one line in
``_SPECS`` below -- no change to the worker body.

Ships ``set``-structured proposers (per the L1 structural evidence-model
vocabulary -- node type lives on the edges, not the model). Each kind is
mined for both subjects:

- ``identity_user`` / ``identity_aiko`` -- cross-cluster / self-memory
  *identity* (what he is into / what she is like).
- ``value_user`` / ``value_aiko`` -- cross-cluster / self-memory *value*
  (the principle beneath the choices; L10). Value proposers share the
  cluster / aiko-memory populations with identity but carry their own
  ``sig_key`` so their dirty-tracking never clobbers identity's.
"""
from __future__ import annotations

from app.core.concepts.proposers import (
    identity_aiko,
    identity_user,
    value_aiko,
    value_user,
)
from app.core.concepts.proposers.base import (
    AIKO_SELF_KINDS,
    MIN_SOURCES,
    CandidateProposal,
    ExistingConcept,
    FocusCluster,
    ProposerContext,
    ProposerSpec,
    clamp01,
    coerce_id_list,
    format_existing,
    resolve_reinforces,
    snippet,
)
from app.core.concepts.proposers.identity_aiko import propose_identity_aiko
from app.core.concepts.proposers.identity_user import propose_identity_user
from app.core.concepts.proposers.value_aiko import propose_value_aiko
from app.core.concepts.proposers.value_user import propose_value_user

CONCEPT_PROPOSERS: tuple[ProposerSpec, ...] = (
    identity_user.SPEC,
    identity_aiko.SPEC,
    value_user.SPEC,
    value_aiko.SPEC,
)


__all__ = [
    "AIKO_SELF_KINDS",
    "CONCEPT_PROPOSERS",
    "MIN_SOURCES",
    "CandidateProposal",
    "ExistingConcept",
    "FocusCluster",
    "ProposerContext",
    "ProposerSpec",
    "clamp01",
    "coerce_id_list",
    "format_existing",
    "propose_identity_aiko",
    "propose_identity_user",
    "propose_value_aiko",
    "propose_value_user",
    "resolve_reinforces",
    "snippet",
]
