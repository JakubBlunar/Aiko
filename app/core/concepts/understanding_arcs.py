"""L29b: bounded, revisable paths through recorded concept changes.

An arc is a read projection of the canonical learning/lifecycle logs, never
another belief store. Hash receipts preserve source identity at detection.
Legacy events without receipts remain explicitly unverified.
"""
from __future__ import annotations

import json
import re

from app.core.concepts.concept_support import memory_revision, revision_hash
from app.core.concepts.concept_evidence_lineage import memory_lineages

PREFIX = "concept.learning_sources:"
_REFLECTION = re.compile(r"\b(?:changed|grown|used to|at first|over time|since we|"
                         r"understanding|understand me|understand us|understand yourself)\b", re.I)


def retrospective_request(text):
    return bool(_REFLECTION.search(text or "") and (
        "?" in (text or "") or re.search(r"\b(?:tell|describe|explain|reflect|remember)\b",
                                         text or "", re.I)))


def requested_subjects(text):
    if re.search(r"\b(?:our relationship|between us|about us|understand us)\b", text or "", re.I):
        return {"relationship"}
    if re.search(
        r"\b(?:yourself|you(?:'ve| have)? changed|your own|about you)\b", text or "", re.I,
    ):
        return {"aiko"}
    if re.search(r"\b(?:about me|understand me|i(?:'ve| have)? changed)\b", text or "", re.I):
        return {"user"}
    return {"user", "aiko", "relationship"}


def _event_hash(event):
    return revision_hash([event.subject, event.concept_id, event.prior_concept_id,
                          event.old_label, event.new_label, event.because, event.decisive_event_id])


def _node(kind, ident, store, memories, graph, *, frozen_members=None):
    ident = int(ident)
    if kind in {"memory", "moment"}:
        row = memories.get(ident) if memories is not None else None
        if row is None:
            raise ValueError("source_missing")
        roots = {root for lineage in memory_lineages(row) for root in lineage.roots
                 if root.startswith("message:")}
        return memory_revision(row), roots
    if kind == "concept":
        row = store.get(ident)
        if row is None or row.status != "active" or not store.is_assertable(ident):
            raise ValueError("source_concept_not_held")
        return revision_hash([row.label, row.rationale, row.subject]), set()
    if kind == "cluster":
        member_ids = frozen_members
        if member_ids is None:
            clusters = graph.topic_clusters() if graph is not None else []
            cluster = next((c for c in clusters if int(c.representative_id) == ident), None)
            member_ids = list(cluster.member_ids) if cluster is not None else []
        if not member_ids or len(member_ids) > 32:
            raise ValueError("cluster_missing_or_over_budget")
        members, roots = [], set()
        for mid in sorted(member_ids):
            fingerprint, origins = _node("memory", mid, store, memories, graph)
            members.append([mid, fingerprint])
            roots.update(origins)
        return revision_hash([ident, members]), roots
    raise ValueError("unknown_source")


def record_learning_sources(db, event, store, memories, graph):
    if not event.event_id:
        return
    nodes, roots, complete = [], set(), True
    clusters = {}
    refs = list(dict.fromkeys(event.evidence_refs))
    for kind, ident in refs[:32]:
        try:
            fingerprint, origins = _node(kind, ident, store, memories, graph)
            if kind == "cluster":
                cluster = next(c for c in graph.topic_clusters()
                               if int(c.representative_id) == int(ident))
                clusters[str(ident)] = sorted(cluster.member_ids)
            nodes.append([kind, str(ident), fingerprint])
            roots.update(origins)
        except (ValueError, TypeError, AttributeError):
            complete = False
    messages = []
    for root in sorted(roots)[:64]:
        row = db.get_message_row(int(root.split(":", 1)[1]))
        if row is None or re.search(
            r"\b(?:role.?play|fictional|hypothetical)\b", row.content, re.I,
        ):
            complete = False
        else:
            messages.append([row.id, revision_hash([row.role, row.content, row.session_id])])
    receipt = {"event_revision": _event_hash(event), "nodes": nodes, "messages": messages,
               "cluster_members": clusters,
               "complete": complete and bool(nodes) and len(refs) <= 32 and len(roots) <= 64}
    db.kv_set(PREFIX + str(event.event_id), json.dumps(receipt))


