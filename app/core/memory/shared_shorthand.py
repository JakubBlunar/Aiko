"""K106: explicitly negotiated meanings, stored through MemoryStore.

These are contextual definitions, never prompts to bring a subject up. Every
read rechecks original message revisions; generic RAG must not bypass scope.
"""
from __future__ import annotations

import logging
import re
from dataclasses import replace

from app.core.concepts.concept_support import revision_hash
from app.core.infra import timephrase

log = logging.getLogger("app.conversation_patterns")
PATTERN_KINDS = frozenset({"shared_term", "strategy"})
EXTRACTION_RULE = """
K106 optional shared definitions: only after explicit USER adoption, a memory
may have kind 'shared_term' and a 'shorthand' object:
The kind catalogue additionally permits 'shared_term' only with this structure.
{term: str, meaning: str, definition: {message_id: int, quote: str},
 adoption: {message_id: int, quote: str}, scope: 'session'|'relationship'|str,
 expires_on: 'YYYY-MM-DD' or null, scope_quote: str or ''}.
Copy meaning literally from the definition, and term from definition or adoption. Adoption must
be the user's explicit naming statement or direct agreement to the immediately
preceding assistant definition. A suggestion, question, joke or silence is not
adoption. Default to session scope. A relationship-wide meaning needs the user's
explicit 'always' or 'from now on'; an activity scope must appear literally in
the user's scope_quote. Omit dates without an exact stated ISO expiry date.
For correction, record the new adopted meaning. For explicit retirement, set
meaning to '' and cite the user's 'stop calling' / 'don't call' statement as
definition and adoption. Do not convert a declined name into a definition.
Set optional revises_memory_id to a listed shared-term memory when correcting
or retiring it; its established scope is inherited without broadening it.
These entries share the existing memory count/output budget; emit none when
the exchange does not establish a useful shared meaning.
"""
_NONLITERAL = re.compile(r"\b(?:role.?play|hypothetical|pretend|imagine|fictional)\b", re.I)
_ADOPT = re.compile(r"\b(?:let'?s (?:always )?call|we(?:'ll| will) call|call (?:it|this|that)|"
                    r"by .{1,60} (?:I|we) mean|means?|rename)\b", re.I)
_NO = re.compile(r"\b(?:no|not|never|don'?t|do not|stop)\b", re.I)
_RETIRE = re.compile(r"\b(?:stop calling|don'?t call|do not call)\b", re.I)
_YES = re.compile(r"^(?:yes|yeah|yep|agreed|exactly|deal|sounds good|let'?s use that)\b", re.I)


def source_receipt(row, quote):
    return {"message_id": row.id, "session_id": row.session_id, "role": row.role,
            "quote": quote, "revision": revision_hash([row.role, row.content, row.session_id])}


def sources_current(metadata, db):
    sources = metadata.get("sources", [])
    if not sources or db is None:
        return False
    for source in sources:
        row = db.get_message_row(source["message_id"])
        if row is None or source["revision"] != revision_hash(
            [row.role, row.content, row.session_id],
        ):
            return False
    return True


