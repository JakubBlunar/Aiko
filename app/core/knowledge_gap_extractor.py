"""Knowledge-gap journal (F2 personality backlog).

Parses ``[[gap:topic:short question]]`` inline tags from Aiko's raw
output and persists each as a ``knowledge_gap`` memory row. The store
wrapper offers add / list / resolve / pick-relevant operations on top
of :class:`MemoryStore`, mirroring the way ``SharedMomentsStore``
augments memories with structured behaviour.

Why a separate kind instead of stuffing this into ``shared_moment``?
Knowledge gaps have radically different semantics: they're open
questions Aiko WANTS answered (confidence=0.0, not a fact), and F1's
background fact-checker can resolve them by writing the answer
alongside and stamping ``metadata.resolved_at``. Mixing them with
moments would require kind-aware special-casing throughout retrieval.

Inline grammar::

    [[gap:topic_slug:question text]]

* ``topic_slug`` — lowercase letters/digits/underscore/hyphen, 1-30
  chars. Used to bucket related gaps (e.g. all music questions cluster
  on the same slug).
* ``question text`` — 4-200 chars. The question Aiko is asking herself.

The store caps unresolved open gaps at ``max_open`` (default 20). On
overflow the oldest unpinned unresolved row is dropped so a chatty
session can't run away with the journal. F1's fact-checker stamps
``metadata.resolved_at`` and ``resolved_by_memory_id`` once it finds an
answer; resolved rows are kept (audit trail) but excluded from the
"things you've been wondering about" prompt block.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import numpy as np

from app.llm.embedder import cosine_similarity

if TYPE_CHECKING:
    from app.core.memory_store import Memory, MemoryStore
    from app.llm.embedder import Embedder


log = logging.getLogger("app.knowledge_gap_extractor")


# Topic slug is intentionally narrow so a typo'd or punctuated body
# can't sneak in. Question allows most printable characters but no
# square brackets (which would break the grammar) or newlines.
_GAP_TAG_RE = re.compile(
    r"\[\[gap:([a-z][a-z0-9_\-]{0,30}):([^\[\]\n]{4,200}?)\]\]",
    flags=re.IGNORECASE,
)

_MIN_QUESTION_CHARS = 4
_MAX_QUESTION_CHARS = 200
_DEFAULT_MAX_OPEN = 20
_DEFAULT_SIMILARITY_THRESHOLD = 0.5
_DEFAULT_TTL_DAYS = 90


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class GapCandidate:
    """One ``[[gap:topic:question]]`` tag pulled from raw assistant text."""

    topic: str
    question: str


def extract_inline_tags(text: str) -> list[GapCandidate]:
    """Pull every well-formed ``[[gap:topic:question]]`` from ``text``.

    Returns each unique ``(topic, question)`` only once per text so a
    repeated tag inside the same reply doesn't flood the journal.
    """
    source = (text or "").strip()
    if not source:
        return []
    seen: set[tuple[str, str]] = set()
    out: list[GapCandidate] = []
    for match in _GAP_TAG_RE.finditer(source):
        topic = (match.group(1) or "").strip().lower()
        question = (match.group(2) or "").strip()
        if not topic or len(question) < _MIN_QUESTION_CHARS:
            continue
        if len(question) > _MAX_QUESTION_CHARS:
            question = question[:_MAX_QUESTION_CHARS].rstrip()
        key = (topic, question.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(GapCandidate(topic=topic, question=question))
    return out


def _format_content(topic: str, question: str) -> str:
    """The flat string that lives in ``memories.content``.

    Embeddings + retrieval operate on this string, so it should read
    well on its own. We use ``"<topic>: <question>"`` so cosine matches
    naturally pick the gap up when the user circles back to the topic.
    """
    return f"{topic}: {question}".strip()


class KnowledgeGapStore:
    """Thin wrapper over :class:`MemoryStore` for the ``knowledge_gap`` kind.

    Holds no state of its own — the underlying memory store is the
    source of truth. The wrapper exists to centralise:
      * Metadata shape (``topic`` / ``question`` / ``resolved_at`` /
        ``resolved_by_memory_id`` / ``source_turn_id``).
      * Open-gap cap enforcement.
      * Resolution stamping.
      * Similarity-based pick for the prompt block.
    """

    def __init__(
        self,
        memory_store: "MemoryStore",
        embedder: "Embedder | None",
        *,
        max_open: int = _DEFAULT_MAX_OPEN,
        similarity_threshold: float = _DEFAULT_SIMILARITY_THRESHOLD,
        ttl_days: int = _DEFAULT_TTL_DAYS,
    ) -> None:
        self._memory_store = memory_store
        self._embedder = embedder
        self._max_open = max(1, int(max_open))
        self._similarity_threshold = float(similarity_threshold)
        self._ttl_days = max(1, int(ttl_days))

    @property
    def ttl_days(self) -> int:
        return self._ttl_days

    @property
    def max_open(self) -> int:
        return self._max_open

    # ── writes ────────────────────────────────────────────────────────

    def add_gap(
        self,
        *,
        topic: str,
        question: str,
        source_session: str | None = None,
        source_turn_id: int | None = None,
    ) -> "Memory | None":
        """Persist a new gap and (best-effort) prune overflow afterward.

        Returns the inserted ``Memory`` or ``None`` if anything went
        wrong (no embedder, validation failure, dedupe collision, etc.).
        """
        topic = (topic or "").strip().lower()
        question = (question or "").strip()
        if not topic or len(question) < _MIN_QUESTION_CHARS:
            return None
        if self._embedder is None:
            log.debug("no embedder configured; cannot persist knowledge gap")
            return None
        content = _format_content(topic, question)
        try:
            emb = self._embedder.embed(content)
        except Exception:
            log.debug("knowledge gap embed failed", exc_info=True)
            return None
        meta: dict[str, Any] = {
            "topic": topic,
            "question": question,
            "resolved_at": None,
            "resolved_by_memory_id": None,
        }
        if source_turn_id is not None:
            meta["source_turn_id"] = int(source_turn_id)
        try:
            mem = self._memory_store.add(
                content=content,
                kind="knowledge_gap",
                embedding=emb,
                # Gaps deserve a moderate baseline salience so they
                # survive the scratchpad sweep before they get
                # resolved, but lower than user-curated anchors so
                # they don't crowd retrieval.
                salience=0.4,
                source_session=source_session,
                source_message_id=source_turn_id,
                metadata=meta,
                tier="long_term",
                # confidence stays at the kind-aware default of 0.0
                # (set by MemoryStore.add when ``kind == knowledge_gap``).
            )
        except Exception:
            log.debug("knowledge gap insert failed", exc_info=True)
            return None
        if mem is not None:
            self.prune_overflow()
        return mem

    def mark_resolved(
        self,
        gap_id: int,
        *,
        answer_memory_id: int | None,
        resolved_by: str | None = None,
        similarity: float | None = None,
    ) -> bool:
        """Stamp ``resolved_at`` on the gap row.

        The companion answer memory (if any) is identified by
        ``answer_memory_id`` so the UI / fact-checker can backlink.
        ``resolved_by`` is a free-form audit string identifying which
        path closed the gap (``"fact_checker"`` for F1, ``"memory_match"``
        for the F2.1 idle resolver, ``"user_answer"`` for the post-turn
        path). ``similarity`` is the cosine score that triggered the
        match, when applicable. Both are optional and merge into
        existing metadata so older callers stay backward compatible.

        Returns True if the row was updated, False otherwise (e.g.
        the id no longer exists).
        """
        existing = self._memory_store.get(int(gap_id))
        if existing is None or existing.kind != "knowledge_gap":
            return False
        meta: dict[str, Any] = {"resolved_at": _now_iso()}
        if answer_memory_id is not None:
            meta["resolved_by_memory_id"] = int(answer_memory_id)
        if resolved_by:
            meta["resolved_by"] = str(resolved_by).strip()
        if similarity is not None:
            try:
                meta["resolved_similarity"] = round(float(similarity), 4)
            except (TypeError, ValueError):
                pass
        try:
            self._memory_store.update(
                int(gap_id),
                metadata=meta,
                metadata_merge=True,
            )
        except Exception:
            log.debug("knowledge gap mark_resolved failed", exc_info=True)
            return False
        return True

    def delete(self, gap_id: int) -> bool:
        existing = self._memory_store.get(int(gap_id))
        if existing is None or existing.kind != "knowledge_gap":
            return False
        return bool(self._memory_store.delete(int(gap_id)))

    # ── reads ─────────────────────────────────────────────────────────

    def list_open(self) -> list["Memory"]:
        """Return unresolved gap rows, newest first."""
        rows = self._all_gap_rows()
        open_rows = [m for m in rows if not _is_resolved(m)]
        open_rows.sort(key=lambda m: m.created_at, reverse=True)
        return open_rows

    def list_all(self, *, include_resolved: bool = True) -> list["Memory"]:
        rows = self._all_gap_rows()
        if not include_resolved:
            rows = [m for m in rows if not _is_resolved(m)]
        rows.sort(key=lambda m: m.created_at, reverse=True)
        return rows

    def pick_relevant(
        self,
        query_text: str,
        *,
        threshold: float | None = None,
    ) -> "Memory | None":
        """Top-1 open gap by cosine similarity to ``query_text``.

        Returns ``None`` when there are no open gaps, no embedder, or
        the best match falls below ``threshold`` (default 0.5).
        """
        if self._embedder is None:
            return None
        query = (query_text or "").strip()
        if not query:
            return None
        thr = self._similarity_threshold if threshold is None else float(threshold)
        try:
            q_emb = self._embedder.embed(query)
        except Exception:
            log.debug("knowledge gap pick_relevant embed failed", exc_info=True)
            return None
        q_arr = np.asarray(q_emb, dtype=np.float32)
        norm = float(np.linalg.norm(q_arr))
        if norm > 0.0:
            q_arr = q_arr / norm
        best_score = -1.0
        best_row: "Memory | None" = None
        for mem in self.list_open():
            try:
                score = float(cosine_similarity(q_arr, mem.embedding))
            except Exception:
                continue
            if score > best_score:
                best_score = score
                best_row = mem
        if best_row is None or best_score < thr:
            return None
        return best_row

    # ── maintenance ───────────────────────────────────────────────────

    def prune_overflow(self) -> int:
        """Drop the oldest unpinned unresolved gaps above ``max_open``.

        Returns how many rows were pruned. Pinned rows are never
        touched (the user explicitly anchored them) and resolved rows
        don't count toward the cap (they're audit trail).
        """
        open_rows = [
            m
            for m in self._all_gap_rows()
            if not _is_resolved(m) and not m.pinned
        ]
        if len(open_rows) <= self._max_open:
            return 0
        open_rows.sort(key=lambda m: m.created_at)
        overflow = len(open_rows) - self._max_open
        pruned = 0
        for victim in open_rows[:overflow]:
            try:
                if self._memory_store.delete(int(victim.id)):
                    pruned += 1
            except Exception:
                log.debug("knowledge gap prune delete failed", exc_info=True)
        return pruned

    def prune_expired(self, *, now: datetime | None = None) -> int:
        """Delete unresolved unpinned gaps older than ``ttl_days``.

        Called periodically from :class:`MemoryDecayWorker`. Returns the
        number of rows pruned. Resolved rows are kept (audit trail).
        """
        cutoff = (now or datetime.now(timezone.utc))
        from datetime import timedelta

        threshold = cutoff - timedelta(days=self._ttl_days)
        pruned = 0
        for mem in self._all_gap_rows():
            if _is_resolved(mem) or mem.pinned:
                continue
            try:
                created = datetime.fromisoformat(
                    str(mem.created_at).replace("Z", "+00:00")
                )
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            if created >= threshold:
                continue
            try:
                if self._memory_store.delete(int(mem.id)):
                    pruned += 1
            except Exception:
                log.debug("knowledge gap expire delete failed", exc_info=True)
        return pruned

    # ── internals ─────────────────────────────────────────────────────

    def _all_gap_rows(self) -> list["Memory"]:
        """Pull every ``knowledge_gap`` row from the underlying store."""
        try:
            mirror = getattr(self._memory_store, "_mirror", None)
            if mirror is not None:
                return [m for m in mirror.values() if m.kind == "knowledge_gap"]
        except Exception:
            pass
        # Defensive fallback for shaped stubs that don't expose
        # ``_mirror`` (e.g. tests): walk ``list_recent`` if available.
        try:
            recent = self._memory_store.list_recent(limit=10_000)
            return [m for m in recent if m.kind == "knowledge_gap"]
        except Exception:
            return []


def _is_resolved(memory: "Memory") -> bool:
    """Return True if the gap row's ``metadata.resolved_at`` is set."""
    meta = getattr(memory, "metadata", None) or {}
    if not isinstance(meta, dict):
        return False
    return bool(meta.get("resolved_at"))
