"""L2 concept proposers package.

Each proposer lives in its own module and exposes a ``SPEC``
(:class:`ProposerSpec`). This package assembles them into the ordered
``CONCEPT_PROPOSERS`` registry the :class:`ConceptSynthesisWorker`
iterates. Adding a kind/subject is a new sibling module plus one line in
``_SPECS`` below -- no change to the worker body.

v1 ships two ``set``-structured identity proposers (per the L1 structural
evidence-model vocabulary -- node type lives on the edges, not the
model):

- ``identity_user`` -- cross-cluster identity over the user's topic
  clusters (``cluster`` evidence edges).
- ``identity_aiko`` -- Aiko's own identity over her ``self`` /
  ``reflection`` / ``diary`` memories (``memory`` evidence edges).
"""
from __future__ import annotations

from app.core.concepts.proposers import identity_aiko, identity_user
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

CONCEPT_PROPOSERS: tuple[ProposerSpec, ...] = (
    identity_user.SPEC,
    identity_aiko.SPEC,
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
    "resolve_reinforces",
    "snippet",
]
