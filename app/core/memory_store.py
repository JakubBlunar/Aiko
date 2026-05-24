"""SQLite-backed long-term memory store with cosine search.

One row per durable fact about the user. Embeddings live alongside the row
as a packed float32 BLOB. The store keeps an in-memory mirror of every row
so cosine search runs in pure NumPy without a per-query SQL roundtrip.

Capacity is bounded (``max_memories``, default 500); ``prune()`` evicts the
oldest least-used / lowest-salience rows once the cap is hit. Cross-session
by design: there's exactly one memory store for the assistant.

Phase C also mirrors every write into a :class:`RagStore` (LanceDB-backed)
when one is attached, so that the new RagRetriever has a single read path.
The SQLite store remains the source of truth for now; if the RagStore
disappears (e.g., embedding-dim swap rebuilds the table), the next search
will simply hit the SQLite path until the RagStore catches up via a fresh
migration.
"""
from __future__ import annotations

import logging
import sqlite3
import struct
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

import numpy as np

from app.llm.embedder import cosine_similarity

if TYPE_CHECKING:
    from app.core.rag_store import RagStore


log = logging.getLogger("app.memory_store")


VALID_KINDS = {
    "fact",
    "preference",
    "event",
    "relationship",
    "self_tagged",
    "self",
    # Phase 2c — produced by ReflectionWorker (LLM journal during the
    # speaking window). open_question = something Aiko wonders about and
    # might surface later. callback = a thread she'd like to pick back up.
    "open_question",
    "callback",
    "reflection",
    # Phase 3c — explicit promises ("I'll do X", "remind me to Y").
    # Surfaced through RAG and consumed by ProactiveDirector.
    "promise",
}


@dataclass(slots=True)
class Memory:
    id: int
    content: str
    kind: str
    salience: float
    embedding: np.ndarray
    source_session: str | None
    source_message_id: int | None
    created_at: str
    last_used_at: str | None
    use_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "content": self.content,
            "kind": self.kind,
            "salience": float(self.salience),
            "source_session": self.source_session,
            "source_message_id": self.source_message_id,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "use_count": int(self.use_count),
        }


@dataclass(slots=True)
class SearchHit:
    memory: Memory
    score: float


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _encode(vec: np.ndarray) -> bytes:
    arr = np.asarray(vec, dtype=np.float32)
    return struct.pack(f"{len(arr)}f", *arr.tolist())


def _decode(blob: bytes) -> np.ndarray:
    count = len(blob) // 4
    return np.array(struct.unpack(f"{count}f", blob), dtype=np.float32)


