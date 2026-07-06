"""Tests for L13 affective concepts.

Covers the full vertical slice:

* the pure ``cluster_affect`` map (EWMA, bucketing, kv roundtrip + prune),
* the ``affective_evidence_gate`` floors,
* the ``affective`` kind registration + relevance-only routing,
* the post-turn ``_sample_cluster_affect`` sampler (cluster match, both maps,
  gating),
* self-memory affect stamping in ``MemoryStore.add``,
* the ``_run_affect_pass`` worker pass (user cluster-only + aiko mixed
  evidence + self-theme aggregation + dirty-tracking),
* the affective proposers (affect annotation in the prompt, min-sources),
* the rendering header.
"""
from __future__ import annotations

import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from app.core.concepts import cluster_affect as ca
from app.core.concepts.concept_kinds import (
    get_kind,
    kinds_for_target,
    core_lane_kinds,
)
from app.core.concepts.concept_lifecycle import affective_evidence_gate
from app.core.concepts.proposers import (
    ProposerContext,
    FocusCluster,
    propose_affective_aiko,
    propose_affective_user,
)
from app.core.infra.chat_database import ChatDatabase

from tests.test_concept_synthesis_worker import (
    ClusterStub,
    MemStub,
    WorkerHarness,
    _agent,
    _mem_settings,
)


# ── cluster_affect (pure) ───────────────────────────────────────────────


class ClusterAffectStoreTests(unittest.TestCase):
    def test_update_state_cold_then_ewma(self) -> None:
        now = "2026-01-01T00:00:00+00:00"
        st = ca.update_state(None, 0.8, 0.9, learning_rate=0.2, now_iso=now)
        self.assertEqual(st.samples, 1)
        self.assertAlmostEqual(st.valence, 0.8, places=5)
        self.assertAlmostEqual(st.arousal, 0.9, places=5)
        st2 = ca.update_state(st, -0.2, 0.1, learning_rate=0.5, now_iso=now)
        self.assertEqual(st2.samples, 2)
        # 0.5*0.8 + 0.5*-0.2 = 0.3
        self.assertAlmostEqual(st2.valence, 0.3, places=5)
        self.assertAlmostEqual(st2.arousal, 0.5, places=5)

    def test_update_state_clamps(self) -> None:
        now = "2026-01-01T00:00:00+00:00"
        st = ca.update_state(None, 5.0, 5.0, now_iso=now)
        self.assertEqual(st.valence, 1.0)
        self.assertEqual(st.arousal, 1.0)
        st2 = ca.update_state(None, -5.0, -5.0, now_iso=now)
        self.assertEqual(st2.valence, -1.0)
        self.assertEqual(st2.arousal, 0.0)

    def test_bucket_and_phrase(self) -> None:
        self.assertEqual(ca.affect_bucket(0.5, 0.7), ("pos", "high"))
        self.assertEqual(ca.affect_bucket(-0.3, 0.3), ("neg", "low"))
        self.assertEqual(ca.affect_bucket(0.0, 0.5), ("neu", "mid"))
        self.assertEqual(ca.affect_phrase(0.5, 0.7), "energizing and upbeat")
        self.assertEqual(ca.affect_phrase(0.0, 0.5), "neutral")

    def test_kv_key_for(self) -> None:
        self.assertEqual(ca.kv_key_for("aiko"), ca.KV_CLUSTER_AFFECT_AIKO)
        self.assertEqual(ca.kv_key_for("user"), ca.KV_CLUSTER_AFFECT_USER)
        self.assertEqual(
            ca.kv_key_for("relationship"), ca.KV_CLUSTER_AFFECT_USER
        )

    def test_kv_roundtrip(self) -> None:
        store: dict[str, str] = {}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        m = {
            "10": ca.update_state(None, 0.5, 0.6, now_iso=now),
            "11": ca.update_state(None, -0.4, 0.2, now_iso=now),
        }
        ca.save_map(store.__setitem__, ca.KV_CLUSTER_AFFECT_USER, m)
        back = ca.load_map(store.get, ca.KV_CLUSTER_AFFECT_USER)
        self.assertEqual(set(back), {"10", "11"})
        self.assertAlmostEqual(back["10"].valence, 0.5, places=4)

    def test_prune_cap_and_age(self) -> None:
        store: dict[str, str] = {}
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=400)).isoformat(timespec="seconds")
        fresh = now.isoformat(timespec="seconds")
        m = {
            "old": ca.ClusterAffectState(0.1, 0.1, 5, old),
            "a": ca.ClusterAffectState(0.1, 0.1, 5, fresh),
            "b": ca.ClusterAffectState(0.1, 0.1, 5, fresh),
            "c": ca.ClusterAffectState(0.1, 0.1, 5, fresh),
        }
        ca.save_map(
            store.__setitem__, ca.KV_CLUSTER_AFFECT_USER, m,
            cap=2, max_age_days=120.0,
        )
        back = ca.load_map(store.get, ca.KV_CLUSTER_AFFECT_USER)
        self.assertNotIn("old", back)  # aged out
        self.assertLessEqual(len(back), 2)  # capped


