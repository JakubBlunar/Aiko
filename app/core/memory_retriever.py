"""Per-turn long-term memory retrieval.

Embeds the user's latest input and pulls the top-K closest memories from
:class:`MemoryStore`. Returns both the raw hits (for logging / API responses)
and a formatted prompt block ready to drop into the system message.
"""
from __future__ import annotations

import logging

from app.core.memory_store import Memory, MemoryStore, SearchHit
from app.llm.embedder import Embedder


log = logging.getLogger("app.memory_retriever")


class MemoryRetriever:
    def __init__(
        self,
        store: MemoryStore,
        embedder: Embedder,
        *,
        top_k: int = 6,
        score_threshold: float = 0.4,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._top_k = max(0, int(top_k))
        self._score_threshold = float(score_threshold)

    @property
    def top_k(self) -> int:
        return self._top_k

    def update_settings(
        self, *, top_k: int | None = None, score_threshold: float | None = None
    ) -> None:
        if top_k is not None:
            self._top_k = max(0, int(top_k))
        if score_threshold is not None:
            self._score_threshold = max(0.0, min(1.0, float(score_threshold)))

    def top_memories(self, query_text: str) -> list[SearchHit]:
        """Return the top-K memories for ``query_text`` (already mark_used)."""
        query = (query_text or "").strip()
        if self._top_k <= 0 or not query:
            return []
        if self._store.count() == 0:
            return []
        try:
            embedding = self._embedder.embed(query)
        except Exception as exc:
            log.debug("retriever: embed failed: %s", exc)
            return []
        hits = self._store.search(
            embedding,
            top_k=self._top_k,
            min_score=self._score_threshold,
        )
        if hits:
            try:
                self._store.mark_used([h.memory.id for h in hits])
            except Exception:
                log.debug("retriever: mark_used failed", exc_info=True)
        return hits

    @staticmethod
    def format_block(
        hits: list[SearchHit],
        *,
        user_display_name: str = "the user",
    ) -> str:
        """Format the retrieved memories into a system-prompt-ready block."""
        if not hits:
            return ""
        # Schema v10: deferred import keeps this fallback lightweight
        # (the LanceDB-backed path already imports the helpers; the
        # SQLite-only path is the cold standby).
        from datetime import datetime, timezone

        from app.core.rag_retriever import _temporal_suffix

        lines = [
            f"What you know about {user_display_name} (long-term memory):"
        ]
        seen: set[str] = set()
        now = datetime.now(timezone.utc)
        for hit in hits:
            content = (hit.memory.content or "").strip()
            if not content:
                continue
            key = content.lower()
            if key in seen:
                continue
            seen.add(key)
            # Schema v9: tag low-confidence memories with "(uncertain)"
            # so the LLM hedges. Confidence lives on Memory itself; we
            # have direct access here (unlike the LanceDB-backed path).
            suffix = ""
            confidence = getattr(hit.memory, "confidence", None)
            if confidence is not None and float(confidence) < 0.5:
                suffix = " (uncertain)"
            # Schema v10: append the temporal time-tag, same rendering
            # as the RAG path so the two retrievers feel identical to
            # the LLM.
            time_suffix = _temporal_suffix(
                temporal_type=getattr(hit.memory, "temporal_type", None),
                event_time=getattr(hit.memory, "event_time", None),
                created_at=getattr(hit.memory, "created_at", None),
                now=now,
            )
            lines.append(f"- {content}{suffix}{time_suffix}")
        if len(lines) == 1:
            return ""
        return "\n".join(lines)

    def block_for(
        self,
        query_text: str,
        *,
        user_display_name: str = "the user",
    ) -> str:
        """Convenience: retrieve and format in one call."""
        hits = self.top_memories(query_text)
        return self.format_block(hits, user_display_name=user_display_name)

    @staticmethod
    def memory_to_dict(memory: Memory) -> dict[str, object]:
        return memory.to_dict()
