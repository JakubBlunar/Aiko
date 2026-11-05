"""Separate a research opportunity from a collection of personal scenes.

Density measures notes, not ignorance or curiosity. This conservative call-free
filter prevents known scene/intention collections being treated as public
research subjects. Actual research/open-question rows can still justify a topic.
It is not a semantic classifier of every possible public/private subject.
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any


_SCENE_KINDS = frozenset({
    "shared_moment", "promise", "relationship", "self", "diary", "dream",
    "reflection", "goal", "goal_progress", "callback", "pursuit_note",
})
_SCENE_LABEL = re.compile(
    r"\b(?:cuddl\w*|snuggl\w*|intimacy|intimate|goodnight|bedtime|"
    r"(?:his|her|their|our) promises)\b|^shared moment\b|\bpromised to\b|"
    r"\b[a-z]+['’]s promises\b|\bpromises to\b", re.I,
)


def explorable_topic(cluster: Any) -> bool:
    label = str(getattr(cluster, "summary", getattr(cluster, "label", "")) or "").strip()
    if not label or label.endswith(("…", "...")):
        return False
    kinds = tuple(getattr(cluster, "member_kinds", ()) or ())
    if any(k in {"knowledge_gap", "open_question"} for k in kinds):
        return True
    if kinds and sum(k in _SCENE_KINDS for k in kinds) * 2 >= len(kinds):
        return False
    research = sum(k in {"knowledge", "curiosity_finding"} for k in kinds)
    if _SCENE_LABEL.search(label) and research * 2 <= len(kinds):
        return False
    return True


def payload_explorable(cue_type: str, payload: dict, graph: Any = None) -> bool:
    """Recheck old queued labels against current topic roles when available."""
    if payload.get("exploration_version") != 1:
        if any(key in payload for key in ("at", "cluster_key", "edge_key")):
            return False  # Old automatic inference of ignorance/curiosity.
        return True  # An explicitly queued move rather than worker output.
    fields = ("topic",) if cue_type == "knowledge_gap_notice" else ("dense_topic", "thin_topic")
    labels = {str(payload.get(field) or "").strip() for field in fields}
    try:
        lookup = getattr(graph, "topic_clusters_for_labels", None)
        clusters = (lookup(labels) if callable(lookup) else graph.topic_clusters()
                    if graph is not None else [])
    except Exception:
        return False
    by_label = {str(c.summary).strip().casefold(): c for c in clusters}
    for field in fields:
        label = payload.get(field)
        if not isinstance(label, str) or not label.strip():
            return False
        cluster = by_label.get(label.strip().casefold())
        # With a current graph, an orphaned label is no longer a live direction.
        if graph is not None and cluster is None:
            return False
        if not explorable_topic(cluster or SimpleNamespace(summary=label)):
            return False
    return True
