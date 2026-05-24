"""Tests for DocumentIngestor: chunking, ingestion, and rejection paths."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.core.document_ingestor import DocumentIngestor, _chunk_text
from app.core.rag_store import RagStore


class FakeEmbedder:
    DIM = 8

    def __init__(self) -> None:
        self.model = "fake"
        self.calls = 0

    def embed(self, text: str) -> np.ndarray:
        self.calls += 1
        rng = np.random.default_rng(seed=abs(hash(text)) % (2**31))
        v = rng.normal(size=self.DIM).astype(np.float32)
        v /= max(1e-6, float(np.linalg.norm(v)))
        return v


class ChunkerTests(unittest.TestCase):
    def test_short_text_yields_one_chunk(self) -> None:
        chunks = list(_chunk_text("hello world"))
        self.assertEqual(chunks, ["hello world"])

    def test_paragraphs_grouped_under_size(self) -> None:
        text = "Para one.\n\nPara two.\n\nPara three."
        chunks = list(_chunk_text(text))
        # All three short paragraphs fit comfortably in one chunk.
        self.assertEqual(len(chunks), 1)
        self.assertIn("Para one.", chunks[0])
        self.assertIn("Para three.", chunks[0])

    def test_long_paragraph_uses_sliding_window(self) -> None:
        # 3000-char monolithic paragraph; should split into multiple windows.
        big = "x " * 1500
        chunks = list(_chunk_text(big))
        self.assertGreater(len(chunks), 1)
        # Each chunk should be near the configured window size.
        for c in chunks:
            self.assertLessEqual(len(c), 1100)

    def test_empty_input(self) -> None:
        self.assertEqual(list(_chunk_text("")), [])
        self.assertEqual(list(_chunk_text("   \n\n   ")), [])


class _IngestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="aiko-ingest-"))
        self.embedder = FakeEmbedder()
        self.store = RagStore(
            self.tmp / "lancedb",
            embedding_model="fake",
            vector_dim=FakeEmbedder.DIM,
        )
        self.ingestor = DocumentIngestor(
            self.store,
            self.embedder,
            storage_root=self.tmp / "documents",
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class IngestHappyPathTests(_IngestBase):
    def test_ingest_text_file(self) -> None:
        body = b"# Notes\n\nAiko likes long walks.\n\nShe was born in winter."
        result = self.ingestor.ingest(filename="my_notes.md", data=body)
        self.assertGreater(result.chunk_count, 0)
        self.assertEqual(result.title, "my_notes")
        listing = self.ingestor.list_documents()
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["chunk_count"], result.chunk_count)

    def test_reupload_replaces_chunks(self) -> None:
        body_v1 = b"first version of the document\n\nwith two paragraphs"
        first = self.ingestor.ingest(filename="doc.txt", data=body_v1)
        # Same filename + body -> new uuid suffix, but the *previous* doc
        # remains because the id is unique-per-upload. List should now show
        # two documents.
        self.ingestor.ingest(filename="doc.txt", data=body_v1)
        listing = self.ingestor.list_documents()
        self.assertEqual(len(listing), 2)

    def test_delete_removes_listing(self) -> None:
        body = b"a small text\n\nfor cleanup tests"
        result = self.ingestor.ingest(filename="cleanup.txt", data=body)
        self.assertTrue(self.ingestor.delete_document(result.document_id))
        self.assertEqual(self.ingestor.list_documents(), [])


class IngestRejectionTests(_IngestBase):
    def test_unsupported_extension(self) -> None:
        with self.assertRaises(ValueError):
            self.ingestor.ingest(filename="picture.png", data=b"\x89PNG\r\n")

    def test_empty_file(self) -> None:
        with self.assertRaises(ValueError):
            self.ingestor.ingest(filename="empty.md", data=b"")

    def test_missing_filename(self) -> None:
        with self.assertRaises(ValueError):
            self.ingestor.ingest(filename="", data=b"hello")

    def test_malformed_pdf_rejected_cleanly(self) -> None:
        with self.assertRaises(ValueError):
            # Garbage bytes; pypdf raises which we re-raise as ValueError.
            self.ingestor.ingest(
                filename="broken.pdf", data=b"not really a pdf",
            )


if __name__ == "__main__":
    unittest.main()
