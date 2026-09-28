"""L49 shadow evidence accounting. Never writes confidence or promotion state."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlsplit


@dataclass(frozen=True)
class SourceLineage:
    roots: frozenset[str]
    category: str
    complete: bool = True


def support_summary(sources: Iterable[SourceLineage]) -> dict[str, object]:
    """Count connected observation groups, not representations of observations.

    Overlapping windows conservatively form one group. Disjoint IDs establish
    only recorded separation, not causal independence. Unknown roots add no vote.
    """
    rows = list(sources)
    groups: list[set[str]] = []
    for row in rows:
        if not row.roots or row.category == "model_reflection":
            continue
        merged = set(row.roots)
        separate = []
        for group in groups:
            if merged.intersection(group):
                merged.update(group)
            else:
                separate.append(group)
        groups = separate + [merged]
    return {
        "mode": "shadow",
        "known_support_groups": len(groups),
        "root_count": len(set().union(*(row.roots for row in rows))) if rows else 0,
        "unknown_sources": sum(not row.complete or not row.roots for row in rows),
        "excluded_reflections": sum(row.category == "model_reflection" for row in rows),
        "by_category": dict(sorted(Counter(row.category for row in rows).items())),
    }


def _ids(value: object) -> list[int]:
    if not isinstance(value, (list, tuple)):
        return []
    return sorted({item for item in value if type(item) is int and item > 0})


def _memory_lineage(memory) -> SourceLineage:
    metadata = getattr(memory, "metadata", None) or {}
    if not isinstance(metadata, dict):
        return SourceLineage(frozenset(), "unknown", complete=False)
    kind = getattr(memory, "kind", "")
    if kind == "knowledge":
        urls = metadata.get("source_urls", [])
        urls = urls if isinstance(urls, list) else []
        roots = set()
        for url in [metadata.get("source_url"), *urls]:
            if not isinstance(url, str):
                continue
            try:
                parsed = urlsplit(url.strip())
                if parsed.scheme in {"http", "https"} and parsed.hostname:
                    roots.add("url:" + parsed._replace(fragment="").geturl())
            except ValueError:
                continue
        return SourceLineage(frozenset(roots), "external_observation", complete=bool(roots))
    admission = metadata.get("admission")
    category = "testimony" if getattr(memory, "provenance", "") == "stated" else "model_inference"
    if kind in {"self", "reflection", "diary", "pre_thought", "goal_progress"}:
        category = "model_reflection"
    if isinstance(admission, dict):
        source_ids = _ids(admission.get("source_message_ids"))
        if admission.get("subject") == "assistant":
            category = "model_reflection"
        window_ids = _ids(admission.get("input_message_ids")) if source_ids else []
    else:
        source_ids = _ids(metadata.get("source_message_ids"))
        source_ids += _ids([getattr(memory, "source_message_id", None)])
        window_ids = []
    roots = frozenset(f"message:{source_id}" for source_id in source_ids + window_ids)
    return SourceLineage(roots, category, complete=bool(source_ids))


def resolve_support(
    store, memory_store, topic_graph, concept_id: int, *, max_nodes: int = 256, max_depth: int = 12,
) -> dict[str, object]:
    """Resolve positive evidence to recorded roots under a strict traversal budget.

    Summaries and concepts are derivations, never observations. Current cluster
    membership is a diagnostic approximation, not a historical source snapshot.
    """
    max_nodes = min(1024, max(1, int(max_nodes)))
    max_depth = min(32, max(0, int(max_depth)))
    sources: list[SourceLineage] = []
    issues: Counter[str] = Counter()
    seen: set[tuple[str, int]] = set()
    concept = store.get(int(concept_id)) if store is not None else None
    pending = [("concept", int(concept_id), 0, frozenset())]
    direct_sources: set[tuple[str, str]] = set()

    def unknown(reason: str) -> None:
        issues[reason] += 1
        sources.append(SourceLineage(frozenset(), "unknown", complete=False))

    while pending:
        if len(seen) >= max_nodes:
            unknown("node_limit")
            break
        node_type, node_id, depth, ancestors = pending.pop()
        key = (node_type, node_id)
        if key in ancestors:
            unknown("cycle")
            continue
        if key in seen:
            continue
        if depth > max_depth:
            unknown("depth_limit")
            continue
        seen.add(key)
        children: list[tuple[str, int]] = []
        if node_type == "concept":
            node = store.get(node_id) if store is not None else None
            if node is None:
                unknown("missing_concept")
                continue
            edges = store.evidence_of(node_id)
            for edge in edges[:max_nodes]:
                if edge.polarity <= 0 or edge.strength <= 0:
                    continue
                if depth == 0:
                    direct_sources.add((edge.src_type, str(edge.src_id)))
                try:
                    children.append((edge.src_type, int(edge.src_id)))
                except (TypeError, ValueError):
                    unknown("invalid_edge")
            if len(edges) > max_nodes:
                unknown("edge_limit")
        elif node_type == "cluster":
            cluster_id = topic_graph.cluster_id_for(node_id) if topic_graph is not None else None
            members = topic_graph.cluster_member_ids(cluster_id) if cluster_id is not None else []
            children = [("memory", member) for member in _ids(members)]
            if not children:
                unknown("missing_cluster")
                continue
        elif node_type == "memory":
            memory = memory_store.get(node_id) if memory_store is not None else None
            if memory is None:
                unknown("missing_memory")
                continue
            metadata = getattr(memory, "metadata", None) or {}
            if not isinstance(metadata, dict):
                unknown("invalid_metadata")
                continue
            if getattr(memory, "kind", "") == "topic_digest":
                children = [("memory", source) for source in _ids(metadata.get("source_ids"))]
                if not children:
                    unknown("ungrounded_summary")
                    continue
            else:
                sources.append(_memory_lineage(memory))
                continue
        else:
            unknown("unsupported_node")
            continue
        if not children:
            unknown("ungrounded_concept")
        if len(children) > max_nodes:
            unknown("edge_limit")
        remaining = max_nodes - len(seen) - len(pending)
        if len(children) > remaining:
            unknown("node_limit")
        pending.extend(
            (child_type, child_id, depth + 1, ancestors | {key})
            for child_type, child_id in children[:max(0, remaining)]
        )

    result = support_summary(sources)
    result.update({
        "concept_id": int(concept_id),
        "stored_distinct_source_count": int(getattr(concept, "distinct_source_count", 0)),
        "direct_source_count": len(direct_sources),
        "visited_nodes": len(seen),
        "complete": not issues and result["unknown_sources"] == 0,
        "issues": dict(sorted(issues.items())),
    })
    return result
