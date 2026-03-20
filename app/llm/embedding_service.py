"""Embedding generation and cosine-similarity search via Ollama."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from app.core.chat_database import ChatDatabase, EmbeddingRow

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SearchResult:
    role: str
    content: str
    created_at: str
    score: float


class EmbeddingService:
    """Generates embeddings via Ollama and searches stored vectors by cosine similarity."""

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "qwen3-embedding:0.6b",
        database: ChatDatabase | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._db = database
        self._embedding_cache: list[EmbeddingRow] | None = None

    def set_database(self, database: ChatDatabase) -> None:
        self._db = database
        self._embedding_cache = None

    def embed(self, text: str) -> np.ndarray | None:
        """Generate an embedding vector for a single text string."""
        import requests
        try:
            resp = requests.post(
                f"{self._base_url}/api/embed",
                json={"model": self._model, "input": text},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            embeddings = data.get("embeddings") or []
            if embeddings and len(embeddings) > 0:
                return np.array(embeddings[0], dtype=np.float32)
        except Exception as exc:
            log.warning("Embedding failed: %s", exc)
        return None

    def embed_and_store(self, message_id: int, session_id: str, content: str) -> None:
        """Generate and persist an embedding for a stored message."""
        if not self._db:
            return
        vec = self.embed(content)
        if vec is not None:
            self._db.add_embedding(message_id, session_id, vec)
            self._embedding_cache = None

    def search(
        self,
        query: str,
        *,
        session_id: str | None = None,
        top_k: int = 5,
        max_candidates: int = 500,
    ) -> list[SearchResult]:
        """Find the top-K most semantically similar messages to the query.

        *max_candidates* caps how many embeddings are loaded from the DB to
        keep search time bounded in long conversations.
        """
        if not self._db:
            return []
        query_vec = self.embed(query)
        if query_vec is None:
            return []

        stored = self._db.get_all_embeddings(session_id, max_rows=max_candidates)
        if not stored:
            return []

        scored: list[tuple[float, EmbeddingRow]] = []
        query_norm = np.linalg.norm(query_vec)
        if query_norm == 0:
            return []

        for row in stored:
            row_norm = np.linalg.norm(row.embedding)
            if row_norm == 0:
                continue
            similarity = float(np.dot(query_vec, row.embedding) / (query_norm * row_norm))
            scored.append((similarity, row))

        scored.sort(key=lambda x: x[0], reverse=True)
        results: list[SearchResult] = []
        for score, row in scored[:top_k]:
            results.append(SearchResult(
                role=row.role,
                content=row.content,
                created_at=row.created_at,
                score=score,
            ))
        return results

    def backfill_embeddings(self, session_id: str) -> int:
        """Generate embeddings for messages that don't have them yet. Returns count of new embeddings."""
        if not self._db:
            return 0
        existing_ids = self._db.get_message_ids_with_embeddings(session_id)
        messages = self._db.get_messages(session_id)
        count = 0
        for msg in messages:
            if msg.id in existing_ids:
                continue
            vec = self.embed(msg.content)
            if vec is not None:
                self._db.add_embedding(msg.id, session_id, vec)
                count += 1
        if count > 0:
            self._embedding_cache = None
        return count
