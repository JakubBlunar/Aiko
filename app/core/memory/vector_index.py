"""A contiguous matrix of the memory mirror's embeddings.

:class:`~app.core.memory.memory_store.MemoryStore` keeps every row in a
``dict[int, Memory]``, which is the right shape for the twenty-odd
accessors that want a memory by id, kind or tier. It is the wrong shape
for the two paths that compare a query against *all* of them --
``search`` and ``add``'s dedupe pass -- because "compare against all of
them" written over a dict of Python objects is an interpreter loop with
a function call per row.

The fix is not a different algorithm, it is a different layout. The same
vectors held as one ``(rows, dim)`` float32 matrix make that comparison a
single BLAS call: measured on the live store's embeddings at 20k rows,
61.3 ms of Python loop becomes 1.9 ms. The bytes are identical (81.9 MB
either way at 20k) -- what disappears is per-object overhead and the
interpreter.

Rows are stored unit-normalised so a similarity is a plain dot product.
That is equivalent to what :func:`~app.llm.embedder.cosine_similarity`
computes, which divides by both norms when either is not already 1.

**Deletion is a tombstone**, not a compaction: removing row *i* from a
contiguous matrix would otherwise copy every row after it, turning an
`O(1)` delete into `O(n)` and making ``prune()`` quadratic. Dead rows are
masked out of every result and reclaimed in one pass once they are worth
reclaiming.

Not thread-safe on its own. Every mutator is called by ``MemoryStore``
from inside its own lock, at the same four places the mirror itself is
mutated (reload, add, update, delete/prune).
"""
from __future__ import annotations

from collections.abc import Iterable

import numpy as np

# Rows to allocate for a store that has none yet. Small: most tests and
# a fresh install never reach it, and growth is geometric anyway.
_INITIAL_CAPACITY = 256

# Reclaim tombstones once they are both a real share of the matrix and
# numerous enough to be worth a rebuild. Without the floor, a store of
# 20 rows would compact on its second delete.
_COMPACT_MIN_DEAD = 64
_COMPACT_DEAD_SHARE = 0.25


