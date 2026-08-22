"""P30: the mirror's embeddings as one matrix instead of N arrays.

Two things are worth testing here and they are quite different. The
first is that :class:`VectorIndex` is a correct associative container
that happens to be backed by a matrix -- tombstones, compaction, growth,
replacement. The second, and the one that would actually bite, is that
it cannot **drift** from ``MemoryStore._mirror``: it is a second copy of
the same facts, so every path that mutates one must mutate the other,
and a missed path shows up as a memory that is silently unfindable (or a
deleted one that keeps matching) rather than as a crash.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore
from app.core.memory.vector_index import VectorIndex
from app.llm.embedder import cosine_similarity

DIM = 16


def _vec(seed: int, dim: int = DIM) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.normal(size=dim).astype(np.float32)
    return v / float(np.linalg.norm(v))


def _basis(i: int, dim: int = DIM) -> np.ndarray:
    """A unit axis vector — orthogonal to every other one, exactly."""
    v = np.zeros(dim, dtype=np.float32)
    v[i] = 1.0
    return v


class VectorIndexBasicsTests(unittest.TestCase):
    def test_empty_index_scores_nothing(self) -> None:
        idx = VectorIndex()
        ids, scores = idx.scores(_vec(1))
        self.assertEqual(ids.size, 0)
        self.assertEqual(scores.size, 0)
        self.assertEqual(idx.above(_vec(1), 0.0), [])
        self.assertEqual(len(idx), 0)

    def test_a_vector_matches_itself_at_one(self) -> None:
        idx = VectorIndex()
        idx.add(7, _vec(1))
        ids, scores = idx.scores(_vec(1))
        self.assertEqual(list(ids), [7])
        self.assertAlmostEqual(float(scores[0]), 1.0, places=5)

    def test_scores_agree_with_the_per_row_function_they_replace(self) -> None:
        # The whole point is that this is the same arithmetic, faster.
        idx = VectorIndex()
        vecs = {i: _vec(i) for i in range(1, 40)}
        for mid, v in vecs.items():
            idx.add(mid, v)
        q = _vec(500)
        ids, scores = idx.scores(q)
        by_id = dict(
            zip((int(i) for i in ids), (float(s) for s in scores), strict=True)
        )
        for mid, v in vecs.items():
            self.assertAlmostEqual(by_id[mid], cosine_similarity(q, v), places=5)

    def test_unnormalised_input_is_normalised_on_the_way_in(self) -> None:
        idx = VectorIndex()
        idx.add(1, _vec(3) * 17.0)
        _, scores = idx.scores(_vec(3))
        self.assertAlmostEqual(float(scores[0]), 1.0, places=5)

    def test_above_returns_best_first(self) -> None:
        # Built from an orthogonal basis rather than random draws: in 16
        # dimensions two random unit vectors are routinely 0.5 apart.
        idx = VectorIndex()
        q = _basis(0)
        idx.add(1, q)                        # cosine 1.0
        idx.add(2, _basis(1))                # exactly 0
        idx.add(3, q + 0.30 * _basis(2))     # high but < 1
        got = idx.above(q, 0.5)
        self.assertEqual([mid for mid, _ in got], [1, 3])
        self.assertGreater(got[0][1], got[1][1])

    def test_above_is_empty_when_nothing_clears_the_floor(self) -> None:
        idx = VectorIndex()
        idx.add(1, _vec(1))
        self.assertEqual(idx.above(_vec(2), 0.99), [])


class VectorIndexUnusableRowsTests(unittest.TestCase):
    """Rows the old loop scored 0.0 against everything must stay invisible."""

    def test_a_row_with_no_embedding_is_skipped(self) -> None:
        idx = VectorIndex()
        self.assertFalse(idx.add(1, None))
        self.assertEqual(len(idx), 0)

    def test_a_zero_vector_is_skipped(self) -> None:
        idx = VectorIndex()
        self.assertFalse(idx.add(1, np.zeros(DIM, dtype=np.float32)))
        self.assertEqual(len(idx), 0)

    def test_a_mismatched_dimension_is_skipped(self) -> None:
        idx = VectorIndex()
        idx.add(1, _vec(1, dim=DIM))
        self.assertFalse(idx.add(2, _vec(2, dim=DIM * 2)))
        self.assertEqual(len(idx), 1)

    def test_a_query_of_the_wrong_dimension_scores_nothing(self) -> None:
        idx = VectorIndex()
        idx.add(1, _vec(1))
        ids, _ = idx.scores(_vec(2, dim=DIM * 2))
        self.assertEqual(ids.size, 0)

    def test_replacing_a_vector_with_an_unusable_one_removes_it(self) -> None:
        # Otherwise the row would keep answering with the vector it no
        # longer has.
        idx = VectorIndex()
        idx.add(1, _vec(1))
        idx.add(1, None)
        self.assertEqual(len(idx), 0)
        self.assertEqual(idx.scores(_vec(1))[0].size, 0)


class VectorIndexMutationTests(unittest.TestCase):
    def test_adding_the_same_id_replaces_rather_than_duplicates(self) -> None:
        idx = VectorIndex()
        idx.add(1, _vec(1))
        idx.add(1, _vec(2))
        ids, scores = idx.scores(_vec(2))
        self.assertEqual(list(ids), [1])
        self.assertAlmostEqual(float(scores[0]), 1.0, places=5)

    def test_removed_ids_disappear_from_results(self) -> None:
        idx = VectorIndex()
        for i in range(1, 6):
            idx.add(i, _vec(i))
        self.assertTrue(idx.remove(3))
        ids, _ = idx.scores(_vec(1))
        self.assertNotIn(3, [int(i) for i in ids])
        self.assertEqual(len(idx), 4)

    def test_removing_an_absent_id_is_a_no_op(self) -> None:
        idx = VectorIndex()
        idx.add(1, _vec(1))
        self.assertFalse(idx.remove(99))
        self.assertEqual(len(idx), 1)

    def test_a_removed_id_can_be_added_back(self) -> None:
        idx = VectorIndex()
        idx.add(1, _vec(1))
        idx.remove(1)
        idx.add(1, _vec(1))
        ids, scores = idx.scores(_vec(1))
        self.assertEqual(list(ids), [1])
        self.assertAlmostEqual(float(scores[0]), 1.0, places=5)

    def test_the_matrix_grows_past_its_initial_capacity(self) -> None:
        idx = VectorIndex()
        for i in range(1, 1000):
            idx.add(i, _vec(i))
        self.assertEqual(len(idx), 999)
        ids, scores = idx.scores(_vec(742))
        best = int(ids[int(np.argmax(scores))])
        self.assertEqual(best, 742)

    def test_tombstones_are_reclaimed_and_survivors_keep_their_vectors(self) -> None:
        # Deletion is a tombstone so prune() stays linear; the compaction
        # that eventually reclaims them must not scramble the mapping.
        idx = VectorIndex()
        for i in range(1, 401):
            idx.add(i, _vec(i))
        for i in range(1, 301):
            idx.remove(i)
        self.assertEqual(len(idx), 100)
        self.assertLess(idx.dead, 300)  # a compaction happened
        for probe in (301, 350, 400):
            ids, scores = idx.scores(_vec(probe))
            self.assertEqual(int(ids[int(np.argmax(scores))]), probe)
        ids, _ = idx.scores(_vec(1))
        self.assertEqual(sorted(int(i) for i in ids), list(range(301, 401)))

    def test_rebuild_replaces_everything(self) -> None:
        idx = VectorIndex()
        for i in range(1, 10):
            idx.add(i, _vec(i))
        idx.rebuild([(50, _vec(50)), (51, None), (52, _vec(52))])
        self.assertEqual(len(idx), 2)
        ids, _ = idx.scores(_vec(50))
        self.assertEqual(sorted(int(i) for i in ids), [50, 52])


class MirrorAgreementTests(unittest.TestCase):
    """The index is a second copy of the mirror; it must not drift.

    Every one of these drives the public ``MemoryStore`` API and then
    asserts the two structures still describe the same set of rows.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self.path = Path(self._tmp) / "mem.db"
        ChatDatabase(self.path)
        self.store = MemoryStore(self.path, dedupe_threshold=0.999)

    def _assert_agrees(self) -> None:
        mirror_ids = {
            mid
            for mid, m in self.store._mirror.items()
            if m.embedding is not None and m.embedding.size
        }
        index_ids = {int(i) for i in self.store._vectors.scores(_vec(1))[0]}
        self.assertEqual(mirror_ids, index_ids)

    def test_after_adds(self) -> None:
        for i in range(1, 12):
            self.store.add(f"memory number {i:03d}", "fact", _vec(i))
        self._assert_agrees()

    def test_after_a_delete(self) -> None:
        kept = []
        for i in range(1, 8):
            mem = self.store.add(f"memory number {i:03d}", "fact", _vec(i))
            if mem is not None:
                kept.append(mem.id)
        self.store.delete(kept[2])
        self._assert_agrees()
        self.assertNotIn(kept[2], self.store._vectors)

    def test_after_an_embedding_changing_update(self) -> None:
        mem = self.store.add("a memory to revise", "fact", _vec(1))
        assert mem is not None
        self.store.update(mem.id, embedding=_vec(900))
        self._assert_agrees()
        # And the index answers with the *new* vector.
        hits = self.store.search(_vec(900), top_k=1, min_score=0.5)
        self.assertEqual([h.memory.id for h in hits], [mem.id])

    def test_after_a_prune(self) -> None:
        self.store.set_tier_caps(scratchpad=50)
        for i in range(1, 60):
            self.store.add(
                f"scratch number {i:03d}", "fact", _vec(i),
                tier="scratchpad", salience=0.01 * i,
            )
        self.store.prune()
        self._assert_agrees()

    def test_after_a_reload(self) -> None:
        for i in range(1, 10):
            self.store.add(f"memory number {i:03d}", "fact", _vec(i))
        self.store._reload_mirror()
        self._assert_agrees()

    def test_a_row_loaded_with_an_empty_embedding_stays_out(self) -> None:
        # ``_decode`` turns an empty BLOB into a zero-length array, so the
        # mirror can hold an unusable row even though ``add`` cannot
        # create one. Such a row scored 0.0 against everything under the
        # old loop; it must stay out of the matrix rather than occupy a
        # row whose similarity would be meaningless.
        self.store.add("a memory with a vector", "fact", _vec(1))
        conn = self.store._get_conn()
        conn.execute(
            "INSERT INTO memories (content, kind, salience, embedding, "
            " created_at, use_count, pinned, tier) "
            "VALUES ('vectorless row', 'fact', 0.5, X'', "
            " datetime('now'), 0, 0, 'long_term')"
        )
        conn.commit()
        self.store._reload_mirror()
        vectorless = next(
            m for m in self.store._mirror.values()
            if m.embedding is None or not m.embedding.size
        )
        self.assertNotIn(vectorless.id, self.store._vectors)
        self._assert_agrees()


