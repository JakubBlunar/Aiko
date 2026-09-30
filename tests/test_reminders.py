from datetime import datetime, timezone

import pytest

from app.core.goals.reminders import ReminderStore
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase


@pytest.fixture
def reminders(tmp_path, monkeypatch):
    monkeypatch.setattr(timephrase, "utcnow", lambda: datetime(2026, 9, 30, tzinfo=timezone.utc))
    return ReminderStore(ChatDatabase(tmp_path / "reminders.db"), "owner")


def test_reminder_persists_normalizes_and_is_user_scoped(reminders):
    created = reminders.create("  Check   dentist ", "2026-10-01T10:00:00+02:00")
    assert created["due_at"] == "2026-10-01T08:00:00+00:00"
    assert created["text"] == "Check dentist"
    assert reminders.create("Check dentist", "2026-10-01T08:00:00Z")["id"] == created["id"]
    assert ReminderStore(reminders.db, "other").get(created["id"]) is None
    assert ReminderStore(ChatDatabase(reminders.db._db_path), "owner").get(created["id"]) == created


@pytest.mark.parametrize("text, due_at", [
    ("", "2026-10-01T08:00:00Z"), ("x", "tomorrow"),
    ("x", "2026-10-01T08:00:00"), ("x", "2026-09-29T08:00:00Z"),
])
def test_reminder_rejects_invalid_or_ambiguous_requests(reminders, text, due_at):
    with pytest.raises(ValueError):
        reminders.create(text, due_at)


def test_reminder_cancel_is_idempotent_and_scoped(reminders):
    created = reminders.create("Check dentist", "2026-10-01T08:00:00Z")
    with pytest.raises(ValueError):
        ReminderStore(reminders.db, "other").cancel(created["id"])
    cancelled = reminders.cancel(created["id"])
    assert cancelled["cancelled_at"] and reminders.cancel(created["id"]) == cancelled
    assert reminders.list_pending() == []


def test_reminder_tool_and_router(reminders):
    import json

    from app.core.session.tool_pass_gate import (
        GateContext, select_active_tool_names, should_run_tool_pass,
    )
    from app.llm.tools.base import ToolError
    from app.llm.tools.reminders import RemindersTool

    tool = RemindersTool(reminders.db, "owner", "chat")
    decision = should_run_tool_pass(
        "Remind me about the dentist tomorrow", ["reminders", "get_time"],
        context=GateContext(),
    )
    assert decision.run and "reminders" in select_active_tool_names(
        decision, ["reminders", "get_time"], router_enabled=True,
    )
    created = json.loads(tool.run({
        "action": "set", "text": "Dentist", "due_at": "2026-10-01T10:00:00+02:00",
    }))["result"]
    assert json.loads(tool.run({"action": "list"}))["result"][0]["id"] == created["id"]
    assert json.loads(tool.run({"action": "cancel", "reminder_id": created["id"]}))["result"][
        "cancelled_at"
    ]
    with pytest.raises(ToolError):
        tool.run({"action": "set", "text": "Dentist", "due_at": "tomorrow"})


def test_reminder_registry_toggle(reminders):
    from types import SimpleNamespace

    from app.core.infra.settings import ToolsSettings
    from app.core.session.tools_registry_mixin import ToolsRegistryMixin

    host = ToolsRegistryMixin()
    host._settings = SimpleNamespace(tools=ToolsSettings(
        reminders=False, web_search=False, weather=False, activity=False,
    ))
    host._chat_db, host._user_id, host.session_key = reminders.db, "owner", "chat"
    host.rebuild_tool_registry()
    assert "reminders" not in host.available_tool_names()
    host._settings.tools.reminders = True
    host.rebuild_tool_registry()
    assert "reminders" in host.available_tool_names()


def test_due_delivery_is_persistent_once_and_scoped(reminders, monkeypatch):
    from app.core.goals.reminder_dispatcher import ReminderDispatcher

    created = reminders.create("Dentist", "2026-10-01T08:00:00Z")
    delivered = []
    dispatcher = ReminderDispatcher(reminders, lambda: "owner:chat", delivered.append)
    assert dispatcher.run_due() == 0
    monkeypatch.setattr(timephrase, "utcnow", lambda: datetime(2026, 10, 1, 8, tzinfo=timezone.utc))
    assert dispatcher.run_due() == 1
    assert dispatcher.run_due() == 0
    assert reminders.get(created["id"])["fired_at"]
    assert reminders.list_pending() == []
    assert delivered[0].content == "Reminder: Dentist"
    assert reminders.db.get_messages("owner:chat")[-1].content == delivered[0].content
    assert ReminderDispatcher(
        ReminderStore(ChatDatabase(reminders.db._db_path), "owner"),
        lambda: "owner:chat", delivered.append,
    ).run_due() == 0


def test_delivery_retries_after_insert_failure(reminders, monkeypatch):
    created = reminders.create("Dentist", "2026-10-01T08:00:00Z")
    monkeypatch.setattr(timephrase, "utcnow", lambda: datetime(2026, 10, 1, 8, tzinfo=timezone.utc))
    conn = reminders.db._get_conn()
    conn.execute(
        "CREATE TRIGGER reject_reminder_message BEFORE INSERT ON messages "
        "WHEN NEW.content LIKE 'Reminder:%' "
        "BEGIN SELECT RAISE(ABORT, 'test failure'); END"
    )
    with pytest.raises(Exception, match="test failure"):
        reminders.deliver_next("owner:chat")
    assert reminders.get(created["id"])["fired_at"] is None
    conn.execute("DROP TRIGGER reject_reminder_message")
    assert reminders.deliver_next("owner:chat").content == "Reminder: Dentist"


def test_reminder_rest_routes_are_user_scoped(reminders):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.web.rest import reminders_routes

    app = FastAPI()
    session = SimpleNamespace(reminder_store=reminders, session_key="owner:chat")
    reminders_routes.register(app, session)
    client = TestClient(app)
    assert client.get("/api/reminders").json() == {"reminders": []}
    invalid = client.post("/api/reminders", json={"text": "Dentist", "due_at": "tomorrow"})
    assert invalid.status_code == 400
    result = client.post("/api/reminders", json={
        "text": "Dentist", "due_at": "2026-10-01T08:00:00Z",
    })
    assert result.status_code == 200
    reminder_id = result.json()["reminder"]["id"]
    assert client.get("/api/reminders").json()["reminders"][0]["id"] == reminder_id
    session.reminder_store = ReminderStore(reminders.db, "other")
    assert client.get("/api/reminders").json()["reminders"] == []
    assert client.delete(f"/api/reminders/{reminder_id}").status_code == 404
    session.reminder_store = reminders
    assert client.delete(f"/api/reminders/{reminder_id}").json()["reminder"]["cancelled_at"]
    assert client.get("/api/reminders").json()["reminders"] == []
