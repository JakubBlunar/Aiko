"""Tests for the L2 :class:`ConceptSynthesisWorker` + proposers.

Covers the two identity proposers (user / aiko), candidate creation,
evidence-edge shape, single-source rejection, dedupe -> reinforce, the
``candidate``-only status, and -- the reason the worker is incremental --
kv_meta dirty-tracking: bounded batches that drain across runs, size-delta
thresholds, and clean-run no-ops with zero LLM calls.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from app.core.concepts.concept_event_store import ConceptEventStore
from app.core.concepts.concept_store import Concept, ConceptStore
from app.core.concepts.concept_synthesis_worker import ConceptSynthesisWorker
from app.core.infra.chat_database import ChatDatabase


# ── stubs ──────────────────────────────────────────────────────────────


class FakeEmbedder:
    """Deterministic per-text unit-ish vectors: identical text -> identical
    vector (cos 1.0, dedupe hit); distinct text -> ~orthogonal (cos ~0)."""

    def embed(self, text: str) -> np.ndarray:
        seed = int(hashlib.md5((text or "").encode()).hexdigest()[:8], 16)
        rng = np.random.RandomState(seed)
        return rng.randn(16).astype(np.float32)


class ClusterStub:
    def __init__(
        self, rep: int, summary: str, size: int, kinds,
        cluster_id: int | None = None, member_ids=None,
    ):
        self.cluster_id = rep if cluster_id is None else cluster_id
        self.representative_id = rep
        self.summary = summary
        self.size = size
        self.member_ids = (
            list(member_ids) if member_ids is not None else list(range(size))
        )
        self.member_kinds = tuple(kinds)


class TopicGraphStub:
    def __init__(self, clusters):
        self.persistent = True
        self._clusters = clusters

    def topic_clusters(self):
        return list(self._clusters)


class MemStub:
    def __init__(
        self, mid: int, content: str, kind: str, salience: float,
        metadata=None, embedding=None, created_at="", event_time=None,
    ):
        self.id = mid
        self.content = content
        self.kind = kind
        self.salience = salience
        self.metadata = metadata or {}
        self.embedding = embedding
        self.created_at = created_at
        # L8 narrative pass orders member memories by ``event_time`` (falling
        # back to ``created_at``).
        self.event_time = event_time


class MemoryStoreStub:
    def __init__(self, self_memories, earliest=None, extra_memories=None):
        self._self = self_memories
        # ``extra_memories`` carries non-self rows (e.g. shared_moment) so the
        # L7 ritual pass can enumerate them via ``iter_by_kind``. Kept separate
        # (not snapshotted into one list) so tests that append to ``_self`` /
        # ``_extra`` after construction are reflected by the iterators.
        self._extra = list(extra_memories or [])
        self._by_id = {
            m.id: m for m in list(self_memories) + self._extra
        }
        self._earliest = earliest

    def _all_rows(self):
        return list(self._self) + list(self._extra)

    def get(self, mid: int):
        return self._by_id.get(int(mid))

    def get_many(self, memory_ids):
        out = {}
        for raw in memory_ids:
            try:
                mid = int(raw)
            except (TypeError, ValueError):
                continue
            mem = self._by_id.get(mid)
            if mem is not None:
                out[mid] = mem
        return out

    def iter_by_kinds(self, kinds):
        ks = set(kinds)
        return [m for m in self._all_rows() if m.kind in ks]

    def iter_by_kind(self, kind: str):
        return [m for m in self._all_rows() if m.kind == kind]

    def earliest_created_at(self):
        return self._earliest


class FakeOllama:
    """``chat_stream`` yields a single JSON blob produced by ``responder``
    (branching on system/user text so user vs aiko passes can differ)."""

    def __init__(self, responder):
        self._responder = responder
        self.calls = 0

    def chat_stream(self, messages, **kw):
        self.calls += 1
        system = messages[0]["content"]
        user = messages[1]["content"]
        yield json.dumps(self._responder(system, user))


def _agent(*, concepts=True, synth=True):
    return types.SimpleNamespace(
        concepts_enabled=concepts,
        concept_synthesis_enabled=synth,
    )


def _mem_settings(
    *,
    cap_clusters=10,
    cap_aiko=40,
    delta=3,
    interval=1800,
    min_clusters=0,
    min_history_days=0.0,
):
    # L21 maturity gate is disabled by default here (min_clusters=0,
    # min_history_days=0) so the proposer/dirty-tracking tests exercise
    # their own logic; the dedicated maturity tests set thresholds.
    return types.SimpleNamespace(
        concept_synthesis_interval_seconds=interval,
        concept_synthesis_max_clusters_per_run=cap_clusters,
        concept_synthesis_max_aiko_memories=cap_aiko,
        concept_synthesis_dirty_size_delta=delta,
        concept_min_clusters=min_clusters,
        concept_min_history_days=min_history_days,
        # L8 narrative caps mirror the cluster cap so the narrative pass drains
        # in one run (matching the identity/value passes) instead of across
        # several -- otherwise the "clean 2nd run" incremental tests would see
        # leftover-dirty narrative clusters still firing.
        concept_synthesis_narrative_min_chain=3,
        concept_synthesis_max_narrative_clusters_per_run=cap_clusters,
        concept_synthesis_max_narrative_memories=cap_aiko,
        # L18 boundary anchor-batch cap (clusters ride the shared cluster cap).
        concept_synthesis_max_boundary_memories=cap_aiko,
        # L23 communication-style anchor-batch cap (same shape as boundary).
        concept_synthesis_max_comm_style_memories=cap_aiko,
    )


def _user_clusters(n: int = 6):
    return [
        ClusterStub(
            rep=100 + i,
            summary=f"topic {i}",
            size=20 - i,  # descending sizes -> deterministic focus order
            kinds=("fact", "preference", "event"),
        )
        for i in range(n)
    ]


def _self_memories():
    return [
        MemStub(1, "I felt proud helping debug the CPU issue.", "self", 0.9),
        MemStub(2, "Reflecting: I prefer being direct over hedging.", "reflection", 0.8),
        MemStub(3, "Diary: today I stayed calm under pressure.", "diary", 0.7),
        MemStub(4, "I noticed I enjoy explaining systems.", "self", 0.6),
    ]


class WorkerHarness:
    def __init__(
        self,
        responder,
        *,
        clusters=None,
        self_memories=None,
        agent=None,
        mem_settings=None,
        user_name=None,
        assistant_name=None,
        earliest=None,
        shared_moments=None,
        user_profile_store=None,
        style_signal_store=None,
        user_id_provider=None,
    ):
        tmp = tempfile.mkdtemp()
        self.path = Path(tmp) / "test.db"
        self.db = ChatDatabase(self.path)
        self.store = ConceptStore(self.db)
        self.events = ConceptEventStore(self.db)
        self.topic = TopicGraphStub(
            clusters if clusters is not None else _user_clusters()
        )
        self.mem = MemoryStoreStub(
            self_memories if self_memories is not None else _self_memories(),
            earliest=earliest,
            extra_memories=shared_moments,
        )
        self.ollama = FakeOllama(responder)
        self.worker = ConceptSynthesisWorker(
            concept_store=self.store,
            topic_graph=self.topic,
            memory_store=self.mem,
            embedder=FakeEmbedder(),
            ollama=self.ollama,
            chat_model="test-model",
            cancel_event=threading.Event(),
            agent_settings=agent or _agent(),
            memory_settings=mem_settings or _mem_settings(),
            kv_get=self.db.kv_get,
            kv_set=self.db.kv_set,
            clock=lambda: datetime.now(timezone.utc),
            concept_event_store=self.events,
            user_display_name_provider=(
                (lambda: user_name) if user_name is not None else None
            ),
            assistant_display_name_provider=(
                (lambda: assistant_name)
                if assistant_name is not None
                else None
            ),
            user_profile_store=user_profile_store,
            style_signal_store=style_signal_store,
            user_id_provider=user_id_provider,
        )


def _both_responder(system, user):
    """Realistic responder: a user identity concept (spans 2 clusters) and
    an aiko identity concept (spans 2 self memories).     Value passes (L10) and boundary passes (L18) are
    a deliberate no-op here so the identity-focused tests keep their counts;
    dedicated value coverage lives in ``ValueProposerTests`` and boundary in
    ``tests/test_l18_boundary_concepts.py``."""
    if "VALUE concepts about" in system or "her own VALUES" in system:
        return {"concepts": []}
    if "BOUNDARIES" in system:
        return {"concepts": []}
    if "communication-style" in system:
        return {"concepts": []}
    if "HERSELF" in system:
        return {
            "concepts": [
                {
                    "label": "I value being direct",
                    "evidence_memory_ids": [1, 2],
                    "rationale": "shows up across self + reflection",
                    "confidence": 0.8,
                }
            ]
        }
    return {
        "concepts": [
            {
                "label": "Systems thinker",
                "evidence_cluster_reps": [100, 101],
                "rationale": "links two technical clusters",
                "confidence": 0.7,
            }
        ]
    }


# ── tests ────────────────────────────────────────────────────────────────


class ProposalTests(unittest.TestCase):
    def test_user_pass_creates_candidate_with_cluster_edges(self) -> None:
        h = WorkerHarness(_both_responder)
        stats = h.worker.run()
        users = h.store.list_by(subject="user", kind="identity")
        self.assertEqual(len(users), 1)
        c = users[0]
        self.assertEqual(c.label, "Systems thinker")
        self.assertEqual(c.status, "candidate")
        self.assertEqual(c.subject, "user")
        self.assertEqual(c.evidence_model, "set")
        ev = h.store.evidence_of(c.concept_id)
        self.assertEqual(len(ev), 2)
        self.assertTrue(all(e.src_type == "cluster" for e in ev))
        self.assertEqual({e.src_id for e in ev}, {"100", "101"})
        self.assertEqual(stats["by_subject"]["user"]["added"], 1)

    def test_aiko_pass_creates_candidate_with_memory_edges(self) -> None:
        h = WorkerHarness(_both_responder)
        h.worker.run()
        aiko = h.store.list_by(subject="aiko", kind="identity")
        self.assertEqual(len(aiko), 1)
        c = aiko[0]
        self.assertEqual(c.label, "I value being direct")
        self.assertEqual(c.subject, "aiko")
        ev = h.store.evidence_of(c.concept_id)
        self.assertEqual(len(ev), 2)
        self.assertTrue(all(e.src_type == "memory" for e in ev))
        self.assertEqual({e.src_id for e in ev}, {"1", "2"})

    def test_single_source_proposal_is_dropped(self) -> None:
        def responder(system, user):
            if "BOUNDARIES" in system:
                return {"concepts": []}
            if "HERSELF" in system:
                return {"concepts": [
                    {"label": "lonely trait", "evidence_memory_ids": [1],
                     "confidence": 0.9}
                ]}
            return {"concepts": [
                {"label": "one cluster only", "evidence_cluster_reps": [100],
                 "confidence": 0.9}
            ]}

        h = WorkerHarness(responder)
        stats = h.worker.run()
        self.assertEqual(h.store.count(), 0)
        self.assertEqual(stats["added"], 0)

    def test_unknown_evidence_ids_are_filtered(self) -> None:
        def responder(system, user):
            if "HERSELF" in system:
                return {"concepts": []}
            # one valid rep + junk -> only 1 valid -> dropped
            return {"concepts": [
                {"label": "ghost", "evidence_cluster_reps": [100, 99999],
                 "confidence": 0.9}
            ]}

        h = WorkerHarness(responder)
        h.worker.run()
        self.assertEqual(h.store.count(), 0)

    def test_all_created_concepts_are_candidate(self) -> None:
        h = WorkerHarness(_both_responder)
        h.worker.run()
        self.assertTrue(all(c.status == "candidate" for c in h.store.all()))


class ReinforceTests(unittest.TestCase):
    def test_second_dirty_run_reinforces_not_duplicates(self) -> None:
        h = WorkerHarness(_both_responder)
        h.worker.run()
        users = h.store.list_by(subject="user", kind="identity")
        self.assertEqual(len(users), 1)
        first_reinforced_at = users[0].last_reinforced_at

        # Make every cluster dirty again (size drift >= delta) and re-run.
        for c in h.topic._clusters:
            c.size += 5
        stats = h.worker.run()

        again = h.store.list_by(subject="user", kind="identity")
        self.assertEqual(len(again), 1, "no duplicate concept row")
        self.assertEqual(stats["by_subject"]["user"]["reinforced"], 1)
        self.assertEqual(stats["by_subject"]["user"].get("added", 0), 0)
        self.assertNotEqual(again[0].last_reinforced_at, first_reinforced_at)
        self.assertEqual(len(h.store.evidence_of(again[0].concept_id)), 2)


class DiscoveryEventTests(unittest.TestCase):
    """The discovery timeline: a ``discovered`` event per NEW concept,
    with novelty derived from the dedupe nearest-neighbour cosine, and
    NO event for a reinforcement/dup."""

    def test_new_concepts_emit_discovered_events(self) -> None:
        h = WorkerHarness(_both_responder)
        h.worker.run()
        events = h.events.list(limit=100)
        self.assertEqual(len(events), 2, "one event per new concept")
        self.assertTrue(all(e.event_type == "discovered" for e in events))
        subjects = {e.subject for e in events}
        self.assertEqual(subjects, {"user", "aiko"})

    def test_event_count_matches_store(self) -> None:
        h = WorkerHarness(_both_responder)
        h.worker.run()
        self.assertEqual(h.events.count(), 2)

    def test_first_concept_of_kind_is_maximally_novel(self) -> None:
        h = WorkerHarness(_both_responder)
        h.worker.run()
        # No prior concepts of either (subject, kind) -> nearest empty ->
        # novelty 1.0 for both.
        for e in h.events.list(limit=100):
            self.assertEqual(e.novelty, 1.0)

    def test_source_kinds_and_reason_recorded(self) -> None:
        h = WorkerHarness(_both_responder)
        h.worker.run()
        by_subject = {e.subject: e for e in h.events.list(limit=100)}
        self.assertEqual(by_subject["user"].source_kinds, "cluster")
        self.assertEqual(by_subject["aiko"].source_kinds, "memory")
        self.assertIn("cluster", by_subject["user"].reason)
        self.assertTrue(by_subject["aiko"].reason)

    def test_reinforcement_emits_no_event(self) -> None:
        h = WorkerHarness(_both_responder)
        h.worker.run()
        self.assertEqual(h.events.count(), 2)
        # Second dirty run reinforces (dedupe hit) -> no new events.
        for c in h.topic._clusters:
            c.size += 5
        h.worker.run()
        self.assertEqual(h.events.count(), 2, "reinforce adds no event")

    def test_no_event_store_is_safe_noop(self) -> None:
        h = WorkerHarness(_both_responder)
        h.worker._concept_event_store = None
        stats = h.worker.run()
        # Concepts still created; just no timeline rows.
        self.assertEqual(stats["added"], 2)
        self.assertEqual(h.events.count(), 0)


class ExistingAwarenessTests(unittest.TestCase):
    def _seed_user_concept(self, store, label="Systems thinker"):
        return store.add(
            Concept(
                label=label,
                kind="identity",
                subject="user",
                embedding=FakeEmbedder().embed(label),
                status="candidate",
            )
        )

    def test_existing_concepts_injected_into_prompt(self) -> None:
        h = WorkerHarness(lambda s, u: {"concepts": []})
        cid = self._seed_user_concept(h.store)
        captured: dict[str, str] = {}

        def responder(system, user):
            # Target the user *identity* pass specifically (value + aiko
            # passes also lack "HERSELF" now).
            if "IDENTITY concepts about" in system:
                captured["user"] = user
            return {"concepts": []}

        h.ollama._responder = responder
        h.worker.run()
        self.assertIn("Systems thinker", captured["user"])
        self.assertIn(f"[{cid}]", captured["user"])

    def test_display_names_injected_into_prompts(self) -> None:
        h = WorkerHarness(
            lambda s, u: {"concepts": []},
            user_name="Jacob",
            assistant_name="Aiko",
        )
        captured: dict[str, str] = {}

        def responder(system, user):
            captured["aiko" if "HERSELF" in system else "user"] = system
            return {"concepts": []}

        h.ollama._responder = responder
        h.worker.run()
        # User proposer must name the user, not say "the user".
        self.assertIn("Jacob", captured["user"])
        self.assertNotIn("about a person", captured["user"])
        # Aiko proposer names both parties.
        self.assertIn("Aiko", captured["aiko"])
        self.assertIn("Jacob", captured["aiko"])

    def test_missing_name_provider_falls_back(self) -> None:
        h = WorkerHarness(lambda s, u: {"concepts": []})
        captured: dict[str, str] = {}

        def responder(system, user):
            if "HERSELF" not in system:
                captured["user"] = system
            return {"concepts": []}

        h.ollama._responder = responder
        h.worker.run()
        self.assertIn("the user", captured["user"])

    def test_reinforce_by_id_attaches_evidence_no_duplicate(self) -> None:
        h = WorkerHarness(lambda s, u: {"concepts": []})
        cid = self._seed_user_concept(h.store)

        def responder(system, user):
            if "HERSELF" in system:
                return {"concepts": []}
            return {"concepts": [
                {"reinforces_id": cid, "evidence_cluster_reps": [100, 101],
                 "rationale": "focus cluster adds support"}
            ]}

        h.ollama._responder = responder
        stats = h.worker.run()

        users = h.store.list_by(subject="user", kind="identity")
        self.assertEqual(len(users), 1, "reinforcement must not duplicate")
        ev = h.store.evidence_of(cid)
        self.assertEqual({e.src_id for e in ev}, {"100", "101"})
        self.assertTrue(all(e.src_type == "cluster" for e in ev))
        self.assertEqual(stats["by_subject"]["user"]["reinforced"], 1)
        self.assertEqual(stats["added"], 0)
        self.assertIsNotNone(h.store.get(cid).last_reinforced_at)

    def test_reinforce_needs_only_one_source(self) -> None:
        h = WorkerHarness(lambda s, u: {"concepts": []})
        cid = self._seed_user_concept(h.store)

        def responder(system, user):
            if "HERSELF" in system:
                return {"concepts": []}
            # single source: rejected for a NEW concept, allowed to reinforce.
            return {"concepts": [
                {"reinforces_id": cid, "evidence_cluster_reps": [100],
                 "rationale": "one new cluster"}
            ]}

        h.ollama._responder = responder
        stats = h.worker.run()
        self.assertEqual(stats["by_subject"]["user"]["reinforced"], 1)
        self.assertEqual({e.src_id for e in h.store.evidence_of(cid)}, {"100"})

    def test_unknown_reinforces_id_is_ignored(self) -> None:
        def responder(system, user):
            if "HERSELF" in system:
                return {"concepts": []}
            return {"concepts": [
                {"reinforces_id": 99999, "evidence_cluster_reps": [100, 101],
                 "rationale": "hallucinated id"}
            ]}

        h = WorkerHarness(responder)
        h.worker.run()
        # No matching existing id + no label -> dropped, nothing created.
        self.assertEqual(h.store.list_by(subject="user", kind="identity"), [])
        self.assertEqual(h.store.count(), 0)


class IncrementalTests(unittest.TestCase):
    def test_bounded_batches_drain_across_runs(self) -> None:
        # 6 dirty clusters, cap 2 -> drains 2/run over 3 runs, then clean.
        h = WorkerHarness(
            lambda s, u: {"concepts": []},
            mem_settings=_mem_settings(cap_clusters=2),
        )
        r1 = h.worker.run()
        self.assertEqual(r1["user_dirty_total"], 6)
        self.assertEqual(r1["user_processed"], 2)
        self.assertEqual(r1["user_dirty_remaining"], 4)

        r2 = h.worker.run()
        self.assertEqual(r2["user_dirty_total"], 4)
        self.assertEqual(r2["user_processed"], 2)
        self.assertEqual(r2["user_dirty_remaining"], 2)

        r3 = h.worker.run()
        self.assertEqual(r3["user_dirty_total"], 2)
        self.assertEqual(r3["user_processed"], 2)
        self.assertEqual(r3["user_dirty_remaining"], 0)

        r4 = h.worker.run()
        self.assertEqual(r4["user_dirty_total"], 0)
        self.assertEqual(r4["user_processed"], 0)

    def test_clean_run_is_noop_with_zero_llm_calls(self) -> None:
        h = WorkerHarness(_both_responder)
        h.worker.run()  # first pass processes everything
        calls_after_first = h.ollama.calls
        self.assertGreater(calls_after_first, 0)

        stats = h.worker.run()  # nothing dirty now
        self.assertEqual(stats["added"], 0)
        self.assertEqual(stats["reinforced"], 0)
        self.assertEqual(stats["llm_calls"], 0)
        self.assertEqual(h.ollama.calls, calls_after_first)

    def test_size_drift_below_delta_not_reprocessed(self) -> None:
        h = WorkerHarness(
            lambda s, u: {"concepts": []},
            mem_settings=_mem_settings(delta=3),
        )
        h.worker.run()  # clean baseline

        # +2 (< delta 3) on one cluster -> still clean.
        h.topic._clusters[0].size += 2
        r = h.worker.run()
        self.assertEqual(r["user_dirty_total"], 0)

        # +1 more (now +3 total >= delta) -> dirty.
        h.topic._clusters[0].size += 1
        r2 = h.worker.run()
        self.assertEqual(r2["user_dirty_total"], 1)

    def test_aiko_dirty_tracking(self) -> None:
        h = WorkerHarness(_both_responder)
        r1 = h.worker.run()
        self.assertTrue(r1["aiko_dirty"])
        r2 = h.worker.run()
        self.assertFalse(r2["aiko_dirty"])

    def test_force_bypasses_dirty_tracking(self) -> None:
        # Reproduces the "can't regenerate after deleting concepts" bug:
        # once the baseline is processed, an incremental run is a no-op,
        # but a forced run must re-propose from a clean corpus.
        h = WorkerHarness(_both_responder)
        h.worker.run()  # baseline: everything processed, sigs saved
        incremental = h.worker.run()
        self.assertEqual(incremental["llm_calls"], 0)
        self.assertFalse(incremental["aiko_dirty"])

        forced = h.worker.run(force=True)
        self.assertTrue(forced["aiko_dirty"])
        self.assertGreater(forced["user_dirty_total"], 0)
        self.assertGreater(forced["llm_calls"], 0)


class DominanceTests(unittest.TestCase):
    def test_aiko_dominant_clusters_excluded_from_user_pass(self) -> None:
        clusters = _user_clusters(2) + [
            ClusterStub(
                rep=900, summary="aiko musings", size=10,
                kinds=("self", "reflection", "self", "diary"),
            )
        ]
        h = WorkerHarness(
            lambda s, u: {"concepts": []}, clusters=clusters
        )
        r = h.worker.run()
        # only the 2 user-dominant clusters are dirty; rep 900 skipped.
        self.assertEqual(r["user_dirty_total"], 2)


def _aiko_clusters():
    """Aiko-dominant self-themes whose rep ids coincide with self-memory
    ids (1, 2) so the representative-dedup path is exercised."""
    return [
        ClusterStub(
            rep=1, summary="staying calm under pressure", size=6,
            kinds=("self", "reflection", "diary", "self"),
        ),
        ClusterStub(
            rep=2, summary="explaining systems clearly", size=5,
            kinds=("self", "self", "reflection"),
        ),
    ]


class AikoCombinedPassTests(unittest.TestCase):
    """L11: the aiko pass mines BOTH her self-themes (aiko-dominant
    clusters) AND her salient self-memories in one combined pass, so a
    self-concept can be grounded by a theme, a memory, or a mix."""

    def _harness(self, responder):
        return WorkerHarness(
            responder,
            clusters=_user_clusters() + _aiko_clusters(),
        )

    def test_concept_can_mix_cluster_and_memory_evidence(self) -> None:
        def responder(system, user):
            if "HERSELF" in system:  # identity_aiko
                return {"concepts": [{
                    "label": "I stay grounded and explain clearly",
                    "evidence_cluster_reps": [1],
                    "evidence_memory_ids": [3],
                    "confidence": 0.8,
                }]}
            return {"concepts": []}

        h = self._harness(responder)
        h.worker.run()
        aiko = h.store.list_by(subject="aiko", kind="identity")
        self.assertEqual(len(aiko), 1)
        c = aiko[0]
        ev = h.store.evidence_of(c.concept_id)
        self.assertEqual(
            {(e.src_type, e.src_id) for e in ev},
            {("cluster", "1"), ("memory", "3")},
        )
        self.assertEqual(c.distinct_source_count, 2)

    def test_cluster_and_memory_count_toward_min_sources(self) -> None:
        # One cluster + one memory = 2 distinct sources -> passes the gate
        # even though neither source type reaches 2 on its own.
        def responder(system, user):
            if "HERSELF" in system:
                return {"concepts": [{
                    "label": "I ground myself in specifics",
                    "evidence_cluster_reps": [2],
                    "evidence_memory_ids": [4],
                    "confidence": 0.7,
                }]}
            return {"concepts": []}

        h = self._harness(responder)
        h.worker.run()
        self.assertEqual(
            len(h.store.list_by(subject="aiko", kind="identity")), 1
        )

    def test_cluster_representative_dropped_from_memory_list(self) -> None:
        captured: dict[str, str] = {}

        def responder(system, user):
            # Exclude the L18 boundary aiko pass (also first-person / "HERSELF")
            # so we capture the identity self-theme prompt, not the boundary one.
            if "HERSELF" in system and "BOUNDARIES" not in system:
                captured["aiko"] = user
            return {"concepts": []}

        h = self._harness(responder)
        h.worker.run()
        prompt = captured["aiko"]
        # Themes list both reps; the memory section drops the rep memories
        # (1, 2) so a theme + its headline memory aren't two sources.
        self.assertIn("RECURRING SELF-THEMES", prompt)
        mem_section = prompt.split("NOTABLE SELF-MEMORIES:")[1]
        self.assertNotIn("[1]", mem_section)
        self.assertNotIn("[2]", mem_section)
        self.assertIn("[3]", mem_section)
        self.assertIn("[4]", mem_section)

    def test_cold_start_memories_only_still_proposes(self) -> None:
        # No aiko-dominant clusters (default user-only graph) -> the pass
        # degrades to memories-only and still creates memory-backed concepts.
        h = WorkerHarness(_both_responder)
        h.worker.run()
        aiko = h.store.list_by(subject="aiko", kind="identity")
        self.assertEqual(len(aiko), 1)
        ev = h.store.evidence_of(aiko[0].concept_id)
        self.assertTrue(all(e.src_type == "memory" for e in ev))

    def test_cluster_drift_alone_refires_aiko_pass(self) -> None:
        h = self._harness(lambda s, u: {"concepts": []})
        h.worker.run()  # baseline
        self.assertFalse(h.worker.run()["aiko_dirty"])  # clean

        for c in h.topic._clusters:
            if c.representative_id in (1, 2):
                c.size += 5  # >= delta
        self.assertTrue(h.worker.run()["aiko_dirty"])

    def test_memory_delta_alone_refires_aiko_pass(self) -> None:
        h = self._harness(lambda s, u: {"concepts": []})
        h.worker.run()  # baseline
        self.assertFalse(h.worker.run()["aiko_dirty"])  # clean

        extra = [
            MemStub(10, "I paused before reacting.", "self", 0.5),
            MemStub(11, "Reflecting on a hard call.", "reflection", 0.5),
            MemStub(12, "Diary: a quiet win today.", "diary", 0.5),
        ]
        h.mem._self.extend(extra)
        h.mem._by_id.update({m.id: m for m in extra})
        self.assertTrue(h.worker.run()["aiko_dirty"])

    def test_user_pass_still_excludes_aiko_clusters(self) -> None:
        h = self._harness(lambda s, u: {"concepts": []})
        r = h.worker.run()
        # 6 user-dominant clusters dirty; the 2 aiko-dominant reps (1, 2)
        # are handled by the aiko pass, not the user pass.
        self.assertEqual(r["user_dirty_total"], 6)


class GatingTests(unittest.TestCase):
    def _worker(self, agent):
        h = WorkerHarness(_both_responder, agent=agent)
        return h.worker

    def test_is_ready_requires_flags_and_persistence(self) -> None:
        now = datetime.now(timezone.utc)
        self.assertTrue(
            self._worker(_agent()).is_ready(now=now, last_run_at=None)
        )
        self.assertFalse(
            self._worker(_agent(concepts=False)).is_ready(
                now=now, last_run_at=None
            )
        )
        self.assertFalse(
            self._worker(_agent(synth=False)).is_ready(
                now=now, last_run_at=None
            )
        )

    def test_is_ready_false_when_graph_not_persistent(self) -> None:
        h = WorkerHarness(_both_responder)
        h.topic.persistent = False
        now = datetime.now(timezone.utc)
        self.assertFalse(h.worker.is_ready(now=now, last_run_at=None))

    def test_run_skips_when_disabled(self) -> None:
        h = WorkerHarness(_both_responder, agent=_agent(concepts=False))
        out = h.worker.run()
        self.assertTrue(out.get("skipped"))
        self.assertEqual(h.store.count(), 0)


class MaturityGateTests(unittest.TestCase):
    """L21 cold-start guard: nothing is proposed while the topic graph is
    too sparse / too young, but a manual ``force`` run always bypasses."""

    def test_immature_cluster_count_blocks_is_ready_and_run(self) -> None:
        # 6 clusters but the floor is 8 -> immature.
        h = WorkerHarness(
            _both_responder,
            clusters=_user_clusters(6),
            mem_settings=_mem_settings(min_clusters=8),
        )
        now = datetime.now(timezone.utc)
        self.assertFalse(h.worker.is_ready(now=now, last_run_at=None))
        out = h.worker.run()
        self.assertEqual(out.get("reason"), "immature_graph")
        self.assertEqual(h.store.count(), 0)

    def test_force_bypasses_immature_gate(self) -> None:
        h = WorkerHarness(
            _both_responder,
            clusters=_user_clusters(6),
            mem_settings=_mem_settings(min_clusters=8),
        )
        out = h.worker.run(force=True)
        self.assertFalse(out.get("skipped"))
        self.assertGreater(h.store.count(), 0)

    def test_mature_graph_runs_normally(self) -> None:
        h = WorkerHarness(
            _both_responder,
            clusters=_user_clusters(8),
            mem_settings=_mem_settings(min_clusters=8),
        )
        now = datetime.now(timezone.utc)
        self.assertTrue(h.worker.is_ready(now=now, last_run_at=None))
        out = h.worker.run()
        self.assertFalse(out.get("skipped"))
        self.assertGreater(h.store.count(), 0)

    def test_history_floor_blocks_until_enough_calendar_age(self) -> None:
        # Enough clusters, but the oldest memory is only ~1 day old and the
        # history floor is 3 days -> still immature.
        recent = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).isoformat()
        h = WorkerHarness(
            _both_responder,
            clusters=_user_clusters(8),
            mem_settings=_mem_settings(min_clusters=8, min_history_days=3.0),
            earliest=recent,
        )
        now = datetime.now(timezone.utc)
        self.assertFalse(h.worker.is_ready(now=now, last_run_at=None))
        # Old enough history clears the floor.
        old = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        h2 = WorkerHarness(
            _both_responder,
            clusters=_user_clusters(8),
            mem_settings=_mem_settings(min_clusters=8, min_history_days=3.0),
            earliest=old,
        )
        self.assertTrue(h2.worker.is_ready(now=now, last_run_at=None))


class SalvageParseTests(unittest.TestCase):
    """Truncated proposer responses must not drop the whole batch."""

    def test_salvage_recovers_complete_objects_from_truncated_array(
        self,
    ) -> None:
        from app.core.concepts.concept_synthesis_worker import (
            ConceptSynthesisWorker,
        )

        # Two full concepts then a third object clipped mid-string, exactly
        # how the aiko pass looked in the logs when it hit num_predict.
        truncated = (
            '{ "concepts": [ '
            '{ "label": "I value tactile objects", '
            '"evidence_memory_ids": [994, 1001], '
            '"rationale": "physical books", "confidence": 0.9 }, '
            '{ "label": "I keep small rituals", '
            '"evidence_memory_ids": [436, 476], '
            '"rationale": "garden and weather", "confidence": 0.85 }, '
            '{ "label": "I modulate my tone", '
            '"evidence_memory_ids": [966, 982], '
            '"rationale": "when Jacob needs ground'
        )
        out = ConceptSynthesisWorker._parse(truncated)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["label"], "I value tactile objects")
        self.assertEqual(out[1]["evidence_memory_ids"], [436, 476])

    def test_parse_prefers_well_formed_json(self) -> None:
        from app.core.concepts.concept_synthesis_worker import (
            ConceptSynthesisWorker,
        )

        good = (
            '{ "concepts": [ { "label": "a", '
            '"evidence_memory_ids": [1, 2], "confidence": 0.5 } ] }'
        )
        out = ConceptSynthesisWorker._parse(good)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["label"], "a")

    def test_parse_returns_empty_without_array(self) -> None:
        from app.core.concepts.concept_synthesis_worker import (
            ConceptSynthesisWorker,
        )

        self.assertEqual(ConceptSynthesisWorker._parse("no json here"), [])


class _ModeStub:
    def __init__(self, reps, labels, strength=0.9, bucket_by="session"):
        self.reps = tuple(reps)
        self.labels = tuple(labels)
        self.strength = strength
        self.bucket_by = bucket_by


class CoactivationHintTests(unittest.TestCase):
    """L4: the user proposer receives the co-activation modes and renders
    the TOPIC MODES hint; an empty/absent signal is a silent no-op."""

    def _capture_harness(self, responder):
        seen: dict[str, str] = {}

        def _wrap(system, user):
            # Capture the user *identity* pass (value/aiko passes also lack
            # "HERSELF" now that L10 value proposers are registered).
            if "IDENTITY concepts about" in system:
                seen["user"] = user
            return responder(system, user)

        h = WorkerHarness(_wrap)
        return h, seen

    def test_modes_section_rendered_in_user_prompt(self) -> None:
        h, seen = self._capture_harness(_both_responder)
        h.topic.cluster_coactivation = lambda **kw: [
            _ModeStub(reps=(100, 101), labels=("topic 0", "topic 1")),
        ]
        h.worker.run()
        prompt = seen.get("user", "")
        self.assertIn("TOPIC MODES", prompt)
        self.assertIn("[100, 101]", prompt)

    def test_empty_modes_omits_section(self) -> None:
        h, seen = self._capture_harness(_both_responder)
        h.topic.cluster_coactivation = lambda **kw: []
        stats = h.worker.run()
        self.assertNotIn("TOPIC MODES", seen.get("user", ""))
        # Synthesis still succeeds normally without the hint.
        self.assertEqual(stats["by_subject"]["user"]["added"], 1)

    def test_missing_coactivation_method_is_safe(self) -> None:
        # The default TopicGraphStub has no cluster_coactivation; the worker
        # must treat that as "no hint" and still run.
        h = WorkerHarness(_both_responder)
        self.assertFalse(hasattr(h.topic, "cluster_coactivation"))
        stats = h.worker.run()
        self.assertEqual(stats["by_subject"]["user"]["added"], 1)

    def test_coactivation_failure_is_swallowed(self) -> None:
        def _boom(**kw):
            raise RuntimeError("nope")

        h, seen = self._capture_harness(_both_responder)
        h.topic.cluster_coactivation = _boom
        stats = h.worker.run()
        self.assertNotIn("TOPIC MODES", seen.get("user", ""))
        self.assertEqual(stats["by_subject"]["user"]["added"], 1)


def _value_responder(system, user):
    """Branches all four proposer passes (identity/value x user/aiko) on a
    token unique to each system prompt, returning the evidence shape that
    pass reads (clusters for user, memories for aiko)."""
    if "VALUE concepts about" in system:  # value_user (cluster pass)
        return {"concepts": [
            {"label": "values owning his data",
             "evidence_cluster_reps": [100, 101], "confidence": 0.7}
        ]}
    if "her own VALUES" in system:  # value_aiko (memory pass)
        return {"concepts": [
            {"label": "I value honesty over agreeableness",
             "evidence_memory_ids": [1, 2], "confidence": 0.8}
        ]}
    if "HERSELF" in system:  # identity_aiko (memory pass)
        return {"concepts": [
            {"label": "I enjoy explaining systems",
             "evidence_memory_ids": [1, 4], "confidence": 0.8}
        ]}
    return {"concepts": [  # identity_user (cluster pass)
        {"label": "Systems thinker",
         "evidence_cluster_reps": [100, 101], "confidence": 0.7}
    ]}


class ValueProposerTests(unittest.TestCase):
    """L10: value proposers create ``kind=value`` candidates for both
    subjects, with the right evidence edges, alongside identity."""

    def test_value_passes_create_value_candidates(self) -> None:
        h = WorkerHarness(_value_responder)
        h.worker.run()
        uv = h.store.list_by(subject="user", kind="value")
        self.assertEqual(len(uv), 1)
        self.assertEqual(uv[0].label, "values owning his data")
        self.assertEqual(uv[0].status, "candidate")
        self.assertEqual(uv[0].evidence_model, "set")
        uev = h.store.evidence_of(uv[0].concept_id)
        self.assertTrue(all(e.src_type == "cluster" for e in uev))

        av = h.store.list_by(subject="aiko", kind="value")
        self.assertEqual(len(av), 1)
        self.assertEqual(av[0].label, "I value honesty over agreeableness")
        aev = h.store.evidence_of(av[0].concept_id)
        self.assertTrue(all(e.src_type == "memory" for e in aev))

    def test_identity_and_value_share_populations_without_clobber(self) -> None:
        # Both cluster proposers (identity_user + value_user) and both aiko
        # proposers run in a single pass. Before the per-spec ``sig_key`` fix,
        # the first cluster/aiko pass would save dirty-state under the shared
        # key and the second would see "clean" and skip. All four must land.
        h = WorkerHarness(_value_responder)
        h.worker.run()
        self.assertEqual(
            len(h.store.list_by(subject="user", kind="identity")), 1
        )
        self.assertEqual(len(h.store.list_by(subject="user", kind="value")), 1)
        self.assertEqual(
            len(h.store.list_by(subject="aiko", kind="identity")), 1
        )
        self.assertEqual(len(h.store.list_by(subject="aiko", kind="value")), 1)


if __name__ == "__main__":
    unittest.main()