class SearchEquivalenceTests(unittest.TestCase):
    """``search`` must return what the per-row loop returned."""

    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self.path = Path(self._tmp) / "mem.db"
        ChatDatabase(self.path)
        self.store = MemoryStore(self.path, dedupe_threshold=0.999)
        for i in range(1, 30):
            self.store.add(
                f"memory number {i:03d}", "fact", _vec(i),
                salience=0.2 + 0.02 * i,
            )

    def _reference(self, q, *, top_k: int, min_score: float):
        """The loop ``search`` used to be, kept as the oracle."""
        scored = []
        for mem in self.store._mirror.values():
            score = cosine_similarity(q, mem.embedding)
            if score >= min_score:
                scored.append((mem.id, score + 0.05 * (mem.salience - 0.5)))
        scored.sort(key=lambda h: h[1], reverse=True)
        return scored[: max(1, top_k)]

    def test_matches_the_reference_for_a_range_of_queries(self) -> None:
        for seed in (1, 5, 17, 400, 999):
            q = _vec(seed)
            for min_score in (0.0, 0.2, 0.4):
                want = self._reference(q, top_k=6, min_score=min_score)
                got = self.store.search(q, top_k=6, min_score=min_score)
                self.assertEqual(
                    [mid for mid, _ in want], [h.memory.id for h in got],
                    msg=f"seed={seed} min_score={min_score}",
                )
                for (_, ws), h in zip(want, got, strict=True):
                    self.assertAlmostEqual(ws, h.score, places=5)

    def test_min_score_gates_on_the_raw_cosine_not_the_boosted_one(self) -> None:
        # The salience boost may reorder survivors; it must not admit a
        # row that failed the threshold, which is what the old loop did.
        q = _vec(1)
        hits = self.store.search(q, top_k=50, min_score=0.99)
        for h in hits:
            mem = self.store._mirror[h.memory.id]
            self.assertGreaterEqual(cosine_similarity(q, mem.embedding), 0.99)

    def test_an_empty_store_returns_nothing(self) -> None:
        tmp = tempfile.mkdtemp()
        path = Path(tmp) / "empty.db"
        ChatDatabase(path)
        self.assertEqual(MemoryStore(path).search(_vec(1)), [])


class DedupeEquivalenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.mkdtemp()
        self.path = Path(self._tmp) / "mem.db"
        ChatDatabase(self.path)

    def test_a_near_identical_write_still_merges(self) -> None:
        store = MemoryStore(self.path, dedupe_threshold=0.92)
        first = store.add("Jacob likes strong coffee", "preference", _vec(1))
        assert first is not None
        again = store.add("Jacob likes strong coffee", "preference", _vec(1))
        self.assertIsNone(again)
        self.assertEqual(len(store._mirror), 1)

    def test_a_distinct_write_still_lands(self) -> None:
        store = MemoryStore(self.path, dedupe_threshold=0.92)
        store.add("Jacob likes strong coffee", "preference", _vec(1))
        second = store.add("Jacob dislikes early mornings", "preference", _vec(2))
        self.assertIsNotNone(second)
        self.assertEqual(len(store._mirror), 2)

    def test_dedupe_merges_into_the_closest_row_not_the_oldest(self) -> None:
        # A deliberate change from the loop this replaces, which merged
        # into whichever matching row it happened to reach first.
        store = MemoryStore(self.path, dedupe_threshold=0.90)
        q = _basis(0)
        # Seeded with dedupe off: the two rows are both close to ``q``,
        # which necessarily makes them close to each other.
        far = store.add(
            "first, a looser match", "fact", q + 0.40 * _basis(1),
            skip_dedupe=True,
        )
        near = store.add(
            "second, a closer match", "fact", q + 0.05 * _basis(2),
            skip_dedupe=True,
        )
        assert far is not None and near is not None
        self.assertIsNone(store.add("the new arrival", "fact", q))
        # ``_touch_existing`` marks the row the write merged into.
        self.assertIsNotNone(store.get(near.id).last_used_at)
        self.assertIsNone(store.get(far.id).last_used_at)

    def test_a_pinned_write_still_bypasses_dedupe(self) -> None:
        store = MemoryStore(self.path, dedupe_threshold=0.5)
        store.add("a moment worth keeping", "event", _vec(1))
        pinned = store.add("a moment worth keeping", "event", _vec(1), pinned=True)
        self.assertIsNotNone(pinned)
