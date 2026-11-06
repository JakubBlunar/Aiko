"""Qualified, bounded tool reads. A search match is not proof of a claim."""
from dataclasses import asdict
import json
from types import SimpleNamespace

from app.core.memory.memory_origin import MemoryOrigin, memory_origin
from app.core.memory.memory_relations import reference_revision
from app.core.memory.reflective_evidence import eligible


def owned(session_key, user_id):
    return not (user_id and session_key and ":" in str(session_key)
                and str(session_key).split(":", 1)[0] != user_id)


def source_ids(row):
    meta = getattr(row, "metadata", None) or {}
    ids = [getattr(row, "source_message_id", None)]
    ids += [c.get("message_id") for c in (meta.get("admission") or {}).get("evidence", [])
            if isinstance(c, dict)]
    for key in ("observation", "claim_fidelity", "moment_evidence", "image_evidence",
                "hypothesis_answer", "catchphrase_evidence"):
        ids += [c.get("message_id") for c in (meta.get(key) or {}).get("sources", [])
                if isinstance(c, dict)]
    ids += [c.get("message_id") for c in (meta.get("reflective_derivation") or {}).get(
        "message_sources", []) if isinstance(c, dict)]
    return list(dict.fromkeys(mid for mid in ids if type(mid) is int))[:16]


def source_state(row, store=None, db=None, user_id=None):
    if not eligible(row):
        return "withdrawn"
    from app.core.infra import timephrase

    until = timephrase.parse_iso(getattr(row, "relevance_until", None))
    if until is not None and timephrase.to_aware(until) < timephrase.utcnow():
        return "outside_validity"
    meta = getattr(row, "metadata", None) or {}
    if store is not None:
        from app.core.memory.observation_receipts import longitudinal_sources_current

        if not longitudinal_sources_current(row, store):
            return "changed"
        derivation = meta.get("reflective_derivation") or {}
        if derivation.get("message_sources"):
            from app.core.memory.worker_sources import current
            if not current(derivation, store):
                return "changed"
    citations = (meta.get("admission") or {}).get("evidence", [])
    if db is not None:
        for mid in source_ids(row):
            original = db.get_message_row(mid)
            if original is None or not owned(original.session_id, user_id):
                return "changed_or_unavailable"
            for citation in citations:
                if citation.get("message_id") == mid and " ".join(
                    str(citation.get("quote") or "").casefold().split()
                ) not in " ".join(original.content.casefold().split()):
                    return "changed"
    return "receipts_present" if any(meta.get(key) for key in (
        "admission", "observation", "claim_fidelity", "moment_evidence", "image_evidence",
        "hypothesis_answer", "reflective_derivation", "pursuit_evidence",
    )) else "untracked"


def memory_record(row, *, store=None, db=None, user_id=None):
    from app.core.memory.observation_receipts import facet_scope

    record = {
        "source": "memory", "memory_id": int(row.id), "kind": row.kind,
        "text": row.content, "revision": reference_revision(row),
        **asdict(memory_origin(row)),
        "provenance": getattr(row, "provenance", "inferred"),
        "created_at": getattr(row, "created_at", None),
        "recorded_at": getattr(row, "created_at", None),
        "event_time": getattr(row, "event_time", None),
        "temporal_type": getattr(row, "temporal_type", "unknown"),
        "relevance_until": getattr(row, "relevance_until", None),
        "confidence": getattr(row, "confidence", None),
        "source_session": getattr(row, "source_session", None),
        "source_message_ids": source_ids(row),
        "source_state": source_state(row, store, db, user_id),
    }
    facets = facet_scope(row)
    if len(json.dumps(facets, default=str)) <= 4000:
        record["source_facets"] = facets
    else:
        record["source_facets_omitted"] = True
    return record


def pack_hits(hits, *, store=None, db=None, user_id=None, max_chars=16000):
    out, omitted, withheld = [], 0, 0
    used = 0
    for hit in hits:
        row = getattr(hit, "record", None) or SimpleNamespace(
            id=None, content=hit.text, kind=getattr(hit, "kind", "unknown"))
        if hit.source == "memory":
            if store is not None:
                row = store.get(int(row.id))
                if row is None:
                    withheld += 1
                    continue
            if not owned(getattr(row, "source_session", None), user_id):
                withheld += 1
                continue
            if store is not None:
                item = memory_record(row, store=store, db=db, user_id=user_id)
            else:
                origin = getattr(hit, "memory_origin", None) or MemoryOrigin(
                    "unknown", "unknown", "unhydrated_recall")
                item = {"source": "memory", "memory_id": row.id, "text": row.content,
                        **asdict(origin), "created_at": getattr(row, "created_at", None),
                        "event_time": getattr(hit, "event_time", None),
                        "temporal_type": getattr(hit, "temporal_type", None),
                        "provenance": getattr(hit, "memory_provenance", None),
                        "source_state": "untracked"}
        else:
            session = getattr(row, "session_id", None)
            if not owned(session, user_id):
                withheld += 1
                continue
            if hit.source == "message" and db is not None:
                row = db.get_message_row(row.message_id)
                if row is None or not owned(row.session_id, user_id):
                    withheld += 1
                    continue
            item = {"source": hit.source, "source_id": str(getattr(row, "id", "")),
                    "text": row.content, "created_at": getattr(row, "created_at", None)}
            for name in ("role", "session_id", "message_id", "document_id", "filename",
                         "chunk_index"):
                if hasattr(row, name):
                    item[name] = getattr(row, name)
        item["score"] = round(float(hit.score), 3)
        item["score_meaning"] = "relevance ranking, not confidence or proof"
        item["expansion"] = bool(getattr(hit, "expansion", False))
        text = item["text"] or ""
        if len(text) > 6000 or len(json.dumps(item, default=str)) + used > max_chars:
            item.update(text=None, text_omitted=True, text_characters=len(text),
                        inspection_required=True)
            omitted += 1
        item["text_complete"] = not item.get("text_omitted", False)
        size = len(json.dumps(item, default=str))
        if used + size > max_chars:
            item.pop("source_facets", None)
            item["source_facets_omitted"] = True
            size = len(json.dumps(item, default=str))
            if used + size > max_chars:
                omitted += 1
                continue
        out.append(item)
        used += size
    return {"hits": out, "coverage": "bounded_search_results", "omitted": omitted,
            "withheld": withheld,
            "note": "Missing event time is unknown. Source receipts are not entailment proof. "
                    "Open omitted memory text with inspect_memory before relying on its meaning."}
