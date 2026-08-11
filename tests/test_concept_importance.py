"""Tests for L32 concept importance -- the second strength axis.

Importance is *derived*, which is the whole reason it is cheap, and also
the whole reason it is easy to break: nothing persists it, so a wrong
answer never shows up as bad data, only as a belief quietly ranked in the
wrong place. These cases pin the properties the design leans on rather
than the arithmetic:

- affect only ever **lifts**, so sparse data is a missing bonus and never
  a penalty (the majority of the live graph has no affect at all);
- the axis is **status-agnostic**, which is what lets the L30 hypothesis
  lane rank candidates with the same context;
- neutral importance and ``strength=0`` are both exact no-ops, so the
  feature can be turned off without re-tuning anything;
- the ``memory_id -> cluster_id`` bridge is followed correctly, since a
  cluster evidence edge names a *memory* and confusing the two id spaces
  would silently produce plausible-looking nonsense.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from app.core.concepts.cluster_affect import ClusterAffectState
from app.core.concepts.concept_importance import (
    IMPORTANCE_NEUTRAL,
    ImportanceContext,
    affect_charge,
    blend_importance,
    cluster_membership,
    importance_factor,
    kind_importance,
    membership_from_clusters,
    memory_ids_from_edges,
    state_charge,
)
from app.core.concepts.concept_kinds import CONCEPT_KINDS
from app.core.concepts.concept_store import Concept, ConceptEdge
from app.core.concepts.concept_surfacing import surface_score

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _state(
    valence: float = 0.0,
    arousal: float = 0.5,
    *,
    samples: int = 9,
    days_ago: float = 1.0,
    valence_samples: int | None = None,
) -> ClusterAffectState:
    return ClusterAffectState(
        valence=valence,
        arousal=arousal,
        samples=samples,
        updated_at=_iso(days_ago),
        valence_samples=samples if valence_samples is None else valence_samples,
    )


def _concept(
    *,
    cid: int = 1,
    kind: str = "identity",
    subject: str = "user",
    status: str = "active",
    confidence: float = 0.8,
) -> Concept:
    return Concept(
        label=f"concept {cid}",
        kind=kind,
        subject=subject,
        status=status,
        confidence=confidence,
        plasticity=0.4,
        evidence_count=3,
        distinct_source_count=3,
        created_at=_iso(30.0),
        embedding=np.zeros(0, dtype=np.float32),
        concept_id=cid,
    )


def _edge(
    src_id: object, *, src_type: str = "cluster", relation: str = "evidence",
) -> ConceptEdge:
    return ConceptEdge(
        src_type=src_type,
        src_id=str(src_id),
        dst_type="concept",
        dst_id="1",
        relation=relation,
    )


class KindPriorTests(unittest.TestCase):
    """The stakes ladder. These are judgement calls, so the tests pin the
    *ordering* the design argues for rather than the exact numbers."""

    def test_a_boundary_outranks_a_taste(self) -> None:
        self.assertGreater(
            kind_importance("boundary"), kind_importance("taste")
        )

    def test_behaviour_gating_kinds_lead_the_ladder(self) -> None:
        # A boundary gates what she does; a value is what he stands for.
        # Both must outrank every kind that is merely descriptive.
        for lesser in ("identity", "narrative", "ritual", "taste"):
            self.assertGreater(
                kind_importance("boundary"), kind_importance(lesser)
            )
            self.assertGreater(
                kind_importance("value"), kind_importance(lesser)
            )

    def test_an_unregistered_kind_is_neutral(self) -> None:
        # The kind axis is an open enum, so an unknown kind must land on
        # the value that leaves scoring untouched, not on zero.
        self.assertEqual(kind_importance("wormhole"), IMPORTANCE_NEUTRAL)
        self.assertEqual(kind_importance(""), IMPORTANCE_NEUTRAL)
        self.assertEqual(kind_importance(None), IMPORTANCE_NEUTRAL)

    def test_every_registered_kind_declares_a_usable_prior(self) -> None:
        for name, kind in CONCEPT_KINDS.items():
            with self.subTest(kind=name):
                self.assertGreaterEqual(kind.importance, 0.0, name)
                self.assertLessEqual(kind.importance, 1.0, name)


class ChargeTests(unittest.TestCase):
    """What makes a topic feel like it matters."""

    def test_neutral_valence_is_never_charged(self) -> None:
        # However keyed-up a topic is, if he feels nothing either way about
        # it there are no stakes -- arousal alone must not manufacture them.
        self.assertEqual(state_charge(_state(0.0, 0.9)), 0.0)

    def test_direction_does_not_matter_only_strength(self) -> None:
        # Loved and dreaded are equally high-stakes.
        self.assertEqual(
            state_charge(_state(0.8, 0.7)), state_charge(_state(-0.8, 0.7))
        )

    def test_a_quiet_but_strong_feeling_keeps_its_weight(self) -> None:
        # "Low and drained" is exactly the wellbeing case L32 exists for,
        # so it must not be zeroed the way a bare |v| * a product would.
        self.assertGreater(state_charge(_state(-0.8, 0.05)), 0.35)

    def test_arousal_scales_a_felt_topic(self) -> None:
        self.assertGreater(
            state_charge(_state(-0.8, 0.9)), state_charge(_state(-0.8, 0.1))
        )

    def test_the_loudest_cluster_wins_not_the_average(self) -> None:
        # A concept touching one charged topic and three flat ones is about
        # the charged one; a mean would wash it out.
        charged = _state(-0.9, 0.8)
        flat = [_state(0.0, 0.3) for _ in range(3)]
        self.assertEqual(
            affect_charge([charged] + flat, now=NOW),
            affect_charge([charged], now=NOW),
        )

    def test_a_thinly_sampled_cluster_is_ignored(self) -> None:
        thin = _state(-0.9, 0.9, samples=1)
        self.assertEqual(affect_charge([thin], min_samples=3, now=NOW), 0.0)

    def test_a_stale_cluster_is_ignored(self) -> None:
        # load_map prunes on write, so a read can see rows the sweep has
        # not reached yet.
        old = _state(-0.9, 0.9, days_ago=400.0)
        self.assertEqual(
            affect_charge([old], max_age_days=120.0, now=NOW), 0.0
        )

    def test_an_unparseable_stamp_reads_as_fresh(self) -> None:
        # Dropping a real signal over a junk timestamp is the worse error.
        junk = ClusterAffectState(
            valence=-0.9, arousal=0.9, samples=9, updated_at="not a date",
            valence_samples=9,
        )
        self.assertGreater(affect_charge([junk], now=NOW), 0.0)

    def test_a_cluster_sampled_only_for_arousal_carries_no_charge(self) -> None:
        # The charge is a valence magnitude, so arousal evidence alone
        # cannot earn one however much of it there is.
        loud = _state(-0.9, 0.9, samples=40, valence_samples=1)
        self.assertEqual(affect_charge([loud], min_samples=3, now=NOW), 0.0)

    def test_a_legacy_row_re_earns_its_charge(self) -> None:
        # Rows written before the axes were counted separately have no
        # valence sample count, and their valence was folded from unread
        # turns, so they should not lift anything until re-measured.
        legacy = ClusterAffectState(
            valence=-0.9, arousal=0.9, samples=40, updated_at=_iso(1.0)
        )
        self.assertEqual(affect_charge([legacy], min_samples=3, now=NOW), 0.0)

    def test_no_clusters_is_no_charge(self) -> None:
        self.assertEqual(affect_charge([], now=NOW), 0.0)


class BlendTests(unittest.TestCase):
    """The one property everything else depends on: affect only lifts."""

    def test_no_affect_leaves_the_prior_untouched(self) -> None:
        for prior in (0.0, 0.3, 0.5, 0.9, 1.0):
            with self.subTest(prior=prior):
                self.assertEqual(
                    blend_importance(prior, 0.0, lift=0.5), prior
                )

    def test_charge_never_lowers_a_prior(self) -> None:
        for prior in (0.0, 0.3, 0.5, 0.9):
            for charge in (0.0, 0.25, 0.5, 1.0):
                with self.subTest(prior=prior, charge=charge):
                    self.assertGreaterEqual(
                        blend_importance(prior, charge, lift=0.5), prior
                    )

    def test_the_lift_is_capped_below_a_kind_promotion(self) -> None:
        # A fully-charged taste must not overtake an uncharged boundary --
        # the emotional weather of a topic is a nudge, not a re-typing.
        hot_taste = blend_importance(
            kind_importance("taste"), 1.0, lift=0.5
        )
        cool_boundary = blend_importance(
            kind_importance("boundary"), 0.0, lift=0.5
        )
        self.assertLess(hot_taste, cool_boundary)

    def test_a_zero_lift_disables_affect_entirely(self) -> None:
        self.assertEqual(blend_importance(0.4, 1.0, lift=0.0), 0.4)

    def test_the_result_stays_in_range(self) -> None:
        self.assertLessEqual(blend_importance(1.0, 1.0, lift=1.0), 1.0)
        self.assertGreaterEqual(blend_importance(0.0, 0.0, lift=1.0), 0.0)


class FactorTests(unittest.TestCase):
    """The multiplier, and its two off switches."""

    def test_neutral_importance_is_exactly_one(self) -> None:
        self.assertEqual(
            importance_factor(IMPORTANCE_NEUTRAL, strength=0.4), 1.0
        )

    def test_zero_strength_is_exactly_one(self) -> None:
        for imp in (0.0, 0.3, 0.9, 1.0):
            with self.subTest(importance=imp):
                self.assertEqual(importance_factor(imp, strength=0.0), 1.0)

    def test_it_cuts_both_ways(self) -> None:
        self.assertGreater(importance_factor(0.9, strength=0.4), 1.0)
        self.assertLess(importance_factor(0.1, strength=0.4), 1.0)

    def test_a_full_strength_factor_cannot_go_negative(self) -> None:
        # strength is clamped to [0, 1] by the settings parser; at the
        # ceiling the worst case is a halving, never a sign flip.
        self.assertGreater(importance_factor(0.0, strength=1.0), 0.0)


class EdgeBridgeTests(unittest.TestCase):
    """A cluster evidence edge names a *memory*, not a cluster. Getting
    that wrong produces plausible-looking nonsense rather than an error,
    so it is worth its own tests."""

    def test_only_cluster_evidence_edges_count(self) -> None:
        edges = [
            _edge(10),
            _edge(11, src_type="memory"),
            _edge(12, src_type="concept"),
            _edge(13, relation="contradicts"),
        ]
        self.assertEqual(memory_ids_from_edges(edges), (10,))

    def test_a_non_numeric_src_id_is_skipped_not_fatal(self) -> None:
        self.assertEqual(memory_ids_from_edges([_edge("abc"), _edge(7)]), (7,))

    def test_membership_maps_every_member_to_its_cluster(self) -> None:
        clusters = [
            _FakeCluster(cluster_id=5, member_ids=(10, 11, 12)),
            _FakeCluster(cluster_id=6, member_ids=(20,)),
        ]
        self.assertEqual(
            membership_from_clusters(clusters),
            {10: 5, 11: 5, 12: 5, 20: 6},
        )

    def test_a_broken_cluster_row_does_not_lose_the_others(self) -> None:
        clusters = [
            _FakeCluster(cluster_id=5, member_ids=(10,)),
            object(),
            _FakeCluster(cluster_id=6, member_ids=(20,)),
        ]
        self.assertEqual(membership_from_clusters(clusters), {10: 5, 20: 6})

    def test_a_missing_graph_yields_an_empty_bridge(self) -> None:
        self.assertEqual(cluster_membership(None), {})

    def test_a_graph_that_raises_yields_an_empty_bridge(self) -> None:
        self.assertEqual(cluster_membership(_ExplodingGraph()), {})


class _FakeCluster:
    def __init__(self, *, cluster_id: int, member_ids: tuple[int, ...]) -> None:
        self.cluster_id = cluster_id
        self.member_ids = member_ids


class _ExplodingGraph:
    def topic_clusters(self):
        raise RuntimeError("no graph today")


class ContextTests(unittest.TestCase):
    """The per-turn bundle: the join, the subject split, and the cache."""

    def _ctx(self, **kwargs) -> ImportanceContext:
        base = {
            "affect_user": {"5": _state(-0.9, 0.9)},
            "affect_aiko": {"5": _state(0.1, 0.3)},
            "cluster_by_memory": {10: 5, 11: 5, 99: 7},
            "memory_ids_by_concept": {1: (10,)},
            "lift": 0.5,
            "now": NOW,
        }
        base.update(kwargs)
        return ImportanceContext(**base)

    def test_a_concept_picks_up_the_affect_of_its_grounding_cluster(self) -> None:
        ctx = self._ctx()
        detail = ctx.detail(_concept(kind="affective"))
        self.assertGreater(detail.charge, 0.0)
        self.assertGreater(detail.importance, detail.prior)
        self.assertEqual(detail.clusters, 1)

    def test_an_ungrounded_concept_falls_back_to_its_kind(self) -> None:
        ctx = self._ctx(memory_ids_by_concept={})
        detail = ctx.detail(_concept(kind="affective"))
        self.assertEqual(detail.charge, 0.0)
        self.assertEqual(detail.importance, kind_importance("affective"))
        self.assertEqual(detail.clusters, 0)

    def test_a_memory_outside_every_cluster_grounds_nothing(self) -> None:
        # A memory the graph has since dropped from clustering has no
        # cluster to borrow affect from; that is a missing lift, not a bug.
        ctx = self._ctx(memory_ids_by_concept={1: (12345,)})
        self.assertEqual(ctx.detail(_concept()).charge, 0.0)

    def test_a_cluster_with_no_affect_row_grounds_nothing(self) -> None:
        ctx = self._ctx(memory_ids_by_concept={1: (99,)})
        self.assertEqual(ctx.detail(_concept()).charge, 0.0)

    def test_the_subject_selects_which_affect_map_is_read(self) -> None:
        # Her feelings about a topic and his are different signals, and a
        # concept must only ever read the one about its own subject.
        ctx = self._ctx()
        his = ctx.detail(_concept(cid=1, kind="affective", subject="user"))
        ctx2 = self._ctx(memory_ids_by_concept={2: (10,)})
        hers = ctx2.detail(_concept(cid=2, kind="affective", subject="aiko"))
        self.assertGreater(his.charge, hers.charge)

    def test_duplicate_edges_onto_one_cluster_count_once(self) -> None:
        ctx = self._ctx(memory_ids_by_concept={1: (10, 11)})
        self.assertEqual(ctx.detail(_concept()).clusters, 1)

    def test_status_does_not_change_the_answer(self) -> None:
        # The property the L30 hypothesis lane depends on: a candidate is
        # scored exactly like an active row with the same inputs.
        ctx = self._ctx()
        active = ctx.detail(_concept(cid=1, status="active"))
        ctx2 = self._ctx()
        candidate = ctx2.detail(_concept(cid=1, status="candidate"))
        self.assertEqual(active.importance, candidate.importance)

    def test_confidence_does_not_change_the_answer(self) -> None:
        # Importance is the axis confidence is *not*; a coupling here would
        # collapse the two back into one number.
        ctx = self._ctx()
        low = ctx.detail(_concept(cid=1, confidence=0.1))
        ctx2 = self._ctx()
        high = ctx2.detail(_concept(cid=1, confidence=0.99))
        self.assertEqual(low.importance, high.importance)

    def test_an_empty_context_is_still_the_kind_prior(self) -> None:
        # Every map unavailable (a cold boot, a failed kv read) must give
        # the prior, not zero -- zero would suppress every concept at once.
        ctx = ImportanceContext(now=NOW)
        self.assertEqual(
            ctx.for_concept(_concept(kind="boundary")),
            kind_importance("boundary"),
        )

    def test_repeat_lookups_are_cached(self) -> None:
        ctx = self._ctx()
        c = _concept()
        self.assertIs(ctx.detail(c), ctx.detail(c))

    def test_an_unsaved_concept_is_scored_without_being_cached(self) -> None:
        # concept_id 0 means "not persisted"; it must still score, and must
        # not collide in the cache with every other unsaved concept.
        ctx = self._ctx()
        unsaved = _concept(cid=0, kind="boundary")
        self.assertEqual(
            ctx.for_concept(unsaved), kind_importance("boundary")
        )
        other = _concept(cid=0, kind="taste")
        self.assertEqual(ctx.for_concept(other), kind_importance("taste"))


class ScoreBlendTests(unittest.TestCase):
    """How importance reaches the ranking."""

    def _score(self, **kwargs) -> float:
        base = {"cosine": 0.6, "confidence": 0.8}
        base.update(kwargs)
        return surface_score(**base)

    def test_the_default_call_is_unchanged(self) -> None:
        # Every existing caller passes neither argument, so the pre-L32
        # score must survive byte for byte.
        self.assertEqual(self._score(), 0.6)

    def test_neutral_importance_changes_nothing(self) -> None:
        self.assertEqual(
            self._score(importance=0.5, importance_strength=0.4),
            self._score(),
        )

    def test_zero_strength_changes_nothing(self) -> None:
        self.assertEqual(
            self._score(importance=0.95, importance_strength=0.0),
            self._score(),
        )

    def test_a_weighty_belief_outranks_a_trivial_one_at_equal_cosine(
        self,
    ) -> None:
        # The headline behaviour: same topical match, different stakes.
        weighty = self._score(
            importance=kind_importance("boundary"), importance_strength=0.4,
        )
        trivial = self._score(
            importance=kind_importance("taste"), importance_strength=0.4,
        )
        self.assertGreater(weighty, trivial)

    def test_importance_can_overturn_a_small_cosine_lead(self) -> None:
        # Otherwise the axis only reorders ties and never actually promotes.
        near = surface_score(
            cosine=0.62, confidence=0.5,
            importance=kind_importance("taste"), importance_strength=0.4,
        )
        weighty = surface_score(
            cosine=0.60, confidence=0.5,
            importance=kind_importance("boundary"), importance_strength=0.4,
        )
        self.assertGreater(weighty, near)

    def test_it_cannot_overturn_a_large_cosine_lead(self) -> None:
        # And it must stay a tilt, not a takeover: an off-topic boundary
        # should not displace a concept that is squarely about the turn.
        on_topic = surface_score(
            cosine=0.9, confidence=0.5,
            importance=kind_importance("taste"), importance_strength=0.4,
        )
        off_topic = surface_score(
            cosine=0.3, confidence=0.5,
            importance=kind_importance("boundary"), importance_strength=0.4,
        )
        self.assertGreater(on_topic, off_topic)

    def test_the_score_stays_in_range(self) -> None:
        self.assertLessEqual(
            surface_score(
                cosine=1.0, confidence=1.0,
                importance=1.0, importance_strength=1.0,
            ),
            1.0,
        )

    def test_it_composes_with_habituation_rather_than_replacing_it(
        self,
    ) -> None:
        # Both are multipliers, so a just-surfaced important concept must
        # still step aside -- importance is not an override.
        rested = self._score(
            importance=0.9, importance_strength=0.4, habituation=1.0,
        )
        just_shown = self._score(
            importance=0.9, importance_strength=0.4, habituation=0.5,
        )
        self.assertLess(just_shown, rested)


if __name__ == "__main__":
    unittest.main()
