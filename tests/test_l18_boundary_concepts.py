"""Tests for L18 boundary (behaviour-gating) concepts.

Covers the full vertical slice:

* the ``boundary_evidence_gate`` -- crucially the single-anchor OVERRIDE (a
  1-source boundary promotes even when the caller floors ``min_sources`` higher)
  plus the age/confidence floors that still apply,
* the ``boundary`` kind registration + always-on core-lane membership + the
  recency-weighted ``surface_weights`` + relevance routing (not ``profile_block``),
* the ``_run_boundary_pass`` worker pass for user AND aiko (single deliberate
  anchor seeds a boundary, dirty-tracking no-op, the ``boundary_synthesis_enabled``
  switch),
* the shared ``propose_boundary`` composition rule (>=1 anchor OR >=2 clusters),
  mixed evidence, reinforce-by-id, and the user/aiko prompt voice,
* the soft/guiding rendering header (both subjects + relationship).
"""
from __future__ import annotations

import types
import unittest

from app.core.concepts.concept_kinds import (
    DEFAULT_SURFACE_WEIGHTS,
    core_lane_kinds,
    get_kind,
    kinds_for_target,
)
from app.core.concepts.concept_lifecycle import boundary_evidence_gate
from app.core.concepts.proposers import ExistingConcept, ProposerContext
from app.core.concepts.proposers.boundary_aiko import propose_boundary_aiko
from app.core.concepts.proposers.boundary_user import propose_boundary_user

from tests.test_concept_synthesis_worker import (
    ClusterStub,
    MemStub,
    WorkerHarness,
    _mem_settings,
)


# ── gate + registry ─────────────────────────────────────────────────────


