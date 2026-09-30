from datetime import datetime, timezone

import pytest

from app.core.goals.calendar import CalendarStore
from app.core.infra.chat_database import ChatDatabase


@pytest.fixture
def calendar(tmp_path):
    return CalendarStore(ChatDatabase(tmp_path / "calendar.db"), "owner")


def _event(calendar, **overrides):
    fields = dict(
        title="Portfolio review", starts_at="2026-10-01T10:00:00+02:00",
        ends_at="2026-10-01T11:00:00+02:00",
    )
    fields.update(overrides)
    return calendar.create(**fields)


def test_calendar_normalizes_offsets_and_create_retries(calendar):
    event = _event(calendar)
    assert event["starts_at"] == "2026-10-01T08:00:00+00:00"
    assert _event(calendar)["id"] == event["id"]
    assert _event(calendar)["revision"] == 1
    assert CalendarStore(calendar.db, "other").get(event["id"]) is None
    assert CalendarStore(calendar.db, "owner").get(event["id"]) == event


@pytest.mark.parametrize("overrides", [
    {"starts_at": "2026-10-01T10:00:00"}, {"starts_at": "tomorrow"},
    {"ends_at": "2026-10-01T09:00:00+02:00"}, {"title": ""}, {"notes": "x" * 501},
])
def test_calendar_rejects_ambiguous_or_invalid_events(calendar, overrides):
    with pytest.raises(ValueError):
        _event(calendar, **overrides)


def test_calendar_rescheduling_cancellation_and_owner_scope(calendar, monkeypatch):
    from app.core.infra import timephrase

    monkeypatch.setattr(timephrase, "utcnow", lambda: datetime(2026, 9, 30, tzinfo=timezone.utc))
    event = _event(calendar)
    assert len(calendar.upcoming()) == 1
    moved = calendar.update(
        event["id"], starts_at="2026-10-04T10:00:00+02:00",
        ends_at="2026-10-04T11:00:00+02:00",
    )
    assert moved["revision"] == 2 and calendar.upcoming() == []
    with pytest.raises(ValueError):
        CalendarStore(calendar.db, "other").update(event["id"], cancel=True)
    cancelled = calendar.update(event["id"], cancel=True)
    assert cancelled["status"] == "cancelled"
    assert calendar.update(event["id"], cancel=True) == cancelled
    assert calendar.upcoming(hours=168) == []


def test_calendar_tool_and_progressive_gate(calendar):
    import json

    from app.core.session.tool_pass_gate import (
        GateContext, should_run_tool_pass, select_active_tool_names,
    )
    from app.llm.tools.calendar import CalendarTool
    from app.llm.tools.base import ToolError

    tool = CalendarTool(calendar.db, "owner", "chat")
    assert tool.schema().name == "calendar"
    decision = should_run_tool_pass(
        "Add an appointment to my calendar", ["calendar", "get_time"], context=GateContext(),
    )
    assert decision.run and "calendar" in select_active_tool_names(
        decision, ["calendar", "get_time"], router_enabled=True,
    )
    assert not should_run_tool_pass("Hello there", ["calendar"], context=GateContext()).run
    created = json.loads(tool.run(dict(
        action="create", title="Portfolio review", starts_at="2026-10-01T08:00:00Z",
        ends_at="2026-10-01T09:00:00Z",
    )))
    event_id = created["result"]["id"]
    assert created["local_only"]
    assert json.loads(tool.run({"action": "get", "event_id": event_id}))["result"]["title"]
    cancelled = json.loads(tool.run({"action": "cancel", "event_id": event_id}))["result"]
    assert cancelled["status"] == "cancelled"
    with pytest.raises(ToolError):
        tool.run({"action": "create", "title": "Missing dates"})
    with pytest.raises(ToolError):
        tool.run({"action": "update", "event_id": event_id})


def test_calendar_worker_dedupes_and_invalidates_changed_sources(calendar, monkeypatch):
    from app.core.infra import timephrase
    from app.core.proactive.calendar_anticipation import CalendarAnticipationWorker, still_upcoming
    from app.core.proactive.cue_store import CueStore

    monkeypatch.setattr(timephrase, "utcnow", lambda: datetime(2026, 9, 30, tzinfo=timezone.utc))
    pool = CueStore(calendar.db, user_id="owner")
    worker = CalendarAnticipationWorker(
        calendar_provider=lambda: calendar, pool_provider=lambda: pool,
        enabled_provider=lambda: True,
    )
    event = _event(calendar)
    assert worker.run() == {"queued": 1}
    row = pool.pending(worker.name)[0]
    assert still_upcoming(calendar, row.payload)
    assert worker.run() == {"queued": 0}
    calendar.update(event["id"], title="Revised portfolio review")
    assert not still_upcoming(calendar, row.payload)
    assert worker.run() == {"queued": 1}
    latest = pool.pending(worker.name)[0]
    calendar.update(event["id"], cancel=True)
    assert not still_upcoming(calendar, latest.payload)
    assert worker.run() == {"queued": 0}
    assert pool.pending(worker.name) == []


def test_calendar_create_retry_after_reschedule_keeps_original_id(calendar):
    event = _event(calendar)
    dates = dict(starts_at="2026-10-02T10:00:00+02:00", ends_at="2026-10-02T11:00:00+02:00")
    calendar.update(event["id"], **dates)
    retried = _event(calendar, **dates)
    assert retried["id"] == event["id"]
    assert retried["revision"] == 2


def test_calendar_registry_defaults_off_and_can_be_enabled(calendar):
    from types import SimpleNamespace
    from app.core.infra.settings import ToolsSettings
    from app.core.session.tools_registry_mixin import ToolsRegistryMixin

    host = ToolsRegistryMixin()
    host._settings = SimpleNamespace(tools=ToolsSettings(
        web_search=False, weather=False, activity=False,
    ))
    host._chat_db, host._user_id, host.session_key = calendar.db, "owner", "chat"
    host.rebuild_tool_registry()
    assert "calendar" not in host.available_tool_names()
    host._settings.tools.calendar = True
    host.rebuild_tool_registry()
    assert "calendar" in host.available_tool_names()


def test_calendar_late_cancellation_does_not_spend_claim(calendar, monkeypatch):
    from app.core.infra import timephrase
    from app.core.proactive.calendar_anticipation import still_upcoming, source_id
    from app.core.proactive.cue_store import CueStore
    from app.core.session.cue_pool_mixin import CuePoolMixin
    from app.core.session.surfacing_attempt import SurfaceAttempt, current_attempt

    monkeypatch.setattr(timephrase, "utcnow", lambda: datetime(2026, 9, 30, tzinfo=timezone.utc))
    event = _event(calendar)
    host = CuePoolMixin()
    host._cue_store = CueStore(calendar.db, user_id="owner")
    cue_id = host._cue_store.add(
        "calendar_anticipation", event["title"], "Confirmed event",
        payload={"event_id": event["id"], "source_id": source_id(event)},
    )
    attempt = SurfaceAttempt()
    token = current_attempt.set(attempt)
    try:
        host.take_pool_cue(
            "calendar_anticipation", still_valid=lambda payload: still_upcoming(calendar, payload),
        )
        calendar.update(event["id"], cancel=True)
        attempt.finish("Confirmed event", included_blocks={"calendar_anticipation_block": 15})
    finally:
        current_attempt.reset(token)
    assert host._cue_store.get(cue_id).surfaced_count == 0
    assert attempt.snapshot()["claims"][0]["state"] == "error:ValueError"
