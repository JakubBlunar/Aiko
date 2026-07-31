"""Tests for the L23 follow-on communication-style concepts.

Covers the full vertical slice:

* the ``communication_style_evidence_gate`` -- the boundary-like single-anchor
  OVERRIDE (a 1-source line promotes even when the caller floors ``min_sources``
  higher) plus the age/confidence floors that still apply,
* the ``communication_style`` kind registration + medium plasticity + relevance
  routing (NOT ``profile_block``, NOT on the always-on core lane),
* the ``_run_comm_style_pass`` worker pass for user AND aiko (single deliberate
  anchor seeds a line, dirty-tracking no-op, digest-shift re-fire, and the
  ``communication_style_synthesis_enabled`` switch),
* ``_build_style_digest`` (K13 labels + profile field; empty when absent),
* the shared ``propose_communication_style`` composition rule (>=1 anchor OR
  >=2 clusters), digest-as-guidance-not-evidence, and the user/aiko voice,
* the soft delivery-style rendering header.
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
from app.core.concepts.concept_lifecycle import (
    communication_style_evidence_gate,
)
from app.core.concepts.proposers import ExistingConcept, ProposerContext
from app.core.concepts.proposers.communication_style_aiko import (
    propose_communication_style_aiko,
)
from app.core.concepts.proposers.communication_style_user import (
    propose_communication_style_user,
)

from tests.test_concept_synthesis_worker import (
    ClusterStub,
    MemStub,
    WorkerHarness,
)


# ── gate + registry ─────────────────────────────────────────────────────


class GateAndRegistryTests(unittest.TestCase):
    def test_single_anchor_promotes(self) -> None:
        self.assertTrue(
            communication_style_evidence_gate(
                distinct_source_count=1, age_days=1.0, confidence=0.7,
                min_sources=2, min_age_days=0.0, min_confidence=0.0,
            )
        )

    def test_source_floor_is_overridden_not_maxed(self) -> None:
        # A higher caller min_sources does NOT block a 1-source style line.
        self.assertTrue(
            communication_style_evidence_gate(
                distinct_source_count=1, age_days=1.0, confidence=0.7,
                min_sources=5, min_age_days=0.0, min_confidence=0.0,
            )
        )

    def test_age_and_confidence_floors_still_bite(self) -> None:
        self.assertFalse(  # age below the 0.5 floor
            communication_style_evidence_gate(
                distinct_source_count=1, age_days=0.2, confidence=0.9,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )
        self.assertFalse(  # confidence below the 0.65 floor
            communication_style_evidence_gate(
                distinct_source_count=1, age_days=1.0, confidence=0.5,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )

    def test_caller_confidence_floor_still_applies_when_higher(self) -> None:
        self.assertFalse(
            communication_style_evidence_gate(
                distinct_source_count=1, age_days=1.0, confidence=0.7,
                min_sources=1, min_age_days=0.0, min_confidence=0.8,
            )
        )

    def test_registry(self) -> None:
        kind = get_kind("communication_style")
        self.assertIsNotNone(kind)
        self.assertEqual(kind.subject, "user")
        self.assertEqual(kind.evidence_model, "set")
        self.assertAlmostEqual(kind.plasticity_default, 0.4)
        self.assertIs(
            kind.promotion_gate, communication_style_evidence_gate
        )

    def test_not_core_lane_and_not_profile_block(self) -> None:
        # A style line surfaces only when its context is live -> relevance path,
        # never pinned every turn and never a user-profile fact.
        self.assertNotIn(
            "communication_style", {k.name for k in core_lane_kinds()}
        )
        self.assertFalse(get_kind("communication_style").core_always_on)
        self.assertNotIn(
            "communication_style", kinds_for_target("profile_block")
        )

    def test_surface_weights_opt_in(self) -> None:
        w = get_kind("communication_style").surface_weights
        self.assertNotEqual(w, DEFAULT_SURFACE_WEIGHTS)
        self.assertGreater(w.stability, 0.0)
        self.assertGreater(w.activation, 0.0)


# ── worker comm-style pass ───────────────────────────────────────────────


def _user_cluster(rep: int = 100) -> ClusterStub:
    return ClusterStub(
        rep=rep, summary="programming help", size=12,
        kinds=("fact", "preference", "event"),
    )


def _user_style_responder(system, user):
    if "communication-style" in system and "FIRST PERSON" not in system:
        return {"concepts": [{
            "label": "Go deep with examples when Jacob asks about code",
            "evidence_memory_ids": [500],
            "rationale": "he asked for detailed code explanations",
            "confidence": 0.7,
        }]}
    return {"concepts": []}


def _aiko_style_responder(system, user):
    if "communication-style" in system and "FIRST PERSON" in system:
        return {"concepts": [{
            "label": "I keep it short and dry in casual back-and-forth",
            "evidence_memory_ids": [600],
            "rationale": "a delivery choice she made",
            "confidence": 0.7,
        }]}
    return {"concepts": []}


class CommStylePassTests(unittest.TestCase):
    def test_user_pass_creates_single_anchor_line(self) -> None:
        anchor = MemStub(
            500, "He asked me to explain code in depth with examples.",
            "self_tagged", 0.9,
        )
        h = WorkerHarness(
            _user_style_responder, clusters=[_user_cluster()],
            self_memories=[anchor],
        )
        stats = h.worker.run()
        self.assertTrue(stats["comm_style_dirty"])
        out = h.store.list_by(subject="user", kind="communication_style")
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c.evidence_model, "set")
        self.assertEqual(c.distinct_source_count, 1)
        ev = h.store.evidence_of(c.concept_id)
        self.assertEqual(
            [(e.src_type, e.src_id) for e in ev], [("memory", "500")]
        )

    def test_aiko_pass_creates_first_person_line(self) -> None:
        anchor = MemStub(
            600, "I keep it short in casual chat with him.", "self", 0.9,
        )
        h = WorkerHarness(
            _aiko_style_responder, clusters=[], self_memories=[anchor],
        )
        stats = h.worker.run()
        self.assertTrue(stats["comm_style_dirty"])
        out = h.store.list_by(subject="aiko", kind="communication_style")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].subject, "aiko")

    def test_clean_rerun_is_noop(self) -> None:
        anchor = MemStub(500, "explain code in depth", "self_tagged", 0.9)
        h = WorkerHarness(
            _user_style_responder, clusters=[_user_cluster()],
            self_memories=[anchor],
        )
        h.worker.run()
        before = h.store.count()
        stats = h.worker.run()
        self.assertFalse(stats["comm_style_dirty"])
        self.assertEqual(h.store.count(), before)

    def test_disabled_switch_skips_pass(self) -> None:
        anchor = MemStub(500, "explain code in depth", "self_tagged", 0.9)
        agent = types.SimpleNamespace(
            concepts_enabled=True,
            concept_synthesis_enabled=True,
            communication_style_synthesis_enabled=False,
        )
        h = WorkerHarness(
            _user_style_responder, clusters=[_user_cluster()],
            self_memories=[anchor], agent=agent,
        )
        stats = h.worker.run()
        self.assertFalse(stats["comm_style_dirty"])
        self.assertEqual(
            h.store.list_by(subject="user", kind="communication_style"), []
        )

    def test_digest_shift_refires_settled_pass(self) -> None:
        # After a clean settled run, a material change in the style digest alone
        # (no new anchors/clusters) re-dirties the comm-style pass.
        anchor = MemStub(500, "explain code in depth", "self_tagged", 0.9)
        profile = _FakeProfileStore({})

        h = WorkerHarness(
            _user_style_responder, clusters=[_user_cluster()],
            self_memories=[anchor],
            user_profile_store=profile,
            user_id_provider=lambda: "u1",
        )
        h.worker.run()
        self.assertFalse(h.worker.run()["comm_style_dirty"])  # settled

        # Profile now names a style -> digest hash changes -> pass re-fires.
        profile.set("communication_style", "concise, prefers examples")
        self.assertTrue(h.worker.run()["comm_style_dirty"])


# ── style digest ─────────────────────────────────────────────────────────


class _FakeProfileStore:
    def __init__(self, fields):
        # fields: {name: value}
        self._fields = dict(fields)

    def set(self, name, value):
        self._fields[name] = value

    def fields(self, uid):
        return {
            k: types.SimpleNamespace(value=v, confidence=0.8)
            for k, v in self._fields.items()
        }


class _FakeStyleStore:
    def __init__(self, blob):
        self._blob = blob

    def load(self, uid):
        return self._blob


def _warmed_blob():
    # Extreme axis values so labels_for_signal fires under default thresholds.
    row = {
        "terseness": 0.95, "formality": 0.95, "emoji_density": 0.95,
        "slang_density": 0.95, "is_question": 0.95, "word_count": 3,
    }
    # >= style_signal_warmup_min (default 8) entries so current_signal() warms.
    return {"warmed": True, "window": [dict(row) for _ in range(10)]}


class StyleDigestTests(unittest.TestCase):
    def _worker(self, **kw):
        return WorkerHarness(
            lambda s, u: {"concepts": []},
            clusters=[_user_cluster()],
            self_memories=[MemStub(1, "x", "self", 0.5)],
            **kw,
        ).worker

    def test_empty_without_uid(self) -> None:
        w = self._worker(
            user_profile_store=_FakeProfileStore(
                {"communication_style": "concise"}
            ),
            user_id_provider=None,  # no uid -> empty digest
        )
        self.assertEqual(w._build_style_digest("user"), "")

    def test_profile_only(self) -> None:
        w = self._worker(
            user_profile_store=_FakeProfileStore(
                {"communication_style": "concise, playful"}
            ),
            user_id_provider=lambda: "u1",
        )
        d = w._build_style_digest("user")
        self.assertIn("noted style: concise, playful", d)
        self.assertIn("user writes", d.lower())

    def test_style_labels_and_aiko_framing(self) -> None:
        w = self._worker(
            style_signal_store=_FakeStyleStore(_warmed_blob()),
            user_profile_store=_FakeProfileStore(
                {"communication_style": "concise"}
            ),
            user_id_provider=lambda: "u1",
        )
        user_digest = w._build_style_digest("user")
        self.assertIn("writes:", user_digest)  # K13 labels present
        self.assertIn("noted style: concise", user_digest)
        # aiko framing reads as "what he responds to", not "the user writes".
        aiko_digest = w._build_style_digest("aiko")
        self.assertIn("responds to", aiko_digest.lower())

    def test_empty_when_no_stores(self) -> None:
        w = self._worker(user_id_provider=lambda: "u1")
        self.assertEqual(w._build_style_digest("user"), "")


# ── proposer (direct): composition + digest + voice ──────────────────────


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


class ProposerTests(unittest.TestCase):
    def test_single_anchor_accepted(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "Go deep with examples about code",
                "evidence_memory_ids": [500],
                "confidence": 0.7,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_communication_style_user(
            ctx, cluster_index=_clusters(100),
            memories=[MemStub(500, "explain in depth", "self_tagged", 0.9)],
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "communication_style")
        self.assertEqual(out[0].subject, "user")
        self.assertEqual(out[0].evidence, [("memory", "500")])

    def test_single_cluster_dropped(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "a lone-cluster style",
                "evidence_cluster_reps": [100],
                "confidence": 0.9,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_communication_style_user(
            ctx, cluster_index=_clusters(100, 101), memories=[],
        )
        self.assertEqual(out, [])

    def test_two_clusters_accepted(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "Give the reasoning across these areas",
                "evidence_cluster_reps": [100, 101],
                "confidence": 0.7,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_communication_style_user(
            ctx, cluster_index=_clusters(100, 101), memories=[],
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(
            out[0].evidence, [("cluster", "100"), ("cluster", "101")]
        )

    def test_reinforce_by_id(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "reinforces_id": 42,
                "evidence_memory_ids": [500],
                "rationale": "fresh support",
            }]}

        ctx, _ = _ctx(responder)
        out = propose_communication_style_user(
            ctx, cluster_index=_clusters(100),
            memories=[MemStub(500, "note", "self_tagged", 0.9)],
            existing=[ExistingConcept(id=42, label="Go deep about code")],
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].reinforces_id, 42)
        self.assertEqual(out[0].label, "")
        self.assertEqual(out[0].kind, "communication_style")

    def test_digest_is_guidance_in_prompt(self) -> None:
        ctx, calls = _ctx(lambda s, u: {"concepts": []})
        propose_communication_style_user(
            ctx, cluster_index=_clusters(100), memories=[],
            style_digest="How the user writes lately -- writes: terse",
        )
        self.assertIn("STYLE SIGNAL", calls["user"])
        self.assertIn("terse", calls["user"])
        self.assertIn("NOT evidence", calls["user"])

    def test_user_voice_third_person(self) -> None:
        ctx, calls = _ctx(lambda s, u: {"concepts": []})
        propose_communication_style_user(
            ctx, cluster_index=_clusters(100), memories=[]
        )
        self.assertIn("Jacob", calls["system"])
        self.assertNotIn("FIRST PERSON", calls["system"])

    def test_aiko_voice_first_person(self) -> None:
        ctx, calls = _ctx(lambda s, u: {"concepts": []})
        propose_communication_style_aiko(
            ctx, memories=[MemStub(600, "note", "self", 0.9)],
        )
        self.assertIn("FIRST PERSON", calls["system"])
        self.assertIn("Aiko", calls["system"])


# ── rendering header ─────────────────────────────────────────────────────


class RenderingTests(unittest.TestCase):
    def test_header_is_soft_delivery_framed(self) -> None:
        from app.core.session.inner_life_part1 import InnerLifePart1Mixin

        user = InnerLifePart1Mixin._concept_group_header(
            "user", "communication_style", "Jacob"
        )
        self.assertIn("Jacob", user)
        self.assertIn("conversation to feel", user)

        aiko = InnerLifePart1Mixin._concept_group_header(
            "aiko", "communication_style", "Jacob"
        )
        self.assertIn("chosen to show up", aiko)
        self.assertIn("Jacob", aiko)


if __name__ == "__main__":
    unittest.main()
