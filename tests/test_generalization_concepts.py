"""Tests for the L20 generalization concepts -- the abstraction *meta* kind.

A generalization is a concept whose evidence is 2+ OTHER active concepts (of any
kind, same subject) that it names a latent super-concept over ("builds things
that last" over several hobbies). It rides the same meta rails as the L12
tension (``evidence_model="meta"``, ``("concept", id)`` evidence edges) but
differs in three ways this suite pins down:

* arity is a RANGE (2..N), not a fixed pair -- the ``generalization_evidence_gate``
  floors at 2 and the proposer accepts up to ``GENERALIZATION_MAX_CHILDREN``;
* it is arity-aware moot -- still live while >= 2 children stay active, so it
  survives losing one (unlike a tension, which needs both sides);
* it DOES render (and pins on the core lane), and its children are suppressed
  when it is present, so Aiko speaks the through-line, not the specifics.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np

from app.core.concepts.concept_kinds import (
    DEFAULT_SURFACE_WEIGHTS,
    core_lane_kinds,
    get_kind,
)
from app.core.concepts.concept_lifecycle import generalization_evidence_gate
from app.core.concepts.concept_meta_depth import meta_depth
from app.core.concepts.concept_store import Concept, ConceptEdge
from app.core.concepts.proposers import (
    GENERALIZATION_MAX_CHILDREN,
    ExistingConcept,
    ProposerContext,
    TensionBase,
)
from app.core.concepts.proposers.generalization_aiko import (
    propose_generalization_aiko,
)
from app.core.concepts.proposers.generalization_user import (
    propose_generalization_user,
)
from app.core.session.context_budget_selector import ContextCandidate
from app.core.session.inner_life_part1 import InnerLifePart1Mixin

from tests.test_concept_synthesis_worker import WorkerHarness

_UTC = timezone.utc


# ── gate + registry ─────────────────────────────────────────────────────


class GateAndRegistryTests(unittest.TestCase):
    def test_two_sources_promote(self) -> None:
        self.assertTrue(
            generalization_evidence_gate(
                distinct_source_count=2, age_days=4.0, confidence=0.75,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )

    def test_one_source_blocked(self) -> None:
        self.assertFalse(
            generalization_evidence_gate(
                distinct_source_count=1, age_days=4.0, confidence=0.9,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )

    def test_source_floor_is_fixed_not_maxed(self) -> None:
        # A higher caller min_sources does NOT raise the fixed-arity floor of 2.
        self.assertTrue(
            generalization_evidence_gate(
                distinct_source_count=2, age_days=4.0, confidence=0.75,
                min_sources=5, min_age_days=0.0, min_confidence=0.0,
            )
        )

    def test_floors_are_higher_than_tension(self) -> None:
        # Age below the 3.0d floor blocks (a tension's 1.0d floor would pass).
        self.assertFalse(
            generalization_evidence_gate(
                distinct_source_count=2, age_days=2.0, confidence=0.9,
                min_sources=2, min_age_days=0.0, min_confidence=0.0,
            )
        )
        # Confidence below the 0.72 floor blocks (a tension's 0.6 would pass).
        self.assertFalse(
            generalization_evidence_gate(
                distinct_source_count=2, age_days=4.0, confidence=0.65,
                min_sources=2, min_age_days=0.0, min_confidence=0.0,
            )
        )

    def test_caller_confidence_floor_still_applies_when_higher(self) -> None:
        self.assertFalse(
            generalization_evidence_gate(
                distinct_source_count=2, age_days=4.0, confidence=0.8,
                min_sources=2, min_age_days=0.0, min_confidence=0.9,
            )
        )

    def test_registry(self) -> None:
        kind = get_kind("generalization")
        self.assertIsNotNone(kind)
        self.assertEqual(kind.evidence_model, "meta")
        self.assertAlmostEqual(kind.plasticity_default, 0.25)
        self.assertIs(kind.promotion_gate, generalization_evidence_gate)

    def test_on_core_lane_at_high_bar(self) -> None:
        # UNLIKE a tension, a generalization joins the always-on core lane so a
        # settled abstraction pins and its children step aside.
        self.assertIn(
            "generalization", {k.name for k in core_lane_kinds()}
        )
        kind = get_kind("generalization")
        self.assertTrue(kind.core_always_on)
        self.assertAlmostEqual(kind.core_min_confidence, 0.8)

    def test_surface_weights_opt_in(self) -> None:
        w = get_kind("generalization").surface_weights
        self.assertNotEqual(w, DEFAULT_SURFACE_WEIGHTS)
        self.assertGreater(w.confidence, 0.0)


# ── proposer (direct): composition + voice ───────────────────────────────


def _ctx(responder):
    calls: dict[str, str] = {}

    def call_llm(system, user):
        calls["system"] = system
        calls["user"] = user
        return responder(system, user)["concepts"]

    return ProposerContext(
        call_llm=call_llm, user_name="Jacob", assistant_name="Aiko"
    ), calls


def _bases(*specs) -> list[TensionBase]:
    """specs: (id, subject, kind, label)."""
    return [
        TensionBase(id=i, subject=s, kind=k, label=lbl, confidence=0.8)
        for (i, s, k, lbl) in specs
    ]


class ProposerTests(unittest.TestCase):
    def test_two_ids_accepted(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "He builds things meant to last",
                "evidence_concept_ids": [1, 2],
                "confidence": 0.75,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_generalization_user(
            ctx,
            concepts=_bases(
                (1, "user", "identity", "home server tinkering"),
                (2, "user", "identity", "woodworking"),
            ),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "generalization")
        self.assertEqual(out[0].subject, "user")
        self.assertEqual(out[0].evidence_model, "meta")
        self.assertEqual(
            out[0].evidence, [("concept", "1"), ("concept", "2")]
        )

    def test_cross_kind_children_accepted(self) -> None:
        # An abstraction may span different kinds (a hobby, a value, a habit).
        def responder(system, user):
            return {"concepts": [{
                "label": "He guards his own time",
                "evidence_concept_ids": [1, 2, 3],
                "confidence": 0.8,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_generalization_user(
            ctx,
            concepts=_bases(
                (1, "user", "boundary", "don't overbook him"),
                (2, "user", "value", "values slow mornings"),
                (3, "user", "identity", "protective of focus blocks"),
            ),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(
            out[0].evidence,
            [("concept", "1"), ("concept", "2"), ("concept", "3")],
        )

    def test_single_id_dropped(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "not an abstraction",
                "evidence_concept_ids": [1],
                "confidence": 0.9,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_generalization_user(
            ctx,
            concepts=_bases(
                (1, "user", "identity", "a"), (2, "user", "value", "b"),
            ),
        )
        self.assertEqual(out, [])

    def test_children_capped(self) -> None:
        # More than GENERALIZATION_MAX_CHILDREN cited -> trimmed to the cap.
        n = GENERALIZATION_MAX_CHILDREN + 3
        cited = list(range(1, n + 1))

        def responder(system, user):
            return {"concepts": [{
                "label": "too broad",
                "evidence_concept_ids": cited,
                "confidence": 0.8,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_generalization_user(
            ctx,
            concepts=_bases(
                *[(i, "user", "identity", f"c{i}") for i in cited]
            ),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(len(out[0].evidence), GENERALIZATION_MAX_CHILDREN)

    def test_reinforce_by_id_needs_two(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "reinforces_id": 42,
                "evidence_concept_ids": [1, 2],
                "rationale": "still the through-line",
            }]}

        ctx, _ = _ctx(responder)
        out = propose_generalization_user(
            ctx,
            concepts=_bases(
                (1, "user", "identity", "a"), (2, "user", "value", "b"),
            ),
            existing=[ExistingConcept(id=42, label="builds to last")],
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].reinforces_id, 42)
        self.assertEqual(out[0].label, "")
        self.assertEqual(out[0].evidence_model, "meta")

    def test_reinforce_with_one_child_dropped(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "reinforces_id": 42,
                "evidence_concept_ids": [1],
                "rationale": "half",
            }]}

        ctx, _ = _ctx(responder)
        out = propose_generalization_user(
            ctx,
            concepts=_bases((1, "user", "identity", "a")),
            existing=[ExistingConcept(id=42, label="x")],
        )
        self.assertEqual(out, [])

    def test_unknown_ids_filtered_then_dropped(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "x",
                "evidence_concept_ids": [1, 999],
                "confidence": 0.9,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_generalization_user(
            ctx, concepts=_bases((1, "user", "identity", "a")),
        )
        self.assertEqual(out, [])

    def test_empty_pool_returns_nothing(self) -> None:
        ctx, calls = _ctx(lambda s, u: {"concepts": []})
        out = propose_generalization_user(ctx, concepts=[])
        self.assertEqual(out, [])
        self.assertNotIn("system", calls)  # LLM never called

    def test_user_voice(self) -> None:
        ctx, calls = _ctx(lambda s, u: {"concepts": []})
        propose_generalization_user(
            ctx, concepts=_bases((1, "user", "identity", "a"))
        )
        self.assertIn("Jacob", calls["system"])
        self.assertIn("abstraction", calls["system"].lower())
        self.assertNotIn("FIRST PERSON", calls["system"])

    def test_aiko_voice_first_person(self) -> None:
        ctx, calls = _ctx(lambda s, u: {"concepts": []})
        propose_generalization_aiko(
            ctx, concepts=_bases((1, "aiko", "value", "a"))
        )
        self.assertIn("HERSELF", calls["system"])
        self.assertIn("FIRST PERSON", calls["system"])


# ── worker generalization pass ───────────────────────────────────────────


def _add_active(store, *, subject, kind, label, confidence=0.8) -> int:
    now = datetime.now(_UTC).isoformat()
    return store.add(
        Concept(
            label=label, kind=kind, subject=subject, evidence_model="set",
            status="active", confidence=confidence, plasticity=0.4,
            evidence_count=2, distinct_source_count=2,
            first_evidence_at=now, last_reinforced_at=now,
        )
    )


def _gen_responder(ids):
    """Emit one generalization only for the generalization prompt (which speaks
    of an 'abstraction'), never for the tension pass that also runs."""
    def responder(system, user):
        if "already-named" in system.lower():
            return {"concepts": []}
        if "abstraction" not in system.lower():
            return {"concepts": []}
        if "HERSELF" in system:  # aiko lens -- keep this test to the user lens
            return {"concepts": []}
        return {"concepts": [{
            "label": "He builds things meant to last",
            "evidence_concept_ids": list(ids),
            "confidence": 0.8,
        }]}
    return responder


class GeneralizationPassTests(unittest.TestCase):
    def _harness(self, responder, agent=None):
        return WorkerHarness(
            responder, clusters=[], self_memories=[], agent=agent,
        )

    def test_user_pass_creates_meta_generalization(self) -> None:
        ids: list[int] = []
        h = self._harness(_gen_responder(ids))
        a = _add_active(h.store, subject="user", kind="identity", label="serv")
        b = _add_active(h.store, subject="user", kind="identity", label="wood")
        c = _add_active(h.store, subject="user", kind="value", label="craft")
        ids[:] = [a, b, c]

        stats = h.worker.run()
        self.assertTrue(stats["generalization_dirty"])
        out = h.store.list_by(subject="user", kind="generalization")
        self.assertEqual(len(out), 1)
        g = out[0]
        self.assertEqual(g.evidence_model, "meta")
        self.assertEqual(g.status, "candidate")
        self.assertEqual(g.distinct_source_count, 3)
        ev = sorted(
            (e.src_type, e.src_id) for e in h.store.evidence_of(g.concept_id)
        )
        self.assertEqual(
            ev,
            sorted(("concept", str(x)) for x in (a, b, c)),
        )
        # Each base gains the generalization as a dependent (the meta edge).
        self.assertIn(g.concept_id, h.store.dependents_of(a))

    def test_pool_below_two_is_noop(self) -> None:
        h = self._harness(_gen_responder([]))
        _add_active(h.store, subject="user", kind="identity", label="only one")
        stats = h.worker.run()
        self.assertFalse(stats["generalization_dirty"])
        self.assertEqual(h.store.list_by(kind="generalization"), [])

    def test_clean_rerun_is_noop(self) -> None:
        ids: list[int] = []
        h = self._harness(_gen_responder(ids))
        a = _add_active(h.store, subject="user", kind="identity", label="a")
        b = _add_active(h.store, subject="user", kind="identity", label="b")
        ids[:] = [a, b]
        h.worker.run()
        before = h.store.count()
        stats = h.worker.run()
        self.assertFalse(stats["generalization_dirty"])
        self.assertEqual(h.store.count(), before)

    def test_disabled_switch_skips_pass(self) -> None:
        ids: list[int] = []
        agent = SimpleNamespace(
            concepts_enabled=True,
            concept_synthesis_enabled=True,
            generalization_synthesis_enabled=False,
        )
        h = self._harness(_gen_responder(ids), agent=agent)
        a = _add_active(h.store, subject="user", kind="identity", label="a")
        b = _add_active(h.store, subject="user", kind="identity", label="b")
        ids[:] = [a, b]
        stats = h.worker.run()
        self.assertFalse(stats["generalization_dirty"])
        self.assertEqual(h.store.list_by(kind="generalization"), [])

    def test_meta_depth_cap_excludes_meta_from_pool(self) -> None:
        # An active generalization (meta) must never be offered as a base.
        seen: dict[str, str] = {}

        def responder(system, user):
            if "abstraction" in system.lower() and "HERSELF" not in system:
                seen["user"] = user
            return {"concepts": []}

        h = self._harness(responder)
        _add_active(h.store, subject="user", kind="identity", label="base-one")
        _add_active(h.store, subject="user", kind="identity", label="base-two")
        h.store.add(
            Concept(
                label="META-SENTINEL", kind="generalization", subject="user",
                evidence_model="meta", status="active", confidence=0.8,
                plasticity=0.25, evidence_count=2, distinct_source_count=2,
                first_evidence_at=datetime.now(_UTC).isoformat(),
                last_reinforced_at=datetime.now(_UTC).isoformat(),
            )
        )
        h.worker.run()
        self.assertIn("user", seen)
        base_section = seen["user"].split("ALREADY-KNOWN")[0]
        self.assertIn("base-one", base_section)
        self.assertNotIn("META-SENTINEL", base_section)


# ── L3 arity-aware meta rules ────────────────────────────────────────────


def _lifecycle_harness():
    from tests.test_concept_lifecycle_worker import _harness

    return _harness()


def _seed_generalization(store, *, base_confs, base_status, gen_conf=0.9):
    now = datetime.now(_UTC).isoformat()
    bids = []
    for i, (conf, status) in enumerate(zip(base_confs, base_status, strict=False)):
        bids.append(store.add(Concept(
            label=f"base{i}", kind="identity", subject="user",
            evidence_model="set", status=status, confidence=conf,
            plasticity=0.4, evidence_count=2, distinct_source_count=2,
            first_evidence_at=now, last_reinforced_at=now,
            last_lifecycle_at=now,
        )))
    gid = store.add(Concept(
        label="builds to last", kind="generalization", subject="user",
        evidence_model="meta", status="active", confidence=gen_conf,
        plasticity=0.25, evidence_count=len(bids),
        distinct_source_count=len(bids),
        first_evidence_at=now, last_reinforced_at=now, last_lifecycle_at=now,
    ))
    for bid in bids:
        store.add_edge(ConceptEdge(
            src_type="concept", src_id=str(bid),
            dst_type="concept", dst_id=str(gid), relation="evidence",
            polarity=1, strength=1.0,
        ))
    return gid, bids


class ArityAwareMetaRuleTests(unittest.TestCase):
    def test_confidence_bounded_to_min_active_child(self) -> None:
        h = _lifecycle_harness()
        gid, _ = _seed_generalization(
            h.store, base_confs=(0.9, 0.5, 0.8),
            base_status=("active", "active", "active"), gen_conf=0.95,
        )
        g = h.store.get(gid)
        bounded, moot = h.worker._apply_meta_rules(g, 0.95)
        self.assertFalse(moot)
        self.assertLessEqual(bounded, 0.5 + 1e-9)

    def test_survives_losing_one_of_three(self) -> None:
        # Robust to losing one child: 2 of 3 still active -> NOT moot.
        h = _lifecycle_harness()
        gid, _ = _seed_generalization(
            h.store, base_confs=(0.9, 0.8, 0.7),
            base_status=("active", "active", "dormant"),
        )
        g = h.store.get(gid)
        bounded, moot = h.worker._apply_meta_rules(g, 0.9)
        self.assertFalse(moot)
        # Confidence bounded by the shakiest *active* child (0.8, not 0.7).
        self.assertLessEqual(bounded, 0.8 + 1e-9)

    def test_moot_when_fewer_than_two_active(self) -> None:
        h = _lifecycle_harness()
        gid, _ = _seed_generalization(
            h.store, base_confs=(0.9, 0.8, 0.7),
            base_status=("active", "dormant", "dormant"),
        )
        g = h.store.get(gid)
        _bounded, moot = h.worker._apply_meta_rules(g, 0.9)
        self.assertTrue(moot)

    def test_cascade_demotes_when_below_two_active(self) -> None:
        h = _lifecycle_harness()
        gid, _ = _seed_generalization(
            h.store, base_confs=(0.9, 0.9),
            base_status=("active", "retired"),
        )
        h.worker.run()
        self.assertEqual(h.store.get(gid).status, "dormant")


# ── surfacing: prefer abstraction, suppress children ─────────────────────


class _FakeEdge:
    def __init__(self, src_id) -> None:
        self.src_type = "concept"
        self.src_id = str(src_id)


class _FakeStore:
    def __init__(self, children_by_parent) -> None:
        self._children = children_by_parent

    def evidence_of(self, cid):
        return [_FakeEdge(c) for c in self._children.get(int(cid), [])]


def _cand(cid, *, kind="identity", confidence=0.8, pinned=False):
    payload = SimpleNamespace(concept_id=cid, kind=kind, confidence=confidence)
    return ContextCandidate(
        source="concept", relevance=confidence, tokens=10, order=cid,
        payload=payload, key=f"k{cid}", pinned=pinned,
    )


def _host(*, store, enabled=True, bar=0.7):
    return SimpleNamespace(
        _memory_settings=SimpleNamespace(
            generalization_suppress_children_enabled=enabled,
            generalization_parent_min_confidence=bar,
        ),
        _concept_store=store,
    )


class SuppressChildrenTests(unittest.TestCase):
    def _suppress(self, host, cands):
        return InnerLifePart1Mixin._suppress_generalized_children(host, cands)

    def test_children_suppressed_parent_kept(self) -> None:
        store = _FakeStore({10: [1, 2]})
        host = _host(store=store)
        cands = [
            _cand(10, kind="generalization", confidence=0.9, pinned=True),
            _cand(1), _cand(2), _cand(3),
        ]
        out = self._suppress(host, cands)
        ids = {int(c.payload.concept_id) for c in out}
        self.assertEqual(ids, {10, 3})  # children 1,2 dropped; parent + 3 stay

    def test_disabled_flag_no_suppression(self) -> None:
        store = _FakeStore({10: [1, 2]})
        host = _host(store=store, enabled=False)
        cands = [_cand(10, kind="generalization", confidence=0.9),
                 _cand(1), _cand(2)]
        out = self._suppress(host, cands)
        self.assertEqual(len(out), 3)

    def test_parent_below_bar_no_suppression(self) -> None:
        store = _FakeStore({10: [1, 2]})
        host = _host(store=store, bar=0.7)
        cands = [_cand(10, kind="generalization", confidence=0.6),
                 _cand(1), _cand(2)]
        out = self._suppress(host, cands)
        self.assertEqual(len(out), 3)

    def test_no_generalization_no_op(self) -> None:
        store = _FakeStore({})
        host = _host(store=store)
        cands = [_cand(1), _cand(2)]
        out = self._suppress(host, cands)
        self.assertEqual(len(out), 2)

    def test_descendant_cone_hides_grandchildren(self) -> None:
        # L2 #20 covers L1 #10, which covers bases 1 and 2. All three drop;
        # the L2 itself survives.
        store = _FakeStore({20: [10], 10: [1, 2]})
        host = _host(store=store)
        cands = [
            _cand(20, kind="generalization", confidence=0.9, pinned=True),
            _cand(10, kind="generalization", confidence=0.85),
            _cand(1), _cand(2), _cand(3),
        ]
        out = self._suppress(host, cands)
        ids = {int(c.payload.concept_id) for c in out}
        self.assertEqual(ids, {20, 3})


# ── L46 stacking ──────────────────────────────────────────────────────────


def _add_l1_gen(
    store, *, subject, label, child_ids, confidence=0.85, embedding=None,
) -> int:
    now = datetime.now(_UTC).isoformat()
    gid = store.add(Concept(
        label=label, kind="generalization", subject=subject,
        evidence_model="meta", status="active", confidence=confidence,
        plasticity=0.25, evidence_count=len(child_ids),
        distinct_source_count=len(child_ids),
        first_evidence_at=now, last_reinforced_at=now,
        embedding=(
            embedding if embedding is not None
            else np.zeros(0, dtype=np.float32)
        ),
    ))
    for cid in child_ids:
        store.add_edge(ConceptEdge(
            src_type="concept", src_id=str(cid),
            dst_type="concept", dst_id=str(gid), relation="evidence",
            polarity=1, strength=1.0,
        ))
    return gid


def _stack_responder(ids):
    def responder(system, user):
        if "already-named" not in system.lower():
            return {"concepts": []}
        if "FIRST PERSON" in system:
            return {"concepts": []}
        return {"concepts": [{
            "label": "He treats making as a way of caring",
            "evidence_concept_ids": list(ids),
            "confidence": 0.8,
        }]}
    return responder


class StackingPassTests(unittest.TestCase):
    def _harness(self, responder, agent=None):
        return WorkerHarness(
            responder, clusters=[], self_memories=[], agent=agent,
        )

    def test_stacking_mints_l2_over_two_l1s(self) -> None:
        ids: list[int] = []
        h = self._harness(_stack_responder(ids))
        a = _add_active(h.store, subject="user", kind="identity", label="serv")
        b = _add_active(h.store, subject="user", kind="identity", label="wood")
        c = _add_active(h.store, subject="user", kind="identity", label="care")
        g1 = _add_l1_gen(
            h.store, subject="user", label="builds things that last",
            child_ids=[a, b],
        )
        g2 = _add_l1_gen(
            h.store, subject="user", label="shows care by paying attention",
            child_ids=[c],
        )
        ids[:] = [g1, g2]
        self.assertEqual(meta_depth(h.store, g1), 1)
        stats = h.worker.run()
        self.assertTrue(stats["generalization_l2_dirty"])
        gens = h.store.list_by(subject="user", kind="generalization")
        l2s = [g for g in gens if meta_depth(h.store, g.concept_id) == 2]
        self.assertEqual(len(l2s), 1)
        self.assertEqual(l2s[0].status, "candidate")
        ev = {e.src_id for e in h.store.evidence_of(l2s[0].concept_id)}
        self.assertEqual(ev, {str(g1), str(g2)})

    def test_stacking_disabled_skips_l2_keeps_l1(self) -> None:
        ids: list[int] = []
        agent = SimpleNamespace(
            concepts_enabled=True,
            concept_synthesis_enabled=True,
            generalization_synthesis_enabled=True,
            generalization_stacking_enabled=False,
        )
        h = self._harness(_stack_responder(ids), agent=agent)
        a = _add_active(h.store, subject="user", kind="identity", label="a")
        b = _add_active(h.store, subject="user", kind="identity", label="b")
        g1 = _add_l1_gen(
            h.store, subject="user", label="g-one", child_ids=[a],
        )
        g2 = _add_l1_gen(
            h.store, subject="user", label="g-two", child_ids=[b],
        )
        ids[:] = [g1, g2]
        stats = h.worker.run()
        self.assertFalse(stats.get("generalization_l2_dirty"))
        l2s = [
            g for g in h.store.list_by(kind="generalization")
            if meta_depth(h.store, g.concept_id) == 2
        ]
        self.assertEqual(l2s, [])

    def test_l2_pool_excludes_tensions_and_l2s(self) -> None:
        seen: dict[str, str] = {}

        def responder(system, user):
            if "already-named" in system.lower() and "FIRST PERSON" not in system:
                seen["l2"] = user
            return {"concepts": []}

        h = self._harness(responder)
        a = _add_active(h.store, subject="user", kind="identity", label="a")
        b = _add_active(h.store, subject="user", kind="identity", label="b")
        g1 = _add_l1_gen(
            h.store, subject="user", label="theme-one", child_ids=[a],
        )
        g2 = _add_l1_gen(
            h.store, subject="user", label="theme-two", child_ids=[b],
        )
        now = datetime.now(_UTC).isoformat()
        h.store.add(Concept(
            label="TENSION-SENTINEL", kind="tension", subject="user",
            evidence_model="meta", status="active", confidence=0.9,
            plasticity=0.35, evidence_count=2, distinct_source_count=2,
            first_evidence_at=now, last_reinforced_at=now,
        ))
        l2_id = _add_l1_gen(
            h.store, subject="user", label="VIEW-SENTINEL",
            child_ids=[g1, g2],
        )
        # VIEW-SENTINEL cites two L1s so its depth is 2 -- must not be offered.
        self.assertEqual(meta_depth(h.store, l2_id), 2)
        h.worker.run()
        self.assertIn("l2", seen)
        pool = seen["l2"].split("ALREADY-KNOWN")[0]
        self.assertIn("theme-one", pool)
        self.assertIn("theme-two", pool)
        self.assertNotIn("TENSION-SENTINEL", pool)
        self.assertNotIn("VIEW-SENTINEL", pool)

    def test_filter_allows_depth1_for_generalization_not_tension(self) -> None:
        h = self._harness(lambda s, u: {"concepts": []})
        a = _add_active(h.store, subject="user", kind="identity", label="a")
        g1 = _add_l1_gen(
            h.store, subject="user", label="theme", child_ids=[a],
        )
        kept_gen = h.worker._filter_meta_evidence(
            [("concept", str(g1)), ("concept", str(a))],
            kind="generalization",
        )
        self.assertEqual(
            set(kept_gen),
            {("concept", str(g1)), ("concept", str(a))},
        )
        kept_ten = h.worker._filter_meta_evidence(
            [("concept", str(g1)), ("concept", str(a))],
            kind="tension",
        )
        self.assertEqual(kept_ten, [("concept", str(a))])

    def test_filter_drops_depth2_as_child(self) -> None:
        h = self._harness(lambda s, u: {"concepts": []})
        a = _add_active(h.store, subject="user", kind="identity", label="a")
        b = _add_active(h.store, subject="user", kind="identity", label="b")
        g1 = _add_l1_gen(
            h.store, subject="user", label="t1", child_ids=[a],
        )
        g2 = _add_l1_gen(
            h.store, subject="user", label="t2", child_ids=[b],
        )
        view = _add_l1_gen(
            h.store, subject="user", label="view", child_ids=[g1, g2],
        )
        self.assertEqual(meta_depth(h.store, view), 2)
        kept = h.worker._filter_meta_evidence(
            [("concept", str(view)), ("concept", str(g1))],
            kind="generalization",
        )
        self.assertEqual(kept, [("concept", str(g1))])

    def test_cycle_on_reinforce_dropped(self) -> None:
        h = self._harness(lambda s, u: {"concepts": []})
        a = _add_active(h.store, subject="user", kind="identity", label="a")
        b = _add_active(h.store, subject="user", kind="identity", label="b")
        g1 = _add_l1_gen(
            h.store, subject="user", label="t1", child_ids=[a],
        )
        g2 = _add_l1_gen(
            h.store, subject="user", label="t2", child_ids=[b],
        )
        view = _add_l1_gen(
            h.store, subject="user", label="view", child_ids=[g1, g2],
        )
        # Citing the view as a child of one of its own L1s would cycle.
        kept = h.worker._filter_meta_evidence(
            [("concept", str(view)), ("concept", str(g2))],
            kind="generalization",
            parent_id=g1,
        )
        self.assertEqual(kept, [("concept", str(g2))])

    def test_child_rename_drops_proposal(self) -> None:
        h = self._harness(lambda s, u: {"concepts": []})
        a = _add_active(h.store, subject="user", kind="identity", label="a")
        b = _add_active(h.store, subject="user", kind="identity", label="b")
        child_vec = h.worker._embedder.embed("builds things that last")
        g1 = _add_l1_gen(
            h.store, subject="user", label="builds things that last",
            child_ids=[a], embedding=child_vec,
        )
        g2 = _add_l1_gen(
            h.store, subject="user", label="shows care",
            child_ids=[b],
            embedding=h.worker._embedder.embed("shows care"),
        )
        from app.core.concepts.proposers.base import CandidateProposal

        proposal = CandidateProposal(
            label="builds things that last",
            rationale="",
            confidence=0.8,
            evidence=[("concept", str(g1)), ("concept", str(g2))],
            kind="generalization",
            subject="user",
            evidence_model="meta",
        )
        before = h.store.count()
        h.worker._persist(proposal, {"added": 0, "reinforced": 0,
                                     "by_subject": {}})
        self.assertEqual(h.store.count(), before)

    def test_l1_diversity_skips_label_twins(self) -> None:
        h = self._harness(lambda s, u: {"concepts": []})
        twin_vec = h.worker._embedder.embed("twin ritual")
        other_vec = h.worker._embedder.embed("totally different theme")
        now = datetime.now(_UTC).isoformat()
        for label, vec, conf in (
            ("twin ritual one", twin_vec, 0.95),
            ("twin ritual two", twin_vec, 0.90),
            ("other theme", other_vec, 0.80),
        ):
            h.store.add(Concept(
                label=label, kind="ritual", subject="user",
                evidence_model="set", status="active", confidence=conf,
                plasticity=0.4, evidence_count=2, distinct_source_count=2,
                first_evidence_at=now, last_reinforced_at=now,
                embedding=vec,
            ))
        pool = h.worker._active_generalization_bases("user", 24)
        labels = {b.label for b in pool}
        self.assertIn("twin ritual one", labels)
        self.assertNotIn("twin ritual two", labels)
        self.assertIn("other theme", labels)
        tension = h.worker._active_tension_bases("user", 24)
        tension_labels = {b.label for b in tension}
        self.assertIn("twin ritual two", tension_labels)

    def test_l2_promotion_age_floor(self) -> None:
        from tests.test_concept_lifecycle_worker import (
            _harness as _life,
            _seed_engaged_days,
        )

        now = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
        h = _life(graph_mature=lambda: True)
        a = _add_active(h.store, subject="user", kind="identity", label="a")
        b = _add_active(h.store, subject="user", kind="identity", label="b")
        g1 = _add_l1_gen(
            h.store, subject="user", label="t1", child_ids=[a],
        )
        g2 = _add_l1_gen(
            h.store, subject="user", label="t2", child_ids=[b],
        )
        view_id = h.store.add(Concept(
            label="view", kind="generalization", subject="user",
            evidence_model="meta", status="candidate", confidence=0.85,
            plasticity=0.25, evidence_count=2, distinct_source_count=2,
            first_evidence_at=(now - timedelta(days=10)).isoformat(),
            first_evidence_engagement=0.0,
            last_reinforced_at=(now - timedelta(days=10)).isoformat(),
        ))
        for cid in (g1, g2):
            h.store.add_edge(ConceptEdge(
                src_type="concept", src_id=str(cid),
                dst_type="concept", dst_id=str(view_id), relation="evidence",
                polarity=1, strength=1.0,
            ))
        view = h.store.get(view_id)
        self.assertEqual(meta_depth(h.store, view_id), 2)
        _seed_engaged_days(h, 4.0)
        self.assertFalse(h.worker._gate(view, now, 0.85))
        _seed_engaged_days(h, 8.0)
        self.assertTrue(h.worker._gate(view, now, 0.85))


class StackingPromptTests(unittest.TestCase):
    def test_stacking_flag_uses_view_wording(self) -> None:
        captured: dict[str, str] = {}

        def responder(system, user):
            captured["system"] = system
            captured["user"] = user
            return {"concepts": []}

        ctx, _ = _ctx(responder)
        propose_generalization_user(
            ctx,
            concepts=_bases(
                (1, "user", "generalization", "theme-a"),
                (2, "user", "generalization", "theme-b"),
            ),
            stacking=True,
        )
        self.assertIn("already-named", captured["system"].lower())
        self.assertIn("ACTIVE ABSTRACTIONS", captured["user"])
        self.assertNotIn("ACTIVE CONCEPTS (each already an established", captured["user"])


if __name__ == "__main__":
    unittest.main()