class VectorIndex:
    """Id-keyed unit-norm vectors in one matrix, with tombstoned deletes."""

    __slots__ = ("_mat", "_ids", "_pos", "_rows", "_dead", "_dim")

    def __init__(self, dim: int | None = None) -> None:
        self._dim: int | None = int(dim) if dim else None
        self._rows = 0
        self._dead = 0
        self._pos: dict[int, int] = {}
        self._ids = np.full(_INITIAL_CAPACITY, -1, dtype=np.int64)
        self._mat: np.ndarray | None = (
            np.zeros((_INITIAL_CAPACITY, self._dim), dtype=np.float32)
            if self._dim
            else None
        )

    # ── introspection ────────────────────────────────────────────────────

    def __len__(self) -> int:
        """Number of live vectors (tombstones excluded)."""
        return len(self._pos)

    @property
    def dim(self) -> int | None:
        return self._dim

    @property
    def capacity(self) -> int:
        return int(self._ids.shape[0])

    @property
    def dead(self) -> int:
        return self._dead

    def __contains__(self, memory_id: object) -> bool:
        try:
            return int(memory_id) in self._pos  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False

    # ── mutation ─────────────────────────────────────────────────────────

    def add(self, memory_id: int, vec: "np.ndarray | None") -> bool:
        """Insert or replace one vector. Returns whether it was stored.

        A row is skipped rather than rejected when it has no usable
        vector -- no embedding, a zero vector, or a dimension that
        disagrees with the rest of the store. Those rows exist in the
        mirror (``_decode`` returns ``None`` for a null BLOB) and scored
        0.0 against everything under the old loop, so leaving them out
        of the matrix reproduces that exactly.
        """
        unit = self._unit(vec)
        if unit is None:
            # A row whose vector became unusable must not keep answering
            # with its old one.
            self.remove(memory_id)
            return False
        mid = int(memory_id)
        row = self._pos.get(mid)
        if row is None:
            if self._rows >= self.capacity:
                self._grow()
            row = self._rows
            self._rows += 1
            self._pos[mid] = row
            self._ids[row] = mid
        assert self._mat is not None
        self._mat[row] = unit
        return True

    def remove(self, memory_id: int) -> bool:
        """Tombstone one vector. Returns whether it was present."""
        try:
            mid = int(memory_id)
        except (TypeError, ValueError):
            return False
        row = self._pos.pop(mid, None)
        if row is None:
            return False
        self._ids[row] = -1
        self._dead += 1
        if (
            self._dead >= _COMPACT_MIN_DEAD
            and self._dead >= _COMPACT_DEAD_SHARE * self._rows
        ):
            self._compact()
        return True

    def rebuild(self, items: "Iterable[tuple[int, np.ndarray | None]]") -> None:
        """Replace the whole index. Used after a full mirror reload."""
        # ``_unit`` adopts the dimension from the first usable vector, so
        # the comprehension has to run before the matrix is sized.
        live = [
            (int(i), unit)
            for i, unit in ((i, self._unit(v)) for i, v in items)
            if unit is not None
        ]
        capacity = max(_INITIAL_CAPACITY, len(live) * 2)
        self._ids = np.full(capacity, -1, dtype=np.int64)
        self._pos = {}
        self._rows = 0
        self._dead = 0
        self._mat = (
            np.zeros((capacity, self._dim), dtype=np.float32)
            if self._dim
            else None
        )
        if not live or self._mat is None:
            return
        for mid, unit in live:
            if unit.shape[0] != self._dim:
                continue
            self._mat[self._rows] = unit
            self._ids[self._rows] = mid
            self._pos[mid] = self._rows
            self._rows += 1

    # ── query ────────────────────────────────────────────────────────────

    def scores(
        self, query: "np.ndarray | None",
    ) -> "tuple[np.ndarray, np.ndarray]":
        """Return ``(ids, cosines)`` for every live vector, unordered.

        One matmul over the used prefix of the matrix, then the dead
        rows are dropped. Empty arrays when there is nothing to compare
        against, so callers need no special case.
        """
        empty = (np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float32))
        unit = self._unit(query)
        if unit is None or self._mat is None or self._rows == 0:
            return empty
        if unit.shape[0] != self._dim:
            return empty
        raw = self._mat[: self._rows] @ unit
        ids = self._ids[: self._rows]
        if self._dead:
            keep = ids >= 0
            return ids[keep], raw[keep]
        return ids, raw

    def above(
        self, query: "np.ndarray | None", threshold: float,
    ) -> "list[tuple[int, float]]":
        """``[(id, score), ...]`` scoring at least ``threshold``, best first.

        The dedupe pass wants only the handful of rows that could
        possibly match, so the sort is over those rather than over the
        corpus.
        """
        ids, raw = self.scores(query)
        if ids.size == 0:
            return []
        hit = raw >= float(threshold)
        if not hit.any():
            return []
        ids_hit = ids[hit]
        raw_hit = raw[hit]
        order = np.argsort(-raw_hit, kind="stable")
        return [(int(ids_hit[i]), float(raw_hit[i])) for i in order]

    # ── internals ────────────────────────────────────────────────────────

    def _unit(self, vec: "np.ndarray | None") -> "np.ndarray | None":
        if vec is None:
            return None
        arr = np.asarray(vec, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            return None
        if self._dim is None:
            self._dim = int(arr.shape[0])
            self._mat = np.zeros((self.capacity, self._dim), dtype=np.float32)
        elif arr.shape[0] != self._dim:
            return None
        norm = float(np.linalg.norm(arr))
        if norm == 0.0 or not np.isfinite(norm):
            return None
        return arr / norm

    def _grow(self) -> None:
        assert self._mat is not None
        capacity = max(_INITIAL_CAPACITY, self.capacity * 2)
        mat = np.zeros((capacity, self._dim or 1), dtype=np.float32)
        mat[: self._rows] = self._mat[: self._rows]
        ids = np.full(capacity, -1, dtype=np.int64)
        ids[: self._rows] = self._ids[: self._rows]
        self._mat = mat
        self._ids = ids

    def _compact(self) -> None:
        """Squeeze the tombstones out, preserving relative order."""
        if self._mat is None:
            return
        keep = np.flatnonzero(self._ids[: self._rows] >= 0)
        rows = int(keep.size)
        capacity = max(_INITIAL_CAPACITY, rows * 2)
        mat = np.zeros((capacity, self._dim or 1), dtype=np.float32)
        ids = np.full(capacity, -1, dtype=np.int64)
        if rows:
            mat[:rows] = self._mat[keep]
            ids[:rows] = self._ids[keep]
        self._mat = mat
        self._ids = ids
        self._rows = rows
        self._dead = 0
        self._pos = {int(mid): i for i, mid in enumerate(ids[:rows])}
