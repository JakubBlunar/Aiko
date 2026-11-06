"""Exact, read-only memory inspection for the brain lane."""
from app.core.memory.recall_records import memory_record, owned


class MemoryReadsMixin:
    def inspect_memory(self, memory_id, *, content_offset=0, revision=None):
        store = getattr(self, "_memory_store", None)
        if store is None:
            raise RuntimeError("Memory storage is unavailable.")
        row = store.get(memory_id)
        user_id = getattr(self, "_user_id", None)
        if row is None or not owned(getattr(row, "source_session", None), user_id):
            return {"status": "not_found", "read_only": True}
        db = getattr(self, "_chat_db", None)
        item = memory_record(row, store=store, db=db, user_id=user_id)
        key = item["revision"]
        if (revision is not None and revision != key) or content_offset and not revision:
            raise ValueError("Memory changed or revision missing; restart at content_offset 0.")
        text = row.content or ""
        if not 0 <= content_offset <= len(text):
            raise ValueError("content_offset is outside this memory.")
        end = min(len(text), content_offset + 6000)
        item.update(text=text[content_offset:end], content_offset=content_offset,
                    text_complete=content_offset == 0 and end == len(text),
                    is_fragment=content_offset != 0 or end < len(text),
                    next_offset=end if end < len(text) else None, text_characters=len(text))
        originals = []
        if db is not None:
            for mid in item["source_message_ids"][:3]:
                source = db.get_message_row(mid)
                if source is None or not owned(source.session_id, user_id):
                    originals.append({"message_id": mid, "status": "unavailable"})
                    continue
                full = source.content or ""
                originals.append({"message_id": mid, "session_id": source.session_id,
                                  "role": source.role, "recorded_at": source.created_at,
                                  "text": full if len(full) <= 1600 else None,
                                  "text_complete": len(full) <= 1600,
                                  "inspection": {"tool": "conversation_history", "mode": "message",
                                                 "message_id": mid}})
        return {"status": "ok", "read_only": True, "memory": item,
                "original_messages": originals,
                "sources_omitted": max(0, len(item["source_message_ids"]) - len(originals)),
                "note": "This is a saved record, not proof of its interpretation. Missing event "
                        "time is unknown. Continue fragments before asserting full meaning. "
                        "Original assistant words are not user testimony."}
