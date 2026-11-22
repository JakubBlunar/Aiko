"""Select scoped user requests once across the ordinary chat concept lanes."""
from __future__ import annotations

from copy import copy
import re

from app.core.concepts.concept_applicability import (
    applicability_state, current_contexts, reported_preference_current, scope_key,
    scope_sources_valid,
)
from app.core.memory.echo_detector import tokens
from app.core.memory.recall_records import owned
from app.core.memory.reflective_evidence import eligible


_ACTIVITY = re.compile(
    r"\b(?:during|while|when)\s+([\w -]{2,65}?)(?=[,.;!?]|\s+so (?:I|he|she|they)\b|$)", re.I,
)
_RECALL = re.compile(r"\b(?:what did I ask|what was my preference|how did I ask)\b", re.I)


def _literal_scope(text):
    found = _ACTIVITY.search(text)
    if (found is None or re.search(r"\b(?:except|unless|until|never|outside)\b", text, re.I)
            or len(_ACTIVITY.findall(text)) != 1):
        return None
    activity = found[1].strip().lower()
    if len(activity.split()) > 6 or not re.fullmatch("[a-z][a-z -]{1,59}", activity):
        return None
    return {"version": 1, "contexts": [{"name": activity}]}


class PreferenceSelection:
    def __init__(self, store, memories, db, owner, text, situation):
        self.store = store
        self.memories = memories
        self.db = db
        self.owner = owner
        self.applicability = getattr(store, "applicability", None)
        self.text = text
        self.situation = situation
        self.keys = {}
        self.projections = {}

    def _prepare(self, concepts):
        candidates = {c.concept_id: c for c in concepts if getattr(c, "subject", "") == "user"
                      and getattr(c, "kind", "") in ("boundary", "communication_style")
                      and c.concept_id not in self.projections}
        if (not candidates or self.memories is None
                or not callable(getattr(self.store, "evidence_for", None))):
            return
        edges = self.store.evidence_for(candidates)
        memory_ids = {int(e.src_id) for group in edges.values() for e in group
                      if e.src_type == "memory" and e.polarity > 0}
        if not memory_ids:
            return
        rows = self.memories.get_many(memory_ids)
        for cid, concept in candidates.items():
            self.projections[cid] = concept
            scope = self.applicability.get(cid) if self.applicability is not None else None
            sources = {}
            for edge in edges.get(cid, ()):
                if edge.src_type != "memory" or edge.polarity <= 0 or self.memories is None:
                    continue
                row = rows.get(int(edge.src_id))
                if row is None or getattr(row, "kind", "") != "preference":
                    continue
                if scope and not any(c.get("memory_id") == row.id for c in scope["contexts"]):
                    continue
                # A sole source is also the limit of what this concept can claim.
                # For mixed evidence require a topical link to this particular note.
                if (not scope and len(edges[cid]) > 1
                        and len(tokens(row.content) & tokens(concept.label)) < 2):
                    continue
                if not scope and not _ACTIVITY.search(row.content):
                    continue
                if not eligible(row) or not owned(row.source_session, self.owner):
                    self.projections[cid] = None
                    continue
                receipt = (row.metadata or {}).get("claim_fidelity")
                if receipt and self.db and reported_preference_current(
                    row, self.memories, self.db, self.owner,
                ):
                    quotes = [s["quote"] for s in receipt["sources"]]
                    source_text = " ".join(dict.fromkeys(quotes))
                elif not receipt:
                    # Legacy notes can scope their own earlier account. Keep it
                    # attributed to the note, even when its pointer is an assistant reply.
                    source_text = row.content
                else:
                    self.projections[cid] = None
                    continue
                explicit = scope or _literal_scope(source_text)
                self.projections[cid] = None
                if explicit:
                    sources[(row.source_message_id or -row.id, source_text)] = explicit
            if scope and (not self.memories or not scope_sources_valid(scope, self.memories)):
                self.projections[cid] = None
                continue
            matching = []
            for source, explicit in sources.items():
                quote = source[1]
                state = applicability_state(explicit, current_contexts(
                    self.text, self.situation, scope=explicit,
                ))
                recalling = (_RECALL.search(self.text) and len(tokens(self.text) & tokens(quote))
                             >= 2 and state in {"unknown_context", "other_context"})
                if state == "matched" or recalling:
                    matching.append((source, explicit))
            if len(matching) == 1:
                (source, explicit), = matching
                projected = copy(concept)
                projected.label = source[1]
                projected.rationale = ""
                self.projections[cid] = projected
                self.keys[cid] = (source, scope_key(explicit))
            elif scope and applicability_state(scope, current_contexts(
                self.text, self.situation, scope=scope,
            )) != "matched":
                self.projections[cid] = None

    def select(self, pairs, excluded=()):
        pairs = list(pairs)
        self._prepare(c for c, _ in pairs)
        seen = {self.keys[cid] for cid in excluded if cid in self.keys}
        chosen = []
        for concept, score in pairs:
            cid = getattr(concept, "concept_id", None)
            projected = self.projections.get(cid, concept)
            key = self.keys.get(cid)
            if projected is None or (key is not None and key in seen):
                continue
            if key is not None:
                seen.add(key)
            chosen.append((projected, score))
        return chosen
