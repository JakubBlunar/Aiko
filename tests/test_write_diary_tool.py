"""Intentional journal writes preserve perspective, persistence and ownership."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore
from app.core.memory.memory_origin import memory_origin, claim_source_refusal
from app.core.session.memory_facade_mixin import MemoryFacadeMixin
from app.core.session.tools_registry_mixin import ToolsRegistryMixin
from app.llm.tools.base import ToolError
from app.llm.tools.diary import ReadDiaryTool, WriteDiaryTool


class Host(MemoryFacadeMixin, ToolsRegistryMixin):
    pass


@pytest.fixture
def host(tmp_path):
    path = tmp_path / "journal.db"
    db = ChatDatabase(path)
    host = Host()
    host._memory_store = MemoryStore(path)
    host._embedder = SimpleNamespace(embed=lambda _: np.ones(4, dtype=np.float32))
    host._settings = SimpleNamespace(memory=SimpleNamespace(enabled=True))
    host.session_key = "user:s1"
    host._notify_memory_added = Mock()
    host._db = db
    return host


def write(host, content="I felt uncertain whether offering another idea would help.", **extra):
    return json.loads(WriteDiaryTool(host).run({"content": content, **extra}))


def test_write_returns_the_real_saved_entry_and_read_can_revisit_it(host):
    result = write(host)
    row = host.memory_store.get(result["entry"]["id"])
    assert result["saved"] and result["created"]
    assert row.kind == "diary" and row.tier == "long_term"
    assert row.temporal_type == "past_event" and row.event_time
    assert row.source_session == "user:s1"
    assert row.metadata["journal"]["authorship"] == "chosen"
    assert memory_origin(row).subject == "aiko"
    assert memory_origin(row).mode == "reflection"
    assert claim_source_refusal(row, subject="user", kind="identity") == "different_subject"
    assert host._notify_memory_added.call_count == 1
    read = json.loads(ReadDiaryTool(host).run({"action": "get", "entry_id": row.id}))
    assert read["entry"] == result["entry"]
    assert host._db._get_conn().execute("SELECT count(*) FROM concepts").fetchone()[0] == 0


def test_retry_returns_existing_receipt_without_another_entry_or_notification(host):
    first, second = write(host), write(host)
    assert first["entry"] == second["entry"]
    assert second["saved"] and not second["created"]
    assert host.memory_store.count() == 1
    assert host._notify_memory_added.call_count == 1


def test_new_perspective_links_old_entry_without_rewriting_it(host):
    first = write(host)["entry"]
    second = write(host, "I want to ask what kind of support fits before offering advice.",
                   revisits_entry_id=first["id"])["entry"]
    assert second["id"] != first["id"]
    assert second["metadata"]["journal"]["revisits_entry_id"] == first["id"]
    assert host.memory_store.get(first["id"]).content == first["content"]


@pytest.mark.parametrize("arguments", [
    {}, {"content": "tiny"}, {"content": ["not prose"]}, {"content": "x" * 2001},
    {"content": "I felt uncertain today about helping."},
    {"content": "[[remember:Some intrusive control tag]]"},
    {"content": "A meaningful private entry.", "kind": "fact"},
    {"content": "A meaningful private entry.", "revisits_entry_id": True},
    {"content": "A meaningful private entry.", "revisits_entry_id": 999},
])
def test_invalid_write_does_not_claim_success_or_change_the_diary(host, arguments):
    with pytest.raises(ToolError):
        WriteDiaryTool(host).run(arguments)
    assert host.memory_store.count() == 0
    host._notify_memory_added.assert_not_called()


def test_non_diary_reference_is_refused(host):
    row = host.memory_store.add("A plain factual note", "fact", np.ones(4))
    with pytest.raises(ToolError, match="earlier diary"):
        write(host, revisits_entry_id=row.id)


def test_asleep_or_unavailable_storage_fails_without_writing(host):
    host._sleep_store = SimpleNamespace(get_state=lambda: SimpleNamespace(status="asleep"))
    with pytest.raises(ToolError, match="asleep"):
        write(host)
    assert host.memory_store.count() == 0
    host._sleep_store = None
    host.memory_store.add = Mock(return_value=None)
    with pytest.raises(ToolError, match="not saved"):
        write(host)
    host._notify_memory_added.assert_not_called()


def test_notification_failure_does_not_hide_a_successful_write(host):
    host._notify_memory_added.side_effect = RuntimeError("listener unavailable")
    assert write(host)["saved"]
    assert host.memory_store.count() == 1


def test_journal_family_routes_writes_without_making_them_premise_recall(host):
    from app.core.session.tool_pass_gate import (
        GateContext, should_run_tool_pass, select_active_tool_names,
    )
    names = ["write_diary", "read_diary"]
    decision = should_run_tool_pass("Write this in your diary", names, context=GateContext())
    assert decision.run and decision.reason != "unknown_tool"
    assert "write_diary" in select_active_tool_names(decision, names, router_enabled=True)
    decision = should_run_tool_pass("That sounds lovely", names,
                                    context=GateContext(recall_need=True))
    assert select_active_tool_names(decision, names, router_enabled=True) == {"read_diary"}


@pytest.mark.parametrize("enabled,embedder,expected", [(True, True, True),
                                                       (False, True, False),
                                                       (True, False, False)])
def test_write_registration_requires_memory_and_writing_capability(
    host, enabled, embedder, expected,
):
    from app.core.infra.settings import ToolsSettings

    host._settings.tools = ToolsSettings(
        weather=False, activity=False, web_search=False, world=False,
    )
    host._settings.memory.enabled = enabled
    host._turn_runner = Mock()
    if not embedder:
        host._embedder = None
    host.rebuild_tool_registry()
    assert ("write_diary" in host.available_tool_names()) == expected