def _sources_valid(db, event, store, memories, graph):
    raw = db.kv_get(PREFIX + str(event.event_id))
    try:
        receipt = json.loads(raw) if raw else {}
        if not receipt.get("complete") or receipt.get("event_revision") != _event_hash(event):
            return False
        if not 1 <= len(receipt.get("nodes", [])) <= 32 or len(receipt.get("messages", [])) > 64:
            return False
        for kind, ident, fingerprint in receipt["nodes"]:
            current, _roots = _node(
                kind, ident, store, memories, graph,
                frozen_members=receipt.get("cluster_members", {}).get(str(ident)),
            )
            if current != fingerprint:
                return False
        for ident, fingerprint in receipt["messages"]:
            row = db.get_message_row(ident)
            if row is None or revision_hash([row.role, row.content, row.session_id]) != fingerprint:
                return False
        return True
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def understanding_arc(concept, *, store, learning, lifecycle, db, memories=None, graph=None):
    packet = {"concept_id": concept.concept_id, "subject": concept.subject,
              "revision": revision_hash([concept.label, concept.rationale]),
              "status": "thin_record", "steps": [], "reason": "insufficient_verified_changes"}
    if (learning is None or lifecycle is None or db is None
            or concept.status != "active" or not store.is_assertable(concept.concept_id)):
        return packet
    # One bounded page, shared across successive endpoints. Never scan the graph.
    events = learning.list(limit=128, subject=concept.subject)
    cursor, label = concept.concept_id, concept.label
    selected, seen = [], set()
    lifecycle_cache = {}
    for event in events:
        if len(selected) >= 3:
            break
        if (event.shape not in {"succession", "relabel", "emergence", "revival"}
                or event.subject != concept.subject or event.new_label != label
                or not event.concept_id or not event.created_at
                or learning.resolve_alias(event.concept_id) != learning.resolve_alias(cursor)
                or event.event_id in seen):
            continue
        if not _sources_valid(db, event, store, memories, graph):
            packet["reason"] = "missing_or_revised_source_receipt"
            break
        # The classified change must still resolve to a structural lifecycle event.
        ids = {event.concept_id, event.prior_concept_id} - {None}
        getter = getattr(lifecycle, "get", None)
        if callable(getter):
            found = getter(event.decisive_event_id)
            proof = [found] if found is not None and found.concept_id in ids else []
        else:
            proof = []
            for cid in ids:
                if cid not in lifecycle_cache:
                    lifecycle_cache[cid] = lifecycle.list(limit=64, concept_id=cid)
                proof.extend(lifecycle_cache[cid])
        if not any(p.event_id == event.decisive_event_id and p.subject == concept.subject
                   and p.event_type in {"promoted", "relabeled", "revived", "dormant", "retired"}
                   for p in proof):
            packet["reason"] = "lifecycle_proof_missing"
            break
        selected.append(event)
        seen.add(event.event_id)
        if not event.old_label:
            break
        label = event.old_label
        cursor = event.prior_concept_id or event.concept_id
    if len(selected) < 2:
        return packet
    selected.reverse()
    packet["steps"] = [{"learning_event_id": e.event_id, "concept_id": e.concept_id,
                        "prior_concept_id": e.prior_concept_id, "at": e.created_at,
                        "earlier": e.old_label[:160], "later": e.new_label[:160],
                        "recorded_reason": e.because[:140],
                        "source_refs": list(e.evidence_refs)[:8],
                        "lifecycle_event_id": e.decisive_event_id} for e in selected]
    packet.update(status="supported", reason="recorded_changes_with_current_sources",
                  fingerprint=revision_hash([concept.subject, [e.event_id for e in selected]]))
    return packet


def render_arc(packet):
    if packet.get("status") != "supported":
        return ""
    subject = {"user": "the user", "aiko": "yourself", "relationship": "your relationship"}[
        packet["subject"]]
    lines = [f"Recorded changes in your understanding of {subject}:"]
    for step in packet["steps"]:
        lines.append(f"- {step['at']}: earlier impression {step['earlier']!r}; "
                     f"later understanding {step['later']!r}. "
                     f"Recorded interpretation: {step['recorded_reason']!r}.")
    lines.append("These are changes in what you believed, not proof a person changed. "
                 "Keep the chronology and uncertainty; do not invent motives or missing episodes.")
    return "\n".join(lines)
