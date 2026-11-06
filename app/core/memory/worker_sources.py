"""Whole source packets for derived worker prose; context is not new testimony."""
import hashlib
import json

from app.core.infra import timephrase


def transcript_packet(rows, *, user_name="User", assistant_name="Aiko", session_key=None,
                      max_chars=2400):
    from app.core.memory.shared_shorthand import source_receipt

    kept, sources, used = [], [], 0
    for row in reversed(list(rows)):
        content = str(getattr(row, "content", "") or "").strip()
        if not content:
            continue
        line = timephrase.format_transcript([row], role_labels={
            "user": user_name, "assistant": assistant_name})
        mid = getattr(row, "id", None)
        if type(mid) is int:
            line = f"ID {mid} " + line
        if len(line) + used > max_chars:
            break
        kept.append(line)
        used += len(line) + 1
        if type(mid) is int and getattr(row, "session_id", None) is not None:
            sources.append({**source_receipt(row, content), "source_at": row.created_at})
    sources.reverse()
    text = "\n".join(reversed(kept))
    key = hashlib.sha256(json.dumps([session_key, sources or text], sort_keys=True,
                                   ensure_ascii=False).encode()).hexdigest()
    return {"version": 1, "scope": "generation_context", "session_key": session_key,
            "message_sources": sources, "memory_inputs": [], "key": key, "text": text,
            "user_name": user_name}


def current(packet, store):
    from app.core.memory.observation_receipts import receipt_sources_current
    from app.core.memory.reflective_evidence import current as memories_current

    sources = packet.get("message_sources", [])
    return ((not sources or receipt_sources_current({"version": 1, "sources": sources}, store))
            and memories_current(packet, store))


def receipt(packet):
    return {key: value for key, value in packet.items() if key != "text"}
