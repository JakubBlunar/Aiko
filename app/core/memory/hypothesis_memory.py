"""Answer receipts are testimony about an exchange, not a restated hypothesis."""
from dataclasses import asdict
import json

from app.core.infra import timephrase
from app.core.memory.memory_origin import MemoryOrigin


def answer_context(memory):
    receipt = (getattr(memory, "metadata", None) or {}).get("hypothesis_answer")
    if not isinstance(receipt, dict):
        return ""
    question = receipt.get("question") or "no recorded question"
    return ("Target-specific reply receipt, not an independent general claim. "
            + "Question context: " + json.dumps(question, ensure_ascii=False)
            + "; verdict=" + str(receipt.get("verdict", "unknown")))


def answer_payload(belief, body, *, evidence, question, verdict, subject, target_id,
                   target_type, event, db=None):
    quote = body if evidence is None else evidence.strip()
    if (not quote or quote not in body or len(quote) > 1000
            or len(question) > 1000 or verdict not in {"confirm", "correct", "deny"}):
        return None
    source = None
    mid = event.get("user_message_id")
    if mid is not None and db is not None:
        from app.core.memory.shared_shorthand import source_receipt

        row = db.get_message_row(mid)
        if (row is None or row.role != "user" or quote not in row.content
                or row.session_id != event.get("session_id")):
            return None
        source = {**source_receipt(row, quote), "source_at": row.created_at}
    recorded_at = (source or {}).get("source_at") or event.get("observed_at")
    at = timephrase.parse_iso(recorded_at)
    stamp = at.isoformat() if at else "an unknown recording time"
    subject = subject if subject in {"user", "aiko", "relationship", "world"} else "unknown"
    receipt = {
        "version": 1, "scope": "answer_receipt", "speaker": "user",
        "mode": "direct" if question else "observation", "question": question,
        "proposed_statement": belief, "evidence": quote, "verdict": verdict,
        "target_subject": subject, "target_id": target_id, "target_type": target_type,
        "recorded_at": recorded_at, "occurrence_time": None,
        "sources": [source] if source else [],
    }
    # The quote remains literal and its relative language is explicitly scoped
    # to the recording date. The proposed belief never enters indexed content.
    return {
        "content": f'User reply recorded at {stamp} ({verdict}): "{quote}"',
        "temporal_type": "past_event", "event_time": None, "provenance": "stated",
        "metadata": {
            "answer_event": dict(event), "hypothesis_answer": receipt,
            "memory_origin": asdict(MemoryOrigin(subject, "declaration", "hypothesis_answer")),
        },
    }


def world_support(row, store):
    from app.core.memory.reflective_evidence import eligible, revision, current

    inputs = []
    for mid in dict.fromkeys(row.answer_memory_ids):
        answer = store.get(mid)
        if answer is None or not eligible(answer):
            return None
        receipt = (getattr(answer, "metadata", None) or {}).get("hypothesis_answer") or {}
        if (receipt.get("verdict") != "confirm" or receipt.get("target_subject") != "world"
                or receipt.get("target_id") != row.hypothesis_id):
            return None
        inputs.append({"memory_id": mid, "revision": revision(answer)})
    if not inputs:
        return None
    packet = {"version": 1, "scope": "hypothesis_support",
              "hypothesis_id": row.hypothesis_id, "memory_inputs": inputs}
    return packet if current(packet, store) else None
