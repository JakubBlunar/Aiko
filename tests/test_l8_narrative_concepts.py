"""Tests for L8 narrative (arc) concepts.

Covers the full vertical slice:

* the ``narrative_evidence_gate`` floors,
* the ``narrative`` kind registration + relevance-only routing (both subjects),
* ordinal (``sequence``) evidence persistence (the chain is stored in order),
* the ``_run_narrative_pass`` worker pass for user AND aiko (creation, open-arc
  rejection, short-chain rejection, dirty-tracking no-op, the
  ``narrative_synthesis_enabled`` switch),
* the shared ``propose_narrative`` body via both proposers (temporal-ordered
  prompt + evidence, order derived from the candidate not the LLM, first-person
  aiko voice, open/short rejection, cold-start, reinforce-by-id),
* the rendering header (both subjects).
"""
from __future__ import annotations

import types
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from app.core.concepts.concept_kinds import (
    core_lane_kinds,
    get_kind,
    kinds_for_target,
)
from app.core.concepts.concept_lifecycle import narrative_evidence_gate
from app.core.concepts.proposers import (
    ExistingConcept,
    NarrativeCandidate,
    ProposerContext,
)
from app.core.concepts.proposers.narrative_aiko import propose_narrative_aiko
from app.core.concepts.proposers.narrative_user import propose_narrative_user

from tests.test_concept_synthesis_worker import (
    ClusterStub,
    MemStub,
    WorkerHarness,
)

_UTC = timezone.utc


def _vec(*xs) -> np.ndarray:
    return np.asarray(xs, dtype=np.float32)


# ── gate + registry ─────────────────────────────────────────────────────


