"""Budgeted contextual definitions and user-requested understanding arcs.

Everything renders inside the existing T3 relevant_context block. Definitions
resolve references and strategies answer the current goal. Retrospective arcs
answer an explicit request: none of these is an optional stance offer.
"""
from __future__ import annotations

from app.core.session.context_budget_selector import ContextCandidate
from app.core.memory.shared_shorthand import fresh_activity, shorthand_hits
from app.core.memory.learned_strategies import fresh_goal_context, strategy_hits
from app.core.concepts.understanding_arcs import (
    render_arc, requested_subjects, retrospective_request,
)


def pattern_candidates(host, rag, *, user_text, session_key, estimate_tokens):
    memories, db = getattr(host, "_memory_store", None), getattr(host, "_chat_db", None)
    owner = getattr(host, "_user_id", "default")
    hits = shorthand_hits(memories, db, rag, user_text=user_text, session_key=session_key,
                          owner=owner, activity=fresh_activity(host, user_text))
    hits += strategy_hits(memories, db, rag, user_text=user_text, owner=owner,
                          goal_context=fresh_goal_context(host, user_text))
    return [ContextCandidate(source="memory", relevance=hit.score,
                             tokens=estimate_tokens(hit.text) + 12, order=-1,
                             payload=hit, key=f"pattern:{hit.record.id}") for hit in hits]


def prepare_arcs(host, candidates, user_text, estimate_tokens):
    provider = getattr(host, "_understanding_arc", None)
    if not callable(provider) or not retrospective_request(user_text):
        return {}
    subjects = requested_subjects(user_text)
    for candidate in sorted(candidates, key=lambda c: -c.relevance)[:6]:
        concept = candidate.payload
        if concept.subject not in subjects:
            continue
        packet = provider(concept)
        block = render_arc(packet)
        if block:
            candidate.tokens += estimate_tokens(block) + 12
            return {concept.concept_id: (block, packet)}
    return {}
