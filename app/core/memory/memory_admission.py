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


def admit_source_note(candidate: dict, rows, *, session_key: str, writer: str,
                      proposal_message_id: int | None = None,
                      context_rows=()) -> tuple[dict, MemoryAdmission]:
    """Retain qualified testimony when a proposed paraphrase lost its condition.

    The rejected interpretation is discarded. This fallback is a bounded,
    attributed literal source, with the same owner and ordinary admission checks.
    """
    rows = list(rows)
    context_rows = tuple(context_rows)
    admission = admit_memory(candidate, rows, session_key=session_key, writer=writer,
                             proposal_message_id=proposal_message_id, context_rows=context_rows)
    if admission.metadata.get("claim_fidelity_refusal") != "lost_condition":
        return candidate, admission
    from app.core.infra import timephrase
    from app.core.memory.promise_evidence import source_clause

    offered = {row.id: row for row in rows if row.session_id == session_key}
    citations, clauses = [], []
    for item in candidate.get("evidence") or []:
        row = offered[item["message_id"]]
        clause = source_clause(item["quote"], row.content)
        # Restore original whitespace so the durable receipt remains literal.
        pattern = r"\s+".join(re.escape(word) for word in clause.split())
        match = re.search(pattern, row.content) if pattern else None
        if match is None or len(match[0]) > 1000:
            return candidate, admission
        if (row.id, match[0]) not in {(citation["message_id"], citation["quote"])
                                    for citation in citations}:
            clauses.append(match[0])
            citations.append({"message_id": row.id, "quote": match[0]})
    if not clauses:
        return candidate, admission
    prefix = "I said: " if candidate.get("kind") == "self" else "The user said: "
    text = prefix + " ".join(clauses)
    if len(text) > 2000 or timephrase.has_relative_deictic(text):
        return candidate, admission
    fallback = {
        "content": text, "kind": candidate.get("kind"),
        "salience": candidate.get("salience", .5),
        "temporal_type": candidate.get("temporal_type", "durable"),
        "provenance": "inferred", "event_time": None,
        "destination": "memory", "evidence": citations, "basis": "reported",
    }
    if candidate.get("observation") is not None:
        fallback["observation"] = candidate["observation"]
    supported = admit_memory(fallback, rows, session_key=session_key, writer=writer,
                             proposal_message_id=proposal_message_id, context_rows=context_rows)
    if supported.metadata.get("claim_fidelity_refusal") == "unsupported_observation":
        # Recheck the literal note on its own when only the proposed facets failed.
        del fallback["observation"]
        supported = admit_memory(fallback, rows, session_key=session_key, writer=writer,
                                 proposal_message_id=proposal_message_id, context_rows=context_rows)
    if supported.accepted:
        supported.metadata["source_note_fallback"] = "lost_condition"
        return fallback, supported
    return candidate, admission


def admit_memory(
    candidate: dict, rows, *, session_key: str, writer: str, proposal_message_id: int | None = None,
    context_rows=(),
) -> MemoryAdmission:
    content = str(candidate.get("content") or "").strip()
    if writer == "extractor":
        from app.core.infra import timephrase

        if timephrase.has_relative_deictic(content):
            return MemoryAdmission(False, "inferred", "scratchpad", None,
                                   {"claim_fidelity_refusal": "relative_time"})
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
    fidelity = None
    # Batch output must carry its own testimony, including tentative inferences.
    # Otherwise omitting evidence bypasses fidelity AND event-time resolution.
    # Inline self notes have a separate, deliberately provisional contract.
    if writer == "extractor" and accepted and not validated:
        return MemoryAdmission(False, "inferred", "scratchpad", None,
                               {"claim_fidelity_refusal": "missing_evidence"})
    if validated and accepted:
        from app.core.memory.claim_fidelity import refusal, event_support
        from app.core.memory.promise_evidence import source_clause

        basis = candidate.get("basis", "reported")
        clauses = [source_clause(item["quote"], source_rows[item["message_id"]].content)
                   for item in validated]
        refused = refusal(content, clauses, basis)
        if refused:
            return MemoryAdmission(False, "inferred", "scratchpad", None,
                                   {"claim_fidelity_refusal": refused})
        fidelity = {"version": 1, "basis": basis, **event_support(candidate, source_rows)}
        from app.core.memory.shared_shorthand import source_receipt

        citations = list(validated)
        if fidelity["precision"] != "unknown":
            citations.append({"message_id": fidelity["message_id"], "quote": fidelity["quote"]})
        fidelity["sources"] = [
            {**source_receipt(source_rows[item["message_id"]], item["quote"]),
             "source_at": str(getattr(source_rows[item["message_id"]], "created_at", "") or "")}
            for item in citations
        ]
    source_ids = sorted({item["message_id"] for item in validated})
    identity = f"{writer}:{session_key}:{proposal_message_id}:{source_ids}:{content}"
    metadata = {
        "admission": {
            "version": 1, "writer": writer, "subject": subject,
            "source_message_ids": source_ids, "evidence": validated,
            "input_message_ids": sorted(source_rows),
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
    if accepted and candidate.get("observation") is not None:
        from app.core.memory.observation_receipts import admit_observation

        try:
            observation = admit_observation(
                candidate["observation"], rows, session_key=session_key, subject=subject,
                context_rows=context_rows, evidence=validated, content=content,
            )
        except ValueError:
            return MemoryAdmission(False, "inferred", "scratchpad", None,
                                   {"claim_fidelity_refusal": "unsupported_observation"})
        if observation is not None:
            metadata["observation"] = observation
    if fidelity is not None:
        metadata["claim_fidelity"] = fidelity
    return MemoryAdmission(
        accepted=accepted, provenance="stated" if stated else "inferred",
        tier=(
            "long_term" if writer == "inline" and (stated or subject == "assistant")
            else "scratchpad"
        ),
        source_message_id=source_ids[-1] if source_ids else None, metadata=metadata,
    )
