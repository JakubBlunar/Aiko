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
       label=None, status="active", sources=3):
    # ``sources`` defaults to the settled bar so a fixture written before
    # L30a reads as an established belief rather than an open question.
    return SimpleNamespace(
        concept_id=cid,
        label=label or f"concept {cid}",
        kind=kind,
        subject=subject,
        confidence=confidence,
        status=status,
        distinct_source_count=sources,
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

    def test_kind_stakes_do_not_reach_the_t0_lanes(self) -> None:
        # L32 guard. Importance is a live, affect-sensitive number, so
        # letting it into these lanes would put exactly the churn back that
        # banding was introduced to remove -- a topic's emotional weather
        # shifting mid-session would resequence the T0 profile block.
        # ``value`` outranks ``identity`` on the L32 stakes ladder, so if
        # importance ever leaked into the sort the value row would lead.
        store = _FakeStore([
            _c(1, kind="identity", confidence=0.93),
            _c(2, kind="value", confidence=0.87),
        ])
        view = ConceptView(store)
        self.assertEqual([c.concept_id for c in view.core()], [1, 2])
        self.assertEqual(
            [
                c.concept_id
                for c in view.for_target("profile_block", subject="user")
            ],
            [1, 2],
        )

    def test_the_t0_lane_module_does_not_import_importance(self) -> None:
        # The structural half of the same guard: a future edit can only
        # break the ordering invariant above by reaching for the module,
        # and this fails at the import rather than at the symptom.
        #
        # ``for_consumer`` does rank on importance and does live in this
        # module, which is exactly why its ranking, its affect context and
        # its settings read were all put in ``concept_diets`` instead. A
        # worker prompt is rebuilt every run and has no cache prefix to
        # protect, so the axis is safe there and unsafe here -- keeping
        # them in separate modules is what lets this stay a blanket check
        # rather than a per-lane ordering assertion.
        import inspect

        from app.core.concepts import concept_view

        self.assertNotIn(
            "concept_importance", inspect.getsource(concept_view)
        )


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

    def test_a_two_subject_kind_does_not_take_twice_the_room(self) -> None:
        """The lane balances between *kinds*, not between (kind, subject)
        buckets. Sharing per bucket quietly hands a kind one share for each
        subject it happens to be mined in, so the kind populated on both
        sides takes double the room of one populated on a single side --
        which is not a difference a reader of the prompt would recognise.
        """
        register_kind(
            ConceptKind(
                name="_clv_guide", core_always_on=True, core_min_confidence=0.5,
            )
        )
        try:
            store = _FakeStore([
                # One kind mined for both subjects, deep on each side.
                _c(1, kind="_clv_guide", subject="user", confidence=0.90),
                _c(2, kind="_clv_guide", subject="user", confidence=0.89),
                _c(3, kind="_clv_guide", subject="user", confidence=0.88),
                _c(4, kind="_clv_guide", subject="aiko", confidence=0.87),
                _c(5, kind="_clv_guide", subject="aiko", confidence=0.86),
                _c(6, kind="_clv_guide", subject="aiko", confidence=0.85),
                # One kind mined for a single subject, equally deep.
                _c(7, kind="identity", subject="user", confidence=0.84),
                _c(8, kind="identity", subject="user", confidence=0.83),
                _c(9, kind="identity", subject="user", confidence=0.82),
            ])
            out = ConceptView(store).core_lane(
                limit=6, default_min_confidence=0.5,
            )
            kinds = [c.kind for c in out]
            self.assertEqual(kinds.count("_clv_guide"), 3)
            self.assertEqual(kinds.count("identity"), 3)
            # And the two-subject kind still alternates inside its share.
            guide_subjects = [
                c.subject for c in out if c.kind == "_clv_guide"
            ]
            self.assertEqual(len(set(guide_subjects)), 2)
        finally:
            from app.core.concepts.concept_kinds import CONCEPT_KINDS

            CONCEPT_KINDS.pop("_clv_guide", None)

    def test_a_kind_with_room_to_spare_still_fills_the_lane(self) -> None:
        """Balance is a ceiling on crowding, not a quota that wastes slots:
        when one kind runs out the others take the remainder.
        """
        register_kind(
            ConceptKind(
                name="_clv_thin", core_always_on=True, core_min_confidence=0.5,
            )
        )
        try:
            store = _FakeStore([
                _c(1, kind="identity", subject="user", confidence=0.90),
                _c(2, kind="identity", subject="user", confidence=0.89),
                _c(3, kind="identity", subject="aiko", confidence=0.88),
                _c(4, kind="_clv_thin", subject="user", confidence=0.87),
            ])
            out = ConceptView(store).core_lane(
                limit=4, default_min_confidence=0.5,
            )
            self.assertEqual(len(out), 4)
        finally:
            from app.core.concepts.concept_kinds import CONCEPT_KINDS

            CONCEPT_KINDS.pop("_clv_thin", None)


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


class HypothesesTests(unittest.TestCase):
    """L30a: the one read path that leaves the active-only contract."""

    def test_reads_candidates_not_actives(self) -> None:
        store = _FakeStore([
            _c(1, status="active", confidence=0.5, sources=1),
            _c(2, status="candidate", confidence=0.5, sources=1),
        ])
        out = ConceptView(store).hypotheses([1.0], k=5)
        self.assertEqual([c.concept_id for c, _s in out], [2])
        self.assertEqual(store.nearest_calls[0]["status"], "candidate")

    def test_ungrounded_proposals_are_excluded(self) -> None:
        # A zero-source candidate scores *highest* on unsettledness
        # precisely because nothing supports it. Without this floor the
        # lane would lead with bare LLM hunches -- on the measured graph
        # the top seven rows were all ungrounded.
        store = _FakeStore([
            _c(1, status="candidate", confidence=0.68, sources=0),
            _c(2, status="candidate", confidence=0.68, sources=1),
        ])
        out = ConceptView(store).hypotheses([1.0], k=5)
        self.assertEqual([c.concept_id for c, _s in out], [2])

    def test_a_belief_waiting_only_on_the_age_floor_is_not_an_open_question(
        self,
    ) -> None:
        # Twice grounded and fully confident: still a candidate, but only
        # because the promotion clock has not run out. It is not something
        # Aiko is unsure about, and the lane must not say she is.
        store = _FakeStore([
            _c(1, status="candidate", confidence=0.85, sources=2),
        ])
        self.assertEqual(ConceptView(store).hypotheses([1.0], k=5), [])

    def test_a_thinly_grounded_candidate_qualifies(self) -> None:
        store = _FakeStore([
            _c(1, status="candidate", confidence=0.85, sources=1),
        ])
        self.assertEqual(len(ConceptView(store).hypotheses([1.0], k=5)), 1)

    def test_high_confidence_alone_does_not_disqualify(self) -> None:
        # The measured candidate pool had a median confidence of 0.82, so
        # a lane that skipped confident rows would surface almost nothing.
        store = _FakeStore([
            _c(1, status="candidate", confidence=0.95, sources=1),
        ])
        self.assertEqual(len(ConceptView(store).hypotheses([1.0], k=5)), 1)

    def test_thresholds_are_caller_tunable(self) -> None:
        store = _FakeStore([
            _c(1, status="candidate", confidence=0.85, sources=2),
        ])
        view = ConceptView(store)
        self.assertEqual(view.hypotheses([1.0], min_unsettled=0.9), [])
        self.assertEqual(len(view.hypotheses([1.0], min_unsettled=0.1)), 1)
        self.assertEqual(view.hypotheses([1.0], min_sources=5), [])

    def test_min_sim_drops_off_topic_questions(self) -> None:
        store = _FakeStore(
            [_c(1, status="candidate", confidence=0.6, sources=1)],
            near_score=0.2,
        )
        view = ConceptView(store)
        self.assertEqual(view.hypotheses([1.0], min_sim=0.5), [])
        self.assertEqual(len(view.hypotheses([1.0], min_sim=0.1)), 1)

    def test_degrades_on_no_store_no_embedding_and_a_raising_store(
        self,
    ) -> None:
        self.assertEqual(ConceptView(None).hypotheses([1.0]), [])
        store = _FakeStore([_c(1, status="candidate", sources=1)])
        self.assertEqual(ConceptView(store).hypotheses(None), [])
        self.assertEqual(ConceptView(store).hypotheses([1.0], k=0), [])

        class _Boom(_FakeStore):
            def nearest(self, *_a, **_kw):
                raise RuntimeError("store down")

        self.assertEqual(
            ConceptView(_Boom([_c(1)])).hypotheses([1.0]), []
        )

    def test_forwards_subject_when_set(self) -> None:
        store = _FakeStore([_c(1, subject="aiko", status="candidate")])
        ConceptView(store).hypotheses([1.0], subject="aiko", k=2)
        self.assertEqual(store.nearest_calls[0]["subject"], "aiko")


class TestableTests(unittest.TestCase):
    """L30b: the subset of open questions an *answer* could settle.

    The off-turn sibling of :meth:`hypotheses`, so the two eligibility
    bars are shared. What is specific here is the age exclusion, and it
    is the reason the read exists: answering adds a distinct source, so
    it can only move a belief held back on sources or conviction. On the
    live graph better than half the candidate pool already clears both
    and is only waiting out its kind's engaged-day floor -- asking about
    one of those spends a question to change nothing.
    """

    def test_it_reads_candidates_with_no_query_vector(self) -> None:
        # The worker runs during quiet windows, so there is no turn to be
        # relevant to and no cosine term to apply.
        store = _FakeStore([
            _c(1, status="active", confidence=0.5, sources=1),
            _c(2, status="candidate", confidence=0.5, sources=1),
        ])
        out = ConceptView(store).testable()
        self.assertEqual([c.concept_id for c, _u in out], [2])
        self.assertEqual(store.nearest_calls, [])

    def test_a_belief_waiting_only_on_the_clock_is_skipped(self) -> None:
        # Three sources at 0.95 clears identity's own gate on everything
        # but age. L3 will promote it unaided; asking reads as a
        # pointless quiz.
        store = _FakeStore([
            _c(1, kind="identity", status="candidate",
               confidence=0.95, sources=3),
        ])
        self.assertEqual(
            ConceptView(store).testable(min_unsettled=0.0), [],
        )

    def test_a_belief_short_on_sources_is_testable(self) -> None:
        # Confident but singly grounded: an answer is exactly the second
        # source it needs.
        store = _FakeStore([
            _c(1, status="candidate", confidence=0.9, sources=1),
        ])
        out = ConceptView(store).testable(min_unsettled=0.0)
        self.assertEqual([c.concept_id for c, _u in out], [1])

    def test_a_belief_short_on_conviction_is_testable(self) -> None:
        store = _FakeStore([
            _c(1, status="candidate", confidence=0.3, sources=3),
        ])
        out = ConceptView(store).testable(min_unsettled=0.0)
        self.assertEqual([c.concept_id for c, _u in out], [1])

    def test_the_age_probe_uses_the_kind_s_own_gate(self) -> None:
        # Every shipped kind floors the three legs with its own constants
        # via ``max`` -- identity wants three sources where the global
        # default wants two. A check written against ``concept_promote_*``
        # alone would call this one settled and never ask about it.
        store = _FakeStore([
            _c(1, kind="identity", status="candidate",
               confidence=0.9, sources=2),
        ])
        out = ConceptView(store).testable(min_unsettled=0.0)
        self.assertEqual([c.concept_id for c, _u in out], [1])

    def test_ungrounded_proposals_are_excluded(self) -> None:
        store = _FakeStore([
            _c(1, status="candidate", confidence=0.68, sources=0),
            _c(2, status="candidate", confidence=0.68, sources=1),
        ])
        out = ConceptView(store).testable()
        self.assertEqual([c.concept_id for c, _u in out], [2])

    def test_settled_enough_rows_are_excluded(self) -> None:
        store = _FakeStore([
            _c(1, status="candidate", confidence=0.6, sources=1),
        ])
        self.assertEqual(ConceptView(store).testable(min_unsettled=0.99), [])

    def test_it_ranks_most_unsettled_first(self) -> None:
        store = _FakeStore([
            _c(1, status="candidate", confidence=0.9, sources=1),
            _c(2, status="candidate", confidence=0.3, sources=1),
        ])
        out = ConceptView(store).testable(min_unsettled=0.0)
        self.assertEqual([c.concept_id for c, _u in out], [2, 1])

    def test_limit_and_subject_are_honoured(self) -> None:
        store = _FakeStore([
            _c(1, subject="user", status="candidate",
               confidence=0.3, sources=1),
            _c(2, subject="aiko", status="candidate",
               confidence=0.3, sources=1),
        ])
        view = ConceptView(store)
        self.assertEqual(len(view.testable(limit=1)), 1)
        self.assertEqual(
            [c.concept_id for c, _u in view.testable(subject="aiko")], [2],
        )
        self.assertEqual(view.testable(limit=0), [])

    def test_it_degrades_rather_than_raising(self) -> None:
        # The ask lane going silent is a worse failure than one odd
        # question, so every read error resolves to an empty list.
        self.assertEqual(ConceptView(None).testable(), [])

        class _Boom(_FakeStore):
            def list_by(self, **_kw):
                raise RuntimeError("store down")

        self.assertEqual(ConceptView(_Boom([_c(1)])).testable(), [])

    def test_a_malformed_row_is_treated_as_unsettled(self) -> None:
        # The gate probe cannot read this row, so it errs toward asking.
        broken = _c(1, status="candidate", confidence=0.9, sources=1)
        broken.confidence = "not a number"
        out = ConceptView(_FakeStore([broken])).testable(min_unsettled=0.0)
        self.assertEqual([c.concept_id for c, _u in out], [1])


class TentativeRegisterIsolationTests(unittest.TestCase):
    """L30a: candidates must reach the hypothesis lane and nowhere else.

    A candidate leaking into ``core`` / ``for_target`` would put an
    unestablished belief into the T0 profile block -- Aiko asserting
    something she has not earned, and a prompt-cache prefix break on top.
    """

    def _mixed(self) -> _FakeStore:
        return _FakeStore([
            _c(1, status="active", confidence=0.9),
            _c(2, status="candidate", confidence=0.95, sources=1),
        ])

    def test_the_core_lane_never_returns_a_candidate(self) -> None:
        view = ConceptView(self._mixed())
        self.assertEqual([c.concept_id for c in view.core()], [1])
        self.assertEqual(
            [c.concept_id for c in view.core_lane(limit=5)], [1]
        )

    def test_the_profile_block_lane_never_returns_a_candidate(self) -> None:
        view = ConceptView(self._mixed())
        self.assertEqual(
            [
                c.concept_id
                for c in view.for_target("profile_block", subject="user")
            ],
            [1],
        )

    def test_the_turn_relevant_lane_never_returns_a_candidate(self) -> None:
        view = ConceptView(self._mixed())
        self.assertEqual(
            [c.concept_id for c, _s in view.relevant([1.0], k=5)], [1]
        )

    def test_only_the_hypothesis_lane_queries_candidates(self) -> None:
        # Structural half of the guard: every other read must ask the
        # store for actives, so a future edit cannot quietly widen one.
        store = self._mixed()
        view = ConceptView(store)
        view.relevant([1.0], k=5)
        view.core()
        view.for_target("profile_block", subject="user")
        self.assertTrue(
            all(call["status"] == "active" for call in store.nearest_calls)
        )
        view.hypotheses([1.0], k=5)
        self.assertEqual(store.nearest_calls[-1]["status"], "candidate")


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

    def test_second_hop_lifts_stacked_parent(self) -> None:
        # Base #1 -> L1 #9 (0.8) -> L2 #20 (0.5).
        store = _FakeStore(
            [_c(1), _c(9), _c(20)],
            deps={1: [9], 9: [20]},
        )
        out = ConceptView(store).activated([], seed_concept_ids=[1])
        got = {c.concept_id: s for c, s in out}
        self.assertEqual(got[9], 0.8)
        self.assertEqual(got[20], 0.5)

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
