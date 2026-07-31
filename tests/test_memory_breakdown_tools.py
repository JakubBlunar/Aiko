"""P29 -- the ``get_memory_breakdown`` MCP surface.

Same `_FakeMCP` pattern as `test_debug_clock_tools.py`: the tool is a
thin reporter, so what matters is that every section survives a hostile
session object, the numbers it does report are right, and the whole thing
still returns usable JSON when ``psutil`` is missing.
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

from app.mcp.server_tools import memory_breakdown_tools as mbt


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _mem(content: str, dim: int = 4) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        embedding=np.zeros(dim, dtype=np.float32),
    )


def _session(**over) -> SimpleNamespace:
    settings = SimpleNamespace(
        stt=SimpleNamespace(
            enabled=True, model="large-v1", device="auto", compute_type="default",
        ),
        tts=SimpleNamespace(enabled=True, provider="pocket", voice="af"),
    )
    base = {
        "_settings": settings,
        "_realtime_stt": None,
        "_tts_engine": None,
        "_memory_store": None,
        "_rag_store": None,
        "_embedder": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


class ToolRegistrationTests(unittest.TestCase):
    def test_registers_and_documents_itself(self) -> None:
        mcp = _FakeMCP()
        mbt.register(mcp, _session())
        self.assertEqual(set(mcp.tools), {"get_memory_breakdown"})
        self.assertTrue((mcp.tools["get_memory_breakdown"].__doc__ or "").strip())

    def test_returns_parseable_json_on_a_bare_session(self) -> None:
        mcp = _FakeMCP()
        mbt.register(mcp, _session())
        payload = json.loads(mcp.tools["get_memory_breakdown"]())
        for section in (
            "process", "stt", "tts", "memory_mirror", "lancedb", "embedder",
        ):
            self.assertIn(section, payload)


class SectionIsolationTests(unittest.TestCase):
    """One broken subsystem must not take the whole report down."""

    def test_a_raising_section_is_reported_not_propagated(self) -> None:
        with mock.patch.object(
            mbt, "mirror_snapshot", side_effect=RuntimeError("boom"),
        ):
            out = mbt.build_breakdown(_session())
        self.assertEqual(out["memory_mirror"], {"error": "boom"})
        self.assertIn("process", out)

    def test_attribute_probing_never_raises_on_a_stripped_session(self) -> None:
        # A session mid-construction has none of these attributes at all.
        out = mbt.build_breakdown(SimpleNamespace())
        self.assertFalse(out["stt"]["service_constructed"])
        self.assertFalse(out["tts"]["engine_constructed"])
        self.assertFalse(out["memory_mirror"]["attached"])


class MirrorAttributionTests(unittest.TestCase):
    def test_counts_rows_vectors_and_caps(self) -> None:
        store = SimpleNamespace(
            _mirror={1: _mem("abc"), 2: _mem("de"), 3: _mem("f")},
            _lock=None,
            _max=5000,
            _tier_caps={"scratchpad": 1000, "long_term": 5000, "archive": 10000},
        )
        out = mbt.mirror_snapshot(_session(_memory_store=store))
        self.assertEqual(out["rows"], 3)
        self.assertEqual(out["rows_with_vector"], 3)
        self.assertEqual(out["vector_dim"], 4)
        self.assertEqual(out["cap_max_memories"], 5000)
        self.assertEqual(out["tier_caps"]["archive"], 10000)

    def test_rows_without_a_vector_are_counted_separately(self) -> None:
        store = SimpleNamespace(
            _mirror={
                1: _mem("has one"),
                2: SimpleNamespace(content="none", embedding=None),
            },
            _lock=None,
        )
        out = mbt.mirror_snapshot(_session(_memory_store=store))
        self.assertEqual(out["rows"], 2)
        self.assertEqual(out["rows_with_vector"], 1)

    def test_takes_the_store_lock_when_present(self) -> None:
        entered = []

        class _Lock:
            def __enter__(self):
                entered.append(True)

            def __exit__(self, *a):
                return False

        store = SimpleNamespace(_mirror={1: _mem("x")}, _lock=_Lock())
        mbt.mirror_snapshot(_session(_memory_store=store))
        self.assertEqual(len(entered), 1)


class EngineAttributionTests(unittest.TestCase):
    def test_tts_reports_loaded_weights_against_the_enabled_flag(self) -> None:
        # The P28 signature: disabled in settings, weights resident anyway.
        engine = SimpleNamespace(_model=object(), _audio_cache={"a": (), "b": ()})
        session = _session(_tts_engine=engine)
        session._settings.tts.enabled = False
        out = mbt.tts_snapshot(session)
        self.assertTrue(out["weights_loaded"])
        self.assertFalse(out["enabled_setting"])
        self.assertEqual(out["audio_cache_entries"], 2)

    def test_tts_unloaded_engine_reports_false(self) -> None:
        out = mbt.tts_snapshot(_session(_tts_engine=SimpleNamespace(_model=None)))
        self.assertFalse(out["weights_loaded"])

    def test_stt_reports_the_lazy_load_state(self) -> None:
        stt = SimpleNamespace(
            is_loaded=False,
            _loaded_model="",
            _loaded_device="",
            _last_error=None,
        )
        out = mbt.stt_snapshot(_session(_realtime_stt=stt))
        self.assertTrue(out["service_constructed"])
        self.assertFalse(out["weights_loaded"])
        self.assertIsNone(out["loaded_model"])

    def test_embedder_reports_lru_occupancy(self) -> None:
        embedder = SimpleNamespace(
            _model="qwen3-embedding:0.6b",
            _cache={"a": 1, "b": 2},
            _cache_size=256,
        )
        out = mbt.embedder_snapshot(_session(_embedder=embedder))
        self.assertEqual(out["cache_entries"], 2)
        self.assertEqual(out["cache_capacity"], 256)


class LanceSnapshotTests(unittest.TestCase):
    def test_measures_the_tree_and_per_table_sizes(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "lancedb"
            (root / "memories.lance").mkdir(parents=True)
            (root / "memories.lance" / "data.bin").write_bytes(b"x" * 2048)
            (root / "meta.json").write_text("{}", encoding="utf-8")
            out = mbt.lancedb_snapshot(_session(_rag_store=SimpleNamespace(_root=root)))
        self.assertTrue(out["exists"])
        self.assertEqual(out["files"], 2)
        self.assertIn("memories.lance", out["tables_mb"])

    def test_missing_directory_is_not_an_error(self) -> None:
        out = mbt.lancedb_snapshot(
            _session(_rag_store=SimpleNamespace(_root="/nope/not/here")),
        )
        self.assertFalse(out["exists"])


class NoPsutilTests(unittest.TestCase):
    def test_report_degrades_to_a_note_without_psutil(self) -> None:
        with mock.patch.object(mbt, "psutil", None):
            out = mbt.process_snapshot()
        self.assertFalse(out["psutil"])
        self.assertIn("psutil", out["note"])
        self.assertNotIn("rss_mb", out)

    def test_the_rest_of_the_report_still_works(self) -> None:
        store = SimpleNamespace(_mirror={1: _mem("x")}, _lock=None)
        with mock.patch.object(mbt, "psutil", None):
            out = mbt.build_breakdown(_session(_memory_store=store))
        self.assertEqual(out["memory_mirror"]["rows"], 1)


class ProcessSnapshotTests(unittest.TestCase):
    def test_reports_own_rss_and_child_tree(self) -> None:
        if mbt.psutil is None:
            self.skipTest("psutil not installed")
        out = mbt.process_snapshot()
        self.assertTrue(out["psutil"])
        self.assertGreater(out["rss_mb"], 0)
        self.assertIsInstance(out["children"], list)
        # A tree total is only meaningful if it includes ourselves.
        self.assertGreaterEqual(out["tree_rss_mb"], out["rss_mb"])

    def test_a_child_that_dies_mid_walk_does_not_kill_the_section(self) -> None:
        if mbt.psutil is None:
            self.skipTest("psutil not installed")

        class _Ghost:
            pid = 4242

            def name(self):
                raise RuntimeError("gone")

            def status(self):
                raise RuntimeError("gone")

            def memory_info(self):
                raise RuntimeError("gone")

            def cmdline(self):
                raise RuntimeError("gone")

        real = mbt.psutil.Process

        class _Me(real):  # type: ignore[misc,valid-type]
            def children(self, recursive: bool = False):
                return [_Ghost()]

        with mock.patch.object(mbt.psutil, "Process", _Me):
            out = mbt.process_snapshot()
        self.assertEqual(out["children_count"], 1)
        self.assertIsNone(out["children"][0]["rss_mb"])
        self.assertEqual(out["children"][0]["cmdline"], "")


if __name__ == "__main__":
    unittest.main()
