"""User-uploaded document ingestion pipeline.

Accepts ``.md``, ``.txt``, and ``.pdf`` files; chunks them into ~1k char
windows with light overlap; embeds each chunk through the shared
:class:`Embedder`; and stores the embeddings in the ``documents`` LanceDB
table via :class:`RagStore`.

Surface area is narrow on purpose: one ``ingest()`` entry point, plus
``list_documents()`` / ``delete_document()`` / ``ensure_storage_dir()``.
The web layer (``app/web/server.py``) wraps these in REST endpoints.

Original document files are persisted under ``data/documents/<doc_id>/``
so we can support re-indexing on embedding-model swap (Phase C wipes the
RagStore tables when the dim changes; documents are then rebuilt on demand
from the source files).
"""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from app.core.rag.rag_store import RagStore
    from app.llm.embedder import Embedder


log = logging.getLogger("app.document_ingestor")


SUPPORTED_EXTENSIONS = {".md", ".markdown", ".txt", ".pdf"}

# Tunable. ~1k chars per chunk strikes a balance between embedding quality
# (longer = more context per vector) and retrieval granularity (shorter =
# higher chance an irrelevant paragraph isn't surfaced).
_CHUNK_SIZE = 1000
_CHUNK_OVERLAP = 120
_MAX_CHUNK_BYTES = 256 * 1024  # safety: refuse absurd single-paragraph blocks


@dataclass(slots=True)
class IngestResult:
    document_id: str
    title: str
    chunk_count: int
    bytes_indexed: int


class DocumentIngestor:
    def __init__(
        self,
        rag: "RagStore",
        embedder: "Embedder",
        *,
        storage_root: Path,
    ) -> None:
        self._rag = rag
        self._embedder = embedder
        self._storage = Path(storage_root)
        self._storage.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ── public API ───────────────────────────────────────────────────────

    def ingest(self, *, filename: str, data: bytes) -> IngestResult:
        if not filename:
            raise ValueError("filename is required")
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise ValueError(
                f"Unsupported file type {suffix!r}; allowed: "
                + ", ".join(sorted(SUPPORTED_EXTENSIONS))
            )
        if not data:
            raise ValueError("file is empty")

        doc_id = _generate_document_id(filename, data)
        title = Path(filename).stem.strip() or "untitled"

        # Persist the source file so we can re-ingest later if needed.
        with self._lock:
            doc_dir = self._storage / doc_id
            doc_dir.mkdir(parents=True, exist_ok=True)
            (doc_dir / Path(filename).name).write_bytes(data)

        text = _extract_text(suffix, data, filename)
        if not text.strip():
            raise ValueError("extracted no readable text from file")

        chunks = list(_chunk_text(text))
        if not chunks:
            raise ValueError("file produced zero chunks (text too short?)")

        # Fresh upload of the same doc_id replaces any previous chunks.
        try:
            self._rag.delete_document(doc_id)
        except Exception:
            log.debug("rag delete_document failed for %s", doc_id, exc_info=True)

        for idx, chunk in enumerate(chunks):
            try:
                vec = self._embedder.embed(chunk)
            except Exception:
                log.warning(
                    "embedder failed on chunk %d of %s; skipping", idx, filename,
                    exc_info=True,
                )
                continue
            try:
                self._rag.add_document_chunk(
                    document_id=doc_id,
                    title=title,
                    chunk_index=idx,
                    content=chunk,
                    embedding=vec,
                )
            except Exception:
                log.warning(
                    "rag add_document_chunk failed for %s/%d", doc_id, idx,
                    exc_info=True,
                )
        log.info("ingested document %s (%d chunks)", doc_id, len(chunks))
        return IngestResult(
            document_id=doc_id,
            title=title,
            chunk_count=len(chunks),
            bytes_indexed=sum(len(c.encode("utf-8")) for c in chunks),
        )

    def list_documents(self) -> list[dict[str, object]]:
        return self._rag.list_documents()

    def delete_document(self, document_id: str) -> bool:
        if not document_id:
            return False
        try:
            self._rag.delete_document(document_id)
        except Exception:
            log.debug("rag delete_document failed", exc_info=True)
        with self._lock:
            doc_dir = self._storage / document_id
            if doc_dir.exists():
                try:
                    shutil.rmtree(doc_dir)
                except Exception:
                    log.debug("doc dir cleanup failed for %s", doc_dir, exc_info=True)
                    return False
        return True


