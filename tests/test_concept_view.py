"""L24 concept integration contract: ``ConceptView`` facade + kind routing.

Covers the single read path (``core`` / ``relevant`` / ``for_target`` /
``for_cluster`` / ``evidence_labels``), subject/kind/confidence filtering,
empty + missing-dep degradation, and the authoritative
``surfacing_targets`` -> ``kinds_for_target`` routing (identity feeds
``profile_block`` for the user; ``subject=aiko`` concepts have no named
for_target block -- they surface via the T3 relevant_context path).
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.concepts.concept_kinds import (
    ConceptKind,
    kinds_for_target,
    register_kind,
    target_for,
    targets_of,
)
from app.core.concepts.concept_view import ConceptView, concept_view_from


def _c(cid, *, subject="user", kind="identity", confidence=0.7,
       label=None, status="active"):
    return SimpleNamespace(
        concept_id=cid,
        label=label or f"concept {cid}",
        kind=kind,
        subject=subject,
        confidence=confidence,
        status=status,
    )


class _Edge:
    def __init__(self, dst_type, dst_id, *, src_type=None, src_id=None,
                 relation="evidence"):
        self.dst_type = dst_type
        self.dst_id = str(dst_id)
        self.src_type = src_type
        self.src_id = str(src_id) if src_id is not None else None
        self.relation = relation


class _FakeStore:
    """In-memory stand-in exposing the ConceptStore read surface used by
    the facade."""

    def __init__(self, concepts, *, near_score=0.6, edges=None, into=None,
                 deps=None):
        self._concepts = {int(c.concept_id): c for c in concepts}
        self._near_score = near_score
        self._edges = edges or {}
        self._into = into or {}
        self._deps = deps or {}
        self.nearest_calls = []

    def evidence_of(self, cid):
        return list(self._into.get(int(cid), []))

    def dependents_of(self, cid):
        return list(self._deps.get(int(cid), []))

    def list_by(self, *, status=None, subject=None, kind=None, user_id=None):
        return [
            c for c in self._concepts.values()
            if (status is None or c.status == status)
            and (subject is None or c.subject == subject)
            and (kind is None or c.kind == kind)
        ]

    def nearest(self, _vec, *, status="active", subject=None, kind=None,
                k=8, **_kw):
        self.nearest_calls.append(
            {"status": status, "subject": subject, "kind": kind, "k": k}
        )
        rows = [
            c for c in self._concepts.values()
            if (status is None or c.status == status)
            and (subject is None or c.subject == subject)
            and (kind is None or c.kind == kind)
        ]
        return [(c, self._near_score) for c in rows[:k]]

    def get(self, cid):
        return self._concepts.get(int(cid))

    def edges_from(self, node_type, node_id):
        return list(self._edges.get((node_type, str(node_id)), []))


class CoreTests(unittest.TestCase):
    def test_filters_by_subject_kind_confidence_and_sorts(self) -> None:
        store = _FakeStore([
            _c(1, subject="user", confidence=0.6),
            _c(2, subject="user", confidence=0.9),
            _c(3, subject="aiko", confidence=0.95),
            _c(4, subject="user", confidence=0.3),
        ])
        view = ConceptView(store)
        out = view.core(subject="user", kind="identity", min_confidence=0.5)
        self.assertEqual([c.concept_id for c in out], [2, 1])

    def test_limit_caps(self) -> None:
        store = _FakeStore([_c(i, confidence=0.9 - i * 0.01) for i in range(5)])
        view = ConceptView(store)
        self.assertEqual(len(view.core(limit=2)), 2)

    def test_none_store_degrades_to_empty(self) -> None:
        self.assertEqual(ConceptView(None).core(), [])


class StableRankTests(unittest.TestCase):
    """P44 — the ordering has to survive L3's per-tick confidence drift.

    These lists are rendered into T0 prompt blocks, so a reshuffle costs
    the cache for everything after them. Raw ``confidence`` cannot be the
    sort key: it moves by thousandths every lifecycle tick while the gaps
    between neighbours are the same size.
    """

    def test_drift_within_a_band_does_not_reorder(self) -> None:
        before = ConceptView(_FakeStore([
            _c(1, confidence=0.9219),
            _c(2, confidence=0.9173),
            _c(3, confidence=0.9127),
        ])).core()
        # One tick of L3: every concept nudged, and 2 overtakes 1 on raw
        # confidence. All three are still in the same 0.90 band, so the
        # rendered order must not move.
        after = ConceptView(_FakeStore([
            _c(1, confidence=0.9188),
            _c(2, confidence=0.9204),
            _c(3, confidence=0.9143),
        ])).core()
        self.assertEqual([c.concept_id for c in before], [1, 2, 3])
        self.assertEqual([c.concept_id for c in after], [1, 2, 3])

    def test_membership_at_the_cap_survives_a_nudge(self) -> None:
        # The expensive failure: the block renders a fixed number of
        # bullets, so a swap across the cut adds one line and drops
        # another -- a visible edit to a block filed as stable.
        def ranked(bump: float) -> list[int]:
            store = _FakeStore([
                _c(1, confidence=0.93),
                _c(2, confidence=0.9127),
                _c(3, confidence=0.9063 + bump),
            ])
            return [c.concept_id for c in ConceptView(store).core(limit=2)]

        self.assertEqual(ranked(0.0), [1, 2])
        self.assertEqual(ranked(0.008), [1, 2])

    def test_a_real_confidence_difference_still_wins(self) -> None:
        # Banding must not flatten genuine belief: a concept a full band
        # above its neighbours still sorts first regardless of id.
        store = _FakeStore([
            _c(9, confidence=0.62),
            _c(1, confidence=0.97),
            _c(5, confidence=0.71),
        ])
        self.assertEqual(
            [c.concept_id for c in ConceptView(store).core()], [1, 5, 9],
        )

    def test_ties_settle_on_id_not_insertion_order(self) -> None:
        store = _FakeStore([
            _c(7, confidence=0.91),
            _c(2, confidence=0.92),
            _c(5, confidence=0.905),
        ])
        # Same band, so the oldest concept leads and the order is a
        # function of the data rather than of dict iteration.
        self.assertEqual(
            [c.concept_id for c in ConceptView(store).core()], [2, 5, 7],
        )

    def test_for_target_merge_keeps_the_stable_order(self) -> None:
        # for_target re-sorts after merging across kinds; re-sorting on
        # raw confidence there would undo everything core() just did.
        store = _FakeStore([
            _c(4, kind="identity", confidence=0.9219),
            _c(2, kind="value", confidence=0.9173),
            _c(9, kind="value", confidence=0.9127),
        ])
        out = ConceptView(store).for_target("profile_block", subject="user")
        self.assertEqual([c.concept_id for c in out], [2, 4, 9])


class CoreLaneTests(unittest.TestCase):
    """L27 always-on core lane: registry-driven, per-kind bars, balanced
    round-robin across (kind, subject)."""

    def test_identity_only_balances_subjects(self) -> None:
        # Two user identity concepts + one aiko: with a tight limit the
        # strongest bucket is drawn first, then the other subject -- so both
        # the user-model and self-model surface instead of two user picks.
        store = _FakeStore([
            _c(1, subject="user", confidence=0.9),
            _c(2, subject="user", confidence=0.85),
            _c(3, subject="aiko", confidence=0.8),
        ])
        out = ConceptView(store).core_lane(limit=2, default_min_confidence=0.75)
        self.assertEqual([c.concept_id for c in out], [1, 3])

    def test_respects_default_min_confidence(self) -> None:
        store = _FakeStore([_c(1, confidence=0.5)])
        self.assertEqual(
            ConceptView(store).core_lane(limit=3, default_min_confidence=0.75), [],
        )

    def test_zero_limit_and_missing_store(self) -> None:
        store = _FakeStore([_c(1, confidence=0.9)])
        self.assertEqual(ConceptView(store).core_lane(limit=0), [])
        self.assertEqual(ConceptView(None).core_lane(limit=3), [])

    def test_per_kind_bar_and_cross_kind_balance(self) -> None:
        # A second kind opts into the lane with a higher bar; its weak
        # candidate is dropped by that bar while identity's lower bar admits
        # a 0.8 concept.
        register_kind(
            ConceptKind(
                name="_clv_value", core_always_on=True, core_min_confidence=0.9,
            )
        )
        try:
            store = _FakeStore([
                _c(1, kind="identity", subject="user", confidence=0.8),
                _c(2, kind="_clv_value", subject="user", confidence=0.95),
                _c(3, kind="_clv_value", subject="user", confidence=0.85),
            ])
            out = ConceptView(store).core_lane(
                limit=5, default_min_confidence=0.75,
            )
            self.assertEqual(sorted(c.concept_id for c in out), [1, 2])
        finally:
            from app.core.concepts.concept_kinds import CONCEPT_KINDS

            CONCEPT_KINDS.pop("_clv_value", None)

    def test_core_lane_kinds_includes_identity_by_default(self) -> None:
        from app.core.concepts.concept_kinds import core_lane_kinds

        self.assertIn("identity", [k.name for k in core_lane_kinds()])


class RelevantTests(unittest.TestCase):
    def test_wraps_nearest_active_and_applies_min_sim(self) -> None:
        store = _FakeStore([_c(1)], near_score=0.4)
        view = ConceptView(store)
        self.assertEqual(view.relevant([1.0], k=1, min_sim=0.5), [])
        self.assertEqual(len(view.relevant([1.0], k=1, min_sim=0.3)), 1)
        # Unfiltered query does not forward subject/kind (fast-path / doubles).
        call = store.nearest_calls[0]
        self.assertEqual(call["status"], "active")
        self.assertIsNone(call["subject"])
        self.assertIsNone(call["kind"])

    def test_forwards_subject_when_set(self) -> None:
        store = _FakeStore([_c(1, subject="aiko")])
        ConceptView(store).relevant([1.0], subject="aiko", k=2)
        self.assertEqual(store.nearest_calls[0]["subject"], "aiko")

    def test_none_embedding_degrades(self) -> None:
        store = _FakeStore([_c(1)])
        self.assertEqual(ConceptView(store).relevant(None), [])


class ForTargetTests(unittest.TestCase):
    def test_user_identity_routes_to_profile_block(self) -> None:
        store = _FakeStore([
            _c(1, subject="user", confidence=0.8),
            _c(2, subject="aiko", confidence=0.9),
        ])
        view = ConceptView(store)
        user = view.for_target("profile_block", subject="user")
        self.assertEqual([c.concept_id for c in user], [1])

    def test_aiko_has_no_for_target_block(self) -> None:
        # subject=aiko concepts surface via the T3 relevant_context path,
        # not a named for_target block; self_image_block was removed.
        store = _FakeStore([_c(2, subject="aiko", confidence=0.9)])
        self.assertEqual(
            ConceptView(store).for_target("self_image_block", subject="aiko"),
            [],
        )

    def test_unknown_target_empty(self) -> None:
        store = _FakeStore([_c(1)])
        self.assertEqual(ConceptView(store).for_target("nope"), [])


class ForClusterTests(unittest.TestCase):
    def test_resolves_active_concepts_spanning_cluster(self) -> None:
        store = _FakeStore(
            [_c(1), _c(2, status="candidate"), _c(3)],
            edges={
                ("cluster", "100"): [
                    _Edge("concept", 1),
                    _Edge("concept", 2),  # not active -> dropped
                    _Edge("concept", 3),
                    _Edge("memory", 9),   # wrong dst_type -> ignored
                ],
            },
        )
        out = ConceptView(store).for_cluster(100)
        self.assertEqual(sorted(c.concept_id for c in out), [1, 3])

    def test_no_edges_empty(self) -> None:
        self.assertEqual(ConceptView(_FakeStore([_c(1)])).for_cluster(1), [])


class ActivatedTests(unittest.TestCase):
    """L23 spreading activation: shared-cluster adjacency (+ dormant meta)."""

    def test_direct_hot_cluster_siblings(self) -> None:
        store = _FakeStore(
            [_c(1), _c(2), _c(3, status="candidate")],
            edges={
                ("cluster", "100"): [
                    _Edge("concept", 1),
                    _Edge("concept", 2),
                    _Edge("concept", 3),  # inactive -> dropped by for_cluster
                ],
            },
        )
        out = ConceptView(store).activated([100], seed_concept_ids=[1])
        # #1 is a seed (excluded); #2 activates at full strength; #3 inactive.
        self.assertEqual([(c.concept_id, s) for c, s in out], [(2, 1.0)])

    def test_two_hop_via_seed_cluster(self) -> None:
        # Seed #1 is backed by cluster rep 100; its sibling #2 activates at the
        # weaker 2-hop strength when the cluster isn't itself hot.
        store = _FakeStore(
            [_c(1), _c(2)],
            edges={("cluster", "100"): [_Edge("concept", 1), _Edge("concept", 2)]},
            into={1: [_Edge("concept", 1, src_type="cluster", src_id=100)]},
        )
        out = ConceptView(store).activated([], seed_concept_ids=[1])
        self.assertEqual([(c.concept_id, s) for c, s in out], [(2, 0.6)])

    def test_direct_beats_two_hop_on_overlap(self) -> None:
        store = _FakeStore(
            [_c(1), _c(2)],
            edges={("cluster", "100"): [_Edge("concept", 1), _Edge("concept", 2)]},
            into={1: [_Edge("concept", 1, src_type="cluster", src_id=100)]},
        )
        # #2 reachable both directly (hot cluster 100) and 2-hop via seed #1 ->
        # the stronger direct strength wins.
        out = ConceptView(store).activated([100], seed_concept_ids=[1])
        self.assertEqual([(c.concept_id, s) for c, s in out], [(2, 1.0)])

    def test_dependents_meta_path_dormant_but_wired(self) -> None:
        # With no concept->concept references (meta not shipped) the path is
        # inert; once such edges exist the referenced concept activates.
        store = _FakeStore(
            [_c(1), _c(9)],
            deps={1: [9]},  # simulates a meta referencing base #1
        )
        out = ConceptView(store).activated([], seed_concept_ids=[1])
        self.assertEqual([(c.concept_id, s) for c, s in out], [(9, 0.8)])

    def test_limit_and_missing_store(self) -> None:
        store = _FakeStore(
            [_c(1), _c(2), _c(3)],
            edges={
                ("cluster", "100"): [
                    _Edge("concept", 1), _Edge("concept", 2), _Edge("concept", 3),
                ],
            },
        )
        self.assertEqual(len(ConceptView(store).activated([100], limit=1)), 1)
        self.assertEqual(ConceptView(None).activated([100]), [])


class EvidenceLabelsTests(unittest.TestCase):
    def test_none_store_and_missing_deps_degrade(self) -> None:
        # No store -> [].
        self.assertEqual(ConceptView(None).evidence_labels(1), [])
        # Store present but no topic_graph / memory_store -> resolves what it
        # can (here nothing), never raises.
        store = _FakeStore([_c(1)])
        store.evidence_of = lambda _cid: []  # type: ignore[attr-defined]
        self.assertEqual(ConceptView(store).evidence_labels(1), [])


class ConceptViewFromTests(unittest.TestCase):
    def test_builds_from_host_attrs(self) -> None:
        store = _FakeStore([_c(1)])
        host = SimpleNamespace(
            _concept_store=store, _topic_graph=None, _memory_store=None,
        )
        view = concept_view_from(host)
        self.assertIsNotNone(view)
        assert view is not None
        self.assertTrue(view.enabled)
        self.assertEqual(len(view.core()), 1)

    def test_none_when_store_absent(self) -> None:
        host = SimpleNamespace(_concept_store=None)
        self.assertIsNone(concept_view_from(host))


class ValueKindTests(unittest.TestCase):
    """L10: the ``value`` kind rides the same routing + core-lane machinery
    as identity, but with a higher core-lane bar."""

    def test_value_routes_user_like_identity(self) -> None:
        from app.core.concepts.concept_kinds import get_kind

        value = get_kind("value")
        assert value is not None
        self.assertEqual(target_for(value, "user"), "profile_block")
        # subject=aiko value has no dedicated for_target block anymore.
        self.assertNotIn("aiko", value.surfacing_targets)

    def test_value_in_core_lane_kinds(self) -> None:
        from app.core.concepts.concept_kinds import core_lane_kinds

        names = [k.name for k in core_lane_kinds()]
        self.assertIn("value", names)

    def test_value_honours_higher_core_bar(self) -> None:
        # A value at 0.8 sits below its 0.85 per-kind bar and is dropped even
        # though the global default (0.75) would admit it; an identity at 0.8
        # (global bar) survives.
        store = _FakeStore([
            _c(1, kind="identity", subject="user", confidence=0.8),
            _c(2, kind="value", subject="user", confidence=0.8),
            _c(3, kind="value", subject="aiko", confidence=0.9),
        ])
        out = ConceptView(store).core_lane(limit=5, default_min_confidence=0.75)
        self.assertEqual(sorted(c.concept_id for c in out), [1, 3])


class KindRoutingTests(unittest.TestCase):
    def test_identity_targets_per_subject(self) -> None:
        from app.core.concepts.concept_kinds import get_kind

        identity = get_kind("identity")
        assert identity is not None
        self.assertEqual(target_for(identity, "user"), "profile_block")
        # subject=aiko identity has no dedicated for_target block anymore.
        self.assertNotIn("aiko", identity.surfacing_targets)

    def test_kinds_for_target_resolves(self) -> None:
        self.assertIn(
            "identity", kinds_for_target("profile_block", subject="user"),
        )
        # self_image_block was removed: no kind routes to it.
        self.assertEqual(kinds_for_target("self_image_block"), set())
        self.assertEqual(kinds_for_target("nonexistent"), set())

    def test_scalar_fallback_when_no_map(self) -> None:
        # A kind with only the scalar surfacing_target still routes.
        register_kind(
            ConceptKind(name="_tv_scalar", surfacing_target="scalar_block")
        )
        try:
            k = ConceptKind(name="_tv_scalar", surfacing_target="scalar_block")
            self.assertEqual(target_for(k, "user"), "scalar_block")
            self.assertEqual(targets_of(k), {"scalar_block"})
            self.assertIn("_tv_scalar", kinds_for_target("scalar_block"))
        finally:
            from app.core.concepts.concept_kinds import CONCEPT_KINDS

            CONCEPT_KINDS.pop("_tv_scalar", None)


if __name__ == "__main__":
    unittest.main()
