"""Tests for the L12 tension concepts -- the first *meta* (concept-over-concept)
kind.

Covers the full vertical slice:

* the ``tension_evidence_gate`` -- the 2-source floor OVERRIDE (a tension is
  exactly a pair, so a higher caller ``min_sources`` neither raises nor lowers
  it) plus the age/confidence floors that still apply,
* the ``tension`` kind registration (meta model, medium-fluid plasticity, NOT on
  the core lane),
* the shared ``propose_tension`` composition rule (exactly two distinct base
  ids), reinforcement, and the user/relationship/aiko voices,
* the ``_run_tension_pass`` worker pass (user + relationship), the pool<2 and
  disabled short-circuits, dirty-tracking no-op, the meta depth cap (only
  non-meta actives offered), and the persist-time ``_filter_meta_evidence``
  guard,
* the L3 meta rules 2 + 3 (confidence bounding + cascade-to-dormant when a base
  leaves ``active``),
* the ``TensionCueWorker`` producer + ``_render_tension_block`` consumer.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app.core.concepts.concept_diets import diet_for
from app.core.concepts.concept_kinds import (
    DEFAULT_SURFACE_WEIGHTS,
    core_lane_kinds,
    get_kind,
    renders_in_static_block,
)
from app.core.concepts.concept_lifecycle import tension_evidence_gate
from app.core.concepts.concept_store import Concept, ConceptEdge
from app.core.concepts.proposers import ExistingConcept, ProposerContext
from app.core.concepts.proposers import TensionBase
from app.core.concepts.proposers.tension_aiko import propose_tension_aiko
from app.core.concepts.proposers.tension_relationship import (
    propose_tension_relationship,
)
from app.core.concepts.proposers.tension_user import propose_tension_user
from app.core.proactive import tension_cue as _tc
from app.core.proactive.tension_cue_worker import TensionCueWorker
from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin

from tests.test_concept_synthesis_worker import WorkerHarness

_UTC = timezone.utc


# ── gate + registry ─────────────────────────────────────────────────────


class GateAndRegistryTests(unittest.TestCase):
    def test_two_sources_promote(self) -> None:
        self.assertTrue(
            tension_evidence_gate(
                distinct_source_count=2, age_days=2.0, confidence=0.7,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )

    def test_one_source_blocked(self) -> None:
        # A tension without both sides is not a tension -- the 2-floor bites
        # even when the caller passes a lower min_sources.
        self.assertFalse(
            tension_evidence_gate(
                distinct_source_count=1, age_days=2.0, confidence=0.9,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )

    def test_source_floor_is_fixed_not_maxed(self) -> None:
        # A higher caller min_sources does NOT raise the fixed-arity floor.
        self.assertTrue(
            tension_evidence_gate(
                distinct_source_count=2, age_days=2.0, confidence=0.7,
                min_sources=5, min_age_days=0.0, min_confidence=0.0,
            )
        )

    def test_age_and_confidence_floors_still_bite(self) -> None:
        self.assertFalse(  # age below the 1.0d floor
            tension_evidence_gate(
                distinct_source_count=2, age_days=0.5, confidence=0.9,
                min_sources=2, min_age_days=0.0, min_confidence=0.0,
            )
        )
        self.assertFalse(  # confidence below the 0.6 floor
            tension_evidence_gate(
                distinct_source_count=2, age_days=2.0, confidence=0.5,
                min_sources=2, min_age_days=0.0, min_confidence=0.0,
            )
        )

    def test_caller_confidence_floor_still_applies_when_higher(self) -> None:
        self.assertFalse(
            tension_evidence_gate(
                distinct_source_count=2, age_days=2.0, confidence=0.65,
                min_sources=2, min_age_days=0.0, min_confidence=0.8,
            )
        )

    def test_registry(self) -> None:
        kind = get_kind("tension")
        self.assertIsNotNone(kind)
        self.assertEqual(kind.evidence_model, "meta")
        self.assertAlmostEqual(kind.plasticity_default, 0.35)
        self.assertIs(kind.promotion_gate, tension_evidence_gate)

    def test_not_on_core_lane(self) -> None:
        self.assertNotIn("tension", {k.name for k in core_lane_kinds()})
        self.assertFalse(get_kind("tension").core_always_on)

    def test_it_declares_itself_out_of_the_static_render(self) -> None:
        # The T3 carve-out is declarative, so the flex lane, the hypothesis
        # lane and the L28 openness reserve all agree with the renderer
        # without any of them naming the kind.
        self.assertFalse(get_kind("tension").static_render)
        self.assertFalse(renders_in_static_block("tension"))
        self.assertTrue(renders_in_static_block("aspiration"))

    def test_surface_weights_opt_in(self) -> None:
        w = get_kind("tension").surface_weights
        self.assertNotEqual(w, DEFAULT_SURFACE_WEIGHTS)
        self.assertGreater(w.activation, 0.0)


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
    def test_exactly_two_ids_accepted(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "He values rest but rarely takes it",
                "evidence_concept_ids": [1, 2],
                "confidence": 0.7,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_tension_user(
            ctx,
            concepts=_bases(
                (1, "user", "value", "values rest"),
                (2, "user", "identity", "always building"),
            ),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "tension")
        self.assertEqual(out[0].subject, "user")
        self.assertEqual(out[0].evidence_model, "meta")
        self.assertEqual(
            out[0].evidence, [("concept", "1"), ("concept", "2")]
        )

    def test_single_id_dropped(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "half a tension",
                "evidence_concept_ids": [1],
                "confidence": 0.9,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_tension_user(
            ctx,
            concepts=_bases(
                (1, "user", "value", "values rest"),
                (2, "user", "identity", "always building"),
            ),
        )
        self.assertEqual(out, [])

    def test_three_ids_dropped(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "a three-way muddle",
                "evidence_concept_ids": [1, 2, 3],
                "confidence": 0.9,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_tension_user(
            ctx,
            concepts=_bases(
                (1, "user", "value", "a"),
                (2, "user", "identity", "b"),
                (3, "user", "affective", "c"),
            ),
        )
        self.assertEqual(out, [])

    def test_unknown_id_filtered_then_dropped(self) -> None:
        # One cited id isn't in the offered pool -> only one valid -> dropped.
        def responder(system, user):
            return {"concepts": [{
                "label": "x",
                "evidence_concept_ids": [1, 999],
                "confidence": 0.9,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_tension_user(
            ctx, concepts=_bases((1, "user", "value", "a")),
        )
        self.assertEqual(out, [])

    def test_reinforce_by_id(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "reinforces_id": 42,
                "evidence_concept_ids": [1, 2],
                "rationale": "still true",
            }]}

        ctx, _ = _ctx(responder)
        out = propose_tension_user(
            ctx,
            concepts=_bases(
                (1, "user", "value", "a"), (2, "user", "identity", "b"),
            ),
            existing=[ExistingConcept(id=42, label="rest vs building")],
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].reinforces_id, 42)
        self.assertEqual(out[0].label, "")
        self.assertEqual(out[0].evidence_model, "meta")

    def test_empty_pool_returns_nothing(self) -> None:
        ctx, calls = _ctx(lambda s, u: {"concepts": []})
        out = propose_tension_user(ctx, concepts=[])
        self.assertEqual(out, [])
        self.assertNotIn("system", calls)  # LLM never called

    def test_user_voice(self) -> None:
        ctx, calls = _ctx(lambda s, u: {"concepts": []})
        propose_tension_user(ctx, concepts=_bases((1, "user", "value", "a")))
        self.assertIn("Jacob", calls["system"])
        self.assertNotIn("HERSELF", calls["system"])

    def test_relationship_voice_cross_subject(self) -> None:
        ctx, calls = _ctx(lambda s, u: {"concepts": []})
        propose_tension_relationship(
            ctx,
            concepts=_bases(
                (1, "user", "value", "a"), (2, "aiko", "value", "b"),
            ),
        )
        self.assertIn("she holds herself", calls["system"])
        self.assertIn("cross-subject", calls["system"])

    def test_aiko_voice_first_person(self) -> None:
        ctx, calls = _ctx(lambda s, u: {"concepts": []})
        propose_tension_aiko(ctx, concepts=_bases((1, "aiko", "value", "a")))
        self.assertIn("HERSELF", calls["system"])
        self.assertIn("FIRST PERSON", calls["system"])


class BoundaryClashShapeTests(unittest.TestCase):
    """L18d: each tension prompt advertises the boundary-clash shape, and a
    boundary-involving pair composes into a tension exactly like any other."""

    def test_user_prompt_names_boundary_shape(self) -> None:
        ctx, calls = _ctx(lambda s, u: {"concepts": []})
        propose_tension_user(ctx, concepts=_bases((1, "user", "value", "a")))
        self.assertIn("boundary", calls["system"])

    def test_aiko_prompt_names_boundary_shape(self) -> None:
        ctx, calls = _ctx(lambda s, u: {"concepts": []})
        propose_tension_aiko(ctx, concepts=_bases((1, "aiko", "value", "a")))
        self.assertIn("boundary", calls["system"])

    def test_relationship_prompt_names_boundary_shape(self) -> None:
        ctx, calls = _ctx(lambda s, u: {"concepts": []})
        propose_tension_relationship(
            ctx,
            concepts=_bases(
                (1, "user", "value", "a"), (2, "aiko", "value", "b"),
            ),
        )
        self.assertIn("boundary", calls["system"])

    def test_boundary_vs_value_pair_composes(self) -> None:
        # A boundary base + a value base cited together yields one tension meta
        # with both as ("concept", id) evidence -- no special-casing by kind.
        def responder(system, user):
            return {"concepts": [{
                "label": "He'd rather not be pushed to decide, yet values "
                         "being decisive",
                "evidence_concept_ids": [1, 2],
                "confidence": 0.7,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_tension_user(
            ctx,
            concepts=_bases(
                (1, "user", "boundary", "don't push him to decide on the spot"),
                (2, "user", "value", "values being decisive"),
            ),
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "tension")
        self.assertEqual(out[0].evidence_model, "meta")
        self.assertEqual(
            out[0].evidence, [("concept", "1"), ("concept", "2")]
        )


# ── worker tension pass ──────────────────────────────────────────────────


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


class TensionPassTests(unittest.TestCase):
    def _harness(self, responder):
        return WorkerHarness(responder, clusters=[], self_memories=[])

    def test_user_pass_creates_meta_tension(self) -> None:
        ids: list[int] = []

        def responder(system, user):
            if "tension" not in system.lower() or "HERSELF" in system:
                return {"concepts": []}
            if "she holds herself" in system:  # relationship lens
                return {"concepts": []}
            return {"concepts": [{
                "label": "He values rest but keeps working late",
                "evidence_concept_ids": ids,
                "confidence": 0.7,
            }]}

        h = self._harness(responder)
        a = _add_active(h.store, subject="user", kind="value", label="rest")
        b = _add_active(
            h.store, subject="user", kind="identity", label="always building"
        )
        ids[:] = [a, b]

        stats = h.worker.run()
        self.assertTrue(stats["tension_dirty"])
        out = h.store.list_by(subject="user", kind="tension")
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c.evidence_model, "meta")
        self.assertEqual(c.status, "candidate")
        self.assertEqual(c.distinct_source_count, 2)
        ev = sorted(
            (e.src_type, e.src_id) for e in h.store.evidence_of(c.concept_id)
        )
        self.assertEqual(ev, [("concept", str(a)), ("concept", str(b))])
        # The base -> tension edges make the tension a dependent of each base.
        self.assertIn(c.concept_id, h.store.dependents_of(a))

    def test_pool_below_two_is_noop(self) -> None:
        h = self._harness(lambda s, u: {"concepts": []})
        _add_active(h.store, subject="user", kind="value", label="only one")
        stats = h.worker.run()
        self.assertFalse(stats["tension_dirty"])
        self.assertEqual(h.store.list_by(kind="tension"), [])

    def test_relationship_needs_both_subjects(self) -> None:
        # Two user actives but no aiko -> relationship lens can't pair -> noop
        # for relationship (the user lens still forms its own tension).
        def responder(system, user):
            return {"concepts": []}

        h = self._harness(responder)
        _add_active(h.store, subject="user", kind="value", label="a")
        _add_active(h.store, subject="user", kind="identity", label="b")
        h.worker.run()
        self.assertEqual(h.store.list_by(subject="relationship", kind="tension"), [])

    def test_relationship_pairs_cross_subject(self) -> None:
        ids: list[int] = []

        def responder(system, user):
            if "she holds herself" not in system:
                return {"concepts": []}
            return {"concepts": [{
                "label": "He wants bluntness; she softens hard truths",
                "evidence_concept_ids": ids,
                "confidence": 0.7,
            }]}

        h = self._harness(responder)
        u = _add_active(h.store, subject="user", kind="value", label="blunt")
        a = _add_active(h.store, subject="aiko", kind="value", label="gentle")
        ids[:] = [u, a]
        h.worker.run()
        out = h.store.list_by(subject="relationship", kind="tension")
        self.assertEqual(len(out), 1)

    def test_clean_rerun_is_noop(self) -> None:
        ids: list[int] = []

        def responder(system, user):
            if "tension" not in system.lower() or "HERSELF" in system:
                return {"concepts": []}
            if "she holds herself" in system:
                return {"concepts": []}
            return {"concepts": [{
                "label": "rest vs building",
                "evidence_concept_ids": ids,
                "confidence": 0.7,
            }]}

        h = self._harness(responder)
        a = _add_active(h.store, subject="user", kind="value", label="rest")
        b = _add_active(h.store, subject="user", kind="identity", label="build")
        ids[:] = [a, b]
        h.worker.run()
        before = h.store.count()
        stats = h.worker.run()
        self.assertFalse(stats["tension_dirty"])
        self.assertEqual(h.store.count(), before)

    def test_disabled_switch_skips_pass(self) -> None:
        agent = SimpleNamespace(
            concepts_enabled=True,
            concept_synthesis_enabled=True,
            tension_synthesis_enabled=False,
        )
        h = WorkerHarness(
            lambda s, u: {"concepts": [{
                "label": "x", "evidence_concept_ids": [1, 2], "confidence": 0.9,
            }]},
            clusters=[], self_memories=[], agent=agent,
        )
        _add_active(h.store, subject="user", kind="value", label="a")
        _add_active(h.store, subject="user", kind="identity", label="b")
        stats = h.worker.run()
        self.assertFalse(stats["tension_dirty"])
        self.assertEqual(h.store.list_by(kind="tension"), [])

    def test_meta_depth_cap_excludes_meta_from_pool(self) -> None:
        # An active tension (meta) must never be offered as a base -> its label
        # must not appear in the proposer prompt.
        seen: dict[str, str] = {}

        def responder(system, user):
            if "tension" in system.lower() and "HERSELF" not in system \
                    and "she holds herself" not in system:
                seen["user"] = user
            return {"concepts": []}

        h = self._harness(responder)
        _add_active(h.store, subject="user", kind="value", label="rest-base")
        _add_active(h.store, subject="user", kind="identity", label="build-base")
        # A pre-existing active *meta* tension.
        h.store.add(
            Concept(
                label="META-SENTINEL", kind="tension", subject="user",
                evidence_model="meta", status="active", confidence=0.8,
                plasticity=0.35, evidence_count=2, distinct_source_count=2,
                first_evidence_at=datetime.now(_UTC).isoformat(),
                last_reinforced_at=datetime.now(_UTC).isoformat(),
            )
        )
        h.worker.run()
        self.assertIn("user", seen)
        # The meta tension may appear in the ALREADY-KNOWN TENSIONS existing
        # list (for reinforce/dedup), but MUST NOT be offered as a citeable
        # base in the ACTIVE CONCEPTS section (the depth cap).
        base_section = seen["user"].split("ALREADY-KNOWN TENSIONS")[0]
        self.assertIn("rest-base", base_section)
        self.assertNotIn("META-SENTINEL", base_section)

    def test_filter_meta_evidence_drops_meta_and_missing(self) -> None:
        h = self._harness(lambda s, u: {"concepts": []})
        base = _add_active(h.store, subject="user", kind="value", label="a")
        meta = h.store.add(
            Concept(
                label="m", kind="tension", subject="user",
                evidence_model="meta", status="active", confidence=0.8,
                plasticity=0.35, evidence_count=2, distinct_source_count=2,
                first_evidence_at=datetime.now(_UTC).isoformat(),
                last_reinforced_at=datetime.now(_UTC).isoformat(),
            )
        )
        kept = h.worker._filter_meta_evidence(
            [("concept", str(meta)), ("concept", str(base)),
             ("concept", "99999"), ("memory", "7")]
        )
        self.assertEqual(kept, [("concept", str(base)), ("memory", "7")])


# ── L3 meta rules (confidence bound + cascade) ───────────────────────────


def _lifecycle_harness():
    from tests.test_concept_lifecycle_worker import _harness

    return _harness()


def _seed_tension(store, *, base_confs, base_status=("active", "active"),
                  tension_conf=0.9):
    now = datetime.now(_UTC).isoformat()
    bids = []
    for i, (conf, status) in enumerate(zip(base_confs, base_status, strict=False)):
        bids.append(store.add(Concept(
            label=f"base{i}", kind="value", subject="user",
            evidence_model="set", status=status, confidence=conf,
            plasticity=0.4, evidence_count=2, distinct_source_count=2,
            first_evidence_at=now, last_reinforced_at=now,
            last_lifecycle_at=now,
        )))
    tid = store.add(Concept(
        label="tension", kind="tension", subject="user",
        evidence_model="meta", status="active", confidence=tension_conf,
        plasticity=0.35, evidence_count=2, distinct_source_count=2,
        first_evidence_at=now, last_reinforced_at=now, last_lifecycle_at=now,
    ))
    for bid in bids:
        store.add_edge(ConceptEdge(
            src_type="concept", src_id=str(bid),
            dst_type="concept", dst_id=str(tid), relation="evidence",
            polarity=1, strength=1.0,
        ))
    return tid, bids


class MetaRuleTests(unittest.TestCase):
    def test_confidence_bounded_to_min_base(self) -> None:
        h = _lifecycle_harness()
        tid, _ = _seed_tension(
            h.store, base_confs=(0.9, 0.3), tension_conf=0.95
        )
        tension = h.store.get(tid)
        bounded, moot = h.worker._apply_meta_rules(tension, 0.95)
        self.assertFalse(moot)
        self.assertLessEqual(bounded, 0.3 + 1e-9)

    def test_moot_when_a_base_not_active(self) -> None:
        h = _lifecycle_harness()
        tid, _ = _seed_tension(
            h.store, base_confs=(0.9, 0.8),
            base_status=("active", "dormant"),
        )
        tension = h.store.get(tid)
        _bounded, moot = h.worker._apply_meta_rules(tension, 0.9)
        self.assertTrue(moot)

    def test_cascade_demotes_tension_when_base_not_active(self) -> None:
        h = _lifecycle_harness()
        tid, _ = _seed_tension(
            h.store, base_confs=(0.9, 0.9),
            base_status=("active", "retired"),
        )
        h.worker.run()
        self.assertEqual(h.store.get(tid).status, "dormant")


# ── tension cue: producer + consumer ─────────────────────────────────────


class _FakeKv:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value


def _concept(cid, *, subject="user", label="rest vs building",
             confidence=0.8):
    return SimpleNamespace(
        concept_id=cid, subject=subject, label=label, confidence=confidence,
    )


class _FakeView:
    def __init__(self, concepts, *, enabled=True) -> None:
        self._concepts = concepts
        self.enabled = enabled
        self.kinds_asked: list[str | None] = []

    def core(self, *, kind=None, subject=None, min_confidence=0.0, limit=None):
        self.kinds_asked.append(kind)
        return [
            c for c in self._concepts
            if getattr(c, "subject", None) == subject
            and float(getattr(c, "confidence", 0.0)) >= float(min_confidence)
        ]


def _cue_worker(view, kv, **overrides):
    kwargs: dict[str, Any] = dict(
        kv_get=kv.get,
        kv_set=kv.set,
        view_provider=lambda: view,
        user_display_name_provider=lambda: "Jacob",
        min_confidence=0.6,
        cooldown_days=6.0,
    )
    kwargs.update(overrides)
    return TensionCueWorker(**kwargs)


class CueProducerTests(unittest.TestCase):
    def test_drafts_cue_for_active_tension(self) -> None:
        kv = _FakeKv()
        view = _FakeView([_concept(1)])
        out = _cue_worker(view, kv).run()
        self.assertEqual(out["drafted"], 1)
        ring = _tc.load_cues(kv.get)
        self.assertEqual(len(ring), 1)
        self.assertEqual(ring[-1]["concept_id"], 1)
        self.assertEqual(
            kv.store.get("tension_cue.last_signature"), _tc.signature(1)
        )
        # Drafting must NOT spend the per-tension cooldown — that is the
        # renderer's to spend, once the cue has actually been shown. See
        # ``_KV_PER_CONCEPT_PREFIX``: stamping it here silenced tensions
        # for six days that were superseded before they ever surfaced.
        self.assertNotIn(_tc.per_concept_cooldown_key(1), kv.store)

    def test_does_not_draft_over_an_unshown_cue(self) -> None:
        kv = _FakeKv()
        view = _FakeView([_concept(1, confidence=0.95), _concept(2)])
        self.assertEqual(_cue_worker(view, kv).run()["drafted"], 1)
        second = _cue_worker(view, kv).run()
        self.assertEqual(second["drafted"], 0)
        self.assertTrue(second.get("unconsumed"))
        self.assertEqual(len(_tc.load_cues(kv.get)), 1)

    def test_drafts_again_once_the_cue_has_surfaced(self) -> None:
        kv = _FakeKv()
        view = _FakeView([_concept(1, confidence=0.95), _concept(2)])
        _cue_worker(view, kv).run()
        kv.store[_tc.KV_LAST_SURFACED_AT] = _tc.load_cues(kv.get)[-1]["at"]
        self.assertEqual(_cue_worker(view, kv).run()["drafted"], 1)
        self.assertEqual(len(_tc.load_cues(kv.get)), 2)

    def test_a_stale_unshown_cue_stops_wedging_the_producer(self) -> None:
        # Otherwise a cue drafted right before a week of silence would block
        # the producer for that whole week.
        kv = _FakeKv()
        view = _FakeView([_concept(1, confidence=0.95), _concept(2)])
        _cue_worker(view, kv).run()
        ring = _tc.load_cues(kv.get)
        ring[-1]["at"] = (
            datetime.now(_UTC)
            - timedelta(hours=_tc.UNCONSUMED_CUE_MAX_AGE_HOURS + 1)
        ).isoformat()
        kv.store[_tc.TENSION_JOURNAL_KEY] = json.dumps(ring)
        self.assertEqual(_cue_worker(view, kv).run()["drafted"], 1)

    def test_demand_reports_no_pressure_while_a_cue_waits(self) -> None:
        kv = _FakeKv()
        view = _FakeView([_concept(1, confidence=0.95), _concept(2)])
        worker = _cue_worker(view, kv)
        worker.run()
        signal = worker.demand(now=datetime.now(_UTC), last_run_at=None)
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "unconsumed")

    def test_per_concept_cooldown_suppresses(self) -> None:
        kv = _FakeKv()
        kv.store[_tc.per_concept_cooldown_key(1)] = (
            datetime.now(_UTC) - timedelta(days=1)
        ).isoformat()
        out = _cue_worker(_FakeView([_concept(1)]), kv).run()
        self.assertEqual(out["drafted"], 0)

    def test_same_signature_suppressed(self) -> None:
        kv = _FakeKv()
        kv.store["tension_cue.last_signature"] = _tc.signature(1)
        out = _cue_worker(_FakeView([_concept(1)]), kv).run()
        self.assertEqual(out["drafted"], 0)
        self.assertTrue(out.get("no_candidate"))

    def test_the_repeat_guard_steps_down_instead_of_stalling(self) -> None:
        # The just-drafted tension is skipped rather than selected and then
        # rejected, so the next-best friction gets the tick. Without this the
        # top-ranked tension wins selection forever and nothing is drafted.
        kv = _FakeKv()
        kv.store["tension_cue.last_signature"] = _tc.signature(1)
        out = _cue_worker(
            _FakeView([_concept(1, confidence=0.95), _concept(2)]), kv
        ).run()
        self.assertEqual(out["drafted"], 1)
        self.assertEqual(out["concept_id"], 2)

    def test_disabled_switch(self) -> None:
        kv = _FakeKv()
        out = _cue_worker(
            _FakeView([_concept(1)]), kv, enabled_provider=lambda: False
        ).run()
        self.assertTrue(out.get("disabled"))
        self.assertEqual(_tc.load_cues(kv.get), [])

    def test_empty_when_none_active(self) -> None:
        kv = _FakeKv()
        out = _cue_worker(_FakeView([]), kv).run()
        self.assertEqual(out["drafted"], 0)
        self.assertTrue(out.get("no_active"))

    def test_force_next_bypasses_gates(self) -> None:
        kv = _FakeKv()
        kv.store[_tc.per_concept_cooldown_key(1)] = datetime.now(_UTC).isoformat()
        kv.store["tension_cue.last_signature"] = _tc.signature(1)
        w = _cue_worker(_FakeView([_concept(1)]), kv)
        w.force_next()
        self.assertEqual(w.run()["drafted"], 1)

    def test_most_confident_wins(self) -> None:
        kv = _FakeKv()
        view = _FakeView([
            _concept(1, confidence=0.7),
            _concept(2, confidence=0.95),
        ])
        self.assertEqual(_cue_worker(view, kv).run()["concept_id"], 2)

    def test_subjects_cover_relationship_and_aiko(self) -> None:
        kv = _FakeKv()
        view = _FakeView([_concept(7, subject="aiko")])
        # default subjects include aiko -> a lone aiko tension still drafts.
        self.assertEqual(_cue_worker(view, kv).run()["drafted"], 1)

    def test_the_kinds_read_come_from_the_declared_diet(self) -> None:
        # The read used to name "tension" inline, which left it out of the
        # one place that records who consumes concepts -- and out of the
        # audit that runs over that record.
        kv = _FakeKv()
        view = _FakeView([_concept(1)])
        _cue_worker(view, kv).run()
        self.assertEqual(
            set(view.kinds_asked), set(diet_for("tension_cue").kinds)
        )


class CueProducerDemandTests(unittest.TestCase):
    """The probe answers "would this run draft?" without drafting."""

    def _probe(self, worker: TensionCueWorker):
        return worker.demand(now=datetime.now(_UTC), last_run_at=None)

    def test_is_ready_is_the_switch_alone(self) -> None:
        kv = _FakeKv()
        w = _cue_worker(_FakeView([]), kv)
        now = datetime.now(_UTC)
        self.assertTrue(w.is_ready(now=now, last_run_at=None))
        self.assertTrue(
            w.is_ready(now=now, last_run_at=now - timedelta(seconds=30))
        )

    def test_an_active_tension_is_full_pressure(self) -> None:
        signal = self._probe(_cue_worker(_FakeView([_concept(1)]), _FakeKv()))
        self.assertEqual(signal.pressure, 1.0)
        self.assertEqual(signal.reason, "tension 1")
        self.assertFalse(signal.needs_llm)

    def test_nothing_active_is_no_pressure(self) -> None:
        signal = self._probe(_cue_worker(_FakeView([]), _FakeKv()))
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "no_active")

    def test_the_per_concept_cooldown_shows_up_as_no_pressure(self) -> None:
        kv = _FakeKv()
        kv.store[_tc.per_concept_cooldown_key(1)] = (
            datetime.now(_UTC) - timedelta(days=1)
        ).isoformat()
        w = _cue_worker(_FakeView([_concept(1)]), kv)
        self.assertEqual(self._probe(w).pressure, 0.0)

    def test_the_signature_watermark_shows_up_as_no_pressure(self) -> None:
        # The repeat guard now filters during selection, so the sole tension
        # is skipped and there is genuinely no candidate left.
        kv = _FakeKv()
        kv.store["tension_cue.last_signature"] = _tc.signature(1)
        signal = self._probe(_cue_worker(_FakeView([_concept(1)]), kv))
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "no_candidate")

    def test_disabled_is_no_pressure(self) -> None:
        w = _cue_worker(
            _FakeView([_concept(1)]), _FakeKv(),
            enabled_provider=lambda: False,
        )
        self.assertEqual(self._probe(w).reason, "disabled")

    def test_probing_writes_nothing_and_drafts_nothing(self) -> None:
        kv = _FakeKv()
        w = _cue_worker(_FakeView([_concept(1)]), kv)
        self._probe(w)
        self._probe(w)
        self.assertEqual(kv.store, {})
        self.assertEqual(_tc.load_cues(kv.get), [])

    def test_probing_does_not_eat_the_force_flag(self) -> None:
        kv = _FakeKv()
        kv.store[_tc.per_concept_cooldown_key(1)] = (
            datetime.now(_UTC).isoformat()
        )
        w = _cue_worker(_FakeView([_concept(1)]), kv)
        w.force_next()
        self.assertEqual(self._probe(w).pressure, 0.0)
        self.assertEqual(w.run()["drafted"], 1)


class _FakeChatDb:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self.store.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.store[key] = value


class _Host(InnerLifeProvidersMixin):
    def __init__(self, *, cues=None, force_next=False, enabled=True) -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(tension_cue_enabled=enabled),
        )
        self._chat_db = _FakeChatDb()
        if cues is not None:
            self._chat_db.store[_tc.TENSION_JOURNAL_KEY] = json.dumps(cues)
        self.debug_overrides.arm("tension_force_next", force_next)
        self.user_display_name = "Jacob"


def _cue(at="2026-06-13T18:55:00+00:00", subject="user",
         label="He values rest but rarely takes it"):
    return {"at": at, "concept_id": 1, "subject": subject, "label": label}


class CueConsumerTests(unittest.TestCase):
    def test_fires_and_advances_watermark(self) -> None:
        host = _Host(cues=[_cue()])
        out = host._render_tension_block()
        self.assertIn("He values rest but rarely takes it", out)
        self.assertIn("Jacob", out)
        self.assertEqual(
            host._chat_db.store.get("tension_cue.last_surfaced_at"),
            _cue()["at"],
        )

    def test_surfacing_is_what_spends_the_per_tension_cooldown(self) -> None:
        host = _Host(cues=[_cue()])
        self.assertNotIn(
            _tc.per_concept_cooldown_key(1), host._chat_db.store
        )
        host._render_tension_block()
        self.assertEqual(
            host._chat_db.store.get(_tc.per_concept_cooldown_key(1)),
            _cue()["at"],
        )

    def test_a_cue_that_does_not_render_spends_nothing(self) -> None:
        # Already-surfaced cue: the block returns empty and must not
        # re-stamp anything.
        host = _Host(cues=[_cue()])
        host._chat_db.store[_tc.KV_LAST_SURFACED_AT] = _cue()["at"]
        self.assertEqual(host._render_tension_block(), "")
        self.assertNotIn(
            _tc.per_concept_cooldown_key(1), host._chat_db.store
        )

    def test_relationship_cue_names_user(self) -> None:
        host = _Host(cues=[_cue(subject="relationship", label="pull between")])
        out = host._render_tension_block()
        self.assertIn("pull between", out)
        self.assertIn("Jacob", out)

    def test_aiko_cue_first_person(self) -> None:
        host = _Host(cues=[_cue(subject="aiko", label="two ways inside")])
        out = host._render_tension_block()
        self.assertIn("two ways inside", out)
        self.assertIn("yourself", out)

    def test_disabled_returns_empty(self) -> None:
        self.assertEqual(_Host(cues=[_cue()], enabled=False)._render_tension_block(), "")

    def test_empty_ring_silent(self) -> None:
        self.assertEqual(_Host(cues=[])._render_tension_block(), "")

    def test_already_surfaced_is_silent(self) -> None:
        host = _Host(cues=[_cue()])
        host._chat_db.store["tension_cue.last_surfaced_at"] = _cue()["at"]
        self.assertEqual(host._render_tension_block(), "")

    def test_force_next_bypasses_watermark(self) -> None:
        host = _Host(cues=[_cue()], force_next=True)
        host._chat_db.store["tension_cue.last_surfaced_at"] = _cue()["at"]
        out = host._render_tension_block()
        self.assertIn("He values rest", out)
        self.assertFalse(host.debug_overrides.peek("tension_force_next"))


if __name__ == "__main__":
    unittest.main()
