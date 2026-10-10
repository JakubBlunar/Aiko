"""The brain diary tool reads existing rows, never turns speculation into facts."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from app.core.memory.memory_store import Memory, MemoryStore
from app.core.session.memory_facade_mixin import MemoryFacadeMixin
from app.core.session.tools_registry_mixin import ToolsRegistryMixin
from app.llm.tools.base import ToolError, ToolRegistry
from app.llm.tools.diary import ReadDiaryTool


class Host(MemoryFacadeMixin, ToolsRegistryMixin):
    pass


def _memory(mid, kind, content, metadata=None):
    return Memory(
        id=mid, kind=kind, content=content, salience=0.5, embedding=np.zeros(2),
        source_session=None, source_message_id=None,
        created_at=f"2026-01-{mid:02d}T08:00:00Z", last_used_at=None, use_count=0,
        metadata=metadata or {},
    )


@pytest.fixture
def host():
    # Exercise MemoryStore's real read-only mirror methods without a database or live install.
    store = object.__new__(MemoryStore)
    store._lock = threading.RLock()
    store._mirror = {
        1: _memory(1, "reflection", "[dream] orchard"),
        2: _memory(2, "fact", "Orchard fact"),
        3: _memory(3, "curiosity_finding", "Orchard reading", {
            "source_open_question_id": 4,
            "source_urls": ["https://example.org/orchard"],
            "research_evidence": [{
                "title": "Orchards", "url": "https://example.org/orchard", "snippet": "Search hit",
            }],
            "evidence_type": "search_excerpt",
        }),
        4: _memory(4, "open_question", "What grows?"),
    }
    instance = Host()
    instance._memory_store = store
    instance._settings = SimpleNamespace(memory=SimpleNamespace(enabled=True))
    return instance


def test_browse_filter_count_pagination_and_empty(host):
    tool = ReadDiaryTool(host)
    result = json.loads(tool.run({"action": "list"}))
    assert result["read_only"] and result["total"] == 3
    assert [entry["id"] for entry in result["entries"]] == [4, 3, 1]
    assert result["limit"] == 5 and result["offset"] == 0
    result = json.loads(tool.run({"action": "list", "q": " ORCHARD ", "limit": 1, "offset": 1}))
    assert result["total"] == 2 and result["entries"][0]["id"] == 1
    assert "not established facts" in result["note"]
    assert json.loads(tool.run({"action": "list", "q": "orch*"}))["entries"] == []
    result = json.loads(tool.run({"action": "list", "kind": "curiosity_finding"}))
    assert result["total"] == 1 and result["entries"][0]["id"] == 3
    assert json.loads(tool.run({"action": "list", "offset": 10000}))["entries"] == []


def test_inspect_preserves_dates_content_and_evidence_without_mutation(host):
    tool = ReadDiaryTool(host)
    before = host.memory_store.get(3).to_dict()
    result = json.loads(tool.run({"action": "get", "entry_id": 3}))
    entry = result["entry"]
    assert entry == {key: before[key] for key in ("id", "kind", "content", "metadata")} | {
        "date": before["created_at"],
    }
    assert entry["metadata"]["evidence_type"] == "search_excerpt"
    assert "not verified full pages" in result["note"]
    assert host.memory_store.get(3).to_dict() == before
    assert host.memory_store.get(3).use_count == 0
    assert "[dream]" in json.loads(tool.run({"action": "get", "entry_id": 1}))["entry"]["content"]


@pytest.mark.parametrize("arguments", [
    {}, {"action": "delete"}, {"action": "get"}, {"action": "get", "entry_id": 0},
    {"action": "get", "entry_id": -1}, {"action": "get", "entry_id": True},
    {"action": "get", "entry_id": 1.5}, {"action": "get", "entry_id": "3"},
    {"action": "get", "entry_id": 999}, {"action": "get", "entry_id": 2},
    {"action": "get", "entry_id": 3, "q": "orchard"},
    {"action": "list", "entry_id": 3}, {"action": "list", "kind": "fact"},
    {"action": "list", "kind": ""}, {"action": "list", "kind": []},
    {"action": "list", "limit": 0}, {"action": "list", "limit": 21},
    {"action": "list", "limit": True}, {"action": "list", "limit": "lots"},
    {"action": "list", "limit": 1.5}, {"action": "list", "offset": -1},
    {"action": "list", "offset": 10001}, {"action": "list", "offset": "0"},
    {"action": "list", "q": "x" * 201}, {"action": "list", "q": None},
    {"action": "list", "unknown": True},
])
def test_invalid_arguments_and_non_diary_ids_are_tool_errors(host, arguments):
    with pytest.raises(ToolError):
        ReadDiaryTool(host).run(arguments)


def test_limit_maximum_and_blank_query(host):
    host._memory_store._mirror.update({
        mid: _memory(mid, "diary", "entry") for mid in range(5, 30)
    })
    result = json.loads(ReadDiaryTool(host).run({"action": "list", "limit": 20, "q": " "}))
    assert len(result["entries"]) == 20 and result["total"] == 28


@pytest.mark.parametrize("method", ["list_diary", "diary_count"])
def test_storage_failures_do_not_return_success(host, method):
    setattr(host, method, Mock(side_effect=RuntimeError("offline")))
    with pytest.raises(ToolError, match="offline"):
        ReadDiaryTool(host).run({"action": "list"})


def test_inspect_failure_and_missing_store_are_errors(host):
    host.memory_store.get = Mock(side_effect=RuntimeError("offline"))
    registry = ToolRegistry()
    registry.register(ReadDiaryTool(host))
    result = registry.dispatch("read_diary", {"action": "get", "entry_id": 3})
    assert not result.ok and "offline" in result.content
    host._memory_store = None
    with pytest.raises(ToolError, match="unavailable"):
        ReadDiaryTool(host).run({"action": "list"})


def test_schema_describes_bounds_and_subjective_evidence(host):
    schema = ReadDiaryTool(host).schema()
    assert schema.name == "read_diary"
    assert "NOT established facts" in schema.description
    assert "not semantic search" in schema.description
    assert "search excerpts" in schema.description
    assert schema.parameters["properties"]["kind"]["enum"] == list(host.DIARY_KINDS)
    assert schema.parameters["properties"]["limit"]["maximum"] == 20
    assert schema.parameters["additionalProperties"] is False


@pytest.mark.parametrize("recall,memory_enabled,store_present,tools_enabled,expected", [
    (True, True, True, True, True), (False, True, True, True, False),
    (True, False, True, True, False), (True, True, False, True, False),
    (True, True, True, False, False),
])
def test_registry_recall_and_memory_gates_without_retriever(
    host, recall, memory_enabled, store_present, tools_enabled, expected,
):
    from app.core.infra.settings import ToolsSettings

    host._settings.tools = ToolsSettings(
        enabled=tools_enabled, recall=recall, weather=False, activity=False,
        web_search=False, world=False,
    )
    host._settings.memory.enabled = memory_enabled
    if not store_present:
        host._memory_store = None
    host._turn_runner = Mock()
    host.rebuild_tool_registry()
    assert ("read_diary" in host.available_tool_names()) == expected
    assert "recall" not in host.available_tool_names()


def test_recall_family_routes_diary_requests_and_skips_banter():
    from app.core.session.tool_pass_gate import (
        GateContext, select_active_tool_names, should_run_tool_pass,
    )

    for request in ["Read your diary", "Your journal", "Your research notes", "Curiosity findings"]:
        decision = should_run_tool_pass(request, ["read_diary"], context=GateContext())
        assert decision.run and decision.reason != "unknown_tool"
        assert "read_diary" in select_active_tool_names(
            decision, ["read_diary"], router_enabled=True,
        )
    assert not should_run_tool_pass("Hello there", ["read_diary"], context=GateContext()).run
