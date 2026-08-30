"""L46: computed meta-graph depth, cycle detection, descendant walks.

The concept graph is a DAG of ``evidence`` edges (child ``src`` -> parent
``dst``). Depth is not stored: a merge would stale a column, and a 2-6
child walk is cheap. Non-meta rows are depth 0; a meta with no concept
children is depth 1; otherwise depth is ``1 + max(children)``.

The walk is memoized per call (never globally) and bounded so a cycle in
legacy data cannot recurse forever.
"""
from __future__ import annotations

from typing import Any, Protocol

# Walks deeper than this are treated as already over the v1 cap. A real
# graph at L46 is height 2; this is just a cycle/runaway brake.
_MAX_WALK = 8


class _DepthStore(Protocol):
    def get(self, concept_id: int) -> Any: ...
    def evidence_of(self, concept_id: int) -> list[Any]: ...


def _concept_child_ids(store: _DepthStore, concept_id: int) -> list[int]:
    try:
        edges = store.evidence_of(int(concept_id))
    except Exception:
        return []
    out: list[int] = []
    for edge in edges:
        if str(getattr(edge, "src_type", "") or "") != "concept":
            continue
        try:
            out.append(int(edge.src_id))
        except (TypeError, ValueError):
            continue
    return out


def meta_depth(
    store: _DepthStore,
    concept_id: int,
    *,
    memo: dict[int, int] | None = None,
    visiting: set[int] | None = None,
    max_walk: int = _MAX_WALK,
) -> int:
    """Depth of ``concept_id`` in the meta evidence DAG.

    ``0`` for missing / non-meta rows. A meta with no walkable concept
    children is ``1``. Cycles and over-long walks return ``max_walk`` so a
    ``depth < cap`` check refuses them rather than hanging.
    """
    cid = int(concept_id)
    if memo is None:
        memo = {}
    if cid in memo:
        return memo[cid]
    if visiting is None:
        visiting = set()
    if cid in visiting or len(visiting) >= int(max_walk):
        return int(max_walk)
    try:
        concept = store.get(cid)
    except Exception:
        concept = None
    if concept is None:
        memo[cid] = 0
        return 0
    if str(getattr(concept, "evidence_model", "") or "") != "meta":
        memo[cid] = 0
        return 0
    visiting.add(cid)
    children = _concept_child_ids(store, cid)
    if not children:
        visiting.discard(cid)
        memo[cid] = 1
        return 1
    depth = 1 + max(
        meta_depth(
            store, child, memo=memo, visiting=visiting, max_walk=max_walk,
        )
        for child in children
    )
    visiting.discard(cid)
    memo[cid] = depth
    return depth


def descendant_ids(
    store: _DepthStore,
    concept_id: int,
    *,
    max_walk: int = _MAX_WALK,
) -> set[int]:
    """Concept ids reachable *down* from ``concept_id`` (children, not self)."""
    out: set[int] = set()
    root = int(concept_id)
    stack: list[tuple[int, int]] = [(root, 0)]
    seen = {root}
    while stack:
        cid, depth = stack.pop()
        if depth >= int(max_walk):
            continue
        for child in _concept_child_ids(store, cid):
            if child in seen:
                continue
            seen.add(child)
            out.add(child)
            stack.append((child, depth + 1))
    return out


def would_cycle(
    store: _DepthStore,
    parent_id: int | None,
    child_id: int,
    *,
    max_walk: int = _MAX_WALK,
) -> bool:
    """True if adding ``child -> parent`` would close a loop.

    A new parent (``parent_id`` missing / non-positive) cannot cycle.
    Otherwise the parent is already the child, or already a descendant of
    the child (the child already abstracts the parent, possibly via hops).
    """
    if parent_id is None:
        return False
    try:
        parent = int(parent_id)
        child = int(child_id)
    except (TypeError, ValueError):
        return False
    if parent <= 0 or child <= 0:
        return False
    if parent == child:
        return True
    return parent in descendant_ids(store, child, max_walk=max_walk)


__all__ = ["descendant_ids", "meta_depth", "would_cycle"]