class BoundaryGateAndRegistryTests(unittest.TestCase):
    def test_single_anchor_promotes(self) -> None:
        # A single source promotes (the whole point of the anchor path).
        self.assertTrue(
            boundary_evidence_gate(
                distinct_source_count=1, age_days=1.0, confidence=0.7,
                min_sources=2, min_age_days=0.0, min_confidence=0.0,
            )
        )

    def test_source_floor_is_overridden_not_maxed(self) -> None:
        # Unlike the other gates, a higher caller ``min_sources`` does NOT block
        # a 1-source boundary -- boundary overrides the floor to 1.
        self.assertTrue(
            boundary_evidence_gate(
                distinct_source_count=1, age_days=1.0, confidence=0.7,
                min_sources=5, min_age_days=0.0, min_confidence=0.0,
            )
        )

    def test_age_and_confidence_floors_still_bite(self) -> None:
        self.assertFalse(  # age below the 0.5 floor
            boundary_evidence_gate(
                distinct_source_count=1, age_days=0.2, confidence=0.9,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )
        self.assertFalse(  # confidence below the 0.65 floor
            boundary_evidence_gate(
                distinct_source_count=1, age_days=1.0, confidence=0.5,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )

    def test_caller_confidence_floor_still_applies_when_higher(self) -> None:
        self.assertFalse(
            boundary_evidence_gate(
                distinct_source_count=1, age_days=1.0, confidence=0.7,
                min_sources=1, min_age_days=0.0, min_confidence=0.8,
            )
        )

    def test_registry(self) -> None:
        kind = get_kind("boundary")
        self.assertIsNotNone(kind)
        self.assertEqual(kind.subject, "user")
        self.assertEqual(kind.evidence_model, "set")
        self.assertAlmostEqual(kind.plasticity_default, 0.45)
        self.assertTrue(kind.core_always_on)
        self.assertAlmostEqual(kind.core_min_confidence, 0.8)
        self.assertIs(kind.promotion_gate, boundary_evidence_gate)

    def test_recency_weighted_surfacing(self) -> None:
        kind = get_kind("boundary")
        self.assertGreater(kind.surface_weights.recency, 0.0)
        self.assertNotEqual(kind.surface_weights, DEFAULT_SURFACE_WEIGHTS)

    def test_joins_core_lane_but_not_profile_block(self) -> None:
        self.assertIn("boundary", {k.name for k in core_lane_kinds()})
        self.assertNotIn("boundary", kinds_for_target("profile_block"))


# ── worker boundary pass ─────────────────────────────────────────────────


def _user_cluster(rep: int = 100) -> ClusterStub:
    return ClusterStub(
        rep=rep, summary="work stress", size=12,
        kinds=("fact", "preference", "event"),
    )


def _user_boundary_responder(system, user):
    if "BOUNDARIES" in system and "for HERSELF" not in system:
        return {"concepts": [{
            "label": "Be gentle with Jacob about his work when he's stressed",
            "evidence_memory_ids": [500],
            "rationale": "he asked me to ease off",
            "confidence": 0.7,
        }]}
    return {"concepts": []}


def _aiko_boundary_responder(system, user):
    if "BOUNDARIES" in system and "for HERSELF" in system:
        return {"concepts": [{
            "label": "I won't fake agreement just to please him",
            "evidence_memory_ids": [600],
            "rationale": "a line she holds for herself",
            "confidence": 0.7,
        }]}
    return {"concepts": []}


class BoundaryPassTests(unittest.TestCase):
    def test_user_pass_creates_single_anchor_boundary(self) -> None:
        anchor = MemStub(
            500, "He asked me to go gentler about work when stressed.",
            "self_tagged", 0.9,
        )
        h = WorkerHarness(
            _user_boundary_responder, clusters=[_user_cluster()],
            self_memories=[anchor],
        )
        stats = h.worker.run()
        self.assertTrue(stats["boundary_dirty"])
        out = h.store.list_by(subject="user", kind="boundary")
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c.evidence_model, "set")
        self.assertEqual(c.distinct_source_count, 1)  # a single anchor
        ev = h.store.evidence_of(c.concept_id)
        self.assertEqual([(e.src_type, e.src_id) for e in ev], [("memory", "500")])

    def test_aiko_pass_creates_first_person_boundary(self) -> None:
        anchor = MemStub(
            600, "I won't fake agreement just to please him.", "self", 0.9,
        )
        h = WorkerHarness(
            _aiko_boundary_responder, clusters=[], self_memories=[anchor],
        )
        stats = h.worker.run()
        self.assertTrue(stats["boundary_dirty"])
        out = h.store.list_by(subject="aiko", kind="boundary")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].subject, "aiko")

    def test_clean_rerun_is_noop(self) -> None:
        anchor = MemStub(500, "gentle about work", "self_tagged", 0.9)
        h = WorkerHarness(
            _user_boundary_responder, clusters=[_user_cluster()],
            self_memories=[anchor],
        )
        h.worker.run()
        before = h.store.count()
        stats = h.worker.run()
        self.assertFalse(stats["boundary_dirty"])
        self.assertEqual(h.store.count(), before)

    def test_disabled_switch_skips_pass(self) -> None:
        anchor = MemStub(500, "gentle about work", "self_tagged", 0.9)
        agent = types.SimpleNamespace(
            concepts_enabled=True,
            concept_synthesis_enabled=True,
            boundary_synthesis_enabled=False,
        )
        h = WorkerHarness(
            _user_boundary_responder, clusters=[_user_cluster()],
            self_memories=[anchor], agent=agent,
        )
        stats = h.worker.run()
        self.assertFalse(stats["boundary_dirty"])
        self.assertEqual(h.store.list_by(subject="user", kind="boundary"), [])


def _pref_boundary_responder(system, user):
    # Cite only the preference memory (no cluster reps), so the boundary
    # stands or falls on whether that memory reached the proposer.
    if "BOUNDARIES" in system and "for HERSELF" not in system:
        return {"concepts": [{
            "label": "Be gentle with Jacob about deadlines",
            "evidence_memory_ids": [550],
            "rationale": "he'd rather not be rushed",
            "confidence": 0.7,
        }]}
    return {"concepts": []}


class L18eBroadeningTests(unittest.TestCase):
    """L18e: a stated ``preference`` memory (never a deliberate anchor) can
    seed a user boundary when broadening is on, and is ignored when off."""

    def _pref(self) -> MemStub:
        return MemStub(
            550, "Jacob would rather not be rushed on deadlines.",
            "preference", 0.9,
        )

    def test_preference_seeds_boundary_when_broadening_on(self) -> None:
        # Default agent leaves the flag unset -> broadening defaults on.
        h = WorkerHarness(
            _pref_boundary_responder, clusters=[_user_cluster()],
            self_memories=[self._pref()],
        )
        h.worker.run()
        out = h.store.list_by(subject="user", kind="boundary")
        self.assertEqual(len(out), 1)
        ev = h.store.evidence_of(out[0].concept_id)
        self.assertEqual(
            [(e.src_type, e.src_id) for e in ev], [("memory", "550")]
        )

    def test_preference_ignored_when_broadening_off(self) -> None:
        agent = types.SimpleNamespace(
            concepts_enabled=True,
            concept_synthesis_enabled=True,
            boundary_evidence_broadening_enabled=False,
        )
        h = WorkerHarness(
            _pref_boundary_responder, clusters=[_user_cluster()],
            self_memories=[self._pref()], agent=agent,
        )
        h.worker.run()
        # Preference never entered the anchor pool, so the cited id is filtered
        # out and the lone-cluster boundary is dropped.
        self.assertEqual(h.store.list_by(subject="user", kind="boundary"), [])


