"""F10j — cluster-scoped memory hygiene.

Both the F5 conflict detector
(:class:`app.core.memory.memory_conflict_worker.MemoryConflictWorker`) and
the K35 consolidation worker
(:class:`app.core.memory.memory_consolidation_worker.MemoryConsolidationWorker`)
do an ``O(n^2)`` all-pairs cosine over their candidate snapshot. That is
two costs in one: the quadratic scan blows up as the store grows (the P30
concern), and most of those pairs are topically unrelated, so the LLM
verifier / merger spends its rate-limited budget on cross-topic noise.

This helper partitions the candidate list by the K9 topic graph so each
worker scans *within* a cluster: the work drops from ``O(n^2)`` to
``sum(O(k_c^2))`` over the (much smaller) per-cluster sizes, and the pairs
that remain are exactly the topically-adjacent ones where contradictions /
near-duplicates actually live ("loves cats" vs "hates cats" are in the
same cluster; "loves cats" vs "works at a bank" never were a useful pair).

**Tradeoff.** A pair whose members landed in *different* clusters is no
longer compared, even if their cosine is in-band. In practice that's rare
— the clustering similarity floor (0.55) is far looser than the conflict
band (``[0.80, 0.92)``) or the dedupe threshold (~0.90), so two memories
that close almost always co-cluster — and it's eventually-consistent: as
the graph re-clusters on later ticks a split pair tends to rejoin. The
``cluster_scoped_memory_hygiene_enabled`` master switch restores the full
sweep when off.

Graceful degradation: when the switch is off, the graph is absent, or the
graph is in the non-persistent / unwarmed mode (``cluster_id_for`` returns
``None`` for everything), every candidate falls into a single group and
the workers behave exactly as they did before F10j.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from app.core.conversation.topic_graph import TopicGraph
    from app.core.memory.memory_store import Memory


log = logging.getLogger("app.memory_cluster_scope")


def partition_by_cluster(
    candidates: Sequence["Memory"],
    topic_graph: "TopicGraph | None",
    *,
    enabled: bool = True,
    min_group: int = 2,
) -> list[list["Memory"]]:
    """Group ``candidates`` by their topic cluster for within-cluster sweeps.

    Returns a list of groups, each a list of :class:`Memory`. Behaviour:

    * **Disabled / no graph / non-persistent** → a single group containing
      every candidate (the pre-F10j behaviour). Empty input → ``[]``.
    * **Partitioned** → one group per ``cluster_id`` plus one extra group
      for the *unclustered* bucket (memories the graph hasn't assigned, or
      that fell below a cluster's min size). Groups with fewer than
      ``min_group`` members are dropped (they can't form a pair). Groups
      are ordered by their newest member descending, so under a shared
      per-run cap the freshest topics are scanned first — preserving the
      "newest first" priority both workers already rely on.

    Never raises: a per-memory ``cluster_id_for`` failure buckets that row
    as unclustered.
    """
    rows = list(candidates)
    if not rows:
        return []
    if (
        not enabled
        or topic_graph is None
        or not bool(getattr(topic_graph, "persistent", False))
    ):
        return [rows]

    groups: dict[Any, list["Memory"]] = {}
    for mem in rows:
        try:
            cid = topic_graph.cluster_id_for(int(mem.id))
        except Exception:
            cid = None
        groups.setdefault(cid, []).append(mem)

    out = [g for g in groups.values() if len(g) >= max(2, int(min_group))]
    out.sort(
        key=lambda g: max((m.created_at or "") for m in g), reverse=True,
    )
    log.debug(
        "cluster-scope partition: candidates=%d clusters=%d scannable_groups=%d",
        len(rows),
        len(groups),
        len(out),
    )
    return out


__all__ = ["partition_by_cluster"]
