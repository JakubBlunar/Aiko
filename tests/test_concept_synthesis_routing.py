"""Source-referenced synthesis retries preserve caps and kind-specific gates."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import test_concept_synthesis_worker as harness_tests

from app.core.concepts.concept_store import Concept, ConceptEdge
from app.core.concepts.concept_synthesis_routing import (
    MAX_ATTEMPTS,
    MAX_RETRIES_PER_RUN,
    QUEUE_BYTES,
    QUEUE_CAP,
    ROUTING_KEY,
    _encode,
)
from app.core.concepts.proposers.base import NarrativeCandidate
from app.core.concepts.proposers.base import ExistingConcept, ProposerContext
from app.core.concepts.proposers.identity_user import SPEC
from app.core.concepts.proposers.narrative_user import SPEC as NARRATIVE_SPEC
from app.core.session.memory_facade_mixin import MemoryFacadeMixin
from app.core.infra import timephrase
from test_concept_synthesis_worker import (
    MemStub,
    WorkerHarness,
    _mem_settings,
    _user_clusters,
)


class SynthesisRoutingTests(unittest.TestCase):
    def _harness(self, *, relation="unspecified", off_topic=False):
        harness = WorkerHarness(
            lambda system, user: {"concepts": []},
            clusters=_user_clusters(2),
            self_memories=[],
            mem_settings=_mem_settings(evidence_max_sources=2),
        )
        vector = harness_tests.EvidenceAdmissionTests._ON
        ident = harness.store.add(
            Concept(
                label="A settled principle",
                subject="user",
                kind="identity",
                embedding=vector,
                status="active",
                last_reinforced_at="2026-01-01T00:00:00+00:00",
                distinct_source_count=2,
                evidence_count=2,
            )
        )
        for source in (900, 901):
            harness.store.add_edge(
                ConceptEdge(
                    src_type="memory",
                    src_id=str(source),
                    dst_type="concept",
                    dst_id=str(ident),
                    relation="evidence",
                )
            )
        for source in (100, 101):
            harness.mem._by_id[source] = MemStub(
                source,
                f"Specific observation {source}",
                "fact",
                0.2,
                embedding=harness_tests.EvidenceAdmissionTests._OFF if off_topic else vector,
            )
        harness.ollama._responder = lambda system, user: {
            "concepts": [
                {
                    "reinforces_id": ident,
                    "evidence_cluster_reps": [100, 101],
                    "evidence_relation": relation,
                    "rationale": "Supporting observations",
                }
            ]
        }
        return harness, ident

    def _run(self, harness):
        with patch("app.core.concepts.concept_synthesis_worker.CONCEPT_PROPOSERS", (SPEC,)):
            return harness.worker.run()

    def _advance(self, harness, hours=7):
        rows = json.loads(harness.worker._kv_get(ROUTING_KEY) or "[]")
        first = min(timephrase.parse_iso(row["first_at"]) for row in rows)
        return patch(
            "app.core.infra.timephrase.utcnow", return_value=first + timedelta(hours=hours)
        )

    def test_exact_support_at_ceiling_does_not_queue_or_create(self):
        harness, ident = self._harness(relation="same")
        stats = self._run(harness)
        self.assertEqual(stats["added"], 0)
        self.assertEqual(stats["reinforced"], 1)
        self.assertEqual(stats["routing"]["pending"], 0)
        self.assertEqual(len(harness.store.evidence_of(ident)), 2)

    def test_related_support_is_not_an_observation_of_the_old_claim(self):
        harness, ident = self._harness(relation="related")
        stats = self._run(harness)
        self.assertEqual(stats["reinforced"], 0)
        self.assertEqual(stats["routing"]["pending"], 1)
        self.assertEqual(harness.store.get(ident).last_reinforced_at, "2026-01-01T00:00:00+00:00")

    def test_unchanged_capped_sources_do_not_reobserve_the_claim(self):
        harness, ident = self._harness(relation="same")
        self._run(harness)
        before = harness.store.get(ident).last_reinforced_at
        stats = self._run(harness)
        self.assertEqual(stats["llm_calls"], 0)
        self._run(harness)
        with patch("app.core.concepts.concept_synthesis_worker.CONCEPT_PROPOSERS", (SPEC,)):
            stats = harness.worker.run(force=True)
        self.assertEqual(harness.store.get(ident).last_reinforced_at, before)
        self.assertEqual(stats["routing"]["fresh_support"], 0)

    def test_changed_cluster_window_is_a_new_observation_at_the_ceiling(self):
        harness, ident = self._harness(relation="same")
        self._run(harness)
        harness.topic._clusters[0].member_ids.append(1000)
        with patch("app.core.concepts.concept_synthesis_worker.CONCEPT_PROPOSERS", (SPEC,)):
            stats = harness.worker.run(force=True)
        self.assertGreater(stats["routing"]["fresh_support"], 0)
        self.assertEqual(len(harness.store.evidence_of(ident)), 2)

    def test_legacy_held_source_is_baselined_without_freshness_credit(self):
        harness, ident = self._harness(relation="same")
        node = ("memory", "900")
        ledger = harness.store.support
        self.assertFalse(ledger.observe(ident, node, revision="old", previously_held=True))
        self.assertFalse(ledger.observe(ident, node, revision="old", previously_held=True))
        self.assertTrue(ledger.observe(ident, node, revision="changed", previously_held=True))

    def test_memory_revision_ignores_maintenance_but_detects_content_edits(self):
        from app.core.concepts.concept_support import memory_revision

        memory = MemStub(100, "An established fact", "fact", 0.2)
        before = memory_revision(memory)
        memory.salience = 0.9
        memory.metadata = {"confidence": 0.8, "updated_at": "2026-10-07"}
        self.assertEqual(memory_revision(memory), before)
        memory.content = "A substantive correction to that fact"
        self.assertNotEqual(memory_revision(memory), before)

    def test_support_receipts_survive_store_reload(self):
        harness, ident = self._harness(relation="same")
        self._run(harness)
        harness.store.load_all()
        with patch("app.core.concepts.concept_synthesis_worker.CONCEPT_PROPOSERS", (SPEC,)):
            stats = harness.worker.run(force=True)
        self.assertEqual(stats["routing"]["fresh_support"], 0)

    def test_capped_uncertainty_settles_as_same_without_a_twin(self):
        harness, ident = self._harness()
        self._run(harness)
        harness.ollama._responder = lambda system, user: {
            "concepts": [
                {
                    "reinforces_id": ident,
                    "evidence_cluster_reps": [100, 101],
                    "evidence_relation": "same",
                    "rationale": "The same claim",
                }
            ]
        }
        with self._advance(harness):
            stats = self._run(harness)
        self.assertEqual(stats["routing"]["same_claim"], 1)
        self.assertEqual(stats["routing"]["pending"], 0)
        self.assertEqual(len(harness.store.all()), 1)
        self.assertEqual(len(harness.store.evidence_of(ident)), 2)

    def test_retry_uses_current_source_text_and_can_create_a_distinct_claim(self):
        harness, ident = self._harness(relation="related")
        self._run(harness)
        harness.mem._by_id[100].content = "An updated concrete observation"
        prompts = []

        def responder(system, user):
            prompts.append(user)
            return {
                "concepts": [
                    {
                        "label": "A different supported principle",
                        "evidence_cluster_reps": [100, 101],
                        "confidence": 0.6,
                        "rationale": "A separate recurring choice",
                        "compared_to": ident,
                        "distinction": "Names a separate choice under different conditions.",
                    }
                ]
            }

        harness.ollama._responder = responder
        with self._advance(harness):
            stats = self._run(harness)
        self.assertEqual(stats["routing"]["new_claim"], 1)
        self.assertEqual(stats["added"], 1)
        self.assertIn("An updated concrete observation", prompts[0])
        self.assertEqual(len(harness.store.evidence_of(ident)), 2)

    def test_retry_pins_target_outside_a_full_original_list(self):
        harness, ident = self._harness(relation="uncertain")
        self._run(harness)
        original = []
        for index in range(40):
            other = Concept(
                label=f"Other claim {index}",
                subject="user",
                kind="identity",
                embedding=harness_tests.EvidenceAdmissionTests._ON,
            )
            harness.store.add(other)
            original.append(harness.worker._existing_row(other))
        receipt = next(iter(harness.worker._reconsiderations.values()))
        inputs = harness.worker._decode_input(receipt["manifest"])
        inputs["existing"] = original
        receipt["manifest"] = _encode(inputs)
        harness.store.get(ident).status = "dormant"
        harness.ollama._responder = lambda system, user: {
            "concepts": [
                {
                    "reinforces_id": ident,
                    "evidence_cluster_reps": [100, 101],
                    "evidence_relation": "same",
                    "rationale": "The archived claim is supported",
                }
            ]
        }
        with (
            self._advance(harness),
            patch.object(harness.worker, "_existing_for", return_value=original),
        ):
            stats = self._run(harness)
        self.assertEqual(stats["routing"]["same_claim"], 1)
        self.assertEqual(stats["routing"]["pending"], 0)

    def test_uncertain_retries_are_bounded_and_cooldown_is_not_reset(self):
        harness, _ = self._harness(relation="uncertain")
        self._run(harness)
        with self._advance(harness, 1):
            self.assertEqual(self._run(harness)["llm_calls"], 0)
        for attempt in range(MAX_ATTEMPTS):
            with self._advance(harness, 7 * (attempt + 1)):
                self._run(harness)
        states = harness.worker.synthesis_diagnostics()["queue_states"]
        self.assertEqual(states.get("exhausted"), 1)
        with self._advance(harness, 30):
            self.assertEqual(self._run(harness)["llm_calls"], 0)

    def test_offtopic_does_not_freeze_out_a_reconsideration(self):
        harness, ident = self._harness(relation="same", off_topic=True)
        stats = self._run(harness)
        self.assertEqual(stats["routing"]["unique_offtopic"], 2)
        self.assertEqual(stats["routing"]["pending"], 1)
        self.assertEqual(harness.store.get(ident).last_reinforced_at, "2026-01-01T00:00:00+00:00")

    def test_queue_expiry_and_storage_caps(self):
        harness, _ = self._harness(relation="uncertain")
        self._run(harness)
        manifest = _encode({"memories": [harness.mem._by_id[100]], "existing": []})
        for source in range(QUEUE_CAP + 20):
            harness.worker._queue_reconsideration(
                "user",
                "identity",
                manifest,
                [("memory", str(source))],
                reason="uncertain",
                targets=[],
            )
        harness.worker._routing_finish({})
        raw = harness.worker._kv_get(ROUTING_KEY)
        self.assertLessEqual(len(json.loads(raw)), QUEUE_CAP)
        self.assertLessEqual(len(raw), QUEUE_BYTES)
        with self._advance(harness, 8 * 24):
            stats = self._run(harness)
        self.assertEqual(stats["routing"]["pending"], 0)
        self.assertGreater(stats["routing"]["expired"], 0)

    def test_pending_receipts_survive_sustained_inflow_until_their_first_retry(self):
        harness, _ = self._harness()
        worker = harness.worker
        start = timephrase.utcnow()
        stats = {}
        with patch("app.core.infra.timephrase.utcnow", return_value=start):
            worker._routing_begin(stats)
            for index in range(3):
                worker._queue_reconsideration(
                    "user", "identity", {"context": "x" * 32000},
                    [("memory", str(index))], reason="unclaimed", targets=[],
                )
            original = list(worker._reconsiderations)
            worker._routing_finish(stats)
        for hour in range(1, 8):
            with patch("app.core.infra.timephrase.utcnow",
                       return_value=start + timedelta(hours=hour)):
                worker._routing_begin(stats)
                for index in range(50):
                    worker._queue_reconsideration(
                        "user", "identity", {"context": "x" * 32000},
                        [("memory", str(hour * 100 + index))], reason="unclaimed", targets=[],
                    )
                worker._routing_finish(stats)
                self.assertTrue(set(original) <= worker._reconsiderations.keys())
                self.assertLessEqual(len(worker._kv_get(ROUTING_KEY)), QUEUE_BYTES)
                self.assertGreater(stats["routing"]["capacity_declined"], 0)
                if hour >= 6:
                    self.assertEqual(worker._reconsider_due[:3], original)
                    self.assertGreaterEqual(stats["routing"]["due"], 3)

    def test_capacity_release_prefers_settled_receipts_and_keeps_pending_age(self):
        harness, _ = self._harness()
        worker = harness.worker
        stats = {}
        worker._routing_begin(stats)
        for index in range(QUEUE_CAP):
            worker._queue_reconsideration(
                "user", "identity", {}, [("memory", str(index))],
                reason="unclaimed", targets=[],
            )
        original = list(worker._reconsiderations)
        before = dict(worker._reconsiderations[original[0]])
        worker._queue_reconsideration(
            "user", "identity", {"fresh": True}, [("memory", "0")],
            reason="unclaimed", targets=[],
        )
        refreshed = worker._reconsiderations[original[0]]
        for field in ("first_at", "next_at", "expires_at", "attempts"):
            self.assertEqual(refreshed[field], before[field])
        worker._reconsiderations[original[1]]["state"] = "no_claim"
        worker._queue_reconsideration(
            "user", "identity", {}, [("memory", "new")], reason="unclaimed", targets=[],
        )
        self.assertNotIn(original[1], worker._reconsiderations)
        self.assertIn(original[0], worker._reconsiderations)
        self.assertEqual(len(worker._reconsiderations), QUEUE_CAP)

    def test_oversize_refresh_keeps_the_original_pending_manifest(self):
        harness, _ = self._harness()
        worker = harness.worker
        worker._routing_begin({})
        worker._queue_reconsideration(
            "user", "identity", {"old": True}, [("memory", "0")],
            reason="unclaimed", targets=[],
        )
        worker._queue_reconsideration(
            "user", "identity", {"context": "x" * QUEUE_BYTES}, [("memory", "0")],
            reason="unclaimed", targets=[],
        )
        self.assertEqual(next(iter(worker._reconsiderations.values()))["manifest"],
                         {"old": True})
        self.assertEqual(worker._routing_stats["manifest_refresh_deferred"], 1)

    def test_retained_oldest_receipt_is_actually_reconsidered_after_inflow(self):
        harness, ident = self._harness(relation="uncertain")
        self._run(harness)
        worker = harness.worker
        original = next(iter(worker._reconsiderations.values()))
        for index in range(QUEUE_CAP + 20):
            worker._queue_reconsideration(
                "user", "identity", original["manifest"], [("memory", str(1000 + index))],
                reason="unclaimed", targets=[],
            )
        worker._routing_finish({})
        harness.ollama._responder = lambda system, user: {
            "concepts": [{"reinforces_id": ident, "evidence_cluster_reps": [100, 101],
                          "evidence_relation": "same", "rationale": "Current support"}],
        }
        with self._advance(harness):
            stats = self._run(harness)
        self.assertEqual(worker._reconsiderations[original["key"]]["state"], "same_claim")
        self.assertEqual(worker._reconsiderations[original["key"]]["attempts"], 1)
        self.assertLessEqual(stats["routing"]["reconsidered"], MAX_RETRIES_PER_RUN)

    def test_disabled_kind_is_not_retried(self):
        harness, _ = self._harness(relation="uncertain")
        self._run(harness)
        with (
            self._advance(harness),
            patch.object(harness.worker, "_retry_enabled", return_value=False),
        ):
            self.assertEqual(self._run(harness)["llm_calls"], 0)

    def test_archive_slots_survive_a_crowded_active_pool(self):
        harness, _ = self._harness()
        for index in range(50):
            harness.store.add(
                Concept(
                    label=f"Active comparison {index}",
                    subject="user",
                    kind="identity",
                    status="active",
                    embedding=harness_tests.EvidenceAdmissionTests._ON,
                )
            )
        archived = Concept(
            label="An archived comparison",
            subject="user",
            kind="identity",
            status="retired",
            embedding=0.8 * harness_tests.EvidenceAdmissionTests._ON
            + 0.6 * harness_tests.EvidenceAdmissionTests._OFF,
        )
        harness.store.add(archived)
        selected = harness.worker._existing_for(SPEC, memories=[harness.mem.get(100)])
        self.assertIn(archived.concept_id, [item.id for item in selected])
        self.assertLessEqual(len(selected), 40)

    def test_unembedded_fallback_rotates_beyond_the_first_page(self):
        harness, _ = self._harness()
        for index in range(50):
            harness.store.add(
                Concept(
                    label=f"Archived comparison {index}",
                    subject="user",
                    kind="identity",
                    status="retired",
                )
            )
        first = {item.id for item in harness.worker._existing_for(SPEC)}
        second = {item.id for item in harness.worker._existing_for(SPEC)}
        self.assertTrue(second - first)
        self.assertLessEqual(len(first), 40)
        self.assertLessEqual(len(second), 40)

    def test_generalization_depth_is_filtered_before_the_comparison_cap(self):
        harness, base = self._harness()
        parents = []
        for index in range(40):
            parent = Concept(
                label=f"First-order view {index}",
                subject="user",
                kind="generalization",
                evidence_model="meta",
            )
            harness.store.add(parent)
            harness.store.add_edge(
                ConceptEdge(
                    src_type="concept",
                    src_id=str(base),
                    dst_type="concept",
                    dst_id=str(parent.concept_id),
                    relation="evidence",
                )
            )
            parents.append(parent)
        stacked = Concept(
            label="Second-order view", subject="user", kind="generalization", evidence_model="meta"
        )
        harness.store.add(stacked)
        harness.store.add_edge(
            ConceptEdge(
                src_type="concept",
                src_id=str(parents[0].concept_id),
                dst_type="concept",
                dst_id=str(stacked.concept_id),
                relation="evidence",
            )
        )
        selected = harness.worker._existing_generalizations(
            replace(SPEC, kind="generalization"), depth=2
        )
        self.assertEqual([item.id for item in selected], [stacked.concept_id])

    def test_diagnostics_are_content_free(self):
        harness, _ = self._harness(relation="uncertain")
        self._run(harness)
        result = json.dumps(harness.worker.synthesis_diagnostics())
        self.assertNotIn("Specific observation", result)
        self.assertNotIn("settled principle", result)
        self.assertEqual(
            harness.worker.synthesis_diagnostics()["limits"]["calls_per_run"], MAX_RETRIES_PER_RUN
        )

    def test_manifests_contain_references_not_memory_text(self):
        harness, _ = self._harness()
        memory = harness.mem._by_id[100]
        manifest = _encode({"memories": [memory], "existing": []})
        self.assertNotIn(memory.content, json.dumps(manifest))
        self.assertEqual(harness.worker._decode_input(manifest)["memories"][0], memory)

    def test_ordered_inputs_keep_source_order(self):
        harness, _ = self._harness()
        memories = [harness.mem._by_id[101], harness.mem._by_id[100]]
        manifest = _encode(NarrativeCandidate(101, "A closed arc", "user", memories))
        decoded = harness.worker._decode_input(manifest)
        self.assertEqual([memory.id for memory in decoded.memories], [101, 100])

    def test_bad_initial_answer_does_not_checkpoint_inputs(self):
        harness, _ = self._harness()
        with patch.object(harness.ollama, "chat_stream", return_value=iter(["not JSON"])):
            stats = self._run(harness)
        self.assertIsNone(harness.worker._kv_get("concept_synth.cluster_sigs"))
        self.assertEqual(stats["routing"]["llm_unusable"], 1)
        self.assertEqual(stats["routing"]["pending"], 1)
        self.assertEqual(self._run(harness)["reinforced"], 1)

    def test_bad_retry_answer_stays_pending(self):
        harness, _ = self._harness(relation="uncertain")
        self._run(harness)
        with (
            self._advance(harness),
            patch.object(
                harness.ollama,
                "chat_stream",
                return_value=iter(["truncated answer"]),
            ),
        ):
            stats = self._run(harness)
        self.assertEqual(stats["routing"]["pending"], 1)
        self.assertEqual(stats["routing"]["llm_unusable"], 1)

    def test_retry_call_budget_is_global(self):
        harness, ident = self._harness(relation="uncertain")
        self._run(harness)
        rows = list(harness.worker._reconsiderations.values())
        for source in (100, 101):
            harness.worker._queue_reconsideration(
                "user",
                "identity",
                rows[0]["manifest"],
                [("cluster", str(source))],
                reason="uncertain",
                targets=[ident],
            )
        harness.worker._routing_finish({})
        with self._advance(harness):
            stats = self._run(harness)
        self.assertEqual(stats["routing"]["reconsidered"], MAX_RETRIES_PER_RUN)
        self.assertEqual(stats["llm_calls"], MAX_RETRIES_PER_RUN)

    def test_retry_preserves_the_closed_arc_gate(self):
        harness, _ = self._harness()
        self._run(harness)
        harness.worker._reconsiderations.clear()
        harness.mem._by_id[102] = MemStub(102, "A third step", "fact", 0.2)
        candidate = NarrativeCandidate(
            100,
            "A possible arc",
            "user",
            [harness.mem._by_id[ident] for ident in (100, 101, 102)],
        )
        harness.worker._queue_reconsideration(
            "user",
            "narrative",
            _encode({"candidates": [candidate], "min_chain": 3, "existing": []}),
            [("memory", str(ident)) for ident in (100, 101, 102)],
            reason="unclaimed",
            targets=[],
        )
        harness.worker._routing_finish({})
        harness.ollama._responder = lambda system, user: {
            "concepts": [
                {
                    "label": "An unjustified arc",
                    "arc_index": 0,
                    "closed": False,
                    "evidence_memory_ids": [100, 101, 102],
                    "rationale": "Not closed",
                }
            ]
        }
        with self._advance(harness):
            stats = {"added": 0, "reinforced": 0, "by_subject": {}}
            harness.worker._routing_begin(stats)
            harness.worker._run_reconsiderations(
                ProposerContext(harness.worker._call_llm),
                (NARRATIVE_SPEC,),
                stats,
            )
        self.assertEqual(stats["added"], 0)
        self.assertEqual(harness.store.list_by(kind="narrative"), [])

    def test_reinforcement_exclusion_survives_a_round_trip(self):
        harness, ident = self._harness()
        existing = ExistingConcept(ident, "Old wording", reinforce=False)
        decoded = harness.worker._decode_input(_encode(existing))
        self.assertFalse(decoded.reinforce)

    def test_coactivation_is_rebuilt_not_serialized(self):
        harness, _ = self._harness()
        manifest = _encode({"coactivation": [object()]})
        self.assertEqual(harness.worker._decode_input(manifest)["coactivation"], [])

    def test_public_diagnostics_work_before_first_run(self):
        harness, _ = self._harness()
        facade = type("Facade", (MemoryFacadeMixin,), {})()
        self.assertEqual(facade.concept_synthesis_diagnostics(), {"enabled": False})
        facade._concept_synthesis_worker = harness.worker
        self.assertEqual(facade.concept_synthesis_diagnostics()["last_run"], {})
