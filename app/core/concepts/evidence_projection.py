"""Bounded, read-only evidence packets for recall and tentative thoughts.

IDs and revisions are receipts, not proof of entailment. Recorded separation
of sources is deliberately distinct from independent real-world observations.
"""
from __future__ import annotations

from collections import deque
from typing import Any

from app.core.concepts.concept_evidence_lineage import memory_lineages, resolve_support
from app.core.concepts.concept_support import memory_revision


def project_evidence(store: Any, memories: Any, graph: Any, concept: Any,
                     *, limit: int = 3) -> dict:
    cap = max(1, min(16, int(limit)))
    cid = int(concept.concept_id)
    packet: dict = {"concept_id": cid, "revision": str(getattr(concept, "updated_at", "")),
                    "evidence": [], "support": {"complete": False},
                    "validity": {"tracked": False, "reason": "unknown"}, "truncated": False}
    if store is None:
        packet["uncertainty"] = uncertainty_reason(concept, packet)
        return packet
    dependencies = getattr(store, "dependencies", None)
    if dependencies is not None:
        packet["validity"] = dependencies.assess(cid, max_nodes=64)
    try:
        packet["support"] = resolve_support(store, memories, graph, cid, max_nodes=64)
    except (AttributeError, TypeError, ValueError):
        pass
    ledger = getattr(store, "applicability", None)
    packet["applicability"] = ledger.get(cid) if ledger is not None else None
    if packet["applicability"]:
        from app.core.concepts.concept_applicability import scope_sources_valid

        packet["applicability_sources_valid"] = scope_sources_valid(
            packet["applicability"], memories,
        )
    clusters = {}
    if graph is not None:
        clusters = {int(c.representative_id): c for c in graph.topic_clusters()}
    pending = deque([("concept", cid, 0)])
    seen: set[tuple[str, int]] = set()
    while pending and len(seen) < 64:
        kind, ident, depth = pending.popleft()
        if (kind, ident) in seen:
            continue
        seen.add((kind, ident))
        if kind in {"memory", "moment"}:
            row = memories.get(ident) if memories is not None else None
            if row is None:
                continue
            lineages = memory_lineages(row)
            packet["evidence"].append({
                "memory_id": ident, "revision": memory_revision(row),
                "text": str(row.content or "")[:280],
                "subject": (getattr(row, "metadata", None) or {}).get("subject", "unknown"),
                "authority": lineages[0].category,
                "source_message_id": getattr(row, "source_message_id", None),
                "source_session": getattr(row, "source_session", None),
                "observed_at": getattr(row, "event_time", None),
                "recorded_at": str(getattr(row, "created_at", "")),
                "source_groups": [sorted(s.roots) for s in lineages[:8]],
            })
            if len(packet["evidence"]) >= cap:
                packet["truncated"] = bool(pending)
                break
        elif kind == "cluster":
            cluster = clusters.get(ident)
            if cluster is not None:
                members = list(cluster.member_ids)
                pending.extend(("memory", int(mid), depth + 1) for mid in members[:64])
                packet["truncated"] |= len(members) > 64
        elif kind == "concept" and depth < 4:
            edges = list(store.evidence_of(ident))
            for edge in edges[:64]:
                if getattr(edge, "polarity", 1) <= 0 or getattr(edge, "strength", 1) <= 0:
                    continue
                try:
                    pending.append((edge.src_type, int(edge.src_id), depth + 1))
                except (TypeError, ValueError):
                    continue
            packet["truncated"] |= len(edges) > 64
        else:
            packet["truncated"] = True
    packet["truncated"] |= bool(pending)
    packet["uncertainty"] = uncertainty_reason(concept, packet)
    return packet


def uncertainty_reason(concept: Any, packet: dict) -> str:
    validity = packet.get("validity", {})
    support = packet.get("support", {})
    if validity.get("tracked") and not validity.get("viable"):
        return "The recorded support has changed or is no longer sufficient."
    if not packet.get("evidence"):
        return "No usable supporting observation was recovered; the basis is unknown."
    if not support.get("complete"):
        return "The source history is incomplete; repeated notes may share one origin."
    if int(support.get("known_support_groups", 0)) < 2:
        return "The recorded basis is narrow; it may describe one episode rather than a pattern."
    if float(getattr(concept, "confidence", 0)) < 0.72:
        return "There are supporting observations, but the interpretation is still unsettled."
    return "The observations do not establish that this holds in other situations."


def compact_basis(packet: dict) -> str:
    """One dated excerpt, explicitly offered as data rather than an instruction."""
    rows = packet.get("evidence", [])
    if not rows:
        return ""
    row = rows[0]
    # Absolute source stamps do not consume the prompt cache with changing ages.
    stamp = row.get("observed_at") or row.get("recorded_at") or "date unknown"
    return (f'Basis [memory:{row["memory_id"]}, {stamp}, {row["authority"]}]: '
            f'{row["text"][:160]!r}. Open point: {packet["uncertainty"]}')