# ── proposer (direct): composition rule + voice ──────────────────────────


def _ctx(responder):
    calls: dict[str, str] = {}

    def call_llm(system, user):
        calls["system"] = system
        calls["user"] = user
        return responder(system, user)["concepts"]

    return ProposerContext(
        call_llm=call_llm, user_name="Jacob", assistant_name="Aiko"
    ), calls


def _clusters(*reps):
    return [(r, f"topic {r}", 10) for r in reps]


class ProposerCompositionTests(unittest.TestCase):
    def test_single_anchor_accepted(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "Be gentle about work",
                "evidence_memory_ids": [500],
                "confidence": 0.7,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_boundary_user(
            ctx, cluster_index=_clusters(100),
            memories=[MemStub(500, "go gentler about work", "self_tagged", 0.9)],
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "boundary")
        self.assertEqual(out[0].subject, "user")
        self.assertEqual(out[0].evidence, [("memory", "500")])

    def test_single_cluster_dropped(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "a lone-cluster boundary",
                "evidence_cluster_reps": [100],
                "confidence": 0.9,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_boundary_user(
            ctx, cluster_index=_clusters(100, 101), memories=[],
        )
        self.assertEqual(out, [])  # one cluster, no anchor -> dropped

    def test_two_clusters_accepted(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "Be mindful across these two areas",
                "evidence_cluster_reps": [100, 101],
                "confidence": 0.7,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_boundary_user(
            ctx, cluster_index=_clusters(100, 101), memories=[],
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(
            out[0].evidence, [("cluster", "100"), ("cluster", "101")]
        )

    def test_mixed_evidence_edges(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "Be gentle about work",
                "evidence_cluster_reps": [100],
                "evidence_memory_ids": [500],
                "confidence": 0.7,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_boundary_user(
            ctx, cluster_index=_clusters(100),
            memories=[MemStub(500, "note", "self_tagged", 0.9)],
        )
        self.assertEqual(
            out[0].evidence, [("cluster", "100"), ("memory", "500")]
        )

    def test_reinforce_by_id(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "reinforces_id": 42,
                "evidence_memory_ids": [500],
                "rationale": "fresh support",
            }]}

        ctx, _ = _ctx(responder)
        out = propose_boundary_user(
            ctx, cluster_index=_clusters(100),
            memories=[MemStub(500, "note", "self_tagged", 0.9)],
            existing=[ExistingConcept(id=42, label="Be gentle about work")],
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].reinforces_id, 42)
        self.assertEqual(out[0].label, "")
        self.assertEqual(out[0].kind, "boundary")

    def test_user_voice_third_person(self) -> None:
        ctx, calls = _ctx(lambda s, u: {"concepts": []})
        propose_boundary_user(ctx, cluster_index=_clusters(100), memories=[])
        self.assertIn("Jacob", calls["system"])
        self.assertNotIn("FIRST PERSON", calls["system"])

    def test_aiko_voice_first_person(self) -> None:
        ctx, calls = _ctx(lambda s, u: {"concepts": []})
        propose_boundary_aiko(
            ctx, memories=[MemStub(600, "note", "self", 0.9)],
        )
        self.assertIn("FIRST PERSON", calls["system"])
        self.assertIn("Aiko", calls["system"])


# ── rendering header ─────────────────────────────────────────────────────


class RenderingTests(unittest.TestCase):
    def test_boundary_header_is_soft(self) -> None:
        from app.core.session.inner_life_part1 import InnerLifePart1Mixin

        user = InnerLifePart1Mixin._concept_group_header(
            "user", "boundary", "Jacob"
        )
        self.assertIn("Jacob", user)
        self.assertIn("gentler", user)
        self.assertIn("mindful", user)

        rel = InnerLifePart1Mixin._concept_group_header(
            "relationship", "boundary", "Jacob"
        )
        self.assertIn("Jacob", rel)

        aiko = InnerLifePart1Mixin._concept_group_header(
            "aiko", "boundary", "Jacob"
        )
        self.assertIn("softly", aiko)
        self.assertIn("renegotiated", aiko)


if __name__ == "__main__":
    unittest.main()
