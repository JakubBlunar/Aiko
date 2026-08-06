"""L17d: clustering corrections into a pattern, and naming it.

Two things are on trial here. The clustering has to tell a habit apart from
noise -- and the specific noise that matters is one belief she keeps
flip-flopping on, which looks identical to a pattern if you count events
instead of beliefs. The proposer has to emit meta evidence pointing at the
beliefs the pattern was learned from, since that is what puts the rule on
the L12 rails.
"""
from __future__ import annotations

import unittest
from datetime import timedelta

import numpy as np

from app.core.concepts.proposers.base import ExistingConcept, ProposerContext
from app.core.concepts.proposers.self_correction_aiko import (
    SPEC,
    propose_self_correction_aiko,
)
from app.core.concepts.self_correction import (
    CorrectionInput,
    cluster_corrections,
)
from app.core.infra import timephrase


NOW = timephrase.utcnow()


def _at(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def _vec(*values: float) -> np.ndarray:
    return np.asarray(values, dtype=np.float32)


# Two directions far enough apart that nothing links across them at 0.55.
_A = _vec(1.0, 0.0, 0.0)
_A2 = _vec(0.95, 0.31, 0.0)
_B = _vec(0.0, 0.0, 1.0)


def _row(
    event_id: int,
    prior_concept_id: int,
    *,
    vec: np.ndarray = _A,
    because: str = "she committed to a first read too early",
    days_ago: float = 0.0,
    salience: float = 0.6,
    kind: str = "identity",
    shape: str = "succession",
) -> CorrectionInput:
    return CorrectionInput(
        event_id=event_id,
        embedding=vec,
        because=because,
        prior_concept_id=prior_concept_id,
        kind=kind,
        shape=shape,
        old_label=f"old {prior_concept_id}",
        new_label=f"new {prior_concept_id}",
        at=_at(days_ago),
        salience=salience,
    )


class BeliefFloorTests(unittest.TestCase):
    """The floor counts beliefs, because that is what makes it a habit."""

    def test_three_corrections_to_one_belief_is_not_a_pattern(self) -> None:
        # The failure mode this gate exists for: one concept she cannot make
        # up her mind about would otherwise mint a rule about her character.
        rows = [
            _row(1, 500, days_ago=30),
            _row(2, 500, days_ago=20),
            _row(3, 500, days_ago=10),
        ]
        self.assertEqual(cluster_corrections(rows, min_beliefs=3), [])

    def test_the_same_reason_across_three_beliefs_is(self) -> None:
        rows = [
            _row(1, 500, days_ago=30),
            _row(2, 501, days_ago=20, vec=_A2),
            _row(3, 502, days_ago=10),
        ]
        clusters = cluster_corrections(rows, min_beliefs=3)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].concept_ids, (500, 501, 502))
        self.assertEqual(clusters[0].belief_count, 3)
        self.assertEqual(clusters[0].size, 3)

    def test_duplicate_beliefs_do_not_inflate_the_count(self) -> None:
        rows = [
            _row(1, 500, days_ago=30),
            _row(2, 500, days_ago=25),
            _row(3, 501, days_ago=10),
        ]
        self.assertEqual(cluster_corrections(rows, min_beliefs=3), [])
        # ... but the same events clear a floor of two, and the repeated
        # belief still contributes only one evidence edge.
        clusters = cluster_corrections(rows, min_beliefs=2)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].concept_ids, (500, 501))
        self.assertEqual(clusters[0].size, 3)


class SpanTests(unittest.TestCase):
    def test_one_afternoon_is_a_mood_not_a_tendency(self) -> None:
        rows = [
            _row(1, 500, days_ago=0.1),
            _row(2, 501, days_ago=0.05),
            _row(3, 502, days_ago=0.0),
        ]
        self.assertEqual(
            cluster_corrections(rows, min_beliefs=3, min_span_days=7.0), []
        )

    def test_the_span_is_reported_in_days(self) -> None:
        rows = [
            _row(1, 500, days_ago=20),
            _row(2, 501, days_ago=10),
            _row(3, 502, days_ago=0),
        ]
        clusters = cluster_corrections(
            rows, min_beliefs=3, min_span_days=7.0
        )
        self.assertEqual(len(clusters), 1)
        self.assertAlmostEqual(clusters[0].span_days, 20.0, delta=0.2)