class MemoryStore:
    """Thread-safe long-term memory backed by the ``memories`` SQLite table.

    The ``memories`` table is created by :class:`ChatDatabase` (schema v3).
    This class is a focused lens on that one table -- no foreign-key joins.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        max_memories: int = 500,
        dedupe_threshold: float = 0.92,
    ) -> None:
        self._db_path = db_path
        self._max = max(50, int(max_memories))
        self._dedupe_threshold = float(dedupe_threshold)
        self._local = threading.local()
        self._lock = threading.Lock()
        # In-memory mirror so cosine search is a single NumPy pass.
        self._mirror: dict[int, Memory] = {}
        self._rag: "RagStore | None" = None
        self._reload_mirror()

    def attach_rag_store(self, rag_store: "RagStore | None") -> None:
        """Hook a :class:`RagStore` so subsequent writes mirror into LanceDB.

        Idempotent. Pass ``None`` to detach.
        """
        self._rag = rag_store

    def migrate_to_rag(self, rag_store: "RagStore") -> int:
        """Copy every existing memory into the RagStore (idempotent).

        Returns how many rows were written. Safe to call multiple times --
        :meth:`RagStore.add_memory` upserts on ``id`` so re-runs are no-ops.
        """
        if rag_store is None:
            return 0
        with self._lock:
            mems = list(self._mirror.values())
        written = 0
        for mem in mems:
            try:
                rag_store.add_memory(
                    record_id=str(mem.id),
                    content=mem.content,
                    kind=mem.kind,
                    embedding=mem.embedding,
                    salience=mem.salience,
                    source_session=mem.source_session,
                    source_message_id=mem.source_message_id,
                    created_at=mem.created_at,
                )
                written += 1
            except Exception:
                log.debug("rag mirror failed for memory id=%s", mem.id, exc_info=True)
        if written:
            log.info("RAG: mirrored %d existing memories into LanceDB", written)
        return written

    # ── lifecycle ─────────────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _reload_mirror(self) -> None:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT id, content, kind, salience, embedding, source_session, "
                "source_message_id, created_at, last_used_at, use_count "
                "FROM memories"
            ).fetchall()
        except sqlite3.OperationalError:
            # The memories table doesn't exist yet (first boot before
            # ChatDatabase created the schema); leave the mirror empty.
            self._mirror = {}
            return
        with self._lock:
            self._mirror = {
                r[0]: Memory(
                    id=r[0],
                    content=r[1],
                    kind=r[2],
                    salience=float(r[3]),
                    embedding=_decode(r[4]),
                    source_session=r[5],
                    source_message_id=r[6],
                    created_at=r[7],
                    last_used_at=r[8],
                    use_count=int(r[9]),
                )
                for r in rows
            }
        log.info("memory store loaded with %d memories", len(self._mirror))

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    # ── writes ────────────────────────────────────────────────────────────

    def add(
        self,
        content: str,
        kind: str,
        embedding: np.ndarray,
        *,
        salience: float = 0.5,
        source_session: str | None = None,
        source_message_id: int | None = None,
    ) -> Memory | None:
        """Insert a memory, deduplicating against near-identical existing rows.

        Returns the newly inserted ``Memory`` or ``None`` if the candidate
        was a near-duplicate of an existing memory (whose salience is bumped
        and ``last_used_at`` refreshed instead).
        """
        cleaned = (content or "").strip()
        if not cleaned or len(cleaned) < 4:
            return None
        kind = kind.strip().lower() or "fact"
        if kind not in VALID_KINDS:
            kind = "fact"
        salience_clipped = max(0.0, min(1.0, float(salience)))
        emb = np.asarray(embedding, dtype=np.float32)
        if emb.size == 0:
            return None
        # Normalize for cosine.
        norm = float(np.linalg.norm(emb))
        if norm > 0.0:
            emb = emb / norm

        # Dedupe pass against in-memory mirror.
        dup_id: int | None = None
        with self._lock:
            for mem in self._mirror.values():
                if cosine_similarity(emb, mem.embedding) >= self._dedupe_threshold:
                    dup_id = mem.id
                    break
        if dup_id is not None:
            self._touch_existing(dup_id, salience_clipped)
            return None

        # Real insert.
        conn = self._get_conn()
        now = _now_iso()
        cursor = conn.execute(
            "INSERT INTO memories ("
            "  content, kind, salience, embedding, source_session, "
            "  source_message_id, created_at, last_used_at, use_count"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (
                cleaned,
                kind,
                salience_clipped,
                _encode(emb),
                source_session,
                source_message_id,
                now,
                None,
            ),
        )
        conn.commit()
        new_id = int(cursor.lastrowid or 0)
        memory = Memory(
            id=new_id,
            content=cleaned,
            kind=kind,
            salience=salience_clipped,
            embedding=emb,
            source_session=source_session,
            source_message_id=source_message_id,
            created_at=now,
            last_used_at=None,
            use_count=0,
        )
        with self._lock:
            self._mirror[new_id] = memory
        if self._rag is not None:
            try:
                self._rag.add_memory(
                    record_id=str(new_id),
                    content=cleaned,
                    kind=kind,
                    embedding=emb,
                    salience=salience_clipped,
                    source_session=source_session,
                    source_message_id=source_message_id,
                    created_at=now,
                )
            except Exception:
                log.debug("rag add_memory failed", exc_info=True)
        if len(self._mirror) > self._max:
            self.prune()
        return memory

    def _touch_existing(self, memory_id: int, candidate_salience: float) -> None:
        """Bump salience and refresh last_used_at on a deduped match."""
        conn = self._get_conn()
        now = _now_iso()
        with self._lock:
            mem = self._mirror.get(memory_id)
            if mem is None:
                return
            new_salience = max(mem.salience, candidate_salience, mem.salience + 0.05)
            new_salience = min(1.0, new_salience)
            mem.salience = new_salience
            mem.last_used_at = now
        conn.execute(
            "UPDATE memories SET salience = ?, last_used_at = ? WHERE id = ?",
            (new_salience, now, memory_id),
        )
        conn.commit()

    def mark_used(self, ids: Iterable[int]) -> None:
        ids_list = [int(i) for i in ids if i]
        if not ids_list:
            return
        conn = self._get_conn()
        now = _now_iso()
        placeholders = ",".join("?" * len(ids_list))
        conn.execute(
            f"UPDATE memories SET last_used_at = ?, use_count = use_count + 1 "
            f"WHERE id IN ({placeholders})",
            (now, *ids_list),
        )
        conn.commit()
        with self._lock:
            for mid in ids_list:
                mem = self._mirror.get(mid)
                if mem is not None:
                    mem.last_used_at = now
                    mem.use_count += 1

    def delete(self, memory_id: int) -> bool:
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM memories WHERE id = ?", (int(memory_id),))
        conn.commit()
        with self._lock:
            self._mirror.pop(int(memory_id), None)
        if self._rag is not None:
            try:
                self._rag.delete_memory(str(int(memory_id)))
            except Exception:
                log.debug("rag delete_memory failed", exc_info=True)
        return cursor.rowcount > 0

    def decay(self, by: float = 0.02) -> None:
        """Slowly forget unused memories. Call from background worker."""
        if by <= 0:
            return
        conn = self._get_conn()
        with self._lock:
            for mem in self._mirror.values():
                mem.salience = max(0.0, mem.salience - float(by))
        conn.execute(
            "UPDATE memories SET salience = MAX(0.0, salience - ?)",
            (float(by),),
        )
        conn.commit()

    def prune(self) -> int:
        """Delete the lowest-priority memories until count <= max_memories."""
        with self._lock:
            count = len(self._mirror)
        if count <= self._max:
            return 0
        # Score = salience + 0.1 * (use_count clamped to 10) - recency penalty
        # Lowest scoring rows are deleted first.
        ranked = sorted(
            self._mirror.values(),
            key=lambda m: (
                m.salience + 0.05 * min(m.use_count, 20)
            ),
        )
        excess = count - self._max
        victims = [m.id for m in ranked[:excess]]
        if not victims:
            return 0
        conn = self._get_conn()
        placeholders = ",".join("?" * len(victims))
        conn.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", victims)
        conn.commit()
        with self._lock:
            for mid in victims:
                self._mirror.pop(mid, None)
        if self._rag is not None:
            for mid in victims:
                try:
                    self._rag.delete_memory(str(mid))
                except Exception:
                    log.debug("rag delete during prune failed", exc_info=True)
        log.info("pruned %d low-priority memories", len(victims))
        return len(victims)

    # ── reads ─────────────────────────────────────────────────────────────

    def search(
        self,
        query_embedding: np.ndarray,
        *,
        top_k: int = 6,
        min_score: float = 0.4,
    ) -> list[SearchHit]:
        """Return the top-k memories by cosine similarity. Empty if store is empty."""
        with self._lock:
            mems = list(self._mirror.values())
        if not mems:
            return []
        q = np.asarray(query_embedding, dtype=np.float32)
        if q.size == 0:
            return []
        qn = float(np.linalg.norm(q))
        if qn > 0.0:
            q = q / qn
        scored: list[SearchHit] = []
        for mem in mems:
            score = cosine_similarity(q, mem.embedding)
            # Light salience boost so two similar memories prefer the more salient one.
            adjusted = score + 0.05 * (mem.salience - 0.5)
            if score >= min_score:
                scored.append(SearchHit(memory=mem, score=adjusted))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[: max(1, int(top_k))]

    def list_recent(self, limit: int = 50) -> list[Memory]:
        with self._lock:
            mems = sorted(
                self._mirror.values(),
                key=lambda m: m.created_at,
                reverse=True,
            )
        return mems[: max(1, int(limit))]

    def list_top(self, limit: int = 50) -> list[Memory]:
        with self._lock:
            mems = sorted(
                self._mirror.values(),
                key=lambda m: (m.salience, m.use_count),
                reverse=True,
            )
        return mems[: max(1, int(limit))]

    def count(self) -> int:
        with self._lock:
            return len(self._mirror)
