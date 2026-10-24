"""D8 deterministic archive contracts; synthetic data only, no model calls."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.core.conversation import archive as archive_module
from app.core.conversation.archive import ConversationArchive, resolve_range
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.daily_overview_worker import DailyOverviewWorker
from app.core.session.tool_pass_gate import (
    GateContext,
    select_active_tool_names,
    should_run_tool_pass,
)
from app.core.session.tools_registry_mixin import ToolsRegistryMixin
from app.llm.tools.base import ToolError
from app.llm.tools.conversation_history import ConversationHistoryTool
from scripts.conversation_archive import ArchiveConnection


@pytest.fixture
def db(tmp_path):
    value = ChatDatabase(tmp_path / "archive.db")
    yield value
    value._get_conn().close()


def add(
    db, text="source text", *, session="alice:main", role="user", stamp="2026-10-20T12:00:00+00:00"
):
    with patch.object(timephrase, "utcnow", return_value=datetime.fromisoformat(stamp)):
        return db.add_message(session, role, text)


def span(first="2026-10-20", last="2026-10-20", zone="Europe/Bratislava"):
    return resolve_range(start=first, end=last, tz_name=zone)


@pytest.mark.parametrize(("day", "hours"), [("2026-03-29", 23), ("2026-10-25", 25)])
def test_calendar_day_uses_historical_dst(day, hours):
    value = span(day, day)
    start = datetime.fromisoformat(value["start"])
    end = datetime.fromisoformat(value["end_exclusive"])
    assert (end - start).total_seconds() == hours * 3600


def test_relative_week_and_month_use_local_calendar_clock():
    clock = datetime(2026, 11, 1, 0, 30, tzinfo=timezone.utc)
    with patch.object(timephrase, "now", return_value=clock):
        week = resolve_range(when="last week", tz_name="Europe/Bratislava")
        month = resolve_range(when="last month", tz_name="Europe/Bratislava")
    assert week["local_start"] == "2026-10-19T00:00:00+02:00"
    assert week["local_end_exclusive"] == "2026-10-26T00:00:00+01:00"
    assert month["local_start"] == "2026-10-01T00:00:00+02:00"
    assert month["local_end_exclusive"] == "2026-11-01T00:00:00+01:00"


@pytest.mark.parametrize(
    "arguments",
    [
        {"start": "2026-10-20"},
        {"start": "2026-10-21", "end": "2026-10-20"},
        {"when": "an unrecognized time"},
        {"start": "2020-01-01", "end": "2026-01-01"},
        {"when": "today", "start": "2026-10-20", "end": "2026-10-20"},
    ],
)
def test_invalid_or_ambiguous_ranges_are_rejected(arguments):
    with pytest.raises(ValueError):
        resolve_range(**arguments)


def test_range_includes_both_sides_of_local_midnight_across_sessions(db):
    before = add(db, stamp="2026-10-19T21:59:59+00:00")
    first = add(db, stamp="2026-10-19T22:00:00+00:00")
    last = add(db, session="alice:other", stamp="2026-10-20T21:59:59+00:00")
    after = add(db, stamp="2026-10-20T22:00:00+00:00")
    result = ConversationArchive(db, "alice").messages(span())
    assert [row["message_id"] for row in result["messages"]] == [first, last]
    assert before not in [first, last] and after not in [first, last]
    assert result["coverage"]["period_complete"]


def test_pagination_same_timestamp_has_no_skips_or_repeated_primary_rows(db):
    expected = [add(db, str(i), session=f"alice:s{i % 2}") for i in range(51)]
    archive = ConversationArchive(db, "alice")
    result = archive.messages(span(), limit=7)
    seen = []
    pages = 0
    while True:
        seen.extend(row["message_id"] for row in result["messages"])
        pages += 1
        if not result["next_cursor"]:
            break
        # Tool objects are recreated on every turn; the cursor must survive.
        archive = ConversationArchive(db, "alice")
        result = archive.messages(cursor=result["next_cursor"], limit=7)
    assert seen == expected and pages == 8
    assert result["matched_count"] == 51
    assert not result["coverage"]["period_complete"]


def test_insertions_do_not_move_snapshot_even_if_timestamp_is_earlier(db):
    original = [add(db, str(i)) for i in range(5)]
    archive = ConversationArchive(db, "alice")
    first = archive.messages(span(), limit=2)
    add(db, "late arrival", stamp="2026-10-20T01:00:00+00:00")
    second = archive.messages(cursor=first["next_cursor"])
    assert [row["message_id"] for row in second["messages"]] == original[2:]
    assert second["matched_count"] == 5


@pytest.mark.parametrize("mutation", ["edit", "delete", "direct_delete"])
def test_source_mutation_invalidates_cursor_and_removes_private_derived_text(db, mutation):
    ids = [add(db, "private original " + str(i)) for i in range(20)]
    archive = ConversationArchive(db, "alice")
    first = archive.messages(span(), limit=2)
    archive.build_day("2026-10-20", "Europe/Bratislava")
    if mutation == "edit":
        db.update_message_content(ids[8], "corrected text")  # not necessarily a highlight
    elif mutation == "delete":
        db.delete_session("alice:main")
    else:
        db.execute_commit("DELETE FROM messages WHERE id=?", (ids[8],))
    assert archive.messages(cursor=first["next_cursor"])["status"] == "stale_cursor"
    assert db.execute_fetchone("SELECT COUNT(*) FROM conversation_daily_overviews")[0] == 0


def test_cursor_owner_signature_filters_and_mode_are_bound(db):
    for i in range(3):
        add(db, str(i))
    archive = ConversationArchive(db, "alice")
    cursor = archive.messages(span(), limit=1)["next_cursor"]
    with pytest.raises(ValueError):
        ConversationArchive(db, "bob").messages(cursor=cursor)
    with pytest.raises(ValueError):
        archive.messages(cursor=cursor[:-6] + "abcdef")
    with pytest.raises(ValueError):
        archive.overviews(cursor=cursor)
    tool = ConversationHistoryTool(db, "alice")
    with pytest.raises(ToolError):
        tool.run({"mode": "messages", "cursor": cursor, "topic": "changed"})


def test_topic_is_literal_selected_matches_with_neighboring_speaker_context(db):
    previous = add(db, "Which option did you choose?")
    match = add(db, "The red guitar %_", role="assistant")
    next_id = add(db, "That one.")
    add(db, "Other conversation", session="alice:other")
    result = ConversationArchive(db, "alice").messages(span(), topic="%_")
    assert [row["message_id"] for row in result["messages"]] == [match]
    assert {row["message_id"] for row in result["context"]} == {previous, next_id}
    assert result["selection"] == "literal_topic_matches"
    assert not result["coverage"]["period_complete"]
    assert result["matched_count"] == 1


def test_cross_user_legacy_and_non_conversation_rows_never_leak(db):
    own = add(db, "mine", session="a%_:main")
    foreign = add(db, "private", session="alice:main")
    add(db, "prefix collision", session="aXX:main")
    add(db, "unowned legacy", session="main")
    add(db, "system internals", session="a%_:main", role="system")
    archive = ConversationArchive(db, "a%_")
    assert [row["message_id"] for row in archive.messages(span())["messages"]] == [own]
    assert archive.message(foreign)["status"] == "not_found"
    assert archive.messages(span(), session="alice:main")["status"] == "empty_range"
    archive.build_day("2026-10-20", "Europe/Bratislava")
    assert ConversationArchive(db, "alice").overviews(span())["overviews"] == []


def test_bounded_scan_does_not_claim_empty_topic_or_complete_period(db, monkeypatch):
    monkeypatch.setattr(archive_module, "SCAN_LIMIT", 3)
    for i in range(6):
        add(db, "needle" if i == 5 else "other")
    archive = ConversationArchive(db, "alice")
    result = archive.messages(span(), topic="needle")
    assert result["status"] == "partial_scan"
    assert result["matched_count"] is None
    assert result["coverage"]["next_action"] == "narrow_date_range"
    overview = archive.build_day("2026-10-20", "Europe/Bratislava")["overview"]
    assert not overview["coverage"]["scan_complete"]
    assert overview["message_count"] is None


def test_large_message_can_be_reassembled_as_exact_original(db):
    source = "corrected wording αβ\n" * 2500
    mid = add(db, source)
    archive = ConversationArchive(db, "alice")
    first = archive.messages(span())["messages"][0]
    assert not first["text_complete"]
    assert first["next_offset"] == 1600
    chunks, offset = [], 0
    while True:
        message = archive.message(mid, offset)["message"]
        chunks.append(message["text"])
        if message["next_offset"] is None:
            break
        offset = message["next_offset"]
    assert "".join(chunks) == source


def test_exact_quote_continuation_expires_after_edit(db):
    mid = add(db, "first version " * 2000)
    tool = ConversationHistoryTool(db, "alice")
    first = json.loads(tool.run({"mode": "message", "message_id": mid}))
    assert first["next_cursor"]
    db.update_message_content(mid, "corrected version " * 2000)
    next_page = json.loads(tool.run({"mode": "message", "cursor": first["next_cursor"]}))
    assert next_page["status"] == "stale_cursor"


def test_archive_range_and_watermark_queries_are_indexed(db):
    plans = db.execute_fetchall(
        "EXPLAIN QUERY PLAN SELECT id FROM messages "
        "WHERE substr(session_id,1,instr(session_id,':')-1)=? "
        "AND created_at>=? AND created_at<? ORDER BY created_at,id LIMIT 5001",
        ("alice", "2026-10-01", "2026-11-01"),
    )
    assert any("idx_messages_archive_owner_time" in row[3] for row in plans)
    assert not any("TEMP B-TREE" in row[3] for row in plans)
    plans = db.execute_fetchall(
        "EXPLAIN QUERY PLAN SELECT MAX(id) FROM messages "
        "WHERE substr(session_id,1,instr(session_id,':')-1)=?",
        ("alice",),
    )
    assert any("idx_messages_archive_owner_id" in row[3] for row in plans)


def test_empty_missing_no_match_and_unavailable_are_distinct(db):
    archive = ConversationArchive(db, "alice")
    assert archive.messages(span())["status"] == "empty_range"
    assert archive.overviews(span())["status"] == "empty_range"
    assert archive.build_day("2026-10-20")["status"] == "empty_range"
    add(db, "a fact never extracted into durable memory")
    assert archive.messages(span(), topic="missing")["status"] == "no_topic_match"
    assert archive.overviews(span())["status"] == "missing_overviews"
    assert archive.overviews(span())["missing_days"] == ["2026-10-20"]
    assert archive.messages(span(), topic="never extracted")["messages"]
    assert (
        json.loads(
            ConversationHistoryTool(None, "alice").run(
                {"mode": "messages"},
            )
        )["status"]
        == "store_unavailable"
    )


def test_daily_overview_is_cross_session_attributed_selective_and_source_linked(db):
    for i in range(30):
        add(db, f"line {i}", session=f"alice:s{i % 2}", role="user" if i % 2 else "assistant")
    archive = ConversationArchive(db, "alice")
    result = archive.build_day("2026-10-20", "Europe/Bratislava")["overview"]
    assert result["message_count"] == 30
    assert result["session_count"] == 2
    assert result["user_messages"] == result["assistant_messages"] == 15
    assert result["kind"] == "extractive_outline"
    assert result["coverage"]["scan_complete"] and result["coverage"]["highlights_selective"]
    assert len(result["excerpts"]) <= 12
    for excerpt in result["excerpts"]:
        source = db.get_message_row(excerpt["message_id"])
        assert excerpt["role"] == source.role and source.content.startswith(excerpt["text"])
    fetched = archive.overviews(span())
    assert fetched["coverage"]["all_source_days_indexed"]
    assert fetched["overviews"][0]["source_hash"] == result["source_hash"]


def test_new_message_invalidates_overview_but_does_not_leave_deleted_text(db):
    mid = add(db, "original")
    archive = ConversationArchive(db, "alice")
    before = archive.build_day("2026-10-20", "Europe/Bratislava")["overview"]
    add(db, "late message")
    assert archive.overviews(span())["status"] == "missing_overviews"
    after = archive.build_day("2026-10-20", "Europe/Bratislava")["overview"]
    assert before["source_hash"] != after["source_hash"]
    db.update_message_content(mid, "correction")
    archive.build_day("2026-10-20", "Europe/Bratislava")
    assert all(
        excerpt["text"] != "original"
        for excerpt in archive.overviews(span())["overviews"][0]["excerpts"]
    )


def test_overview_directory_is_paged_and_single_day_has_excerpts(db):
    for i in range(31):
        stamp = datetime(2026, 9, 1, 12, tzinfo=timezone.utc) + timedelta(days=i)
        add(db, stamp=stamp.isoformat())
        ConversationArchive(db, "alice").build_day(stamp.date().isoformat(), "UTC")
    archive = ConversationArchive(db, "alice")
    result = archive.overviews(span("2026-09-01", "2026-10-01", "UTC"))
    assert len(result["overviews"]) == 14 and result["next_cursor"]
    assert len(json.dumps(result)) < 24000
    seen = [day["local_day"] for day in result["overviews"]]
    assert all(day["excerpts_omitted"] for day in result["overviews"])
    while result["next_cursor"]:
        result = archive.overviews(cursor=result["next_cursor"])
        seen.extend(day["local_day"] for day in result["overviews"])
    assert len(seen) == len(set(seen)) == 31
    assert "excerpts" in archive.overviews(span("2026-09-01", "2026-09-01", "UTC"))["overviews"][0]


def test_overview_build_cannot_resurrect_sources_deleted_during_generation(db, monkeypatch):
    add(db, "private source")
    archive = ConversationArchive(db, "alice")
    original = archive._scan

    def race(*args, **kwargs):
        rows = original(*args, **kwargs)
        # Separate SQLite writer, while the builder holds its read snapshot.
        other = ChatDatabase(db._db_path)
        other.delete_session("alice:main")
        other._get_conn().close()
        return rows

    monkeypatch.setattr(archive, "_scan", race)
    result = archive.build_day("2026-10-20", "Europe/Bratislava")
    assert result["status"] == "source_changed"
    assert not db.execute_fetchall("SELECT overview_json FROM conversation_daily_overviews")


def test_idle_worker_fills_recent_days_one_at_a_time_and_retries(db):
    add(db)
    add(db, stamp="2026-10-19T12:00:00+00:00")
    add(db, stamp="2026-09-01T12:00:00+00:00")
    worker = DailyOverviewWorker(
        db, "alice", enabled=lambda: True, timezone_provider=lambda: "Europe/Bratislava"
    )
    clock = datetime(2026, 10, 20, 15, tzinfo=timezone.utc)
    with patch.object(timephrase, "now", return_value=clock):
        assert worker.demand(now=clock, last_run_at=None).pressure > 0
        with patch.object(ConversationArchive, "build_day", side_effect=RuntimeError("busy")):
            with pytest.raises(RuntimeError):
                worker.run()
        assert worker.run()["status"] == "built"
        assert worker.run()["status"] == "built"
        assert worker.run()["status"] == "caught_up"
        assert worker.demand(now=clock, last_run_at=None).pressure == 0
        assert not worker.demand(now=clock, last_run_at=None).needs_llm
    assert db.execute_fetchone("SELECT COUNT(*) FROM conversation_daily_overviews")[0] == 2


@pytest.mark.parametrize(
    "text",
    [
        "What did we talk about last week?",
        "What did we discuss last month?",
        "Show our conversation archive",
        "Give me a daily overview",
        "Recap our conversations",
    ],
)
def test_tool_gate_and_skill_router_expose_archive(text):
    decision = should_run_tool_pass(text, ["conversation_history"], context=GateContext())
    assert decision.run
    assert "conversation_history" in select_active_tool_names(
        decision,
        ["conversation_history"],
        router_enabled=True,
    )


def test_registry_exposes_archive_without_embeddings_and_honors_switch(db):
    host = ToolsRegistryMixin()
    host._settings = SimpleNamespace(
        tools=SimpleNamespace(
            enabled=True,
            conversation_history=True,
            get_time=False,
            calendar=False,
            reminders=False,
            weather=False,
            activity=False,
            web_search=False,
            world=False,
            goals=False,
        )
    )
    host._chat_db = db
    host._user_id = "alice"
    host._turn_runner = Mock()
    host.rebuild_tool_registry()
    assert "conversation_history" in host.available_tool_names()
    host._settings.tools.conversation_history = False
    host.rebuild_tool_registry()
    assert "conversation_history" not in host.available_tool_names()


@pytest.mark.parametrize(
    "arguments",
    [
        {"mode": "message"},
        {"mode": "message", "message_id": True},
        {"mode": "messages", "limit": 999},
        {"mode": "messages", "limit": "2"},
        {"mode": "messages", "topic": ["bad"]},
        {"mode": "messages", "extra": "bad"},
        {"mode": "overviews", "topic": "bad"},
        {"mode": "messages", "timezone": "Mars/Bad"},
        {"mode": ["messages"]},
    ],
)
def test_tool_rejects_invalid_arguments(db, arguments):
    with pytest.raises(ToolError):
        ConversationHistoryTool(db, "alice").run(arguments)


def test_read_only_review_adapter_never_writes_source_or_config(db):
    add(db)
    adapter = ArchiveConnection(db._db_path, write=False)
    try:
        result = ConversationArchive(adapter, "alice").messages(span())
        assert result["messages"]
        with pytest.raises(Exception, match="readonly"):
            adapter._get_conn().execute("DELETE FROM messages")
    finally:
        adapter._get_conn().close()
