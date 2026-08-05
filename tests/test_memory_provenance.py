"""F16 (schema v30) testimony-vs-inference provenance tests.

Covers:
- ``memories.provenance`` lands (fresh CREATE + v29 -> v30 ALTER, default
  ``inferred``).
- ``Memory.provenance`` round-trips through ``add``; default + coercion;
  ``to_dict`` carries it; the mirror reload preserves it.
- ``MemoryExtractor._validate_entries`` coerces provenance (missing /
  unknown -> ``inferred``) and the system prompt teaches the field.
- ``RagRetriever._provenance_penalty`` math + that a ``stated`` hit
  outranks an ``inferred`` one at equal cosine.
- ``RagRetriever.format_block`` appends ``(inferred)`` only on durable
  user-fact kinds, never on ``self`` / ``self_tagged``, and the master
  toggle suppresses it.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from app.core.infra.chat_database import _SCHEMA_VERSION, ChatDatabase
from app.core.memory.memory_extractor import MemoryExtractor, _build_system_prompt
from app.core.memory.memory_store import (
    _DEFAULT_PROVENANCE,
    VALID_PROVENANCE,
    MemoryStore,
    _coerce_provenance,
)


class _FakeEmbedder:
    DIM = 16

    def embed(self, text: str) -> np.ndarray:
        rng = np.random.default_rng(seed=hash(text) & 0xFFFFFFFF)
        v = rng.normal(size=self.DIM).astype(np.float32)
        v /= max(1e-6, float(np.linalg.norm(v)))
        return v


def _store_factory() -> "tuple[Path, MemoryStore]":
    d = tempfile.mkdtemp()
    path = Path(d) / "mem.db"
    ChatDatabase(path)
    store = MemoryStore(path)
    return path, store


def _emb(text: str) -> np.ndarray:
    return _FakeEmbedder().embed(text)


# The v29 ``memories`` shape (everything through the v10 temporal columns,
# but without the v30 ``provenance`` column). Used to exercise the
# v29 -> v30 ALTER + backfill.
_V29_MEMORIES_CREATE = """
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    kind TEXT NOT NULL,
    salience REAL NOT NULL DEFAULT 0.5,
    embedding BLOB NOT NULL,
    source_session TEXT,
    source_message_id INTEGER,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    use_count INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    metadata TEXT,
    tier TEXT NOT NULL DEFAULT 'long_term',
    revival_score REAL NOT NULL DEFAULT 0.0,
    confidence REAL NOT NULL DEFAULT 0.7,
    event_time TEXT,
    temporal_type TEXT NOT NULL DEFAULT 'durable',
    relevance_until TEXT
);
"""


class TestSchemaMigration(unittest.TestCase):
    def test_schema_version_is_at_least_v30(self) -> None:
        # provenance landed in v30; later migrations are additive so we
        # only assert the floor, never lock the version literal.
        self.assertGreaterEqual(_SCHEMA_VERSION, 30)

    def test_fresh_database_has_provenance_column(self) -> None:
        path, _store = _store_factory()
        conn = sqlite3.connect(str(path))
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
        finally:
            conn.close()
        self.assertIn("provenance", cols)

    def test_v29_to_v30_alter_lands_default_inferred(self) -> None:
        d = tempfile.mkdtemp()
        path = Path(d) / "v29.db"
        conn = sqlite3.connect(str(path))
        try:
            conn.executescript(
                "CREATE TABLE schema_version (version INTEGER NOT NULL);\n"
                "INSERT INTO schema_version (version) VALUES (29);\n"
                + _V29_MEMORIES_CREATE
            )
            conn.execute(
                "INSERT INTO memories "
                "(content, kind, salience, embedding, created_at) "
                "VALUES ('legacy row', 'fact', 0.6, X'00', "
                "'2026-01-01T00:00:00Z')"
            )
            conn.commit()
        finally:
            conn.close()

        # Opening through ChatDatabase runs _init_schema which performs the
        # guarded v29 -> v30 ALTER + backfill.
        ChatDatabase(path)

        conn = sqlite3.connect(str(path))
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
            self.assertIn("provenance", cols)
            row = conn.execute(
                "SELECT content, provenance FROM memories ORDER BY id"
            ).fetchone()
            version = conn.execute(
                "SELECT version FROM schema_version"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(version, _SCHEMA_VERSION)
        self.assertEqual(row[0], "legacy row")
        # Legacy rows backfill to the safe ``inferred`` default.
        self.assertEqual(row[1], "inferred")


class TestCoerceProvenance(unittest.TestCase):
    def test_valid_values_pass_through(self) -> None:
        self.assertEqual(_coerce_provenance("stated"), "stated")
        self.assertEqual(_coerce_provenance("inferred"), "inferred")

    def test_case_and_whitespace_normalised(self) -> None:
        self.assertEqual(_coerce_provenance("  STATED "), "stated")

    def test_none_and_unknown_default_to_inferred(self) -> None:
        self.assertEqual(_coerce_provenance(None), _DEFAULT_PROVENANCE)
        self.assertEqual(_coerce_provenance("confirmed"), "inferred")
        self.assertEqual(_coerce_provenance(""), "inferred")

    def test_default_constant_is_inferred(self) -> None:
        self.assertEqual(_DEFAULT_PROVENANCE, "inferred")
        self.assertEqual(set(VALID_PROVENANCE), {"stated", "inferred"})

    def test_non_string_raises(self) -> None:
        with self.assertRaises(TypeError):
            _coerce_provenance(123)  # type: ignore[arg-type]


class TestProvenanceRoundTrip(unittest.TestCase):
    def test_default_provenance_is_inferred(self) -> None:
        _, store = _store_factory()
        mem = store.add("a plain fact", "fact", _emb("plain fact"))
        assert mem is not None
        self.assertEqual(mem.provenance, "inferred")

    def test_stated_round_trips(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            "he stated this", "fact", _emb("stated"), provenance="stated"
        )
        assert mem is not None
        self.assertEqual(mem.provenance, "stated")
        # And it survives a fresh mirror load from the same DB.
        reread = store.get(mem.id)
        assert reread is not None
        self.assertEqual(reread.provenance, "stated")

    def test_unknown_provenance_coerces_to_inferred(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            "weird prov", "fact", _emb("weird"), provenance="guessed"
        )
        assert mem is not None
        self.assertEqual(mem.provenance, "inferred")

    def test_reload_preserves_provenance(self) -> None:
        path, store = _store_factory()
        mem = store.add(
            "durable stated fact", "fact", _emb("durable"), provenance="stated"
        )
        assert mem is not None
        mem_id = mem.id
        # A brand-new store over the same DB reads the column back.
        fresh = MemoryStore(path)
        reread = fresh.get(mem_id)
        assert reread is not None
        self.assertEqual(reread.provenance, "stated")

    def test_to_dict_includes_provenance(self) -> None:
        _, store = _store_factory()
        mem = store.add(
            "a thing", "fact", _emb("thing"), provenance="stated"
        )
        assert mem is not None
        snapshot = mem.to_dict()
        self.assertIn("provenance", snapshot)
        self.assertEqual(snapshot["provenance"], "stated")


class TestExtractorProvenance(unittest.TestCase):
    def _extractor(self):
        # _validate_entries doesn't touch db/store/embedder/ollama.
        return MemoryExtractor(object(), object(), object(), object(), model="x")

    def test_prompt_teaches_provenance(self) -> None:
        prompt = _build_system_prompt("Jacob")
        self.assertIn("provenance", prompt)
        self.assertIn("stated", prompt)
        self.assertIn("inferred", prompt)

    def test_valid_provenance_preserved(self) -> None:
        ex = self._extractor()
        out = ex._validate_entries(
            [{"content": "Jacob said he is vegetarian", "kind": "fact",
              "provenance": "stated"}]
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["provenance"], "stated")

    def test_missing_provenance_defaults_to_inferred(self) -> None:
        ex = self._extractor()
        out = ex._validate_entries(
            [{"content": "Jacob seems to like jazz", "kind": "preference"}]
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["provenance"], "inferred")

    def test_unknown_provenance_coerces_to_inferred(self) -> None:
        ex = self._extractor()
        out = ex._validate_entries(
            [{"content": "Jacob probably works late", "kind": "fact",
              "provenance": "hunch"}]
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["provenance"], "inferred")


class TestProvenancePenalty(unittest.TestCase):
    def test_inferred_is_penalised(self) -> None:
        from app.core.rag.rag_retriever import (
            _MEMORY_PROVENANCE_PENALTY,
            _provenance_penalty,
        )

        self.assertAlmostEqual(
            _provenance_penalty("inferred"), -_MEMORY_PROVENANCE_PENALTY, places=5
        )

    def test_stated_and_unknown_and_none_are_zero(self) -> None:
        from app.core.rag.rag_retriever import _provenance_penalty

        self.assertAlmostEqual(_provenance_penalty("stated"), 0.0, places=5)
        self.assertAlmostEqual(_provenance_penalty(None), 0.0, places=5)
        self.assertAlmostEqual(_provenance_penalty("confirmed"), 0.0, places=5)

    def test_stated_outranks_inferred_at_equal_cosine(self) -> None:
        from app.core.rag.rag_retriever import _provenance_penalty

        cosine = 0.62
        stated_score = cosine + _provenance_penalty("stated")
        inferred_score = cosine + _provenance_penalty("inferred")
        self.assertGreater(stated_score, inferred_score)


def _mem_hit(content: str, kind: str, provenance: str | None):
    from app.core.rag.rag_store import MemoryRecord, RagHit

    now_iso = datetime.now(timezone.utc).isoformat()
    record = MemoryRecord(
        id="1",
        content=content,
        kind=kind,
        salience=0.9,
        source_session=None,
        source_message_id=None,
        created_at=now_iso,
        last_used_at=now_iso,
        use_count=1,
    )
    return RagHit(
        source="memory",
        score=0.5,
        record=record,
        confidence=0.9,
        memory_tier="long_term",
        memory_pinned=False,
        memory_provenance=provenance,
    )


class TestFormatBlockInferredSuffix(unittest.TestCase):
    def _block(self, hits, *, enabled: bool = True) -> str:
        from app.core.rag.rag_retriever import RagRetriever

        return RagRetriever.format_block(
            hits,
            user_display_name="Friend",
            memory_provenance_enabled=enabled,
        )

    def test_inferred_fact_gets_suffix(self) -> None:
        block = self._block([_mem_hit("Jacob prefers late nights", "fact", "inferred")])
        self.assertIn("Jacob prefers late nights (inferred)", block)

    def test_stated_fact_has_no_suffix(self) -> None:
        block = self._block([_mem_hit("Jacob lives in Prague", "fact", "stated")])
        self.assertIn("- Jacob lives in Prague", block)
        self.assertNotIn("(inferred)", block)

    def test_missing_provenance_has_no_suffix(self) -> None:
        # ``None`` (unresolved join) is treated as the unmarked default.
        block = self._block([_mem_hit("Jacob has a cat", "fact", None)])
        self.assertNotIn("(inferred)", block)

    def test_self_kind_never_tagged(self) -> None:
        block = self._block([_mem_hit("I enjoy quiet mornings", "self", "inferred")])
        self.assertNotIn("(inferred)", block)

    def test_toggle_off_suppresses_suffix(self) -> None:
        block = self._block(
            [_mem_hit("Jacob prefers late nights", "fact", "inferred")],
            enabled=False,
        )
        self.assertNotIn("(inferred)", block)


if __name__ == "__main__":
    unittest.main()
