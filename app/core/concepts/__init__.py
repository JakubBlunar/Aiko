"""L1 higher-order concept layer.

A concept is a cross-cluster abstraction that sits above topic clusters
(see ``docs/personality-backlog/concepts.md``). This package owns the
data substrate only: the persistence store (:mod:`concept_store`) and
the kind registry (:mod:`concept_kinds`). The proposer (L2), lifecycle
engine (L3), and prompt surfacing (L5) build on top of these.
"""
from __future__ import annotations

from app.core.concepts.concept_kinds import (
    CONCEPT_KINDS,
    ConceptKind,
    get_kind,
    register_kind,
)
from app.core.concepts.proposers import (
    CONCEPT_PROPOSERS,
    CandidateProposal,
    ProposerContext,
    ProposerSpec,
)
from app.core.concepts.concept_store import Concept, ConceptEdge, ConceptStore
from app.core.concepts.concept_synthesis_worker import ConceptSynthesisWorker

__all__ = [
    "CONCEPT_KINDS",
    "CONCEPT_PROPOSERS",
    "CandidateProposal",
    "Concept",
    "ConceptEdge",
    "ConceptKind",
    "ConceptStore",
    "ConceptSynthesisWorker",
    "ProposerContext",
    "ProposerSpec",
    "get_kind",
    "register_kind",
]
