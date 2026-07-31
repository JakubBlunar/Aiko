"""Tests for L14 aspiration (trajectory) concepts.

Covers the full vertical slice (the open-ended sibling of L8 narrative):

* the ``aspiration_evidence_gate`` floors (higher age floor than narrative),
* the ``aspiration`` kind registration + relevance-only routing (both subjects),
* the ``_run_aspiration_pass`` worker pass for user AND aiko (creation with
  ordered ``sequence`` evidence, non-directional rejection, short-chain
  rejection, insufficient-span rejection, dirty-tracking no-op, the
  ``aspiration_synthesis_enabled`` switch),
* the shared ``propose_ordered_concept`` body via both aspiration proposers
  (temporal-ordered prompt + evidence, order from the candidate not the LLM,
  first-person aiko voice, non-directional/short rejection, cold-start,
  reinforce-by-id),
* the rendering header (both subjects).
"""
from __future__ import annotations

import types
import unittest
from datetime import datetime, timedelta, timezone

from app.core.concepts.concept_kinds import (
    core_lane_kinds,
    get_kind,
    kinds_for_target,
)
from app.core.concepts.concept_lifecycle import aspiration_evidence_gate
from app.core.concepts.proposers import (
    ExistingConcept,
    NarrativeCandidate,
    ProposerContext,
)
from app.core.concepts.proposers.aspiration_aiko import propose_aspiration_aiko
from app.core.concepts.proposers.aspiration_user import propose_aspiration_user

from tests.test_concept_synthesis_worker import (
    ClusterStub,
    MemStub,
    WorkerHarness,
    _mem_settings,
)

_UTC = timezone.utc


# ── gate + registry ─────────────────────────────────────────────────────


