"""Tests for :mod:`app.core.concepts.concept_store` (schema v21, L1)."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.core.concepts.concept_kinds import CONCEPT_KINDS, get_kind
from app.core.concepts.concept_store import (
    Concept,
    ConceptEdge,
    ConceptStore,
)
from app.core.infra.chat_database import ChatDatabase, _SCHEMA_VERSION


def _build() -> tuple[ChatDatabase, ConceptStore, Path]:
    tmp = tempfile.mkdtemp()
    path = Path(tmp) / "test.db"
    db = ChatDatabase(path)
    return db, ConceptStore(db), path


def _c(label: str, vec: list[float] | None = None, **kw) -> Concept:
    emb = (
        np.array(vec, dtype=np.float32)
        if vec is not None
        else np.zeros(0, dtype=np.float32)
    )
    return Concept(label=label, embedding=emb, **kw)


class SchemaTests(unittest.TestCase):
    def test_fresh_db_is_v21_with_concept_tables(self) -> None:
        _, _, path = _build()
        conn = sqlite3.connect(str(path))
        ver = conn.execute(
            "SELECT version FROM schema_version LIMIT 1"
        ).fetchone()
        self.assertEqual(ver[0], _SCHEMA_VERSION)
        self.assertGreaterEqual(_SCHEMA_VERSION, 21)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn("concepts", tables)
        self.assertIn("concept_edges", tables)
        indices = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        self.assertIn("idx_concepts_status", indices)
        self.assertIn("idx_concept_edges_src", indices)
        self.assertIn("idx_concept_edges_dst", indices)


class RegistryTests(unittest.TestCase):
    def test_identity_registered(self) -> None:
        self.assertIn("identity", CONCEPT_KINDS)
        k = get_kind("identity")
        assert k is not None
        self.assertEqual(k.subject, "user")
        self.assertEqual(k.evidence_model, "set")

    def test_unknown_kind_is_none(self) -> None:
        self.assertIsNone(get_kind("does_not_exist"))


class CrudTests(unittest.TestCase):
    def test_add_get_and_ids(self) -> None:
        _, store, _ = _build()
        cid = store.add(_c("enjoys systems", confidence=0.7))
        self.assertGreater(cid, 0)
        got = store.get(cid)
        assert got is not None
        self.assertEqual(got.label, "enjoys systems")
        self.assertEqual(got.concept_id, cid)
        self.assertTrue(got.created_at)
        self.assertTrue(got.updated_at)
        self.assertEqual(store.count(), 1)

    def test_update_persists_across_reload(self) -> None:
        db, store, _ = _build()
        cid = store.add(_c("candidate trait", status="candidate"))
        c = store.get(cid)
        assert c is not None
        c.status = "active"
        c.confidence = 0.91
        c.promoted_at = "2026-01-01T00:00:00+00:00"
        store.update(c)

        fresh = ConceptStore(db)
        fresh.load_all()
        reloaded = fresh.get(cid)
        assert reloaded is not None
        self.assertEqual(reloaded.status, "active")
        self.assertAlmostEqual(reloaded.confidence, 0.91, places=5)
        self.assertEqual(reloaded.promoted_at, "2026-01-01T00:00:00+00:00")

    def test_list_by_filters(self) -> None:
        _, store, _ = _build()
        store.add(_c("u1", subject="user", status="active"))
        store.add(_c("a1", subject="aiko", status="active"))
        store.add(_c("u2", subject="user", status="candidate"))
        self.assertEqual(len(store.list_by(subject="user")), 2)
        self.assertEqual(len(store.list_by(status="active")), 2)
        self.assertEqual(
            len(store.list_by(subject="user", status="active")), 1
        )

    def test_delete_removes_concept(self) -> None:
        _, store, _ = _build()
        cid = store.add(_c("gone soon"))
        store.delete(cid)
        self.assertIsNone(store.get(cid))
        self.assertEqual(store.count(), 0)


class EmbeddingMirrorTests(unittest.TestCase):
    def test_embedding_round_trips_via_load_all(self) -> None:
        db, store, _ = _build()
        vec = [0.1, 0.2, 0.3, 0.4]
        cid = store.add(_c("with vec", vec=vec, status="active"))

        fresh = ConceptStore(db)
        loaded = fresh.load_all()
        self.assertEqual(len(loaded), 1)
        got = fresh.get(cid)
        assert got is not None
        np.testing.assert_allclose(
            got.embedding, np.array(vec, dtype=np.float32), rtol=1e-5
        )

    def test_nearest_ranks_by_cosine(self) -> None:
        _, store, _ = _build()
        store.add(_c("x-axis", vec=[1.0, 0.0], status="active"))
        store.add(_c("y-axis", vec=[0.0, 1.0], status="active"))
        store.add(_c("diag", vec=[0.7, 0.7], status="active"))
        hits = store.nearest(np.array([0.9, 0.1], dtype=np.float32), k=3)
        self.assertEqual(hits[0][0].label, "x-axis")
        self.assertGreaterEqual(hits[0][1], hits[1][1])

    def test_nearest_excludes_non_active_by_default(self) -> None:
        _, store, _ = _build()
        store.add(_c("candidate", vec=[1.0, 0.0], status="candidate"))
        store.add(_c("active", vec=[1.0, 0.0], status="active"))
        hits = store.nearest(np.array([1.0, 0.0], dtype=np.float32))
        self.assertEqual([h[0].label for h in hits], ["active"])
        # explicit status filter can reach candidates
        cand = store.nearest(
            np.array([1.0, 0.0], dtype=np.float32), status="candidate"
        )
        self.assertEqual([h[0].label for h in cand], ["candidate"])

    def test_nearest_filters_subject_and_kind(self) -> None:
        _, store, _ = _build()
        store.add(
            _c("user id", vec=[1.0, 0.0], subject="user", kind="identity",
               status="active")
        )
        store.add(
            _c("aiko id", vec=[1.0, 0.0], subject="aiko", kind="identity",
               status="active")
        )
        q = np.array([1.0, 0.0], dtype=np.float32)
        self.assertEqual(
            [h[0].label for h in store.nearest(q, subject="user")],
            ["user id"],
        )
        self.assertEqual(
            [h[0].label for h in store.nearest(q, subject="aiko")],
            ["aiko id"],
        )

    def test_nearest_empty_query_returns_empty(self) -> None:
        _, store, _ = _build()
        store.add(_c("a", vec=[1.0, 0.0], status="active"))
        self.assertEqual(
            store.nearest(np.zeros(0, dtype=np.float32)), []
        )
        self.assertEqual(
            store.nearest(np.zeros(2, dtype=np.float32)), []
        )

    def test_delete_refreshes_mirror(self) -> None:
        _, store, _ = _build()
        cid = store.add(_c("a", vec=[1.0, 0.0], status="active"))
        store.add(_c("b", vec=[0.0, 1.0], status="active"))
        store.delete(cid)
        labels = [
            h[0].label
            for h in store.nearest(np.array([1.0, 0.0], dtype=np.float32), k=5)
        ]
        self.assertNotIn("a", labels)


class EdgeTests(unittest.TestCase):
    def test_evidence_edges_and_ordering(self) -> None:
        _, store, _ = _build()
        cid = store.add(_c("narrative", evidence_model="sequence"))
        store.add_edge(
            ConceptEdge("memory", "20", "concept", str(cid), "evidence",
                        ordinal=2)
        )
        store.add_edge(
            ConceptEdge("memory", "10", "concept", str(cid), "evidence",
                        ordinal=1)
        )
        ev = store.evidence_of(cid)
        self.assertEqual([e.src_id for e in ev], ["10", "20"])

    def test_edge_upsert_dedupes(self) -> None:
        _, store, _ = _build()
        cid = store.add(_c("c"))
        e1 = store.add_edge(
            ConceptEdge("cluster", "7", "concept", str(cid), "evidence",
                        strength=0.5)
        )
        e2 = store.add_edge(
            ConceptEdge("cluster", "7", "concept", str(cid), "evidence",
                        strength=0.9)
        )
        self.assertEqual(e1, e2)  # same unique key -> same row
        edges = store.edges_into("concept", cid)
        self.assertEqual(len(edges), 1)
        self.assertAlmostEqual(edges[0].strength, 0.9, places=5)

    def test_dependents_of_walks_src_to_dst(self) -> None:
        _, store, _ = _build()
        base = store.add(_c("Maker Mode"))
        meta = store.add(_c("tension", evidence_model="meta"))
        # base -> meta (base supports the meta that references it)
        store.add_edge(
            ConceptEdge("concept", str(base), "concept", str(meta),
                        "references")
        )
        self.assertEqual(store.dependents_of(base), [meta])
        self.assertEqual(store.dependents_of(meta), [])

    def test_delete_concept_cascades_edges(self) -> None:
        db, store, path = _build()
        cid = store.add(_c("c"))
        store.add_edge(
            ConceptEdge("memory", "1", "concept", str(cid), "evidence")
        )
        store.add_edge(
            ConceptEdge("concept", str(cid), "concept", "999", "references")
        )
        store.delete(cid)
        conn = sqlite3.connect(str(path))
        n = conn.execute("SELECT COUNT(*) FROM concept_edges").fetchone()[0]
        self.assertEqual(n, 0)

    def test_delete_for_memory_drops_memory_edges(self) -> None:
        _, store, path = _build()
        cid = store.add(_c("c"))
        store.add_edge(
            ConceptEdge("memory", "42", "concept", str(cid), "evidence")
        )
        store.add_edge(
            ConceptEdge("cluster", "3", "concept", str(cid), "evidence")
        )
        store.delete_for_memory(42)
        remaining = store.edges_into("concept", cid)
        self.assertEqual([e.src_type for e in remaining], ["cluster"])

    def test_affected_concepts_for_memory_both_sides(self) -> None:
        _, store, _ = _build()
        c_ev = store.add(_c("supported"))
        c_contra = store.add(_c("disproven"))
        # evidence: memory 5 -> concept c_ev
        store.add_edge(
            ConceptEdge("memory", "5", "concept", str(c_ev), "evidence")
        )
        # contradicts: concept c_contra -> memory 5
        store.add_edge(
            ConceptEdge("concept", str(c_contra), "memory", "5", "contradicts",
                        polarity=-1)
        )
        # unrelated edge on a different memory
        store.add_edge(
            ConceptEdge("memory", "6", "concept", str(c_ev), "evidence")
        )
        self.assertEqual(
            store.affected_concepts_for_memory(5), {c_ev, c_contra}
        )
        self.assertEqual(store.affected_concepts_for_memory(6), {c_ev})
        self.assertEqual(store.affected_concepts_for_memory(999), set())

    def test_repoint_memory_edges_moves_and_merges(self) -> None:
        _, store, _ = _build()
        cid = store.add(_c("c"))
        other = store.add(_c("other"))
        # evidence from victim memory 10 -> cid
        store.add_edge(
            ConceptEdge("memory", "10", "concept", str(cid), "evidence")
        )
        # contradicts: other -> memory 10
        store.add_edge(
            ConceptEdge("concept", str(other), "memory", "10", "contradicts",
                        polarity=-1)
        )
        # survivor 20 already supports cid -> repoint should merge (dedupe)
        store.add_edge(
            ConceptEdge("memory", "20", "concept", str(cid), "evidence")
        )
        moved = store.repoint_memory_edges(10, 20)
        self.assertEqual(moved, 2)
        # victim memory 10 no longer referenced anywhere
        self.assertEqual(store.edges_from("memory", 10), [])
        self.assertEqual(store.edges_into("memory", 10), [])
        # cid still has exactly one evidence edge (merged onto survivor 20)
        ev = store.evidence_of(cid)
        self.assertEqual([e.src_id for e in ev], ["20"])
        # the contradicts edge now points at survivor 20
        contra = store.edges_into("memory", 20)
        self.assertEqual(
            [(e.src_id, e.relation) for e in contra],
            [(str(other), "contradicts")],
        )

    def test_repoint_same_id_is_noop(self) -> None:
        _, store, _ = _build()
        cid = store.add(_c("c"))
        store.add_edge(
            ConceptEdge("memory", "10", "concept", str(cid), "evidence")
        )
        self.assertEqual(store.repoint_memory_edges(10, 10), 0)
        self.assertEqual(len(store.evidence_of(cid)), 1)

    def test_orphaned_memory_edges_finds_missing_targets(self) -> None:
        db, store, _ = _build()
        conn = db._get_conn()
        conn.execute(
            "INSERT INTO memories (id, content, kind, embedding, created_at) "
            "VALUES (100, 'alive', 'fact', X'00', '2020-01-01')"
        )
        conn.commit()
        cid = store.add(_c("c"))
        # edge to a surviving memory -> not orphaned
        store.add_edge(
            ConceptEdge("memory", "100", "concept", str(cid), "evidence")
        )
        # edge to a vanished memory -> orphaned
        store.add_edge(
            ConceptEdge("memory", "200", "concept", str(cid), "evidence")
        )
        # contradicts edge to a vanished memory -> also orphaned
        store.add_edge(
            ConceptEdge("concept", str(cid), "memory", "300", "contradicts",
                        polarity=-1)
        )
        orphans = store.orphaned_memory_edges(50)
        orphan_mems = sorted(
            e.src_id if e.src_type == "memory" else e.dst_id for e in orphans
        )
        self.assertEqual(orphan_mems, ["200", "300"])

    def test_orphaned_memory_edges_respects_limit(self) -> None:
        _, store, _ = _build()
        cid = store.add(_c("c"))
        for mem_id in range(500, 505):
            store.add_edge(
                ConceptEdge("memory", str(mem_id), "concept", str(cid),
                            "evidence")
            )
        self.assertEqual(len(store.orphaned_memory_edges(3)), 3)

    def test_delete_concept_leaves_memories_intact(self) -> None:
        """Deleting a concept must remove only the concept + its edges,
        never the memory rows the evidence edges point at."""
        db, store, path = _build()
        conn = db._get_conn()
        conn.execute(
            "INSERT INTO memories (id, content, kind, embedding, created_at) "
            "VALUES (777, 'a real memory', 'fact', X'00', '2020-01-01')"
        )
        conn.commit()

        cid = store.add(_c("c"))
        store.add_edge(
            ConceptEdge("memory", "777", "concept", str(cid), "evidence")
        )
        store.delete(cid)

        self.assertIsNone(store.get(cid))
        self.assertEqual(store.evidence_of(cid), [])
        row = conn.execute(
            "SELECT content FROM memories WHERE id = 777"
        ).fetchone()
        self.assertIsNotNone(row, "memory row must survive concept delete")
        self.assertEqual(row[0], "a real memory")


class MergeIntoTests(unittest.TestCase):
    def test_merges_ordinary_near_duplicate(self) -> None:
        _, store, _ = _build()
        can = store.add(
            _c("enjoys systems", subject="user", kind="identity")
        )
        absorbed = store.add(
            _c("likes systems", subject="user", kind="identity")
        )
        store.add_edge(
            ConceptEdge("memory", "7", "concept", str(absorbed), "evidence")
        )
        self.assertTrue(
            store.merge_into(canonical_id=can, absorbed_id=absorbed)
        )
        self.assertIsNone(store.get(absorbed))
        surviving = store.get(can)
        assert surviving is not None
        # The absorbed row's evidence edge re-points onto the canonical.
        self.assertEqual(
            [e.src_id for e in store.evidence_of(can)], ["7"]
        )

    def test_refuses_co_bases_of_same_tension(self) -> None:
        _, store, _ = _build()
        base_a = store.add(
            _c("values deep focus", subject="user", kind="identity")
        )
        base_b = store.add(
            _c("wants more walks", subject="user", kind="identity")
        )
        meta = store.add(
            _c("focus vs movement pull", subject="user", kind="tension",
               evidence_model="meta")
        )
        # Tension bases point at the meta via concept->concept evidence edges.
        store.add_edge(
            ConceptEdge("concept", str(base_a), "concept", str(meta),
                        "evidence")
        )
        store.add_edge(
            ConceptEdge("concept", str(base_b), "concept", str(meta),
                        "evidence")
        )
        self.assertFalse(
            store.merge_into(canonical_id=base_a, absorbed_id=base_b)
        )
        # Both bases survive.
        self.assertIsNotNone(store.get(base_a))
        self.assertIsNotNone(store.get(base_b))

    def test_refuses_direct_conflict_edge(self) -> None:
        _, store, _ = _build()
        can = store.add(_c("a", subject="user", kind="identity"))
        absorbed = store.add(_c("b", subject="user", kind="identity"))
        store.add_edge(
            ConceptEdge("concept", str(absorbed), "concept", str(can),
                        "tension")
        )
        self.assertFalse(
            store.merge_into(canonical_id=can, absorbed_id=absorbed)
        )
        self.assertIsNotNone(store.get(absorbed))

    def test_merges_bases_of_different_tensions(self) -> None:
        # Co-bases guard is specific: two concepts each in a *different*
        # tension are still fair game for a near-dup merge.
        _, store, _ = _build()
        can = store.add(_c("a", subject="user", kind="identity"))
        absorbed = store.add(_c("a2", subject="user", kind="identity"))
        m1 = store.add(
            _c("t1", subject="user", kind="tension", evidence_model="meta")
        )
        m2 = store.add(
            _c("t2", subject="user", kind="tension", evidence_model="meta")
        )
        store.add_edge(
            ConceptEdge("concept", str(can), "concept", str(m1), "evidence")
        )
        store.add_edge(
            ConceptEdge("concept", str(absorbed), "concept", str(m2),
                        "evidence")
        )
        self.assertTrue(
            store.merge_into(canonical_id=can, absorbed_id=absorbed)
        )
        self.assertIsNone(store.get(absorbed))


if __name__ == "__main__":
    unittest.main()
