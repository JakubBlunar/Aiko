"""User-entered answers retain their question without inventing a subject."""
from dataclasses import asdict

from app.core.memory.memory_origin import MemoryOrigin


def gap_revision(gap):
    from app.core.concepts.concept_support import revision_hash

    meta = gap.metadata or {}
    return revision_hash([gap.kind, gap.content, meta.get("question"), meta.get("resolved_at")])


def answer_payload(gap, answer, at):
    question = str((gap.metadata or {}).get("question") or gap.content)
    if len(answer) > 2000 or len(question) > 1000:
        raise ValueError("The question or answer is too long to save without truncating it.")
    return {
        "content": (f'Manual answer recorded at {at.isoformat()}. '
                    f'Question context: "{question}". User answer: "{answer}"'),
        "temporal_type": "past_event", "event_time": None, "provenance": "stated",
        "metadata": {
            "source": "manual_gap_answer", "resolved_gap_id": int(gap.id),
            "gap_answer": {"version": 1, "scope": "answer_receipt", "question": question,
                           "gap_revision": gap_revision(gap),
                           "answer": answer, "recorded_at": at.isoformat(),
                           "occurrence_time": None},
            # Knowing who supplied an answer does not identify its subject.
            # Manual testimony is also not independent external verification.
            "memory_origin": asdict(MemoryOrigin("unknown", "declaration", "manual_gap_answer")),
        },
    }