class NarrativeGateAndRegistryTests(unittest.TestCase):
    def test_gate_floors(self) -> None:
        # only 2 steps < chain floor 3
        self.assertFalse(
            narrative_evidence_gate(
                distinct_source_count=2, age_days=10.0, confidence=0.99,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )
        # age below the 1.0 floor
        self.assertFalse(
            narrative_evidence_gate(
                distinct_source_count=3, age_days=0.5, confidence=0.99,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )
        # confidence below the 0.6 floor
        self.assertFalse(
            narrative_evidence_gate(
                distinct_source_count=3, age_days=2.0, confidence=0.5,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )
        self.assertTrue(
            narrative_evidence_gate(
                distinct_source_count=3, age_days=1.0, confidence=0.6,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )

    def test_caller_thresholds_still_apply_when_higher(self) -> None:
        # A higher caller floor (e.g. L21 young-graph bar) wins over the built-in.
        self.assertFalse(
            narrative_evidence_gate(
                distinct_source_count=4, age_days=2.0, confidence=0.6,
                min_sources=5, min_age_days=0.0, min_confidence=0.0,
            )
        )

    def test_registry(self) -> None:
        kind = get_kind("narrative")
        self.assertIsNotNone(kind)
        self.assertEqual(kind.subject, "user")
        self.assertEqual(kind.evidence_model, "sequence")
        self.assertAlmostEqual(kind.plasticity_default, 0.3)
        self.assertFalse(kind.core_always_on)
        self.assertEqual(kind.surfacing_targets, {})
        self.assertIs(kind.promotion_gate, narrative_evidence_gate)

    def test_relevance_only_routing(self) -> None:
        self.assertNotIn("narrative", kinds_for_target("profile_block"))
        self.assertNotIn("narrative", {k.name for k in core_lane_kinds()})


# ── worker narrative pass ────────────────────────────────────────────────


def _arc_memories(start_id: int, n: int, kind: str, base: datetime):
    """N memories whose ``event_time`` increases with id."""
    return [
        MemStub(
            start_id + i,
            f"beat {i}: something happened in the arc",
            kind,
            0.5 + i * 0.05,
            event_time=(base + timedelta(days=i)).isoformat(),
        )
        for i in range(n)
    ]


def _user_arc(n: int = 4):
    base = datetime(2026, 1, 1, 12, 0, tzinfo=_UTC)
    mems = _arc_memories(201, n, "event", base)
    ids = [m.id for m in mems]
    # member_ids deliberately scrambled -> the pass must reorder by event_time.
    scrambled = ids[::-1]
    cluster = ClusterStub(
        rep=ids[0], summary="the CPU saga", size=n,
        kinds=("event",) * n, member_ids=scrambled,
    )
    return cluster, mems, ids


def _aiko_arc(n: int = 3):
    base = datetime(2026, 2, 1, 9, 0, tzinfo=_UTC)
    kinds = ("self", "reflection", "diary")
    mems = [
        MemStub(
            301 + i, f"self beat {i}: I noticed a change", kinds[i % 3], 0.6,
            event_time=(base + timedelta(days=i)).isoformat(),
        )
        for i in range(n)
    ]
    ids = [m.id for m in mems]
    cluster = ClusterStub(
        rep=ids[0], summary="learning to be gentle", size=n,
        kinds=tuple(m.kind for m in mems), member_ids=ids[::-1],
    )
    return cluster, mems, ids


def _user_responder(system, user):
    if "NARRATIVE ARCS in" in system and "OWN inner life" not in system:
        return {"concepts": [{
            "label": "The CPU saga",
            "arc_index": 0,
            # scrambled ids -> ordinals must follow the candidate order
            "evidence_memory_ids": [203, 201, 204, 202],
            "closed": True,
            "rationale": "beginning to resolution",
            "confidence": 0.7,
        }]}
    return {"concepts": []}


def _aiko_responder(system, user):
    if "OWN inner life" in system:
        return {"concepts": [{
            "label": "The stretch where I learned to be gentle",
            "arc_index": 0,
            "evidence_memory_ids": [303, 301, 302],
            "closed": True,
            "rationale": "a settled change in her",
            "confidence": 0.7,
        }]}
    return {"concepts": []}


class NarrativePassTests(unittest.TestCase):
    def test_user_pass_creates_ordered_sequence_concept(self) -> None:
        cluster, mems, ids = _user_arc()
        h = WorkerHarness(
            _user_responder, clusters=[cluster], self_memories=[],
            shared_moments=mems,  # non-self rows so get_many can resolve them
        )
        stats = h.worker.run()
        self.assertTrue(stats["narrative_dirty"])
        out = h.store.list_by(subject="user", kind="narrative")
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c.label, "The CPU saga")
        self.assertEqual(c.evidence_model, "sequence")
        # Evidence is returned in ordinal order == temporal (event_time) order,
        # NOT the scrambled order the LLM listed.
        ev = h.store.evidence_of(c.concept_id)
        self.assertTrue(all(e.src_type == "memory" for e in ev))
        self.assertEqual([e.src_id for e in ev], [str(i) for i in ids])
        self.assertEqual([e.ordinal for e in ev], list(range(len(ids))))

    def test_aiko_pass_creates_first_person_arc(self) -> None:
        cluster, mems, ids = _aiko_arc()
        h = WorkerHarness(
            _aiko_responder, clusters=[cluster], self_memories=mems,
        )
        stats = h.worker.run()
        self.assertTrue(stats["narrative_dirty"])
        out = h.store.list_by(subject="aiko", kind="narrative")
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c.subject, "aiko")
        ev = h.store.evidence_of(c.concept_id)
        self.assertEqual([e.src_id for e in ev], [str(i) for i in ids])
        self.assertEqual([e.ordinal for e in ev], [0, 1, 2])

    def test_open_arc_rejected(self) -> None:
        cluster, mems, _ids = _user_arc()

        def responder(system, user):
            if "NARRATIVE ARCS in" in system and "OWN inner life" not in system:
                return {"concepts": [{
                    "label": "An ongoing thread",
                    "arc_index": 0,
                    "evidence_memory_ids": [201, 202, 203, 204],
                    "closed": False,  # not resolved -> dropped
                    "confidence": 0.9,
                }]}
            return {"concepts": []}

        h = WorkerHarness(
            responder, clusters=[cluster], self_memories=[],
            shared_moments=mems,
        )
        h.worker.run()
        self.assertEqual(h.store.list_by(subject="user", kind="narrative"), [])

    def test_short_chain_not_offered(self) -> None:
        # Only two members -> below narrative_min_chain (3); no candidate is
        # offered, so the proposer never runs for it.
        cluster, mems, _ids = _user_arc(n=2)
        called = {"narrative": 0}

        def responder(system, user):
            if "NARRATIVE ARCS in" in system and "OWN inner life" not in system:
                called["narrative"] += 1
            return {"concepts": []}

        h = WorkerHarness(
            responder, clusters=[cluster], self_memories=[],
            shared_moments=mems,
        )
        h.worker.run()
        self.assertEqual(called["narrative"], 0)
        self.assertEqual(h.store.list_by(subject="user", kind="narrative"), [])

    def test_clean_rerun_is_noop(self) -> None:
        cluster, mems, _ids = _user_arc()
        h = WorkerHarness(
            _user_responder, clusters=[cluster], self_memories=[],
            shared_moments=mems,
        )
        h.worker.run()
        before = h.store.count()
        stats = h.worker.run()
        self.assertFalse(stats["narrative_dirty"])
        self.assertEqual(h.store.count(), before)

    def test_disabled_switch_skips_pass(self) -> None:
        cluster, mems, _ids = _user_arc()
        agent = types.SimpleNamespace(
            concepts_enabled=True,
            concept_synthesis_enabled=True,
            narrative_synthesis_enabled=False,
        )
        h = WorkerHarness(
            _user_responder, clusters=[cluster], self_memories=[],
            shared_moments=mems, agent=agent,
        )
        stats = h.worker.run()
        self.assertFalse(stats["narrative_dirty"])
        self.assertEqual(h.store.list_by(subject="user", kind="narrative"), [])


# ── proposer (direct) ────────────────────────────────────────────────────


def _ctx(responder):
    calls: dict[str, str] = {}

    def call_llm(system, user):
        calls["system"] = system
        calls["user"] = user
        return responder(system, user)["concepts"]

    ctx = ProposerContext(
        call_llm=call_llm, user_name="Jacob", assistant_name="Aiko"
    )
    return ctx, calls


def _candidate(ids, *, subject="user", label="the saga"):
    base = datetime(2026, 1, 1, tzinfo=_UTC)
    mems = [
        MemStub(mid, f"beat {mid}", "event", 0.5,
                event_time=(base + timedelta(days=n)).isoformat())
        for n, mid in enumerate(ids)
    ]
    return NarrativeCandidate(rep=ids[0], label=label, subject=subject,
                             memories=mems)


class ProposerTests(unittest.TestCase):
    def test_prompt_ordered_and_evidence_follows_candidate(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "The CPU saga",
                "arc_index": 0,
                "evidence_memory_ids": [13, 11, 12],  # out of order
                "closed": True,
                "confidence": 0.7,
            }]}

        ctx, calls = _ctx(responder)
        out = propose_narrative_user(
            ctx, candidates=[_candidate([11, 12, 13])], min_chain=3,
        )
        # Prompt shows the arc in temporal (candidate) order.
        self.assertIn("CANDIDATE ARCS", calls["user"])
        self.assertIn("[11]", calls["user"])
        self.assertLess(calls["user"].index("[11]"), calls["user"].index("[13]"))
        self.assertIn("Jacob", calls["system"])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "narrative")
        self.assertEqual(out[0].subject, "user")
        self.assertEqual(out[0].evidence_model, "sequence")
        # Evidence re-ordered by the candidate order, not the LLM's order.
        self.assertEqual(
            [i for _t, i in out[0].evidence], ["11", "12", "13"]
        )

    def test_aiko_voice_is_first_person(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "The stretch where I grew",
                "arc_index": 0,
                "evidence_memory_ids": [11, 12, 13],
                "closed": True,
                "confidence": 0.7,
            }]}

        ctx, calls = _ctx(responder)
        out = propose_narrative_aiko(
            ctx, candidates=[_candidate([11, 12, 13], subject="aiko")],
        )
        self.assertIn("FIRST person", calls["system"])
        self.assertIn("Aiko", calls["system"])
        self.assertEqual(out[0].subject, "aiko")

    def test_open_arc_rejected(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "still going",
                "arc_index": 0,
                "evidence_memory_ids": [11, 12, 13],
                "closed": False,
                "confidence": 0.9,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_narrative_user(ctx, candidates=[_candidate([11, 12, 13])])
        self.assertEqual(out, [])

    def test_short_chain_rejected(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "too short",
                "arc_index": 0,
                "evidence_memory_ids": [11, 12],  # < min_chain
                "closed": True,
                "confidence": 0.9,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_narrative_user(
            ctx, candidates=[_candidate([11, 12, 13])], min_chain=3,
        )
        self.assertEqual(out, [])

    def test_cold_start_no_candidates(self) -> None:
        ctx, _ = _ctx(lambda s, u: {"concepts": []})
        self.assertEqual(propose_narrative_user(ctx, candidates=[]), [])

    def test_reinforce_by_id(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "reinforces_id": 42,
                "arc_index": 0,
                "evidence_memory_ids": [11, 12, 13],
                "rationale": "fresh beats for the known arc",
            }]}

        ctx, _ = _ctx(responder)
        out = propose_narrative_user(
            ctx, candidates=[_candidate([11, 12, 13])],
            existing=[ExistingConcept(id=42, label="The CPU saga")],
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].reinforces_id, 42)
        self.assertEqual(out[0].label, "")
        self.assertEqual(out[0].evidence_model, "sequence")


# ── rendering header ─────────────────────────────────────────────────────


class RenderingTests(unittest.TestCase):
    def test_narrative_header_dispatch(self) -> None:
        from app.core.session.inner_life_part1 import InnerLifePart1Mixin

        user = InnerLifePart1Mixin._concept_group_header(
            "user", "narrative", "Jacob"
        )
        self.assertIn("Story-arcs", user)
        self.assertIn("Jacob", user)
        rel = InnerLifePart1Mixin._concept_group_header(
            "relationship", "narrative", "Jacob"
        )
        self.assertIn("Story-arcs", rel)
        self.assertIn("Jacob", rel)
        aiko = InnerLifePart1Mixin._concept_group_header(
            "aiko", "narrative", "Jacob"
        )
        self.assertIn("Story-arcs", aiko)
        self.assertIn("your own", aiko)


if __name__ == "__main__":
    unittest.main()
