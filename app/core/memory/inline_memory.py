"""Deliberate brain notes with optional literal, owner-bound conversation support."""
from __future__ import annotations

import json
import logging

from app.core.memory.memory_admission import admit_source_note
from app.core.infra import timephrase
from app.core.memory.memory_store import VALID_TEMPORAL_TYPES
from app.core.memory.observation_receipts import receipt_sources_current
from app.core.services.memory_tags import REMEMBER_TAG_RE

log = logging.getLogger("app.inline_memory")


def memory_awareness(enabled: bool, user_name: str = "the user") -> str:
    if not enabled:
        return ""
    user = user_name or "the user"
    example = json.dumps({"content": f"{user} prefers clear explanations when learning a new tool.",
                          "quote": "Clear explanations help me when learning a new tool."})
    return (
        "Your private memory notebook is available during this reply. Deliberately keep "
        "something that should change a later conversation: an explicit remember request, "
        "correction, important personal fact/project, communication need, boundary, agreed "
        "signal, or your own chosen recurring approach. Do not wait for the user to ask you "
        "to remember an important agreement. No note is needed for ordinary banter or "
        "one-off gestures. At most two notes per reply.\n"
        "For a user memory, write a short third-person note in an invisible tag: "
        '[[remember:{"content":"...","quote":"exact words from the user"}]]. '
        "Quote literally from the current or previous user turn; do not quote yourself "
        "as evidence of their preference. Example, only after the user actually says that: "
        f"[[remember:{example}]].\n"
        "For your own chosen stance or interaction practice, use "
        '[[remember:self:{"content":"I ...","quote":"your exact words in this reply"}]]. '
        "You may quote either of your preceding two replies instead. Preserve the trigger, purpose "
        "and exceptions, including an explicit if/when clause for a conditional preference. "
        "Prefer the substantive statement over a terse later agreement. "
        "If a playful cue helps someone recognize joking, keep their need "
        "and your chosen response as separate notes; a proposed refinement does not replace "
        "their request. A decision to try is an intention, not proof that you already did it. "
        'Optional temporal_type is durable|preference|ongoing|past_event|future_plan. '
        "Plain legacy "
        "remember tags still work for provisional notes, and plain self tags for a new "
        "personal decision. Never claim a user agreement, successful outcome or verified "
        "fact merely because you wrote a note. Keep the conversation natural; tags are private.\n"
        "For stored note content only (copy evidence quotes verbatim): "
        + timephrase.STORED_TEXT_TIME_RULE
    )


def _candidate(body: str, kind: str, rows) -> dict:
    if not body.startswith("{"):
        return {"content": body, "kind": kind, "temporal_type": "durable"}
    try:
        value = json.loads(body)
    except (ValueError, TypeError):
        raise ValueError("invalid_note_json") from None
    if not isinstance(value, dict) or set(value) - {"content", "quote", "temporal_type"}:
        raise ValueError("invalid_note_fields")
    content, quote = value.get("content"), value.get("quote")
    temporal = value.get("temporal_type", "durable")
    if (not isinstance(content, str) or not 4 <= len(content.strip()) <= 2000
            or not isinstance(quote, str) or not 3 <= len(quote.strip()) <= 1000
            or not isinstance(temporal, str) or temporal not in VALID_TEMPORAL_TYPES):
        raise ValueError("invalid_note_fields")
    if timephrase.has_relative_deictic(content) or "[[" in content or "]]" in content:
        raise ValueError("unstable_note_text")
    role = "assistant" if kind == "self" else "user"
    source = next((row for row in reversed(rows)
                   if row.role == role and quote in row.content), None)
    if source is None:
        raise ValueError("quote_not_in_owner_turn")
    return {"content": content.strip(), "kind": kind, "temporal_type": temporal,
            "evidence": [{"message_id": source.id, "quote": quote}]}


def capture_inline_memories(raw_text, *, db, store, embedder, session_key,
                            assistant_message_id, salience, notify=None):
    if store is None or embedder is None or not raw_text or not assistant_message_id:
        return
    try:
        rows = list(db.get_messages_before(session_key, before_id=assistant_message_id, limit=4))
        current = db.get_message_row(assistant_message_id)
        if (current is None or current.session_id != session_key
                or current.role != "assistant"):
            return
        rows.append(current)
    except Exception:
        log.debug("inline memory source read failed", exc_info=True)
        return
    seen = set()
    for match in REMEMBER_TAG_RE.finditer(raw_text):
        body = match["body"].strip()
        kind = "self" if (match["kind"] or "").lower() == "self" else "self_tagged"
        try:
            candidate = _candidate(body, kind, rows)
        except ValueError as exc:
            log.info("inline memory refused: reason=%s", exc)
            continue
        if len(candidate["content"]) < 4:
            continue
        key = (kind, candidate["content"].casefold())
        if key in seen:
            continue
        candidate, admission = admit_source_note(
            candidate, rows, session_key=session_key, writer="inline",
            proposal_message_id=assistant_message_id,
        )
        if not admission.accepted:
            log.info("inline memory refused: reason=%s",
                     admission.metadata.get("claim_fidelity_refusal", "invalid_evidence"))
            continue
        seen.add(key)
        if len(seen) > 2:
            break
        try:
            embedding = embedder.embed(candidate["content"])
            if not receipt_sources_current(admission.metadata.get("claim_fidelity"), store):
                log.info("inline memory refused: reason=source_changed")
                continue
            memory = store.add(
                content=candidate["content"], kind=kind, embedding=embedding, salience=salience,
                source_session=session_key, source_message_id=admission.source_message_id,
                metadata=admission.metadata, tier=admission.tier,
                confidence=0.85 if admission.provenance == "stated" or kind == "self" else 0.5,
                temporal_type=candidate["temporal_type"], provenance=admission.provenance,
            )
        except Exception:
            log.debug("inline memory write failed", exc_info=True)
            continue
        if memory is not None:
            log.info("inline memory saved: kind=%s memory_id=%s cited=%s fallback=%s",
                     kind, getattr(memory, "id", None),
                     bool(admission.metadata.get("claim_fidelity")),
                     admission.metadata.get("source_note_fallback"))
        if memory is not None and notify is not None:
            try:
                notify(memory)
            except Exception:
                log.debug("inline memory notification failed", exc_info=True)
