"""L26 - per-turn concept trace + MCP observability.

Covers three seams:
  * the L5 / L4 renderers stamp a structured trace on their controller
    slots at selection time (surfaced ids / reason; mode + quiet cluster);
  * the prompt assembler captures that trace onto ``_StaticSlices`` (so it
    rides the slice cache) and forwards it to ``PromptTelemetry`` tagged
    with ``slice_cache_event`` + ``aggressive``;
  * the three new MCP tools (``get_last_concept_trace`` /
    ``get_concept_graph`` / ``get_concept_transitions``) return valid JSON.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import MethodType, SimpleNamespace

from app.core.infra.chat_database import ChatDatabase
from app.core.session.inner_life_part1 import InnerLifePart1Mixin
from app.core.session.prompt_assembler import PromptAssembler, PromptTelemetry


# ── shared fakes ──────────────────────────────────────────────────────


class _StoreStub:
    def __init__(self, concepts: list[SimpleNamespace]) -> None:
        self._concepts = concepts

    def list_by(self, *, status: str, subject: str, kind: str):
        return list(self._concepts)

    def evidence_of(self, concept_id: int):
        # L9 supporting-grounding resolves evidence edges; the trace tests
        # don't exercise real edges, so an empty list keeps grounding "".
        return []


class _MatureGraph:
    persistent = True

    def __init__(self, modes=None, activity=None) -> None:
        self._modes = modes or []
        self._activity = activity or []

    def mature(self, *, min_clusters: int = 6) -> bool:
        return True

    def cluster_coactivation(self, **_kw):
        return list(self._modes)

    def cluster_activity(self, *, top_n: int = 64):
        return list(self._activity)


def _concept_stub(
    *,
    concepts: list[SimpleNamespace],
    concepts_enabled: bool = True,
    block_enabled: bool = True,
) -> SimpleNamespace:
    """Minimal ``self`` for calling the unbound renderer methods."""
    stub = SimpleNamespace(
        _settings=SimpleNamespace(
            agent=SimpleNamespace(
                concepts_enabled=concepts_enabled,
                concept_block_enabled=block_enabled,
                coactivation_block_enabled=True,
                coactivation_block_max_modes=4,
            ),
        ),
        _memory_settings=SimpleNamespace(
            concept_min_clusters=6,
            concept_surface_min_confidence=0.55,
            concept_surface_max_items=3,
            coactivation_min_pair_support=2,
            coactivation_min_strength=0.25,
            coactivation_max_reps_per_mode=4,
            coactivation_quiet_min_days=10.0,
        ),
        _concept_store=_StoreStub(concepts),
        _topic_graph=_MatureGraph(),
        user_display_name="Jacob",
    )
    stub._hedge_for_confidence = InnerLifePart1Mixin._hedge_for_confidence
    stub._join_labels = InnerLifePart1Mixin._join_labels
    stub._concept_supporting_labels = MethodType(
        InnerLifePart1Mixin._concept_supporting_labels, stub
    )
    stub._concept_grounding_phrase = (
        InnerLifePart1Mixin._concept_grounding_phrase
    )
    stub._short_evidence_label = InnerLifePart1Mixin._short_evidence_label
    return stub


# ── renderer-level trace capture ──────────────────────────────────────


class CoactivationBlockTraceTests(unittest.TestCase):
    def test_mode_and_quiet_recorded(self) -> None:
        stub = _concept_stub(concepts=[])
        stub._topic_graph = _MatureGraph(
            modes=[
                SimpleNamespace(
                    reps=(1, 2),
                    labels=("guitar", "recording"),
                    strength=0.6,
                    bucket_by="session",
                ),
            ],
            activity=[
                SimpleNamespace(label="running", days_since=20.0),
                SimpleNamespace(label="guitar", days_since=1.0),
            ],
        )
        text = InnerLifePart1Mixin._render_coactivation_block(stub)
        self.assertIn("guitar", text)
        trace = stub._coactivation_block_trace
        self.assertEqual(trace["reason"], "surfaced")
        self.assertEqual(trace["mode"]["labels"], ["guitar", "recording"])
        self.assertEqual(trace["mode"]["strength"], 0.6)
        self.assertEqual(trace["mode"]["bucket_by"], "session")
        self.assertEqual(trace["quiet"]["label"], "running")

    def test_no_mode_reason(self) -> None:
        stub = _concept_stub(concepts=[])
        stub._topic_graph = _MatureGraph(modes=[])
        self.assertEqual(
            InnerLifePart1Mixin._render_coactivation_block(stub), "",
        )
        self.assertEqual(stub._coactivation_block_trace["reason"], "no_mode")


# ── assembler-level trace flow ────────────────────────────────────────


class _TempDb:
    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._db: ChatDatabase | None = None

    def __enter__(self) -> ChatDatabase:
        self._db = ChatDatabase(Path(self._tmp.name) / "test.db")
        return self._db

    def __exit__(self, *exc: object) -> None:
        if self._db is not None:
            conn = getattr(self._db._local, "conn", None)
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
        try:
            self._tmp.cleanup()
        except Exception:
            pass


def _make_assembler(db: ChatDatabase) -> PromptAssembler:
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8",
    )
    tmp.write("Persona body.")
    tmp.close()
    return PromptAssembler(db, persona_path=Path(tmp.name), recent_window=20)


_TRACE = {
    "surfaced": [{"concept_id": 7, "label": "systems", "confidence": 0.9}],
    "reason": "surfaced",
}


class ConceptTraceTelemetryTests(unittest.TestCase):
    """The concept trace now flows from the T3 relevant_context region
    (built fresh each turn, turn-relevance scored) rather than the retired
    slice-cached concept block."""

    def _region(self, trace: dict):
        from app.core.session.context_budget_selector import RelevantContext

        def _provider(**_kw: object) -> RelevantContext:
            return RelevantContext(
                text="Things:\n- x", concept_trace=dict(trace),
            )

        return _provider

    def test_trace_flows_to_telemetry_tagged(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db)
            db.add_message(
                session_id="s1", role="user", content="hi", token_count=2,
            )
            assembler.set_relevant_context_provider(self._region(_TRACE))
            _, telem = assembler.assemble_with_budget(
                "s1", "x", context_window=4096, response_budget=256,
            )
            self.assertIsInstance(telem, PromptTelemetry)
            surfaced = telem.concepts_surfaced
            self.assertEqual(surfaced["reason"], "surfaced")
            self.assertEqual(surfaced["surfaced"][0]["concept_id"], 7)
            self.assertFalse(surfaced["aggressive"])
            # Rides the serialised telemetry too (get_last_response_detail).
            self.assertIn("concepts_surfaced", telem.as_dict())

    def test_aggressive_tags_trace(self) -> None:
        # Under the unified budget the concept trace is reserved (floors),
        # not dropped; it is tagged aggressive for the reader.
        with _TempDb() as db:
            assembler = _make_assembler(db)
            db.add_message(
                session_id="s2", role="user", content="hi", token_count=2,
            )
            assembler.set_relevant_context_provider(self._region(_TRACE))
            _, telem = assembler.assemble_with_budget(
                "s2", "x", context_window=4096, response_budget=256,
                aggressive=True,
            )
            self.assertTrue(telem.concepts_surfaced["aggressive"])
            self.assertEqual(
                telem.context_budget.get("degrade_level"), 2,
            )


# ── MCP tools ─────────────────────────────────────────────────────────


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco

    def resource(self, *_a, **_k):
        def deco(fn):
            return fn

        return deco


class _FakeSession:
    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled

    def __getattr__(self, _name):
        return None

    def get_last_metrics(self) -> dict:
        return {
            "mode": "typed",
            "concepts_surfaced": {"surfaced": [{"concept_id": 7}],
                                  "reason": "surfaced"},
            "coactivation_surfaced": {"mode": None, "reason": "no_mode"},
        }

    def concepts_snapshot(self) -> dict:
        return {"enabled": self._enabled, "total": 1, "concepts": [{"id": 7}]}

    def concept_timeline(self, *, limit=200, subject=None,
                         event_type=None, before_id=None) -> dict:
        if not self._enabled:
            return {"enabled": False, "total": 0, "events": []}
        data = {
            "promoted": [{"id": 10, "event_type": "promoted", "label": "A"}],
            "dormant": [{"id": 8, "event_type": "dormant", "label": "B"}],
            "retired": [],
            "revived": [{"id": 12, "event_type": "revived", "label": "C"}],
            "discovered": [{"id": 99, "event_type": "discovered"}],
        }
        return {
            "enabled": True,
            "total": 99,
            "events": data.get(event_type or "", [])[:limit],
        }


def _register_tools(session: _FakeSession) -> dict[str, object]:
    from app.mcp.server_tools import proactive_task_tools

    mcp = _FakeMCP()
    proactive_task_tools.register(mcp, session)
    return mcp.tools


class ConceptMcpToolTests(unittest.TestCase):
    def test_get_last_concept_trace(self) -> None:
        tools = _register_tools(_FakeSession())
        out = json.loads(tools["get_last_concept_trace"]())
        self.assertEqual(out["mode"], "typed")
        self.assertEqual(out["concepts"]["reason"], "surfaced")
        self.assertEqual(out["coactivation"]["reason"], "no_mode")

    def test_get_concept_graph(self) -> None:
        tools = _register_tools(_FakeSession())
        out = json.loads(tools["get_concept_graph"]())
        self.assertTrue(out["enabled"])
        self.assertEqual(out["total"], 1)

    def test_get_concept_transitions_lifecycle_only_newest_first(self) -> None:
        tools = _register_tools(_FakeSession())
        out = json.loads(tools["get_concept_transitions"](limit=50))
        self.assertTrue(out["enabled"])
        ids = [e["id"] for e in out["events"]]
        self.assertEqual(ids, [12, 10, 8])  # newest-first, no discovered(99)
        self.assertNotIn(
            "discovered", {e["event_type"] for e in out["events"]},
        )

    def test_get_concept_transitions_disabled(self) -> None:
        tools = _register_tools(_FakeSession(enabled=False))
        out = json.loads(tools["get_concept_transitions"]())
        self.assertFalse(out["enabled"])


if __name__ == "__main__":
    unittest.main()