class SeparationTests(unittest.TestCase):
    def test_unlike_reasons_do_not_merge(self) -> None:
        # Three of one reason, two of another: only the first is a pattern.
        rows = [
            _row(1, 500, days_ago=30),
            _row(2, 501, days_ago=20),
            _row(3, 502, days_ago=10),
            _row(4, 600, vec=_B, days_ago=30, because="he had told her twice"),
            _row(5, 601, vec=_B, days_ago=10, because="he had told her twice"),
        ]
        clusters = cluster_corrections(rows, min_beliefs=3)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].concept_ids, (500, 501, 502))

    def test_the_number_of_rules_per_run_is_capped(self) -> None:
        rows = [
            _row(1, 500, days_ago=30),
            _row(2, 501, days_ago=20),
            _row(3, 502, days_ago=10),
            _row(4, 600, vec=_B, days_ago=30),
            _row(5, 601, vec=_B, days_ago=20),
            _row(6, 602, vec=_B, days_ago=10),
        ]
        self.assertEqual(
            len(cluster_corrections(rows, min_beliefs=3, max_clusters=1)), 1
        )
        self.assertEqual(
            len(cluster_corrections(rows, min_beliefs=3, max_clusters=2)), 2
        )


class HygieneTests(unittest.TestCase):
    def test_an_emergence_has_no_prior_belief_and_is_dropped(self) -> None:
        rows = [
            _row(1, 0, days_ago=30),
            _row(2, 0, days_ago=20),
            _row(3, 0, days_ago=10),
        ]
        self.assertEqual(cluster_corrections(rows, min_beliefs=3), [])

    def test_a_blank_reason_is_dropped(self) -> None:
        rows = [
            _row(1, 500, because="   ", days_ago=30),
            _row(2, 501, days_ago=20),
            _row(3, 502, days_ago=10),
        ]
        self.assertEqual(cluster_corrections(rows, min_beliefs=3), [])

    def test_the_key_follows_the_beliefs_not_the_events(self) -> None:
        first = cluster_corrections(
            [
                _row(1, 500, days_ago=30),
                _row(2, 501, days_ago=20),
                _row(3, 502, days_ago=10),
            ],
            min_beliefs=3,
        )
        # Same beliefs, different event ids and order: the cooldown has to
        # recognise this as the same pattern rather than a new one.
        second = cluster_corrections(
            [
                _row(9, 502, days_ago=9),
                _row(8, 500, days_ago=29),
                _row(7, 501, days_ago=19),
            ],
            min_beliefs=3,
        )
        self.assertEqual(first[0].key, second[0].key)

    def test_the_most_salient_correction_leads(self) -> None:
        rows = [
            _row(1, 500, days_ago=30, salience=0.4),
            _row(2, 501, days_ago=20, salience=0.9),
            _row(3, 502, days_ago=10, salience=0.6),
        ]
        clusters = cluster_corrections(rows, min_beliefs=3)
        self.assertEqual(clusters[0].members[0].event_id, 2)
        self.assertAlmostEqual(clusters[0].salience_max, 0.9, places=3)


class _FakeLLM:
    def __init__(self, items: list[dict]) -> None:
        self.items = items
        self.calls: list[tuple[str, str]] = []

    def __call__(self, system: str, user: str) -> list[dict]:
        self.calls.append((system, user))
        return self.items


class ProposerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clusters = cluster_corrections(
            [
                _row(1, 500, days_ago=30),
                _row(2, 501, days_ago=20),
                _row(3, 502, days_ago=10),
            ],
            min_beliefs=3,
        )
        self.key = self.clusters[0].key

    def _ctx(self, items: list[dict]) -> tuple[ProposerContext, _FakeLLM]:
        llm = _FakeLLM(items)
        return (
            ProposerContext(
                call_llm=llm, user_name="Ben", assistant_name="Aiko"
            ),
            llm,
        )

    def test_the_rule_cites_the_beliefs_it_was_learned_from(self) -> None:
        ctx, _llm = self._ctx(
            [
                {
                    "group_key": self.key,
                    "label": "you decide what he means from one short "
                    "message -- ask instead",
                    "rationale": "three reads walked back",
                    "confidence": 0.7,
                }
            ]
        )
        [proposal] = propose_self_correction_aiko(
            ctx, clusters=self.clusters
        )
        self.assertEqual(proposal.kind, "communication_style")
        self.assertEqual(proposal.subject, "aiko")
        # The whole point: meta evidence, so it rides the L12 rails.
        self.assertEqual(proposal.evidence_model, "meta")
        self.assertEqual(
            proposal.evidence,
            [("concept", "500"), ("concept", "501"), ("concept", "502")],
        )

    def test_an_unknown_group_key_is_ignored(self) -> None:
        ctx, _llm = self._ctx(
            [{"group_key": "invented", "label": "x", "confidence": 0.9}]
        )
        self.assertEqual(
            propose_self_correction_aiko(ctx, clusters=self.clusters), []
        )

    def test_one_rule_per_group_at_most(self) -> None:
        ctx, _llm = self._ctx(
            [
                {"group_key": self.key, "label": "first", "confidence": 0.7},
                {"group_key": self.key, "label": "second", "confidence": 0.7},
            ]
        )
        proposals = propose_self_correction_aiko(ctx, clusters=self.clusters)
        self.assertEqual([p.label for p in proposals], ["first"])

    def test_it_can_reinforce_a_rule_she_already_holds(self) -> None:
        ctx, _llm = self._ctx(
            [{"group_key": self.key, "reinforces_id": 77, "rationale": "same"}]
        )
        [proposal] = propose_self_correction_aiko(
            ctx,
            clusters=self.clusters,
            existing=[ExistingConcept(id=77, label="ask before assuming")],
        )
        self.assertEqual(proposal.reinforces_id, 77)
        self.assertEqual(proposal.label, "")
        self.assertEqual(len(proposal.evidence), 3)

    def test_a_reinforce_id_she_was_not_shown_is_refused(self) -> None:
        ctx, _llm = self._ctx(
            [{"group_key": self.key, "reinforces_id": 999}]
        )
        # No label and no valid id leaves nothing to persist.
        self.assertEqual(
            propose_self_correction_aiko(ctx, clusters=self.clusters), []
        )

    def test_nothing_is_proposed_without_material(self) -> None:
        ctx, llm = self._ctx([{"group_key": self.key, "label": "x"}])
        self.assertEqual(propose_self_correction_aiko(ctx, clusters=[]), [])
        self.assertEqual(llm.calls, [], "no material must mean no LLM call")

    def test_the_prompt_is_grounded_in_the_reasons(self) -> None:
        ctx, llm = self._ctx([])
        propose_self_correction_aiko(ctx, clusters=self.clusters)
        [(system, user)] = llm.calls
        self.assertIn("she committed to a first read too early", user)
        self.assertIn(self.key, user)
        self.assertIn("Ben", system)
        # She is noticing a habit, not reading a report.
        for machinery in ("salience", "confidence score", "cluster", "ledger"):
            self.assertNotIn(machinery, user.lower())

    def test_the_spec_shares_the_comm_style_kind_but_not_its_state(
        self,
    ) -> None:
        self.assertEqual(SPEC.kind, "communication_style")
        self.assertEqual(SPEC.subject, "aiko")
        self.assertEqual(SPEC.evidence_model, "meta")
        self.assertEqual(SPEC.population, "self_correction")
        self.assertEqual(
            SPEC.sig_key, "concept_synth.self_correction_sig.aiko"
        )