class AspirationGateAndRegistryTests(unittest.TestCase):
    def test_gate_floors(self) -> None:
        # only 2 steps < chain floor 3
        self.assertFalse(
            aspiration_evidence_gate(
                distinct_source_count=2, age_days=10.0, confidence=0.99,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )
        # age below the 3.0 floor (higher than narrative's 1.0)
        self.assertFalse(
            aspiration_evidence_gate(
                distinct_source_count=3, age_days=2.0, confidence=0.99,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )
        # confidence below the 0.6 floor
        self.assertFalse(
            aspiration_evidence_gate(
                distinct_source_count=3, age_days=5.0, confidence=0.5,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )
        self.assertTrue(
            aspiration_evidence_gate(
                distinct_source_count=3, age_days=3.0, confidence=0.6,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )

    def test_caller_thresholds_still_apply_when_higher(self) -> None:
        self.assertFalse(
            aspiration_evidence_gate(
                distinct_source_count=4, age_days=5.0, confidence=0.6,
                min_sources=5, min_age_days=0.0, min_confidence=0.0,
            )
        )

    def test_registry(self) -> None:
        kind = get_kind("aspiration")
        self.assertIsNotNone(kind)
        self.assertEqual(kind.subject, "user")
        self.assertEqual(kind.evidence_model, "sequence")
        self.assertAlmostEqual(kind.plasticity_default, 0.4)
        self.assertFalse(kind.core_always_on)
        self.assertEqual(kind.surfacing_targets, {})
        self.assertIs(kind.promotion_gate, aspiration_evidence_gate)

    def test_relevance_only_routing(self) -> None:
        self.assertNotIn("aspiration", kinds_for_target("profile_block"))
        self.assertNotIn("aspiration", {k.name for k in core_lane_kinds()})


# ── worker aspiration pass ───────────────────────────────────────────────


def _dir_memories(start_id: int, n: int, kind: str, base: datetime, step_days):
    return [
        MemStub(
            start_id + i,
            f"step {i}: moving further in the same direction",
            kind,
            0.5 + i * 0.05,
            event_time=(base + timedelta(days=i * step_days)).isoformat(),
        )
        for i in range(n)
    ]


def _user_direction(n: int = 4, step_days: float = 7.0):
    """A user-dominant cluster spanning n*step_days days (>= the 14d floor by
    default). member_ids scrambled -> the pass must reorder by event_time."""
    base = datetime(2026, 1, 1, 12, 0, tzinfo=_UTC)
    mems = _dir_memories(201, n, "event", base, step_days)
    ids = [m.id for m in mems]
    cluster = ClusterStub(
        rep=ids[0], summary="toward self-hosting", size=n,
        kinds=("event",) * n, member_ids=ids[::-1],
    )
    return cluster, mems, ids


def _aiko_direction(n: int = 3, step_days: float = 8.0):
    base = datetime(2026, 2, 1, 9, 0, tzinfo=_UTC)
    kinds = ("self", "reflection", "diary")
    mems = [
        MemStub(
            301 + i, f"self step {i}: I keep growing this way", kinds[i % 3],
            0.6, event_time=(base + timedelta(days=i * step_days)).isoformat(),
        )
        for i in range(n)
    ]
    ids = [m.id for m in mems]
    cluster = ClusterStub(
        rep=ids[0], summary="becoming steadier", size=n,
        kinds=tuple(m.kind for m in mems), member_ids=ids[::-1],
    )
    return cluster, mems, ids


def _user_responder(system, user):
    if "ASPIRATIONS" in system and "OWN inner life" not in system:
        return {"concepts": [{
            "label": "Building toward a self-hosted life",
            "arc_index": 0,
            "evidence_memory_ids": [204, 201, 203, 202],  # scrambled
            "directional": True,
            "rationale": "consistent pull over weeks",
            "confidence": 0.7,
        }]}
    return {"concepts": []}


def _aiko_responder(system, user):
    if "ASPIRATIONS" in system and "OWN inner life" in system:
        return {"concepts": [{
            "label": "Growing into someone he can rely on",
            "arc_index": 0,
            "evidence_memory_ids": [303, 301, 302],
            "directional": True,
            "rationale": "a sustained direction in her",
            "confidence": 0.7,
        }]}
    return {"concepts": []}


def _settings_lowspan():
    # Keep the narrative caps sane (drain in one run) and use a small span floor
    # so a compact test cluster still qualifies as a trajectory.
    s = _mem_settings()
    s.concept_synthesis_aspiration_min_chain = 3
    s.concept_synthesis_aspiration_min_span_days = 14.0
    s.concept_synthesis_max_aspiration_clusters_per_run = 10
    s.concept_synthesis_max_aspiration_memories = 40
    return s


class AspirationPassTests(unittest.TestCase):
    def test_user_pass_creates_ordered_sequence_concept(self) -> None:
        cluster, mems, ids = _user_direction()
        h = WorkerHarness(
            _user_responder, clusters=[cluster], self_memories=[],
            shared_moments=mems, mem_settings=_settings_lowspan(),
        )
        stats = h.worker.run()
        self.assertTrue(stats["aspiration_dirty"])
        out = h.store.list_by(subject="user", kind="aspiration")
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c.label, "Building toward a self-hosted life")
        self.assertEqual(c.evidence_model, "sequence")
        ev = h.store.evidence_of(c.concept_id)
        self.assertTrue(all(e.src_type == "memory" for e in ev))
        # ordinal order == temporal order, not the scrambled LLM order.
        self.assertEqual([e.src_id for e in ev], [str(i) for i in ids])
        self.assertEqual([e.ordinal for e in ev], list(range(len(ids))))

    def test_aiko_pass_creates_first_person_direction(self) -> None:
        cluster, mems, ids = _aiko_direction()
        h = WorkerHarness(
            _aiko_responder, clusters=[cluster], self_memories=mems,
            mem_settings=_settings_lowspan(),
        )
        stats = h.worker.run()
        self.assertTrue(stats["aspiration_dirty"])
        out = h.store.list_by(subject="aiko", kind="aspiration")
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c.subject, "aiko")
        ev = h.store.evidence_of(c.concept_id)
        self.assertEqual([e.src_id for e in ev], [str(i) for i in ids])
        self.assertEqual([e.ordinal for e in ev], [0, 1, 2])

    def test_non_directional_rejected(self) -> None:
        cluster, mems, _ids = _user_direction()

        def responder(system, user):
            if "ASPIRATIONS" in system and "OWN inner life" not in system:
                return {"concepts": [{
                    "label": "just repetition, no direction",
                    "arc_index": 0,
                    "evidence_memory_ids": [201, 202, 203, 204],
                    "directional": False,
                    "confidence": 0.9,
                }]}
            return {"concepts": []}

        h = WorkerHarness(
            responder, clusters=[cluster], self_memories=[],
            shared_moments=mems, mem_settings=_settings_lowspan(),
        )
        h.worker.run()
        self.assertEqual(
            h.store.list_by(subject="user", kind="aspiration"), []
        )

    def test_short_chain_not_offered(self) -> None:
        cluster, mems, _ids = _user_direction(n=2, step_days=20.0)
        called = {"aspiration": 0}

        def responder(system, user):
            if "ASPIRATIONS" in system and "OWN inner life" not in system:
                called["aspiration"] += 1
            return {"concepts": []}

        h = WorkerHarness(
            responder, clusters=[cluster], self_memories=[],
            shared_moments=mems, mem_settings=_settings_lowspan(),
        )
        h.worker.run()
        self.assertEqual(called["aspiration"], 0)

    def test_insufficient_span_not_offered(self) -> None:
        # 4 steps (chain ok) but only spanning ~3 days total (< 14d floor).
        cluster, mems, _ids = _user_direction(n=4, step_days=1.0)
        called = {"aspiration": 0}

        def responder(system, user):
            if "ASPIRATIONS" in system and "OWN inner life" not in system:
                called["aspiration"] += 1
            return {"concepts": []}

        h = WorkerHarness(
            responder, clusters=[cluster], self_memories=[],
            shared_moments=mems, mem_settings=_settings_lowspan(),
        )
        h.worker.run()
        self.assertEqual(called["aspiration"], 0)
        self.assertEqual(
            h.store.list_by(subject="user", kind="aspiration"), []
        )

    def test_clean_rerun_is_noop(self) -> None:
        cluster, mems, _ids = _user_direction()
        h = WorkerHarness(
            _user_responder, clusters=[cluster], self_memories=[],
            shared_moments=mems, mem_settings=_settings_lowspan(),
        )
        h.worker.run()
        before = h.store.count()
        stats = h.worker.run()
        self.assertFalse(stats["aspiration_dirty"])
        self.assertEqual(h.store.count(), before)

    def test_disabled_switch_skips_pass(self) -> None:
        cluster, mems, _ids = _user_direction()
        agent = types.SimpleNamespace(
            concepts_enabled=True,
            concept_synthesis_enabled=True,
            aspiration_synthesis_enabled=False,
        )
        h = WorkerHarness(
            _user_responder, clusters=[cluster], self_memories=[],
            shared_moments=mems, agent=agent,
            mem_settings=_settings_lowspan(),
        )
        stats = h.worker.run()
        self.assertFalse(stats["aspiration_dirty"])
        self.assertEqual(
            h.store.list_by(subject="user", kind="aspiration"), []
        )


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


def _candidate(ids, *, subject="user", label="the direction"):
    base = datetime(2026, 1, 1, tzinfo=_UTC)
    mems = [
        MemStub(mid, f"step {mid}", "event", 0.5,
                event_time=(base + timedelta(days=n * 7)).isoformat())
        for n, mid in enumerate(ids)
    ]
    return NarrativeCandidate(rep=ids[0], label=label, subject=subject,
                             memories=mems)


class ProposerTests(unittest.TestCase):
    def test_prompt_ordered_and_evidence_follows_candidate(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "Toward self-hosting",
                "arc_index": 0,
                "evidence_memory_ids": [13, 11, 12],  # out of order
                "directional": True,
                "confidence": 0.7,
            }]}

        ctx, calls = _ctx(responder)
        out = propose_aspiration_user(
            ctx, candidates=[_candidate([11, 12, 13])], min_chain=3,
        )
        self.assertIn("CANDIDATE DIRECTIONS", calls["user"])
        self.assertIn("[11]", calls["user"])
        self.assertLess(
            calls["user"].index("[11]"), calls["user"].index("[13]")
        )
        self.assertIn("Jacob", calls["system"])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "aspiration")
        self.assertEqual(out[0].subject, "user")
        self.assertEqual(out[0].evidence_model, "sequence")
        self.assertEqual(
            [i for _t, i in out[0].evidence], ["11", "12", "13"]
        )

    def test_aiko_voice_is_first_person(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "Growing steadier",
                "arc_index": 0,
                "evidence_memory_ids": [11, 12, 13],
                "directional": True,
                "confidence": 0.7,
            }]}

        ctx, calls = _ctx(responder)
        out = propose_aspiration_aiko(
            ctx, candidates=[_candidate([11, 12, 13], subject="aiko")],
        )
        self.assertIn("FIRST person", calls["system"])
        self.assertIn("Aiko", calls["system"])
        self.assertEqual(out[0].subject, "aiko")

    def test_non_directional_rejected(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "no real direction",
                "arc_index": 0,
                "evidence_memory_ids": [11, 12, 13],
                "directional": False,
                "confidence": 0.9,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_aspiration_user(
            ctx, candidates=[_candidate([11, 12, 13])]
        )
        self.assertEqual(out, [])

    def test_short_chain_rejected(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "too short",
                "arc_index": 0,
                "evidence_memory_ids": [11, 12],
                "directional": True,
                "confidence": 0.9,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_aspiration_user(
            ctx, candidates=[_candidate([11, 12, 13])], min_chain=3,
        )
        self.assertEqual(out, [])

    def test_cold_start_no_candidates(self) -> None:
        ctx, _ = _ctx(lambda s, u: {"concepts": []})
        self.assertEqual(propose_aspiration_user(ctx, candidates=[]), [])

    def test_reinforce_by_id(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "reinforces_id": 42,
                "arc_index": 0,
                "evidence_memory_ids": [11, 12, 13],
                "rationale": "fresh movement in the known direction",
            }]}

        ctx, _ = _ctx(responder)
        out = propose_aspiration_user(
            ctx, candidates=[_candidate([11, 12, 13])],
            existing=[ExistingConcept(id=42, label="Toward self-hosting")],
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].reinforces_id, 42)
        self.assertEqual(out[0].label, "")
        self.assertEqual(out[0].kind, "aspiration")
        self.assertEqual(out[0].evidence_model, "sequence")


# ── rendering header ─────────────────────────────────────────────────────


class RenderingTests(unittest.TestCase):
    def test_aspiration_header_dispatch(self) -> None:
        from app.core.session.inner_life_part1 import InnerLifePart1Mixin

        user = InnerLifePart1Mixin._concept_group_header(
            "user", "aspiration", "Jacob"
        )
        self.assertIn("Directions", user)
        self.assertIn("Jacob", user)
        rel = InnerLifePart1Mixin._concept_group_header(
            "relationship", "aspiration", "Jacob"
        )
        self.assertIn("Directions", rel)
        self.assertIn("Jacob", rel)
        aiko = InnerLifePart1Mixin._concept_group_header(
            "aiko", "aspiration", "Jacob"
        )
        self.assertIn("Directions", aiko)
        self.assertIn("becoming", aiko)


if __name__ == "__main__":
    unittest.main()