# ── helpers ─────────────────────────────────────────────────────────────────


def _generate_document_id(filename: str, data: bytes) -> str:
    """Stable per-content id with a uuid suffix to allow re-uploads.

    Same content + filename hashes to the same prefix, but uniqueness is
    guaranteed by the uuid suffix so a re-upload doesn't silently overwrite
    a previous one.
    """
    h = hashlib.sha1()
    h.update(filename.encode("utf-8", errors="ignore"))
    h.update(b"\x00")
    h.update(data[: 64 * 1024])  # first 64KB is plenty for fingerprint
    short = h.hexdigest()[:10]
    return f"doc_{short}_{uuid.uuid4().hex[:8]}"


def _extract_text(suffix: str, data: bytes, filename: str) -> str:
    if suffix in (".md", ".markdown", ".txt"):
        return _decode_text(data)
    if suffix == ".pdf":
        return _extract_pdf_text(data, filename)
    return ""


def _decode_text(data: bytes) -> str:
    for enc in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _extract_pdf_text(data: bytes, filename: str) -> str:
    try:
        from io import BytesIO

        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover -- missing optional dep
        raise ValueError(
            "pypdf is required to ingest PDF files; install with `pip install pypdf`"
        ) from exc
    try:
        reader = PdfReader(BytesIO(data))
    except Exception as exc:
        raise ValueError(f"could not open PDF {filename!r}: {exc}") from exc
    parts: list[str] = []
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _chunk_text(text: str) -> Iterable[str]:
    """Split into ~_CHUNK_SIZE-character windows with overlap.

    We try to break on paragraph / sentence boundaries first so chunks make
    semantic sense. If a single paragraph is huge we fall back to a fixed
    sliding-window split.
    """
    cleaned = _normalize_whitespace(text)
    if not cleaned:
        return []
    paragraphs = [p for p in re.split(r"\n{2,}", cleaned) if p.strip()]
    chunks: list[str] = []
    buffer = ""
    for para in paragraphs:
        if len(para.encode("utf-8")) > _MAX_CHUNK_BYTES:
            # Refuse pathological input rather than blowing up the embedder.
            log.warning("skipping chunk: paragraph exceeds %d bytes", _MAX_CHUNK_BYTES)
            continue
        candidate = (buffer + "\n\n" + para).strip() if buffer else para
        if len(candidate) <= _CHUNK_SIZE:
            buffer = candidate
            continue
        # Flush the existing buffer (if any) before starting a new chunk.
        if buffer:
            chunks.append(buffer)
            buffer = ""
        # If the paragraph itself is too big, slide a window over it with
        # overlap so embeddings still capture connecting context.
        if len(para) > _CHUNK_SIZE:
            chunks.extend(_sliding_window(para, _CHUNK_SIZE, _CHUNK_OVERLAP))
            buffer = ""
        else:
            buffer = para
    if buffer:
        chunks.append(buffer)
    return chunks


def _sliding_window(text: str, size: int, overlap: int) -> Iterable[str]:
    if size <= 0:
        return
    step = max(1, size - max(0, overlap))
    n = len(text)
    i = 0
    while i < n:
        yield text[i : i + size]
        if i + size >= n:
            return
        i += step


def _normalize_whitespace(text: str) -> str:
    # Collapse Windows line endings and trailing spaces, but keep paragraph
    # breaks (used by the chunker).
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