class _RecordingProposer:
    """Stands in for the proposer so the worker's gates are what is tested."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, ctx, *, clusters=(), existing=()):  # noqa: ANN001
        self.calls.append(tuple(clusters))
        return []


class WorkerGateTests(unittest.TestCase):
    """The worker's own protections: cooldown, watermark, and the switch.

    The proposer is stubbed out, because what matters here is *when* the
    worker is willing to look at her history at all -- a pass that re-fires
    every tick would both burn LLM calls and let her rewrite her working
    strategy from the same material.
    """

    def setUp(self) -> None:
        from app.core.concepts.concept_learning_event_store import (
            ConceptLearningEventStore,
            LearningEvent,
        )
        from tests.test_concept_synthesis_worker import (
            WorkerHarness,
            _both_responder,
        )

        self.h = WorkerHarness(_both_responder)
        self.learning = ConceptLearningEventStore(self.h.db)
        self.h.worker._learning_store = self.learning
        for i, cid in enumerate((500, 501, 502)):
            self.learning.add(
                LearningEvent(
                    shape="succession",
                    concept_id=900 + i,
                    prior_concept_id=cid,
                    subject="aiko",
                    kind="identity",
                    old_label=f"old {cid}",
                    new_label=f"new {cid}",
                    because="she committed to a first read too early",
                    salience=0.7,
                    fingerprint=f"fp-{cid}",
                    created_at=_at(30 - i * 10),
                )
            )
        self.proposer = _RecordingProposer()
        self.spec = type(SPEC)(
            kind=SPEC.kind,
            subject=SPEC.subject,
            evidence_model=SPEC.evidence_model,
            population=SPEC.population,
            propose=self.proposer,
            sig_key=SPEC.sig_key,
        )

    def _run(self, *, force: bool = False) -> dict:
        stats: dict = {}
        ctx = ProposerContext(call_llm=lambda s, u: [])
        self.h.worker._run_self_correction_pass(
            ctx, self.spec, stats, force
        )
        return stats

    def test_a_pattern_in_the_record_reaches_the_proposer(self) -> None:
        stats = self._run()
        self.assertTrue(stats["self_correction_dirty"])
        self.assertEqual(stats["self_correction_clusters"], 1)
        [clusters] = self.proposer.calls
        self.assertEqual(clusters[0].concept_ids, (500, 501, 502))

    def test_an_unchanged_record_is_a_no_op(self) -> None:
        self._run()
        self.proposer.calls.clear()
        # Same events, so nothing new to learn from: no clustering, no LLM.
        self._run()
        self.assertEqual(self.proposer.calls, [])

    def test_the_cooldown_outlasts_fresh_history(self) -> None:
        from app.core.concepts.concept_learning_event_store import LearningEvent

        self._run()
        self.proposer.calls.clear()
        self.learning.add(
            LearningEvent(
                shape="succession",
                concept_id=910,
                prior_concept_id=503,
                subject="aiko",
                because="she committed to a first read too early",
                salience=0.8,
                fingerprint="fp-503",
                created_at=_at(0),
            )
        )
        # New material *and* a moved watermark, but she may not rewrite her
        # strategy again this fortnight.
        self.assertEqual(self._run(), {})
        self.assertEqual(self.proposer.calls, [])
        # A forced run (button / MCP) still bypasses it.
        self._run(force=True)
        self.assertEqual(len(self.proposer.calls), 1)

    def test_emergences_alone_never_fire_the_pass(self) -> None:
        from app.core.concepts.concept_learning_event_store import (
            ConceptLearningEventStore,
            LearningEvent,
        )
        from tests.test_concept_synthesis_worker import (
            WorkerHarness,
            _both_responder,
        )

        h = WorkerHarness(_both_responder)
        learning = ConceptLearningEventStore(h.db)
        h.worker._learning_store = learning
        for i in range(4):
            learning.add(
                LearningEvent(
                    shape="emergence",
                    concept_id=800 + i,
                    prior_concept_id=None,
                    subject="aiko",
                    because="it kept coming up",
                    salience=0.9,
                    fingerprint=f"emg-{i}",
                    created_at=_at(20 - i * 5),
                )
            )
        stats: dict = {}
        h.worker._run_self_correction_pass(
            ProposerContext(call_llm=lambda s, u: []), self.spec, stats, False
        )
        self.assertFalse(stats["self_correction_dirty"])
        self.assertEqual(self.proposer.calls, [])

    def test_the_switch_skips_it_without_touching_the_record(self) -> None:
        self.h.worker._agent_settings.concept_self_correction_enabled = False
        self.assertEqual(self._run(), {})
        self.assertEqual(self.proposer.calls, [])

    def test_no_learning_store_is_simply_skipped(self) -> None:
        self.h.worker._learning_store = None
        self.assertEqual(self._run(), {})
        self.assertEqual(self.proposer.calls, [])

    def test_beliefs_since_merged_count_once(self) -> None:
        from app.core.concepts.concept_learning_event_store import ConceptAlias

        # 502 was later absorbed into 500, so these are corrections to two
        # beliefs, not three -- and the floor of three is not met.
        self.learning.record_alias(
            ConceptAlias(absorbed_id=502, canonical_id=500, subject="aiko")
        )
        stats = self._run()
        self.assertEqual(stats["self_correction_clusters"], 0)
        self.assertEqual(self.proposer.calls, [])


class SettingsTests(unittest.TestCase):
    """The knobs, and the fact that they do not collide with K38's.

    ``self_correction_*`` was already taken by the in-reply "I got that
    wrong" cue, so every name here carries the ``concept_`` prefix.
    """

    def test_the_knobs_round_trip(self) -> None:
        from app.core.infra.memory_settings import parse_memory_settings as _p

        defaults = _p({})
        self.assertEqual(defaults.concept_self_correction_evidence_floor, 3)
        self.assertAlmostEqual(
            defaults.concept_self_correction_min_span_days, 7.0
        )
        self.assertAlmostEqual(
            defaults.concept_self_correction_cooldown_days, 14.0
        )
        # K38's own knobs are untouched.
        self.assertAlmostEqual(defaults.self_correction_min_confidence, 0.6)

        tuned = _p(
            {
                # A floor of one would let a single correction speak for her
                # character, so it is clamped to two.
                "concept_self_correction_evidence_floor": 1,
                "concept_self_correction_similarity": 5.0,
                "concept_self_correction_cooldown_days": -3,
            }
        )
        self.assertEqual(tuned.concept_self_correction_evidence_floor, 2)
        self.assertAlmostEqual(tuned.concept_self_correction_similarity, 1.0)
        self.assertAlmostEqual(
            tuned.concept_self_correction_cooldown_days, 0.0
        )

    def test_the_feature_has_its_own_switch(self) -> None:
        from app.core.infra.agent_settings_parse import parse_agent_settings

        base = parse_agent_settings({})
        self.assertTrue(base.concept_self_correction_enabled)
        off = parse_agent_settings(
            {"concept_self_correction_enabled": False}
        )
        self.assertFalse(off.concept_self_correction_enabled)
        # Turning the concept feature off leaves the K38 cue alone.
        self.assertTrue(off.self_correction_enabled)


class RegistryTests(unittest.TestCase):
    def test_it_runs_with_the_metas_at_the_end(self) -> None:
        from app.core.concepts.proposers import CONCEPT_PROPOSERS

        populations = [spec.population for spec in CONCEPT_PROPOSERS]
        self.assertIn("self_correction", populations)
        # Its bases must already be settled, like every other meta.
        self.assertGreater(
            populations.index("self_correction"),
            populations.index("comm_style"),
        )

    def test_the_comm_style_kind_treats_meta_bases_as_history(self) -> None:
        from app.core.concepts.concept_kinds import get_kind

        # Without this the rule would be permanently moot: its bases are
        # exactly the beliefs that stopped being active.
        self.assertEqual(
            get_kind("communication_style").meta_min_active_bases, 0
        )
        self.assertEqual(get_kind("generalization").meta_min_active_bases, 2)
        self.assertIsNone(get_kind("tension").meta_min_active_bases)


if __name__ == "__main__":
    unittest.main()
