"""Scoped understanding reads, with no new inference or confidence writes."""
import json

from app.core.infra.read_paging import json_page


class UnderstandingReadsMixin:
    def recall_beliefs(self, query, *, kind=None, limit=5):
        from app.core.relationship.belief_interpretation import is_interpretation

        store = getattr(self, "_belief_store", None)
        db = getattr(self, "_chat_db", None)
        if store is None or db is None:
            raise RuntimeError("Current interpretation storage is unavailable.")
        rows = store.list_context(user_id=self._user_id, session_key=self.session_key,
                                  user_text=query, limit=200)
        output, withheld, omitted = [], 0, 0
        budget = 16000
        for row in rows:
            if not is_interpretation(row.metadata) or kind and row.kind != kind:
                continue
            meta = row.metadata
            evidence = meta.get("evidence") or []
            sources = []
            for citation in evidence:
                original = db.get_message_row(citation.get("message_id"))
                if (original is None or original.role != "user"
                        or original.session_id != self.session_key
                        or (citation.get("created_at")
                            and citation["created_at"] != original.created_at)
                        or not citation.get("quote") or citation["quote"] not in original.content):
                    break
                sources.append({**citation, "source_check": "quoted_span_present",
                                "full_revision_tracked": False,
                                "original_text": original.content if len(original.content) <= 1600
                                                 else None,
                                "original_text_complete": len(original.content) <= 1600})
            else:
                if evidence and len(output) < limit:
                    item = {"belief_id": row.id, "kind": row.kind, "topic": row.topic,
                            "interpretation": row.predicted_state, "status": row.status,
                            "confidence": row.confidence, "observed_at": row.observed_at,
                            **{key: meta.get(key) for key in (
                                "reason", "situation", "uncertainty", "revise_when", "scope",
                                "expires_at", "last_supported_at",
                            )}, "evidence": sources}
                    size = len(json.dumps(item, default=str))
                    if size <= budget:
                        output.append(item)
                        budget -= size
                    else:
                        omitted += 1
                continue
            withheld += 1
        return {"status": "ok" if output else "no_match", "read_only": True,
                "beliefs": output, "withheld": withheld, "omitted": omitted,
                "coverage": "current relevant readings from a bounded 200-row active scan",
                "note": "Tentative situation-specific interpretations, not settled concepts or "
                        "proof of the user's state. Confidence is Aiko's uncertainty. A quoted "
                        "span can remain after an edit; full-message revision is untracked. "
                        "Inspect original messages with conversation_history before relying on "
                        "missing context. Empty results do not prove there are no other readings."}

    def _history_concept(self, concept_id):
        store = getattr(self, "_concept_store", None)
        learning = getattr(self, "_concept_learning_store", None)
        if store is None or learning is None:
            raise RuntimeError("Understanding history is unavailable.")
        canonical = learning.resolve_alias(concept_id)
        row = store.get(canonical)
        return row if row is not None and getattr(row, "user_id", None) in (
            None, self._user_id,
        ) else None

    def understanding_history(self, *, mode="timeline", subject=None, query="", concept_id=None,
                              eras=6, limit=5, before_id=None, entry_id=None, event_id=None,
                              content_offset=0, revision=None):
        learning = getattr(self, "_concept_learning_store", None)
        store = getattr(self, "_concept_store", None)
        if learning is None or store is None:
            raise RuntimeError("Understanding history is unavailable.")
        if mode not in {"timeline", "diary", "diary_entry", "learning_event"}:
            raise ValueError("Unknown understanding history mode.")
        if mode == "timeline":
            from app.core.concepts.self_history import build_self_history
            canonical = (learning.resolve_alias(concept_id) if concept_id is not None else None)
            arc = build_self_history(concept_store=store, learning_store=learning,
                                     subject=subject or "aiko", query=query, concept_id=canonical,
                                     user_id=self._user_id, strict=True, max_eras=eras)
            payload = arc.as_dict()
            remaining, omitted = 16000, 0
            for era in payload["eras"]:
                kept = []
                for item in era["entries"]:
                    size = len(json.dumps(item))
                    if size <= remaining:
                        kept.append(item)
                        remaining -= size
                    else:
                        omitted += 1
                era["entries"] = kept
            return {"status": "ok", "enabled": True, "read_only": True, **payload,
                    "omitted": omitted, "coverage": "bounded recorded understanding history",
                    "note": "A change in Aiko's understanding does not prove a change in the "
                            "user or relationship. Thin/omitted history is incomplete."}
        if mode == "learning_event":
            event = learning.get(event_id)
            ids = [i for i in (event.concept_id, event.prior_concept_id) if i] if event else []
            if (event is None or subject and event.subject != subject or not ids
                    or any(self._history_concept(i) is None for i in ids)):
                return {"status": "not_found", "read_only": True}
            return {"status": "ok", "record_type": "learning_event", "read_only": True,
                    **json_page(event.as_dict(), content_offset=content_offset, revision=revision)}
        diary = getattr(self, "_evolution_diary_store", None)
        if diary is None:
            raise RuntimeError("Evolution diary storage is unavailable.")

        def matches(entry):
            concepts = [self._history_concept(cid) for cid in entry.concept_ids]
            return (bool(concepts) and all(c is not None for c in concepts)
                    and (subject is None or any(c.subject == subject for c in concepts))
                    and (concept_id is None or any(
                        c.concept_id == learning.resolve_alias(concept_id) for c in concepts))
                    and (not query or query.casefold() in entry.entry.casefold()))

        if mode == "diary_entry":
            entry = diary.get(entry_id)
            if entry is None or not matches(entry):
                return {"status": "not_found", "read_only": True}
            return {"status": "ok", "record_type": "evolution_diary", "read_only": True,
                    **json_page(entry.as_dict(), content_offset=content_offset, revision=revision)}
        rows = diary.list(limit=100, before_id=before_id, strict=True)
        selected = [row for row in rows if matches(row)][:limit]
        summaries = [{"entry_id": row.entry_id, "period_start": row.period_start,
                      "period_end": row.period_end, "recorded_at": row.created_at,
                      "concept_ids": list(row.concept_ids),
                      "learning_event_ids": list(row.learning_event_ids)} for row in selected]
        cursor = (selected[-1].entry_id if len(selected) == limit else rows[-1].entry_id
                  if len(rows) == 100 else None)
        return {"status": "ok" if summaries else "no_match", "read_only": True,
                "entries": summaries, "next_before_id": cursor, "scanned": len(rows),
                "coverage": "bounded evolution diary; open entry_id for original paragraph",
                "note": "Recorded understanding change, separate from the subjective diary."}
