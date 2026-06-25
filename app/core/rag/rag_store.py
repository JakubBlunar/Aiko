"""LanceDB-backed RAG store for memories, chat messages, and documents.

This is the single retrieval substrate for lean-v1 phase C onward. It owns
three Lance tables in ``data/lancedb/``:

    memories     -- durable cross-session facts (replaces app.core.memory.memory_store
                    on the read path; legacy SQLite ``memories`` table is
                    migrated once at startup, then kept in sync via
                    :class:`MemoryStore.add_listener`).
    messages     -- per-message embeddings of chat history; populated lazily
                    by :class:`MessageIndexer` and on every new message.
    documents    -- chunks from user-uploaded files (md / txt / pdf).

Each table has a fixed pyarrow schema with ``vector_dim`` floats. The
embedding-model name is stamped into a sidecar file (``data/lancedb/meta.json``)
so we can detect a model swap and rebuild instead of mixing dimensions.

Why LanceDB? Native vector search, columnar storage, zero-server overhead, and
the same tables can be queried by external tools (Datasette etc.) if we ever
need to debug. Keeping the tables small (sub-millions of rows) means the
default IVF index is plenty.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pyarrow as pa

try:
    import lancedb
    from lancedb.table import Table
except Exception as exc:  # pragma: no cover -- import-time issue
    raise RuntimeError(
        "lancedb is required for RagStore; install with `pip install lancedb`"
    ) from exc


log = logging.getLogger("app.rag_store")


# ── Table identifiers ───────────────────────────────────────────────────────

TABLE_MEMORIES = "memories"
TABLE_MESSAGES = "messages"
TABLE_DOCUMENTS = "documents"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Public dataclasses (pure-python views; LanceDB rows are dicts) ──────────


@dataclass(slots=True)
class MemoryRecord:
    """LanceDB-side view of a memory row.

    Intentionally narrower than :class:`app.core.memory.memory_store.Memory`:
    fields that are queried during retrieval but not used for vector
    search (``pinned``, ``metadata``, ``tier``, ``revival_score``,
    ``confidence``, and the v10 temporal fields ``event_time``,
    ``temporal_type``, ``relevance_until``) are joined from SQLite by
    :class:`RagRetriever` at query time. The join is cheap (in-memory
    dict lookup against ``MemoryStore``'s mirror) and lets us avoid a
    LanceDB schema migration every time we add a new lifecycle field.
    """

    id: str
    content: str
    kind: str  # "fact" | "preference" | "event" | "relationship" | "self"
    salience: float
    source_session: str | None
    source_message_id: int | None
    created_at: str
    last_used_at: str | None
    use_count: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "MemoryRecord":
        return cls(
            id=str(row["id"]),
            content=str(row.get("content", "")),
            kind=str(row.get("kind", "fact")),
            salience=float(row.get("salience", 0.5)),
            source_session=row.get("source_session"),
            source_message_id=(
                int(row["source_message_id"])
                if row.get("source_message_id") is not None
                else None
            ),
            created_at=str(row.get("created_at", "")),
            last_used_at=row.get("last_used_at"),
            use_count=int(row.get("use_count", 0)),
        )


@dataclass(slots=True)
class MessageRecord:
    id: str  # e.g. "<session>:<message_id>"
    session_id: str
    message_id: int
    role: str  # "user" | "assistant"
    content: str
    created_at: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "MessageRecord":
        return cls(
            id=str(row["id"]),
            session_id=str(row.get("session_id", "")),
            message_id=int(row.get("message_id", 0)),
            role=str(row.get("role", "user")),
            content=str(row.get("content", "")),
            created_at=str(row.get("created_at", "")),
        )


@dataclass(slots=True)
class DocumentChunk:
    id: str
    document_id: str
    title: str
    chunk_index: int
    content: str
    created_at: str

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "DocumentChunk":
        return cls(
            id=str(row["id"]),
            document_id=str(row.get("document_id", "")),
            title=str(row.get("title", "")),
            chunk_index=int(row.get("chunk_index", 0)),
            content=str(row.get("content", "")),
            created_at=str(row.get("created_at", "")),
        )


@dataclass(slots=True)
class RagHit:
    """One result from a hybrid search across one or more tables."""

    source: str  # "memory" | "message" | "document"
    score: float  # higher is better; cosine in [0, 1]
    record: MemoryRecord | MessageRecord | DocumentChunk
    # Schema v9 — confidence in [0, 1] for memory hits. Stamped by
    # :class:`RagRetriever` during retrieve() via a join against the
    # SQLite mirror (LanceDB's MemoryRecord does not carry confidence).
    # ``None`` for non-memory hits or when the join could not resolve.
    confidence: float | None = None
    # Schema v10 — temporal-awareness fields, also joined from SQLite
    # by :class:`RagRetriever` for memory hits. ``temporal_type`` is
    # always set (defaults to ``'durable'``) for resolved memory rows;
    # ``event_time`` and ``relevance_until`` are ``None`` when the
    # extractor didn't or couldn't anchor a wall-clock moment to the
    # memory. All three stay ``None`` for non-memory hits and for
    # legacy pre-v10 rows that haven't been migrated yet.
    temporal_type: str | None = None
    event_time: str | None = None
    relevance_until: str | None = None
    # K7 — memory tier joined from the SQLite mirror for memory hits so
    # ``RagRetriever.format_block`` can stamp a "(faded)" suffix on
    # ``archive``-tier rows. ``None`` for non-memory hits or when the
    # join did not resolve.
    memory_tier: str | None = None
    # K25 — pinned flag joined from the SQLite mirror so the
    # ``(distant)`` time-decay suffix can bypass user-trusted rows
    # without re-reading SQLite. ``None`` for non-memory hits or when
    # the join did not resolve (treated as "not pinned" by the
    # downstream predicate, since the helper only fires on an explicit
    # truthy ``pinned``).
    memory_pinned: bool | None = None
    # F10c — topic multi-hop expansion flag. ``True`` for sibling
    # memories pulled from the same topic cluster as a strong query hit
    # (rather than matched directly on cosine). ``RagRetriever.format_block``
    # routes these into a separate "Related notes from the same topic"
    # section so Aiko reads them as associative context, not direct recall.
    expansion: bool = False

    @property
    def text(self) -> str:
        return self.record.content


# ── Schemas ─────────────────────────────────────────────────────────────────


def _memories_schema(dim: int) -> pa.Schema:
    return pa.schema([
        pa.field("id", pa.string()),
        pa.field("content", pa.string()),
        pa.field("kind", pa.string()),
        pa.field("salience", pa.float32()),
        pa.field("source_session", pa.string()),
        pa.field("source_message_id", pa.int64()),
        pa.field("created_at", pa.string()),
        pa.field("last_used_at", pa.string()),
        pa.field("use_count", pa.int64()),
        pa.field("vector", pa.list_(pa.float32(), dim)),
    ])


def _messages_schema(dim: int) -> pa.Schema:
    return pa.schema([
        pa.field("id", pa.string()),
        pa.field("session_id", pa.string()),
        pa.field("message_id", pa.int64()),
        pa.field("role", pa.string()),
        pa.field("content", pa.string()),
        pa.field("created_at", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), dim)),
    ])


def _documents_schema(dim: int) -> pa.Schema:
    return pa.schema([
        pa.field("id", pa.string()),
        pa.field("document_id", pa.string()),
        pa.field("title", pa.string()),
        pa.field("chunk_index", pa.int64()),
        pa.field("content", pa.string()),
        pa.field("created_at", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), dim)),
    ])


# ── Implementation ──────────────────────────────────────────────────────────


class RagStore:
    """Wrapper around a single ``lancedb`` connection with three managed tables.

    All public methods are thread-safe via a coarse lock. Lance is itself
    process-safe but our calling pattern is single-process, mostly read.
    """

    def __init__(
        self,
        root: Path,
        *,
        embedding_model: str,
        vector_dim: int,
    ) -> None:
        if vector_dim <= 0:
            raise ValueError(f"RagStore: invalid vector_dim={vector_dim}")
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._embedding_model = str(embedding_model)
        self._vector_dim = int(vector_dim)
        self._lock = threading.Lock()
        # Lazily opened tables; ``_open_*`` is idempotent.
        self._db = lancedb.connect(str(self._root))
        self._meta_path = self._root / "meta.json"
        self._validate_or_stamp_meta()
        self._memories: Table = self._open_or_create(
            TABLE_MEMORIES, _memories_schema(self._vector_dim)
        )
        self._messages: Table = self._open_or_create(
            TABLE_MESSAGES, _messages_schema(self._vector_dim)
        )
        self._documents: Table = self._open_or_create(
            TABLE_DOCUMENTS, _documents_schema(self._vector_dim)
        )
        log.info(
            "RagStore ready @ %s (model=%s dim=%d)",
            self._root,
            self._embedding_model,
            self._vector_dim,
        )

    # ── meta + schema management ────────────────────────────────────────

    @property
    def vector_dim(self) -> int:
        return self._vector_dim

    @property
    def embedding_model(self) -> str:
        return self._embedding_model

    def _validate_or_stamp_meta(self) -> None:
        """If a meta file exists with a *different* embedding model or dim,
        nuke the data dir and start fresh -- mixing dimensions silently
        breaks search. We log loudly so the operator notices.
        """
        existing: dict[str, Any] = {}
        if self._meta_path.exists():
            try:
                existing = json.loads(self._meta_path.read_text(encoding="utf-8"))
            except Exception:
                log.warning("ragstore meta.json unreadable; rebuilding", exc_info=True)
                existing = {}
        if existing:
            same_model = existing.get("embedding_model") == self._embedding_model
            same_dim = int(existing.get("vector_dim", -1)) == self._vector_dim
            if same_model and same_dim:
                return
            log.warning(
                "RagStore embedding swap detected (%s/%s -> %s/%d); rebuilding tables",
                existing.get("embedding_model"),
                existing.get("vector_dim"),
                self._embedding_model,
                self._vector_dim,
            )
            # Drop every table by removing the directory contents and
            # reconnecting. We keep meta.json after writing the new value.
            for path in list(self._root.iterdir()):
                if path.name == "meta.json":
                    continue
                try:
                    if path.is_dir():
                        import shutil

                        shutil.rmtree(path)
                    else:
                        path.unlink()
                except Exception:
                    log.debug("failed to remove %s", path, exc_info=True)
            self._db = lancedb.connect(str(self._root))
        # Stamp / restamp.
        self._meta_path.write_text(
            json.dumps(
                {
                    "embedding_model": self._embedding_model,
                    "vector_dim": self._vector_dim,
                    "stamped_at": _now_iso(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _open_or_create(self, name: str, schema: pa.Schema) -> Table:
        names = self._existing_table_names()
        if name in names:
            return self._db.open_table(name)
        # Empty table with a fixed schema. Lance requires non-empty data on
        # ``create_table`` unless ``schema=`` is provided; modern lancedb
        # supports the schema-only path.
        return self._db.create_table(name, schema=schema, mode="create")

    def _existing_table_names(self) -> set[str]:
        """Return the set of table names regardless of lancedb version.

        Newer ``list_tables()`` returns a ``ListTablesResponse`` with a
        ``.tables`` attribute; older versions return a plain ``list[str]``.
        """
        try:
            response = self._db.list_tables()  # type: ignore[attr-defined]
            tables = getattr(response, "tables", response)
            return {str(t) for t in tables}
        except (AttributeError, TypeError):
            return set(self._db.table_names())

    # ── shared write helpers ────────────────────────────────────────────

    @staticmethod
    def _norm(vec: Sequence[float] | np.ndarray) -> list[float]:
        arr = np.asarray(vec, dtype=np.float32)
        n = float(np.linalg.norm(arr))
        if n > 0.0:
            arr = arr / n
        return arr.astype(np.float32).tolist()

    def _check_dim(self, vec: Sequence[float]) -> None:
        size = len(vec)
        if size != self._vector_dim:
            raise ValueError(
                f"RagStore: vector dim mismatch (got {size}, expected {self._vector_dim})"
            )

    # ── memories ────────────────────────────────────────────────────────

    def add_memory(
        self,
        *,
        record_id: str,
        content: str,
        kind: str,
        embedding: Sequence[float],
        salience: float = 0.5,
        source_session: str | None = None,
        source_message_id: int | None = None,
        created_at: str | None = None,
    ) -> None:
        cleaned = (content or "").strip()
        if not cleaned:
            return
        self._check_dim(embedding)
        row = {
            "id": str(record_id),
            "content": cleaned,
            "kind": (kind or "fact").strip().lower() or "fact",
            "salience": float(max(0.0, min(1.0, salience))),
            "source_session": source_session,
            "source_message_id": (
                int(source_message_id) if source_message_id is not None else None
            ),
            "created_at": created_at or _now_iso(),
            "last_used_at": None,
            "use_count": 0,
            "vector": self._norm(embedding),
        }
        with self._lock:
            # Upsert by deleting existing with same id then adding.
            self._memories.delete(f"id = '{row['id']}'")
            self._memories.add([row])

    def add_memories_bulk(
        self,
        records: Iterable[Mapping[str, Any]],
        *,
        chunk_size: int = 500,
    ) -> int:
        """Upsert many memories in batched delete + add ops.

        Each record dict needs the same fields :meth:`add_memory`
        takes (``record_id``, ``content``, ``kind``, ``embedding``,
        ``salience``, ``source_session``, ``source_message_id``,
        ``created_at``). Empty-content rows are skipped silently —
        same behaviour as the per-row path. Vectors are normalised
        and dim-checked once per row up front; nothing is written if
        any row fails validation. Returns the number of rows written.

        Each chunk lands in two LanceDB ops: one ``delete`` with an
        ``id IN (...)`` predicate, one ``add`` with the row batch.
        That collapses the per-row delete+add storm in
        :meth:`MemoryStore.migrate_to_rag` from O(2*N) write ops to
        O(2*ceil(N/chunk_size)). ``chunk_size`` keeps the SQL
        predicate length bounded for very large memory stores.
        """
        rows: list[dict[str, Any]] = []
        for record in records:
            content = (str(record.get("content") or "")).strip()
            if not content:
                continue
            embedding = record.get("embedding")
            if embedding is None:
                continue
            self._check_dim(embedding)
            rid = str(record.get("record_id") or "").strip()
            if not rid:
                continue
            source_message_id = record.get("source_message_id")
            rows.append({
                "id": rid,
                "content": content,
                "kind": (str(record.get("kind") or "fact").strip().lower() or "fact"),
                "salience": float(max(0.0, min(1.0, float(record.get("salience", 0.5))))),
                "source_session": record.get("source_session"),
                "source_message_id": (
                    int(source_message_id) if source_message_id is not None else None
                ),
                "created_at": record.get("created_at") or _now_iso(),
                "last_used_at": None,
                "use_count": 0,
                "vector": self._norm(embedding),
            })
        if not rows:
            return 0
        chunk = max(1, int(chunk_size))
        with self._lock:
            for start in range(0, len(rows), chunk):
                batch = rows[start:start + chunk]
                ids_csv = ", ".join(
                    "'" + r["id"].replace("'", "''") + "'" for r in batch
                )
                # Delete any existing rows with these ids in one op,
                # then bulk-insert the batch. LanceDB writes a single
                # fragment per ``add`` call, so we land at most two
                # write ops per chunk regardless of batch size.
                self._memories.delete(f"id IN ({ids_csv})")
                self._memories.add(batch)
        return len(rows)

    def delete_memory(self, record_id: str) -> None:
        with self._lock:
            self._memories.delete(f"id = '{record_id}'")

    def search_memories(
        self,
        query_embedding: Sequence[float],
        *,
        top_k: int = 6,
        min_score: float = 0.4,
        kinds: Iterable[str] | None = None,
    ) -> list[RagHit]:
        return self._search_table(
            self._memories,
            query_embedding,
            top_k=top_k,
            min_score=min_score,
            source="memory",
            row_factory=MemoryRecord.from_row,
            extra_filter=_kinds_filter(kinds),
            salience_boost=True,
        )

    # ── messages ────────────────────────────────────────────────────────

    def add_message(
        self,
        *,
        session_id: str,
        message_id: int,
        role: str,
        content: str,
        embedding: Sequence[float],
        created_at: str | None = None,
    ) -> None:
        cleaned = (content or "").strip()
        if not cleaned:
            return
        self._check_dim(embedding)
        row = {
            "id": _message_row_id(session_id, message_id),
            "session_id": str(session_id),
            "message_id": int(message_id),
            "role": str(role),
            "content": cleaned,
            "created_at": created_at or _now_iso(),
            "vector": self._norm(embedding),
        }
        with self._lock:
            self._messages.delete(f"id = '{row['id']}'")
            self._messages.add([row])

    def has_message(self, session_id: str, message_id: int) -> bool:
        rid = _message_row_id(session_id, message_id)
        with self._lock:
            try:
                df = (
                    self._messages.search()
                    .where(f"id = '{rid}'", prefilter=True)
                    .limit(1)
                    .to_list()
                )
            except Exception:
                return False
        return bool(df)

    def list_recent_user_vectors(
        self,
        *,
        user_id_prefix: str,
        limit: int = 12,
    ) -> list[np.ndarray]:
        """Return the last ``limit`` user-message vectors (most recent first).

        Used by the K6 novelty detector to warm its rolling-centroid
        ring buffer from past sessions of the same user. The scan
        pulls only the columns we need (``role``, ``session_id``,
        ``created_at``, ``vector``) via PyArrow so we don't drag the
        ``content`` payload through memory for hundreds of rows.

        Filtering:

        - ``role == 'user'``.
        - ``session_id`` starts with ``f"{user_id_prefix}:"`` when
          ``user_id_prefix`` is provided. Empty / falsy prefix
          matches all sessions (single-user installs).

        The returned vectors are already L2-normalized -- ``add_message``
        applies :meth:`_norm` before write -- so callers can dot them
        directly without renormalisation.

        P5/P23: the ``role`` / session-prefix filter is pushed into the
        Lance scan (``where(..., prefilter=True)``) and only the three
        columns we actually read are projected -- so the ``content``
        payload and every assistant / other-user row never leave disk.
        On an aged corpus this turns a full-table materialisation
        (which sat on the K6 warm-up and the K28 "welcome back" turn)
        into a filtered scan over just this user's messages. Falls back
        to the legacy full ``to_arrow`` path if the predicate query
        raises (older Lance builds / odd schemas).
        """
        cap = max(1, int(limit))
        prefix = (user_id_prefix or "").strip()
        predicate = "role = 'user'"
        if prefix:
            safe = prefix.replace("'", "''")
            predicate = f"{predicate} AND session_id LIKE '{safe}:%'"
        with self._lock:
            try:
                table = (
                    self._messages.search()
                    .where(predicate, prefilter=True)
                    .select(["created_at", "vector"])
                    .to_arrow()
                )
            except Exception:
                log.debug(
                    "list_recent_user_vectors filtered scan failed; "
                    "falling back to full scan",
                    exc_info=True,
                )
                table = self._recent_user_vectors_fallback(prefix)
                if table is None:
                    return []
        if table.num_rows == 0:
            return []
        created_ats = table.column("created_at").to_pylist()
        vectors = table.column("vector").to_pylist()
        rows: list[tuple[str, np.ndarray]] = []
        for created_at, vec in zip(created_ats, vectors):
            if vec is None:
                continue
            try:
                arr = np.asarray(vec, dtype=np.float32)
            except Exception:
                continue
            if arr.size == 0:
                continue
            rows.append((str(created_at or ""), arr))
        rows.sort(key=lambda r: r[0], reverse=True)
        return [arr for _, arr in rows[:cap]]

    def _recent_user_vectors_fallback(self, prefix: str):
        """Legacy full-table scan for :meth:`list_recent_user_vectors`.

        Only reached when the pushed-down predicate query raises. Returns
        a PyArrow table projected to ``created_at`` / ``vector`` with the
        ``role`` / prefix filter applied row-wise, or ``None`` on failure.
        """
        try:
            full = self._messages.to_arrow().select(
                ["role", "session_id", "created_at", "vector"],
            )
        except Exception:
            log.debug(
                "list_recent_user_vectors fallback to_arrow failed",
                exc_info=True,
            )
            return None
        if full.num_rows == 0:
            return full.select(["created_at", "vector"])
        import pyarrow.compute as pc

        mask = pc.equal(full.column("role"), "user")
        if prefix:
            scope = f"{prefix}:"
            starts = pc.starts_with(full.column("session_id"), scope)
            mask = pc.and_(mask, starts)
        return full.filter(mask).select(["created_at", "vector"])

    def search_messages(
        self,
        query_embedding: Sequence[float],
        *,
        top_k: int = 6,
        min_score: float = 0.4,
        session_id: str | None = None,
    ) -> list[RagHit]:
        extra = None
        if session_id:
            safe = session_id.replace("'", "''")
            extra = f"session_id = '{safe}'"
        return self._search_table(
            self._messages,
            query_embedding,
            top_k=top_k,
            min_score=min_score,
            source="message",
            row_factory=MessageRecord.from_row,
            extra_filter=extra,
        )

    # ── documents ───────────────────────────────────────────────────────

    def add_document_chunk(
        self,
        *,
        document_id: str,
        title: str,
        chunk_index: int,
        content: str,
        embedding: Sequence[float],
        created_at: str | None = None,
    ) -> None:
        cleaned = (content or "").strip()
        if not cleaned:
            return
        self._check_dim(embedding)
        row = {
            "id": f"{document_id}:{chunk_index}",
            "document_id": str(document_id),
            "title": str(title),
            "chunk_index": int(chunk_index),
            "content": cleaned,
            "created_at": created_at or _now_iso(),
            "vector": self._norm(embedding),
        }
        with self._lock:
            self._documents.delete(f"id = '{row['id']}'")
            self._documents.add([row])

    def delete_document(self, document_id: str) -> None:
        safe = document_id.replace("'", "''")
        with self._lock:
            self._documents.delete(f"document_id = '{safe}'")

    def list_documents(self) -> list[dict[str, Any]]:
        """Return one row per unique ``document_id`` with title + chunk count.

        We pull the ``document_id`` / ``title`` / ``created_at`` columns via
        PyArrow so the runtime doesn't need pandas just for this aggregation.
        """
        with self._lock:
            try:
                table = self._documents.to_arrow().select(
                    ["document_id", "title", "created_at"],
                )
            except Exception:
                log.warning("list_documents to_arrow failed", exc_info=True)
                return []
        if table.num_rows == 0:
            return []
        doc_ids = table.column("document_id").to_pylist()
        titles = table.column("title").to_pylist()
        created_ats = table.column("created_at").to_pylist()
        agg: dict[str, dict[str, Any]] = {}
        for doc_id, title, created_at in zip(doc_ids, titles, created_ats):
            if doc_id is None:
                continue
            key = str(doc_id)
            entry = agg.get(key)
            if entry is None:
                agg[key] = {
                    "document_id": key,
                    "title": str(title or ""),
                    "chunk_count": 1,
                    "created_at": str(created_at or ""),
                }
            else:
                entry["chunk_count"] += 1
                # Track the earliest timestamp for stable ordering.
                ca = str(created_at or "")
                if ca and (not entry["created_at"] or ca < entry["created_at"]):
                    entry["created_at"] = ca
        out = list(agg.values())
        out.sort(key=lambda r: r["created_at"], reverse=True)
        return out

    def search_documents(
        self,
        query_embedding: Sequence[float],
        *,
        top_k: int = 6,
        min_score: float = 0.4,
    ) -> list[RagHit]:
        return self._search_table(
            self._documents,
            query_embedding,
            top_k=top_k,
            min_score=min_score,
            source="document",
            row_factory=DocumentChunk.from_row,
        )

    # ── shared search core ──────────────────────────────────────────────

    def _search_table(
        self,
        table: Table,
        query_embedding: Sequence[float],
        *,
        top_k: int,
        min_score: float,
        source: str,
        row_factory: Any,
        extra_filter: str | None = None,
        salience_boost: bool = False,
    ) -> list[RagHit]:
        if top_k <= 0:
            return []
        self._check_dim(query_embedding)
        q = self._norm(query_embedding)
        with self._lock:
            try:
                builder = table.search(q).metric("cosine").limit(int(top_k * 2 + 4))
                if extra_filter:
                    builder = builder.where(extra_filter, prefilter=True)
                rows = builder.to_list()
            except Exception:
                log.debug("search failed on %s", source, exc_info=True)
                return []
        hits: list[RagHit] = []
        for row in rows:
            # Lance returns ``_distance`` (cosine distance in [0, 2]); convert
            # to similarity in [-1, 1] which we then clamp.
            distance = float(row.get("_distance", 0.0))
            similarity = max(-1.0, min(1.0, 1.0 - distance))
            if similarity < min_score:
                continue
            score = similarity
            if salience_boost:
                # Mirrors MemoryStore.search: tiny salience nudge so two
                # near-equal hits prefer the more important one.
                sal = float(row.get("salience", 0.5))
                score = similarity + 0.05 * (sal - 0.5)
            try:
                rec = row_factory(row)
            except Exception:
                log.debug("row_factory failed on %s", source, exc_info=True)
                continue
            hits.append(RagHit(source=source, score=score, record=rec))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[: int(top_k)]

    def knn_memories(
        self,
        query_embedding: Sequence[float],
        *,
        top_k: int,
        min_score: float = 0.0,
        exclude_id: str | None = None,
    ) -> list[tuple[int, float]]:
        """Return ``[(memory_id, cosine_sim), ...]`` nearest to the query.

        A lean nearest-neighbour helper for the topic-graph clustering
        paths (incremental assignment + batch k-NN graph build) that
        skips the ``RagHit`` / SQLite-join machinery of
        :meth:`search_memories`. Uses whatever Lance index is present
        (ANN when :meth:`ensure_vector_index` has built one, flat scan
        otherwise) so it scales without the caller changing. ``exclude_id``
        drops the query memory itself when finding a node's neighbours.
        """
        if top_k <= 0:
            return []
        self._check_dim(query_embedding)
        q = self._norm(query_embedding)
        want = int(top_k) + (1 if exclude_id is not None else 0)
        with self._lock:
            try:
                rows = (
                    self._memories.search(q)
                    .metric("cosine")
                    .limit(want + 2)
                    .to_list()
                )
            except Exception:
                log.debug("knn_memories search failed", exc_info=True)
                return []
        out: list[tuple[int, float]] = []
        for row in rows:
            rid = str(row.get("id", ""))
            if exclude_id is not None and rid == exclude_id:
                continue
            distance = float(row.get("_distance", 0.0))
            similarity = max(-1.0, min(1.0, 1.0 - distance))
            if similarity < min_score:
                continue
            try:
                out.append((int(rid), similarity))
            except (TypeError, ValueError):
                continue
            if len(out) >= int(top_k):
                break
        return out

    def ensure_vector_index(self, *, min_rows: int = 256) -> bool:
        """Build an ANN index on the ``memories`` vector column once the
        table is large enough to benefit.

        Below ``min_rows`` a flat (brute-force) cosine scan is faster and
        more accurate than IVF_PQ, so we skip. Idempotent + best-effort:
        any failure (old lancedb, index already present, too few rows for
        the chosen partition count) is swallowed and we simply keep the
        flat path. Returns ``True`` when an index now exists / was built.

        Safe to call repeatedly (e.g. after a bulk knowledge ingest); the
        ``replace=False`` default means a second call is a cheap no-op
        once the index exists.
        """
        with self._lock:
            try:
                rows = self._memories.count_rows()
            except Exception:
                return False
            if rows < int(min_rows):
                return False
            try:
                # Modern lancedb picks sensible IVF_PQ params from the row
                # count when called bare; the metric must match the search
                # path (cosine).
                self._memories.create_index(metric="cosine")
                log.info("RagStore: built ANN index on memories (rows=%d)", rows)
                return True
            except TypeError:
                try:
                    self._memories.create_index()
                    log.info(
                        "RagStore: built ANN index on memories (rows=%d, default metric)",
                        rows,
                    )
                    return True
                except Exception:
                    log.debug("ensure_vector_index fallback failed", exc_info=True)
                    return False
            except Exception:
                # Most commonly "index already exists" or "not enough rows
                # for N partitions" -- both are fine, keep flat search.
                log.debug("ensure_vector_index skipped", exc_info=True)
                return False

    # ── stats / maintenance ─────────────────────────────────────────────

    def counts(self) -> dict[str, int]:
        with self._lock:
            try:
                return {
                    TABLE_MEMORIES: self._memories.count_rows(),
                    TABLE_MESSAGES: self._messages.count_rows(),
                    TABLE_DOCUMENTS: self._documents.count_rows(),
                }
            except Exception:
                return {TABLE_MEMORIES: 0, TABLE_MESSAGES: 0, TABLE_DOCUMENTS: 0}

    def delete_messages_for_session(self, session_id: str) -> None:
        safe = session_id.replace("'", "''")
        with self._lock:
            self._messages.delete(f"session_id = '{safe}'")

    def close(self) -> None:
        # LanceDB doesn't require an explicit close, but we drop our handles
        # so subsequent calls fail fast instead of silently using a stale db.
        with self._lock:
            self._db = None  # type: ignore[assignment]


# ── helpers ─────────────────────────────────────────────────────────────────


def _kinds_filter(kinds: Iterable[str] | None) -> str | None:
    if not kinds:
        return None
    cleaned = [str(k).strip().lower() for k in kinds if str(k).strip()]
    if not cleaned:
        return None
    quoted = ", ".join(f"'{k}'" for k in cleaned)
    return f"kind IN ({quoted})"


def _message_row_id(session_id: str, message_id: int) -> str:
    return f"{session_id}:{int(message_id)}"


def auto_open(
    root: Path,
    *,
    embedder_model: str,
    embedder_probe: Any,
) -> "RagStore | None":
    """Open the RagStore, probing the current embedding dim from the embedder.

    Returns ``None`` if the embedder fails -- callers should treat that as
    "RAG disabled" and skip retrieval.
    """
    try:
        probe_vec = embedder_probe.embed("Aiko")
        dim = int(np.asarray(probe_vec, dtype=np.float32).size)
    except Exception:
        log.warning("RagStore probe failed; disabling RAG", exc_info=True)
        return None
    try:
        return RagStore(root, embedding_model=embedder_model, vector_dim=dim)
    except Exception:
        log.exception("RagStore failed to initialize")
        return None
