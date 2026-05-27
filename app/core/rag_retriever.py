"""Multi-source retrieval over the LanceDB :class:`RagStore`.

Supersedes :class:`app.core.memory_retriever.MemoryRetriever`. Searches three
tables in parallel and merges the hits with source-aware scoring:

  - ``memories`` (durable cross-session facts; high prior weight)
  - ``messages`` (chat history embeddings; recency-aware)
  - ``documents`` (uploaded notes / PDFs)

The output is a structured ``list[RagHit]`` *plus* a ready-to-paste prompt
block. The prompt block intentionally splits "What you know about Jacob"
(memories) from "Snippets you remembered" (messages) and "From your notes"
(documents), so the LLM can use them with appropriate confidence.

Design notes:
  - Memory hits get a small salience boost inside :class:`RagStore`; we
    additionally bias message hits down so a strong memory always wins ties.
  - We dedupe by content-text after merging.
  - Recency is folded into message scores via an exponential decay on
    ``created_at``; older messages are penalized so RAG doesn't unearth a
    five-month-old line on every turn.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from app.core.rag_store import RagHit

if TYPE_CHECKING:
    from app.core.memory_store import MemoryStore
    from app.core.rag_store import RagStore
    from app.llm.embedder import Embedder


log = logging.getLogger("app.rag_retriever")


# Tuning knobs. Kept small so we can iterate without breaking callers.
_MEMORY_PRIOR = 0.05
_MESSAGE_PRIOR = -0.04
_DOCUMENT_PRIOR = 0.0
# Half-life in days for message recency decay; messages older than ~3 weeks
# get heavily penalized in the merged score.
_MESSAGE_HALFLIFE_DAYS = 21.0

# Memory recency / revival tuning. The plain cosine + salience scoring is
# stateless across turns, so a single high-salience callback would surface
# every turn until the user manually deletes it. These two adjustments fix
# that without weakening relevance:
#
#   * If a memory was last surfaced within ``_MEMORY_RECENCY_PENALTY_HOURS``
#     we subtract ``_MEMORY_RECENCY_PENALTY`` from its score so the
#     second-best candidate gets a real chance to win.
#   * If a memory was used at least once but hasn't been touched in
#     ``_MEMORY_REVIVAL_DAYS`` days, we add ``_MEMORY_REVIVAL_BONUS`` so
#     stale threads can re-emerge with an "oh, whatever happened with…"
#     framing instead of being lost forever under fresher hits.
#
# The deltas are intentionally smaller than the typical cosine gap between
# top results (~0.05-0.10) so the recency signal nudges ordering without
# overpowering relevance. ``_MEMORY_RECENCY_PENALTY`` is roughly twice the
# revival bonus because suppressing a stale repeat is more valuable than
# resurrecting a dormant one.
_MEMORY_RECENCY_PENALTY_HOURS = 6.0
_MEMORY_RECENCY_PENALTY = 0.08
_MEMORY_REVIVAL_DAYS = 7.0
_MEMORY_REVIVAL_BONUS = 0.04
# Bonus for memories the user has explicitly pinned via the Memory tab.
# Pinning is a curation signal ("I want this surfaced when relevant"); a
# small additive nudge gives pinned hits the edge over equally-similar
# unpinned siblings without overpowering raw cosine relevance.
_MEMORY_PINNED_BONUS = 0.05
# Bonus for ``shared_moment`` memories whose ``metadata.when`` matches an
# anniversary window today (1mo / 3mo / 6mo / 1yr / Nyr). The size is the
# same as the pinned bonus so a moment having its anniversary also gets
# a small leg-up against an equally-similar non-anniversary sibling.
_MEMORY_ANNIVERSARY_BONUS = 0.05
_ANNIVERSARY_WINDOW_DAYS: tuple[int, ...] = (30, 90, 180, 365, 730, 1095, 1460, 1825)
_ANNIVERSARY_TOLERANCE_DAYS = 1.0

# Schema v8 — per-tier score offset applied to memory hits.
# ``scratchpad`` rows are still probationary and pre-revival, so we
# bias them slightly down; ``archive`` rows are cold history, so they
# need a strong cosine match to surface (avoids unburying noise on
# weak queries). ``long_term`` is the neutral baseline. The deltas are
# small enough that a revived scratchpad row (revival_score > 0.5) or
# a high-salience archive row still wins against an equally-similar
# long_term sibling.
_MEMORY_TIER_OFFSET: dict[str, float] = {
    "scratchpad": -0.02,
    "long_term": 0.0,
    "archive": -0.03,
}

# Schema v9 — confidence-tier penalty for low-confidence memory hits.
# Memories with ``confidence < 0.5`` get a proportional score penalty
# capped at ``-0.15`` (when confidence == 0). Never hides — just demotes,
# so a strong cosine match on an uncertain memory still surfaces but with
# a small handicap against a high-confidence sibling. Per the F3 backlog
# spec: "never hiding things from Aiko is the simpler invariant".
_MEMORY_CONFIDENCE_PENALTY_THRESHOLD = 0.5
_MEMORY_CONFIDENCE_PENALTY_MAX = 0.15


def _confidence_penalty(confidence: float | None) -> float:
    """Return a non-positive penalty for low-confidence memory hits.

    ``confidence >= 0.5`` -> 0.0 (no nudge).
    ``confidence == 0.0`` -> ``-_MEMORY_CONFIDENCE_PENALTY_MAX``.
    Linear ramp in between.
    """
    if confidence is None:
        return 0.0
    try:
        value = float(confidence)
    except (TypeError, ValueError):
        return 0.0
    if value >= _MEMORY_CONFIDENCE_PENALTY_THRESHOLD:
        return 0.0
    gap = _MEMORY_CONFIDENCE_PENALTY_THRESHOLD - max(0.0, value)
    return -(gap / _MEMORY_CONFIDENCE_PENALTY_THRESHOLD) * _MEMORY_CONFIDENCE_PENALTY_MAX


def _is_anniversary_today(metadata: dict | None) -> bool:
    """True if ``metadata.when`` falls inside an anniversary window today.

    Safe to call with arbitrary dicts and on rows whose ``when`` is
    missing or malformed; returns ``False`` in those cases.
    """
    if not metadata or not isinstance(metadata, dict):
        return False
    when_raw = metadata.get("when")
    if not when_raw:
        return False
    try:
        when = datetime.fromisoformat(str(when_raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = (datetime.now(timezone.utc) - when).total_seconds() / 86400.0
    if delta <= 0:
        return False
    for window in _ANNIVERSARY_WINDOW_DAYS:
        if abs(delta - window) <= _ANNIVERSARY_TOLERANCE_DAYS:
            return True
    return False


class RagRetriever:
    def __init__(
        self,
        store: "RagStore",
        embedder: "Embedder",
        *,
        top_k: int = 6,
        score_threshold: float = 0.4,
        per_source_top_k: int = 6,
        include_messages: bool = True,
        include_documents: bool = True,
        memory_store: "MemoryStore | None" = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._top_k = max(0, int(top_k))
        self._score_threshold = float(score_threshold)
        self._per_source_top_k = max(1, int(per_source_top_k))
        self._include_messages = bool(include_messages)
        self._include_documents = bool(include_documents)
        # Optional handle to the canonical SQLite memory store. When set,
        # ``retrieve`` calls ``MemoryStore.mark_used`` on the memory hits
        # it surfaces so subsequent turns can apply the recency penalty
        # / revival bonus tuned above. Plumbing tolerates ``None`` so
        # tests and lean deployments can run without it; the recency
        # signal then simply stays at 0.
        self._memory_store = memory_store
        # Schema v8 — IDs of memories surfaced in the last
        # :meth:`retrieve` call. ``SessionController._post_turn_inner_life``
        # reads this snapshot to run the keyword-overlap revival check
        # against Aiko's reply and bump ``revival_score`` on rows she
        # actually cited.
        self._last_surfaced_memory_ids: list[int] = []

    @property
    def top_k(self) -> int:
        return self._top_k

    def update_settings(
        self,
        *,
        top_k: int | None = None,
        score_threshold: float | None = None,
        include_messages: bool | None = None,
        include_documents: bool | None = None,
    ) -> None:
        if top_k is not None:
            self._top_k = max(0, int(top_k))
        if score_threshold is not None:
            self._score_threshold = max(0.0, min(1.0, float(score_threshold)))
        if include_messages is not None:
            self._include_messages = bool(include_messages)
        if include_documents is not None:
            self._include_documents = bool(include_documents)

    # ── retrieval ───────────────────────────────────────────────────────

    def retrieve(
        self,
        query_text: str,
        *,
        recent_turns: Iterable[str] | None = None,
        exclude_session_id: str | None = None,
    ) -> list[RagHit]:
        """Return up to ``top_k`` merged hits across all sources.

        ``recent_turns`` is an optional list of recent message texts used to
        widen the query (concatenated). This dramatically improves retrieval
        on follow-up questions that share little surface form with the prior
        turn (e.g. "what did I say earlier?").
        """
        if self._top_k <= 0:
            return []
        query = self._build_query(query_text, recent_turns)
        if not query:
            return []
        try:
            embedding = self._embedder.embed(query)
        except Exception:
            log.debug("rag retriever: embed failed", exc_info=True)
            return []

        merged: list[RagHit] = []
        try:
            mem_hits = self._store.search_memories(
                embedding,
                top_k=self._per_source_top_k,
                min_score=self._score_threshold,
            )
            for h in mem_hits:
                h.score += _MEMORY_PRIOR + _memory_recency_adjust(
                    last_used_at=getattr(h.record, "last_used_at", None),
                    use_count=int(getattr(h.record, "use_count", 0) or 0),
                )
                # Pin status lives in the SQLite mirror, not LanceDB --
                # apply the bonus by joining against ``MemoryStore`` here.
                # Wrapped in a broad try/except because a misbehaving or
                # duck-typed memory store must not abort retrieval (the
                # outer except for the whole memory branch would drop
                # every memory hit, see test_mark_used_failure_does_not_
                # break_retrieval).
                if self._memory_store is not None:
                    try:
                        raw_id = getattr(h.record, "id", None)
                        if raw_id is not None and hasattr(
                            self._memory_store, "get"
                        ):
                            mem = self._memory_store.get(int(raw_id))
                            if mem is not None:
                                if getattr(mem, "pinned", False):
                                    h.score += _MEMORY_PINNED_BONUS
                                # Schema v7: anniversary nudge for
                                # ``shared_moment`` rows whose ``when``
                                # matches one of the 1mo/3mo/6mo/1yr/Nyr
                                # windows today. Keeps the rendering of
                                # this hint out of the hot path.
                                if mem.kind == "shared_moment" and _is_anniversary_today(
                                    getattr(mem, "metadata", None)
                                ):
                                    h.score += _MEMORY_ANNIVERSARY_BONUS
                                # Schema v8: tier offset. Reads from the
                                # SQLite mirror (tier is not stored in
                                # the LanceDB record). Defaults to 0
                                # when the row predates v8 / tier is
                                # missing so callers stay safe.
                                tier = getattr(mem, "tier", "long_term")
                                h.score += _MEMORY_TIER_OFFSET.get(tier, 0.0)
                                # Schema v9: confidence penalty. Same
                                # join path as the tier offset above;
                                # low-confidence hits are demoted (never
                                # hidden) so they only surface when the
                                # cosine match is strong enough to
                                # overcome the handicap. The confidence
                                # is also stamped on the hit so
                                # ``format_block`` can append the
                                # "(uncertain)" suffix without a second
                                # SQLite roundtrip.
                                mem_confidence = getattr(mem, "confidence", None)
                                h.score += _confidence_penalty(mem_confidence)
                                if mem_confidence is not None:
                                    h.confidence = float(mem_confidence)
                    except Exception:
                        log.debug("pinned-bonus lookup failed", exc_info=True)
                merged.append(h)
        except Exception:
            log.debug("memory search failed", exc_info=True)

        if self._include_messages:
            try:
                msg_hits = self._store.search_messages(
                    embedding,
                    top_k=self._per_source_top_k,
                    min_score=self._score_threshold,
                )
                for h in msg_hits:
                    if exclude_session_id and h.source == "message":
                        # Don't surface lines from the *current* session --
                        # they're already in the recent-window context.
                        if getattr(h.record, "session_id", None) == exclude_session_id:
                            continue
                    h.score = h.score + _MESSAGE_PRIOR + _recency_bonus(
                        getattr(h.record, "created_at", "")
                    )
                    merged.append(h)
            except Exception:
                log.debug("message search failed", exc_info=True)

        if self._include_documents:
            try:
                doc_hits = self._store.search_documents(
                    embedding,
                    top_k=self._per_source_top_k,
                    min_score=self._score_threshold,
                )
                for h in doc_hits:
                    h.score += _DOCUMENT_PRIOR
                    merged.append(h)
            except Exception:
                log.debug("document search failed", exc_info=True)

        # Dedupe by content text (case-insensitive, whitespace-stripped).
        seen: set[str] = set()
        unique: list[RagHit] = []
        for h in sorted(merged, key=lambda x: x.score, reverse=True):
            key = (h.text or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(h)
            if len(unique) >= self._top_k:
                break

        # Bump ``last_used_at`` / ``use_count`` for every memory we're
        # actually surfacing this turn. Closes the loop with the recency
        # penalty above: a memory we just sent the LLM gets penalised on
        # the very next turn, breaking the "always wins" feedback loop
        # the legacy retriever suffered from. Best-effort — a broken
        # store must not abort the prompt build.
        ids: list[int] = []
        for hit in unique:
            if hit.source != "memory":
                continue
            raw = getattr(hit.record, "id", None)
            if raw is None:
                continue
            try:
                ids.append(int(raw))
            except (TypeError, ValueError):
                # Lance memory ids are stringified ints; anything
                # that doesn't parse cleanly is a non-canonical row
                # we can't reach via the SQLite mirror anyway.
                continue
        # Schema v8: snapshot the surfaced memory IDs so
        # SessionController can run the post-turn revival check (keyword
        # overlap between Aiko's reply and each surfaced memory) before
        # the next retrieve() clobbers the list.
        self._last_surfaced_memory_ids = list(ids)
        if self._memory_store is not None and ids:
            try:
                self._memory_store.mark_used(ids)
            except Exception:
                log.debug("mark_used failed", exc_info=True)
        return unique

    @property
    def last_surfaced_memory_ids(self) -> list[int]:
        """Snapshot of memory IDs surfaced by the most recent ``retrieve``.

        Empty list when the last call returned no memory hits or before
        the first call. Consumed by :class:`SessionController` to
        run the post-turn revival keyword-overlap check.
        """
        return list(self._last_surfaced_memory_ids)

    # ── formatting ──────────────────────────────────────────────────────

    @staticmethod
    def format_block(
        hits: list[RagHit],
        *,
        user_display_name: str = "the user",
    ) -> str:
        """Render hits into a system-prompt-ready block.

        Three sections, in this order, each only emitted when non-empty:
          - "What you know about <user> (long-term memory):" -- memories with
            ``kind`` in {fact, preference, event, relationship}.
          - "Things you've shared / decided about yourself:" -- memories with
            ``kind == "self"``.
          - "Snippets you remembered from past chats:" -- message hits.
          - "From your notes:" -- document hits.
        """
        if not hits:
            return ""
        user_lines: list[str] = []
        self_lines: list[str] = []
        message_lines: list[str] = []
        document_lines: list[str] = []
        for hit in hits:
            text = (hit.text or "").strip()
            if not text:
                continue
            if hit.source == "memory":
                kind = (getattr(hit.record, "kind", "") or "").lower()
                # Schema v9: append "(uncertain)" so the LLM hedges when
                # the underlying memory has a low confidence score (the
                # F1 fact-checker may have flagged it, or it never had
                # a high-confidence source to begin with).
                suffix = ""
                confidence = getattr(hit, "confidence", None)
                if confidence is not None and float(confidence) < 0.5:
                    suffix = " (uncertain)"
                if kind in ("self", "self_tagged"):
                    self_lines.append(f"- {text}{suffix}")
                else:
                    user_lines.append(f"- {text}{suffix}")
            elif hit.source == "message":
                role = (getattr(hit.record, "role", "") or "").lower()
                speaker = (
                    f"{user_display_name} said" if role == "user" else "You said"
                )
                message_lines.append(f'- {speaker}: "{_truncate(text, 200)}"')
            elif hit.source == "document":
                title = getattr(hit.record, "title", "")
                head = f"({title}) " if title else ""
                document_lines.append(f"- {head}{_truncate(text, 240)}")
        sections: list[str] = []
        if user_lines:
            sections.append(
                f"What you know about {user_display_name} (long-term memory):\n"
                + "\n".join(user_lines)
            )
        if self_lines:
            sections.append(
                "Things you've shared / decided about yourself:\n"
                + "\n".join(self_lines)
            )
        if message_lines:
            sections.append(
                "Snippets you remembered from past chats:\n"
                + "\n".join(message_lines)
            )
        if document_lines:
            sections.append("From your notes:\n" + "\n".join(document_lines))
        return "\n\n".join(sections)

    def block_for(
        self,
        query_text: str,
        *,
        recent_turns: Iterable[str] | None = None,
        exclude_session_id: str | None = None,
        user_display_name: str = "the user",
    ) -> str:
        hits = self.retrieve(
            query_text,
            recent_turns=recent_turns,
            exclude_session_id=exclude_session_id,
        )
        return self.format_block(hits, user_display_name=user_display_name)

    # ── internals ───────────────────────────────────────────────────────

    @staticmethod
    def _build_query(query_text: str, recent_turns: Iterable[str] | None) -> str:
        base = (query_text or "").strip()
        if not recent_turns:
            return base
        # Prepend a small recent-context snippet (last 2-3 turns) so search
        # can pick up referents like "that" / "earlier".
        chunks: list[str] = []
        for t in recent_turns:
            t = (t or "").strip()
            if not t:
                continue
            chunks.append(t)
        if not chunks:
            return base
        ctx = " | ".join(chunks[-3:])
        if not base:
            return ctx
        return f"{ctx} || {base}"


# ── helpers ─────────────────────────────────────────────────────────────────


def _truncate(s: str, limit: int) -> str:
    s = s.strip()
    if len(s) <= limit:
        return s
    return s[: max(20, limit - 1)].rstrip() + "…"


def _recency_bonus(created_at: str) -> float:
    """Tiny bonus for recent messages, penalty for ancient ones.

    Returns a value in roughly ``[-0.06, 0.06]``. Combined with the cosine
    score (which is in ``[score_threshold, 1.0]`` by then), this nudges the
    final ordering without overpowering raw similarity.
    """
    if not created_at:
        return 0.0
    try:
        ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except Exception:
        return 0.0
    delta_days = (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0
    if delta_days < 0:
        delta_days = 0.0
    # Exponential decay halved every _MESSAGE_HALFLIFE_DAYS; bonus shrinks
    # from 0.06 to ~0 as messages age.
    import math

    weight = math.pow(0.5, delta_days / _MESSAGE_HALFLIFE_DAYS)
    return 0.06 * (weight - 0.5)


def _hours_since(iso_ts: str | None) -> float | None:
    """Hours elapsed between ``iso_ts`` (UTC ISO-8601) and now, or
    ``None`` for missing / unparseable values.

    Negative deltas (clock skew) are clamped to 0.0 so a future-dated
    timestamp doesn't accidentally trigger the revival bonus.
    """
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    seconds = (datetime.now(timezone.utc) - ts).total_seconds()
    return max(0.0, seconds / 3600.0)


def _memory_recency_adjust(
    *,
    last_used_at: str | None,
    use_count: int,
) -> float:
    """Per-memory score nudge based on how recently it was surfaced.

    See the constants at the top of the module for the rationale. The
    function is total: it returns 0.0 for never-used memories, for
    parse failures, and for the "in-between" zone (used some time ago,
    not yet stale enough to revive).
    """
    hours = _hours_since(last_used_at)
    if hours is None:
        # Never surfaced -> no adjustment. Fresh discovery wins on its
        # own merits.
        return 0.0
    if hours < _MEMORY_RECENCY_PENALTY_HOURS:
        return -_MEMORY_RECENCY_PENALTY
    days = hours / 24.0
    if days >= _MEMORY_REVIVAL_DAYS and use_count > 0:
        return _MEMORY_REVIVAL_BONUS
    return 0.0