# ── gate + registry ─────────────────────────────────────────────────────


class AffectiveGateAndRegistryTests(unittest.TestCase):
    def test_gate_floors(self) -> None:
        # Passing caller thresholds are lifted to the affective floors.
        self.assertFalse(
            affective_evidence_gate(
                distinct_source_count=1, age_days=10.0, confidence=0.99,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )  # only 1 source < floor 2
        self.assertFalse(
            affective_evidence_gate(
                distinct_source_count=2, age_days=0.1, confidence=0.99,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )  # age below 0.5 floor
        self.assertFalse(
            affective_evidence_gate(
                distinct_source_count=2, age_days=10.0, confidence=0.5,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )  # confidence below 0.6 floor
        self.assertTrue(
            affective_evidence_gate(
                distinct_source_count=2, age_days=1.0, confidence=0.65,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )

    def test_registry(self) -> None:
        kind = get_kind("affective")
        self.assertIsNotNone(kind)
        self.assertEqual(kind.evidence_model, "set")
        self.assertAlmostEqual(kind.plasticity_default, 0.5)
        self.assertFalse(kind.core_always_on)
        self.assertEqual(kind.surfacing_targets, {})
        self.assertIs(kind.promotion_gate, affective_evidence_gate)

    def test_relevance_only_routing(self) -> None:
        # No named surfacing target -> never routed via for_target, and not in
        # the always-on core lane.
        self.assertNotIn("affective", kinds_for_target("profile_block"))
        self.assertNotIn(
            "affective", {k.name for k in core_lane_kinds()}
        )


# ── sampler ─────────────────────────────────────────────────────────────


class _GraphStub:
    def __init__(self, matches):
        self.persistent = True
        self._matches = matches

    def best_clusters_for(self, qvec, *, top_n, min_sim):
        return list(self._matches)[:top_n]


class _Embedder:
    def embed(self, text):
        return np.ones(4, dtype=np.float32)


def _sampler_obj(graph, chat_db, *, enabled=True):
    from app.core.session.post_turn_helpers_mixin import (
        PostTurnHelpersMixin,
    )

    obj = types.SimpleNamespace(
        _settings=types.SimpleNamespace(
            agent=types.SimpleNamespace(affect_sampler_enabled=enabled)
        ),
        _topic_graph=graph,
        _embedder=_Embedder(),
        _chat_db=chat_db,
        _memory_settings=types.SimpleNamespace(),
    )
    obj._sample_cluster_affect = types.MethodType(
        PostTurnHelpersMixin._sample_cluster_affect, obj
    )
    return obj


class SamplerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.db = ChatDatabase(Path(self.tmp) / "s.db")

    def test_updates_both_maps_when_user_affect_present(self) -> None:
        graph = _GraphStub([(42, "debugging", 0.9)])
        obj = _sampler_obj(graph, self.db)
        state = types.SimpleNamespace(valence=0.6, arousal=0.7)
        obj._sample_cluster_affect(
            user_text="the debugging is driving me nuts",
            user_affect=(-0.5, 0.8),
            state=state,
        )
        umap = ca.load_map(self.db.kv_get, ca.KV_CLUSTER_AFFECT_USER)
        amap = ca.load_map(self.db.kv_get, ca.KV_CLUSTER_AFFECT_AIKO)
        self.assertIn("42", umap)
        self.assertIn("42", amap)
        self.assertAlmostEqual(umap["42"].valence, -0.5, places=4)
        self.assertAlmostEqual(amap["42"].valence, 0.6, places=4)

    def test_aiko_only_when_user_affect_none(self) -> None:
        graph = _GraphStub([(7, "love", 0.95)])
        obj = _sampler_obj(graph, self.db)
        state = types.SimpleNamespace(valence=0.2, arousal=0.9)
        obj._sample_cluster_affect(
            user_text="tell me about romance and love please",
            user_affect=None,
            state=state,
        )
        self.assertEqual(
            ca.load_map(self.db.kv_get, ca.KV_CLUSTER_AFFECT_USER), {}
        )
        amap = ca.load_map(self.db.kv_get, ca.KV_CLUSTER_AFFECT_AIKO)
        self.assertIn("7", amap)

    def test_disabled_is_noop(self) -> None:
        graph = _GraphStub([(1, "x", 0.9)])
        obj = _sampler_obj(graph, self.db, enabled=False)
        obj._sample_cluster_affect(
            user_text="a long enough message here",
            user_affect=(0.1, 0.1),
            state=types.SimpleNamespace(valence=0.1, arousal=0.1),
        )
        self.assertEqual(
            ca.load_map(self.db.kv_get, ca.KV_CLUSTER_AFFECT_AIKO), {}
        )

    def test_no_match_is_noop(self) -> None:
        graph = _GraphStub([])
        obj = _sampler_obj(graph, self.db)
        obj._sample_cluster_affect(
            user_text="a long enough message here",
            user_affect=(0.1, 0.1),
            state=types.SimpleNamespace(valence=0.1, arousal=0.1),
        )
        self.assertEqual(
            ca.load_map(self.db.kv_get, ca.KV_CLUSTER_AFFECT_AIKO), {}
        )

    def test_short_text_is_noop(self) -> None:
        graph = _GraphStub([(1, "x", 0.9)])
        obj = _sampler_obj(graph, self.db)
        obj._sample_cluster_affect(
            user_text="hi",
            user_affect=(0.1, 0.1),
            state=types.SimpleNamespace(valence=0.1, arousal=0.1),
        )
        self.assertEqual(
            ca.load_map(self.db.kv_get, ca.KV_CLUSTER_AFFECT_AIKO), {}
        )


# ── self-memory affect stamping ─────────────────────────────────────────


class AffectStampTests(unittest.TestCase):
    def _store(self):
        from app.core.memory.memory_store import MemoryStore

        tmp = tempfile.mkdtemp()
        path = Path(tmp) / "m.db"
        ChatDatabase(path)  # initialise the shared schema (memories table)
        return MemoryStore(path)

    def test_stamps_self_kinds(self) -> None:
        store = self._store()
        store.set_affect_provider(lambda: (0.7, 0.8), enabled=True)
        emb = np.ones(8, dtype=np.float32)
        m = store.add("I felt proud today", kind="self", embedding=emb)
        self.assertIsNotNone(m)
        self.assertIn("affect", m.metadata)
        self.assertAlmostEqual(m.metadata["affect"]["valence"], 0.7, places=3)
        self.assertAlmostEqual(m.metadata["affect"]["arousal"], 0.8, places=3)

    def test_does_not_stamp_other_kinds(self) -> None:
        store = self._store()
        store.set_affect_provider(lambda: (0.7, 0.8), enabled=True)
        emb = np.ones(8, dtype=np.float32)
        m = store.add("a plain fact", kind="fact", embedding=emb)
        self.assertIsNotNone(m)
        self.assertNotIn("affect", m.metadata)

    def test_disabled_provider_no_stamp(self) -> None:
        store = self._store()
        store.set_affect_provider(lambda: (0.7, 0.8), enabled=False)
        emb = np.ones(8, dtype=np.float32)
        m = store.add("I felt proud today", kind="self", embedding=emb)
        self.assertIsNotNone(m)
        self.assertNotIn("affect", m.metadata)


# ── worker affect pass ──────────────────────────────────────────────────


def _seed_affect_map(db, key, entries) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    m = {
        str(cid): ca.ClusterAffectState(v, a, samples, now)
        for cid, (v, a, samples) in entries.items()
    }
    ca.save_map(db.kv_set, key, m)


def _affective_responder(system, user):
    if "AFFECTIVE concepts about" in system:
        # user affective proposer: span two clusters
        return {"concepts": [{
            "label": "technical work energizes him",
            "evidence_cluster_reps": [100, 101],
            "rationale": "both tech clusters read upbeat",
            "confidence": 0.8,
        }]}
    if "durably AFFECT HER" in system:
        # aiko affective proposer: mixed cluster + memory
        return {"concepts": [{
            "label": "explaining systems lifts me",
            "evidence_cluster_reps": [200],
            "evidence_memory_ids": [1],
            "rationale": "theme + memory both positive",
            "confidence": 0.75,
        }]}
    return {"concepts": []}


class AffectPassTests(unittest.TestCase):
    def test_user_affect_pass_creates_cluster_concept(self) -> None:
        clusters = [
            ClusterStub(rep=100, summary="python", size=10,
                        kinds=("fact",), cluster_id=100),
            ClusterStub(rep=101, summary="rust", size=8,
                        kinds=("fact",), cluster_id=101),
        ]
        h = WorkerHarness(_affective_responder, clusters=clusters)
        _seed_affect_map(
            h.db, ca.KV_CLUSTER_AFFECT_USER,
            {100: (0.6, 0.7, 5), 101: (0.5, 0.65, 4)},
        )
        h.worker.run()
        rows = h.store.list_by(subject="user", kind="affective")
        self.assertEqual(len(rows), 1)
        c = rows[0]
        self.assertEqual(c.label, "technical work energizes him")
        ev = h.store.evidence_of(c.concept_id)
        self.assertEqual({e.src_id for e in ev}, {"100", "101"})
        self.assertTrue(all(e.src_type == "cluster" for e in ev))

    def test_min_samples_excludes_thin_clusters(self) -> None:
        clusters = [
            ClusterStub(rep=100, summary="python", size=10,
                        kinds=("fact",), cluster_id=100),
            ClusterStub(rep=101, summary="rust", size=8,
                        kinds=("fact",), cluster_id=101),
        ]
        h = WorkerHarness(_affective_responder, clusters=clusters)
        # both below min_samples (3) -> nothing annotated -> no concept
        _seed_affect_map(
            h.db, ca.KV_CLUSTER_AFFECT_USER,
            {100: (0.6, 0.7, 1), 101: (0.5, 0.65, 2)},
        )
        h.worker.run()
        self.assertEqual(
            len(h.store.list_by(subject="user", kind="affective")), 0
        )

    def test_aiko_affect_pass_mixed_evidence(self) -> None:
        # A self-theme cluster (aiko-dominant, members 1&4 carry affect) plus
        # affect-stamped self-memories.
        self_mems = [
            MemStub(1, "I love explaining how things work.", "self", 0.9,
                    metadata={"affect": {"valence": 0.7, "arousal": 0.6}}),
            MemStub(4, "Walking through a design lifts me.", "reflection",
                    0.6, metadata={"affect": {"valence": 0.6, "arousal": 0.5}}),
        ]
        clusters = [
            ClusterStub(rep=200, summary="explaining systems", size=2,
                        kinds=("self", "reflection"), cluster_id=200,
                        member_ids=[1, 4]),
        ]
        ms = _mem_settings()
        ms.concept_synthesis_affect_min_samples = 2
        h = WorkerHarness(
            _affective_responder, clusters=clusters, self_memories=self_mems,
            mem_settings=ms,
        )
        h.worker.run()
        rows = h.store.list_by(subject="aiko", kind="affective")
        self.assertEqual(len(rows), 1)
        c = rows[0]
        self.assertEqual(c.label, "explaining systems lifts me")
        ev = h.store.evidence_of(c.concept_id)
        self.assertEqual(
            {(e.src_type, e.src_id) for e in ev},
            {("cluster", "200"), ("memory", "1")},
        )

    def test_clean_rerun_is_noop(self) -> None:
        clusters = [
            ClusterStub(rep=100, summary="python", size=10,
                        kinds=("fact",), cluster_id=100),
            ClusterStub(rep=101, summary="rust", size=8,
                        kinds=("fact",), cluster_id=101),
        ]
        h = WorkerHarness(_affective_responder, clusters=clusters)
        _seed_affect_map(
            h.db, ca.KV_CLUSTER_AFFECT_USER,
            {100: (0.6, 0.7, 5), 101: (0.5, 0.65, 4)},
        )
        h.worker.run()
        before = h.store.count()
        calls = h.ollama.calls
        h.worker.run()
        self.assertEqual(h.store.count(), before)
        # user + aiko affect passes should both short-circuit (no new LLM
        # calls beyond whatever the identity/value passes make on a clean
        # rerun, which is also zero here).
        self.assertEqual(h.ollama.calls, calls)


# ── proposers (direct) ──────────────────────────────────────────────────


def _ctx(responder):
    calls = {}

    def call_llm(system, user):
        calls["system"] = system
        calls["user"] = user
        return responder(system, user)["concepts"]

    ctx = ProposerContext(call_llm=call_llm, user_name="Jacob",
                          assistant_name="Aiko")
    return ctx, calls


class ProposerTests(unittest.TestCase):
    def test_user_prompt_shows_affect_annotation(self) -> None:
        ctx, calls = _ctx(_affective_responder)
        fc = [FocusCluster(rep=100, label="python", size=10)]
        out = propose_affective_user(
            ctx,
            focus_clusters=fc,
            cluster_index=[(100, "python", 10), (101, "rust", 8)],
            affect_by_rep={100: "energizing and upbeat",
                           101: "warm and positive"},
        )
        self.assertIn("feels: energizing and upbeat", calls["user"])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "affective")
        self.assertEqual(out[0].subject, "user")

    def test_user_single_cluster_rejected(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "x drains him",
                "evidence_cluster_reps": [100],
                "confidence": 0.9,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_affective_user(
            ctx,
            focus_clusters=[FocusCluster(rep=100, label="admin", size=5)],
            cluster_index=[(100, "admin", 5)],
            affect_by_rep={100: "downbeat and heavy"},
        )
        self.assertEqual(out, [])  # < min_sources (2)

    def test_aiko_prompt_shows_memory_affect(self) -> None:
        ctx, calls = _ctx(_affective_responder)
        mem = MemStub(1, "I love explaining things", "self", 0.9)
        out = propose_affective_aiko(
            ctx,
            focus_clusters=[FocusCluster(rep=200, label="explaining", size=2)],
            cluster_index=[(200, "explaining", 2)],
            affect_by_rep={200: "energizing and upbeat"},
            memories=[mem],
            memory_affect={1: "warm and positive"},
        )
        self.assertIn("feels: energizing and upbeat", calls["user"])
        self.assertIn("felt: warm and positive", calls["user"])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].subject, "aiko")

    def test_aiko_cold_start_no_sources(self) -> None:
        ctx, _ = _ctx(_affective_responder)
        out = propose_affective_aiko(
            ctx, focus_clusters=[], cluster_index=[],
            affect_by_rep={}, memories=[], memory_affect={},
        )
        self.assertEqual(out, [])


# ── rendering header ────────────────────────────────────────────────────


class RenderingTests(unittest.TestCase):
    def test_affective_header_dispatch(self) -> None:
        from app.core.session.inner_life_part1 import InnerLifePart1Mixin

        aiko = InnerLifePart1Mixin._concept_group_header(
            "aiko", "affective", "Jacob"
        )
        self.assertIn("move you", aiko.lower())
        user = InnerLifePart1Mixin._concept_group_header(
            "user", "affective", "Jacob"
        )
        self.assertIn("Jacob", user)
        self.assertIn("emotional weather", user.lower())


if __name__ == "__main__":
    unittest.main()