def admit_shorthand(candidate, rows, context_rows=(), *, session_key, owner,
                     memories=None, db=None):
    value = candidate.get("shorthand")
    if not isinstance(value, dict):
        return None
    offered = {row.id: row for row in [*context_rows, *rows] if row.session_id == session_key}

    def citation(field):
        item = value.get(field, {})
        if not isinstance(item, dict) or type(item.get("message_id")) is not int:
            raise ValueError("citation")
        row = offered.get(item["message_id"])
        quote = item.get("quote")
        if (row is None or not isinstance(quote, str) or not 3 <= len(quote) <= 500
                or quote not in row.content or _NONLITERAL.search(row.content)):
            raise ValueError("quote")
        return row, quote

    try:
        definition, quote = citation("definition")
        adopter, agreement = citation("adoption")
        term, meaning = value.get("term"), value.get("meaning")
        if (not isinstance(term, str) or not 2 <= len(term) <= 60
                or not isinstance(meaning, str) or len(meaning) > 180
                or adopter.role != "user" or adopter.id not in {row.id for row in rows}
                or term.casefold() not in (quote + " " + agreement).casefold() or "?" in agreement):
            return None
        retired = not meaning and bool(_RETIRE.search(agreement))
        if not retired:
            if not meaning or meaning not in quote or _NO.search(agreement):
                return None
            if definition.role == "assistant":
                previous = max((i for i in offered if i < adopter.id), default=0)
                explicit_name = term.casefold() in agreement.casefold() and _ADOPT.search(agreement)
                if previous != definition.id or not (_YES.search(agreement) or explicit_name):
                    return None
            elif definition.id != adopter.id or not _ADOPT.search(agreement):
                return None
        scope = value.get("scope", "session")
        scope_quote = value.get("scope_quote", "")
        prior_scope = None
        revises = value.get("revises_memory_id")
        if revises is not None:
            prior = memories.get(revises) if memories is not None and type(revises) is int else None
            prior_scope = (prior.metadata or {}).get("shared_shorthand", {}) if prior else {}
            if (not prior_scope or prior_scope.get("owner") != owner
                    or prior_scope.get("term", "").casefold() != term.casefold()
                    or not sources_current(prior_scope, db)):
                return None
            scope = prior_scope["scope"]
        if scope != "session" and prior_scope is None:
            if (not isinstance(scope, str) or not 2 <= len(scope) <= 60
                    or not isinstance(scope_quote, str) or not scope_quote
                    or scope_quote not in adopter.content):
                return None
            if scope == "relationship":
                if not re.search(r"\b(?:always|from now on)\b", scope_quote, re.I):
                    return None
            elif not re.search(r"\b" + re.escape(scope) + r"\b", scope_quote, re.I):
                return None
        expiry = value.get("expires_on")
        if expiry is not None:
            from datetime import date

            expiry = date.fromisoformat(expiry).isoformat()
            if not re.search(r"\b(?:until|before) " + re.escape(expiry), adopter.content, re.I):
                return None
        elif prior_scope is not None:
            expiry = prior_scope.get("expires_on")
        if timephrase.has_relative_deictic(term + " " + meaning):
            return None
        metadata = {"version": 1, "owner": owner, "term": term, "meaning": meaning,
                    "scope": scope, "expires_on": expiry, "retired": retired,
                    "revises_memory_id": revises,
                    "sources": [source_receipt(definition, quote),
                                source_receipt(adopter, agreement)]}
        content = (f"Shared term {term!r}: {meaning}" if meaning
                   else f"Retired shared term {term!r}.")
        return {**candidate, "content": content, "kind": "shared_term", "provenance": "stated",
                "source_message_id": adopter.id, "temporal_type": "durable",
                "metadata": {"shared_shorthand": metadata,
                             "source_message_ids": sorted({definition.id, adopter.id}),
                             "admission_key": revision_hash(["shared_term", owner, metadata])}}
    except (ValueError, TypeError, AttributeError):
        return None


def shorthand_hits(memories, db, rag, *, user_text, session_key, owner, activity=""):
    if memories is None or db is None or rag is None:
        return []
    rows = memories.list_recent(limit=256, kind="shared_term")
    # Order by actual adoption, not pinned display order or extraction time.
    rows.sort(key=lambda m: int(m.source_message_id or 0), reverse=True)
    seen = set()
    preferred = {}
    hits = []
    for row in rows:
        value = (row.metadata or {}).get("shared_shorthand", {})
        term = value.get("term", "")
        scope = value.get("scope", "session")
        if (value.get("owner") != owner or not term
                or (scope == "session" and row.source_session != session_key)):
            continue
        if scope not in {"session", "relationship"} and not re.search(
            r"\b" + re.escape(scope) + r"\b", activity + " " + user_text, re.I,
        ):
            continue
        key = (term.casefold(), scope)
        if key in seen:
            continue
        seen.add(key)
        priority = (2 if scope == "session" else 0 if scope == "relationship" else 1)
        preferred[term.casefold()] = max(preferred.get(term.casefold(), -1), priority)
        if (not sources_current(value, db) or value.get("retired")
                or (value.get("expires_on") or "9999-12-31")
                <= timephrase.now().date().isoformat()):
            continue
        if not re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", user_text, re.I):
            continue
        hit = rag._memory_to_hit(row, score=1.0)
        text = (f"Agreed meaning ({scope}; memory:{row.id}): {term!r} means "
                f"{value['meaning']!r}. Resolve naturally; no need to retell its origin.")
        hit.record = replace(hit.record, content=text)
        hit.memory_provenance = "validated_pattern"
        hits.append(hit)
        if len(hits) >= 16:
            break
    # A local definition wins over a relationship-wide one; equal-scope
    # ambiguity stays visible as no usable meaning instead of two assertions.
    by_term = {}
    for hit in hits:
        value = memories.get(int(hit.record.id)).metadata["shared_shorthand"]
        priority = (2 if value["scope"] == "session" else
                    0 if value["scope"] == "relationship" else 1)
        term = value["term"].casefold()
        old = by_term.get(term)
        if old is None or priority > old[0]:
            by_term[term] = (priority, hit)
        elif priority == old[0]:
            by_term[term] = (priority, None)
    return [item[1] for term, item in by_term.items()
            if item[1] is not None and item[0] == preferred[term]][:2]


def fresh_activity(host, user_text):
    reader = getattr(host, "conversation_situation_snapshot", None)
    if not callable(reader):
        return ""
    snapshot = reader(user_text=user_text)
    state = getattr(snapshot, "inferred", None)
    if (state is None or getattr(snapshot, "inferred_stale", True)
            or not getattr(snapshot, "world_compatible", False)
            or getattr(state, "status", "") != "active"):
        return ""
    return str(getattr(state, "shared_activity", "") or "")
