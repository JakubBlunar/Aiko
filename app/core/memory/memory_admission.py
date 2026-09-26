"""Conservative evidence admission shared by inline and batch memory writers."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re


def _literal(text: str) -> str:
    return " ".join(str(text).casefold().split()).rstrip(".! ")


@dataclass(frozen=True)
class MemoryAdmission:
    accepted: bool
    provenance: str
    tier: str
    source_message_id: int | None
    metadata: dict


def admit_memory(
    candidate: dict, rows, *, session_key: str, writer: str, proposal_message_id: int | None = None,
) -> MemoryAdmission:
    content = str(candidate.get("content") or "").strip()
    subject = "assistant" if candidate.get("kind") == "self" else "user"
    destination = str(candidate.get("destination") or "memory")
    evidence = candidate.get("evidence")
    source_rows = {
        row.id: row for row in rows
        if getattr(row, "session_id", None) == session_key and row.role == subject
    }
    validated = []
    rejected = False
    if evidence is not None:
        if not isinstance(evidence, list) or len(evidence) > 4:
            rejected = True
        else:
            for item in evidence:
                if not isinstance(item, dict) or type(item.get("message_id")) is not int:
                    rejected = True
                    break
                row = source_rows.get(item["message_id"])
                quote = item.get("quote")
                if row is None or not isinstance(quote, str) or not quote.strip():
                    rejected = True
                    break
                if _literal(quote) not in _literal(row.content):
                    rejected = True
                    break
                validated.append({"message_id": row.id, "quote": quote[:1000]})
    elif writer == "inline":
        for row in source_rows.values():
            if _literal(content) == _literal(row.content):
                validated.append({"message_id": row.id, "quote": row.content[:1000]})
                break

    literal_support = any(
        _literal(content) == _literal(item["quote"])
        == _literal(source_rows[item["message_id"]].content)
        for item in validated
    )
    uncertain = bool(
        re.search(r"\b(maybe|might|perhaps|guess|suppose|if|would|could)\b|[?\"]", content, re.I)
    )
    stated = subject == "user" and literal_support and not uncertain and not rejected
    reason = (
        "invalid_evidence" if rejected else "working_context" if destination == "working"
        else "literal_testimony" if stated else "self_note" if subject == "assistant"
        else "provisional_inference"
    )
    accepted = not rejected and destination == "memory"
    source_ids = sorted({item["message_id"] for item in validated})
    identity = f"{writer}:{session_key}:{proposal_message_id}:{source_ids}:{content}"
    metadata = {
        "admission": {
            "version": 1, "writer": writer, "subject": subject,
            "source_message_ids": source_ids, "evidence": validated,
            "proposal_message_id": proposal_message_id,
            "temporal_scope": candidate.get("temporal_type", "durable"),
            "destination": destination, "reason": reason,
            "supersedes_memory_id": (
                candidate.get("supersedes_memory_id")
                if type(candidate.get("supersedes_memory_id")) is int else None
            ),
        },
        "admission_key": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
    }
    return MemoryAdmission(
        accepted=accepted, provenance="stated" if stated else "inferred",
        tier=(
            "long_term" if writer == "inline" and (stated or subject == "assistant")
            else "scratchpad"
        ),
        source_message_id=source_ids[-1] if source_ids else None, metadata=metadata,
    )
